import pytest

from app.services.agent_trust_scorer import AgentTrustScorer


@pytest.fixture
def scorer():
    return AgentTrustScorer()


class TestComputeFinancialScore:
    def test_perfect_credit(self, scorer):
        stats = {"unpaid_ratio": 0.0, "current_credit_delay_days": 0}
        score = scorer._compute_financial_score(stats)
        assert score == 100.0

    def test_high_unpaid_ratio(self, scorer):
        stats = {"unpaid_ratio": 50.0, "current_credit_delay_days": 0}
        score = scorer._compute_financial_score(stats)
        assert score == 50.0

    def test_delay_penalty(self, scorer):
        stats = {"unpaid_ratio": 0.0, "current_credit_delay_days": 30}
        score = scorer._compute_financial_score(stats)
        assert score == 85.0  # 100 - min((30/10)*5, 40) = 100 - 15

    def test_max_penalty(self, scorer):
        stats = {"unpaid_ratio": 0.0, "current_credit_delay_days": 100}
        score = scorer._compute_financial_score(stats)
        assert score == 60.0  # 100 - min(50, 40) = 60

    def test_combined_penalty(self, scorer):
        stats = {"unpaid_ratio": 30.0, "current_credit_delay_days": 20}
        score = scorer._compute_financial_score(stats)
        assert score == 60.0  # 100 - 30 - min(10, 40) = 60

    def test_floor_at_5(self, scorer):
        stats = {"unpaid_ratio": 100.0, "current_credit_delay_days": 100}
        score = scorer._compute_financial_score(stats)
        assert score == 5.0


class TestComputeExperienceScore:
    def test_no_data(self, scorer):
        stats = {"created_at": None, "lifetime_bookings": 0}
        score = scorer._compute_experience_score(stats)
        assert score == 0.0

    def test_moderate_experience(self, scorer):
        from datetime import datetime, timedelta
        stats = {
            "created_at": datetime.utcnow() - timedelta(days=365),
            "lifetime_bookings": 100,
        }
        score = scorer._compute_experience_score(stats)
        assert 0 < score <= 100

    def test_high_experience_is_capped(self, scorer):
        from datetime import datetime, timedelta
        stats = {
            "created_at": datetime.utcnow() - timedelta(days=3650),
            "lifetime_bookings": 5000,
        }
        score = scorer._compute_experience_score(stats)
        assert score <= 100.0


class TestComputeReliabilityScore:
    def test_no_activity(self, scorer):
        batch = {365: {"bookings": 0, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        score = scorer._compute_reliability_score(batch, exp)
        assert score == 80.0

    def test_all_bookings_successful(self, scorer):
        batch = {365: {"bookings": 100, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 100}
        score = scorer._compute_reliability_score(batch, exp)
        assert score > 90.0

    def test_high_cancellation_rate(self, scorer):
        batch = {365: {"bookings": 50, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 50, "lifetime_bookings": 50}
        score = scorer._compute_reliability_score(batch, exp)
        assert score < 88.0


class TestDetermineTier:
    def test_platinum(self, scorer):
        assert scorer._determine_tier(85, 0, 0.0, 0) == "Platinum"

    def test_gold(self, scorer):
        assert scorer._determine_tier(70, 0, 0.0, 0) == "Gold"

    def test_silver(self, scorer):
        assert scorer._determine_tier(55, 0, 0.0, 0) == "Silver"

    def test_bronze(self, scorer):
        assert scorer._determine_tier(40, 0, 0.0, 0) == "Bronze"

    def test_high_risk_low_score(self, scorer):
        assert scorer._determine_tier(20, 0, 0.0, 0) == "High Risk"

    def test_high_risk_via_unpaid_ratio(self, scorer):
        assert scorer._determine_tier(80, 0, 60.0, 0) == "High Risk"

    def test_high_risk_via_delay(self, scorer):
        assert scorer._determine_tier(80, 0, 0.0, 70) == "High Risk"

    def test_high_risk_via_unpaid_count(self, scorer):
        assert scorer._determine_tier(80, 10, 0.0, 0) == "High Risk"

    def test_platinum_requires_zero_unpaid(self, scorer):
        assert scorer._determine_tier(85, 1, 0.0, 0) == "Gold"

    def test_platinum_requires_low_delay(self, scorer):
        assert scorer._determine_tier(85, 0, 0.0, 5) == "Gold"


class TestDetermineBadges:
    def test_trusted_partner(self, scorer):
        badges = scorer._determine_badges(
            overall_score=95, experience_score=40.0,
            financial_score=100.0, unpaid_ratio=0.0,
            exp_stats={"lifetime_bookings": 500},
        )
        assert "Trusted Partner" in badges

    def test_perfect_payer(self, scorer):
        badges = scorer._determine_badges(
            overall_score=80, experience_score=20.0,
            financial_score=100.0, unpaid_ratio=0.0,
            exp_stats={"lifetime_bookings": 100},
        )
        assert "Perfect Payer" in badges

    def test_booking_champion(self, scorer):
        badges = scorer._determine_badges(
            overall_score=70, experience_score=20.0,
            financial_score=80.0, unpaid_ratio=10.0,
            exp_stats={"lifetime_bookings": 1000},
        )
        assert "Booking Champion" in badges

    def test_no_badges_for_poor_score(self, scorer):
        badges = scorer._determine_badges(
            overall_score=30, experience_score=5.0,
            financial_score=30.0, unpaid_ratio=70.0,
            exp_stats={"lifetime_bookings": 0},
        )
        assert badges == []


class TestCalculateCreditScore:
    def test_clean_credit(self, scorer):
        operational, credit, delay_penalty = scorer._calculate_credit_score(
            {"current_credit_delay_days": 0, "unpaid_ratio": 0.0, "unpaid_count": 0}
        )
        assert operational == 100
        assert credit == 100
        assert delay_penalty == 0

    def test_delayed_credit(self, scorer):
        operational, credit, delay_penalty = scorer._calculate_credit_score(
            {"current_credit_delay_days": 20, "unpaid_ratio": 20.0, "unpaid_count": 2}
        )
        assert operational == 80
        assert delay_penalty == 10
        assert credit == 64  # 80 - 10 - 6

    def test_max_penalties(self, scorer):
        operational, credit, _ = scorer._calculate_credit_score(
            {"current_credit_delay_days": 100, "unpaid_ratio": 95.0, "unpaid_count": 10}
        )
        assert operational == 5
        assert credit == 5  # floored at 5
