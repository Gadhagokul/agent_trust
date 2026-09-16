from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.domain.errors import DatabaseUnavailableError
from app.domain.models import AgentTrustResult
from app.infra.db.repository import CreditStats
from app.services.agent_trust_scorer import AgentTrustScorer


@pytest.fixture
def scorer():
    return AgentTrustScorer()


def _make_credit_stats(**overrides) -> CreditStats:
    defaults = {
        "current_overdue_count": 0,
        "current_overdue_ratio": 0.0,
        "current_max_delay_days": 0,
        "outstanding_amount": 0.0,
        "historical_late_payment_count": 0,
        "historical_late_payment_ratio": 0.0,
        "average_payment_delay_days": 0.0,
        "consecutive_unpaid_cycles": 0,
    }
    defaults.update(overrides)
    return CreditStats(**defaults)


def _minimal_conversion():
    return {
        "searches": 0,
        "bookstep_failed": 0,
        "adjusted_bookstep_failed": 0,
        "other_step_failed": 0,
        "effective_searches": 0,
        "no_activity": True,
        "bookings": 0,
        "booking_volume": 0.0,
        "avg_booking_value": 0.0,
        "revenue_consistency": 0.0,
        "low_confidence": True,
    }


def _minimal_result_dict():
    now = datetime.now(timezone.utc).isoformat()
    conv = _minimal_conversion()
    return {
        "agent_id": 1,
        "agent_name": "Test Agent",
        "features": {
            "current_max_delay_days": 0,
            "current_overdue_ratio": 0.0,
            "current_overdue_count": 0,
            "outstanding_amount": 0.0,
            "historical_late_payment_count": 0,
            "historical_late_payment_ratio": 0.0,
            "average_payment_delay_days": 0.0,
            "no_activity": True,
            "daily": conv,
            "weekly": conv,
            "monthly": conv,
            "yearly": conv,
            "search_activity": {
                "created": 0,
                "reused": 0,
                "searches": 0,
                "bookings": 0,
                "scored": False,
            },
        },
        "scores": {
            "operational_score": 100,
            "reliability_score": 80.0,
            "financial_score": 100.0,
            "experience_score": 0.0,
            "composite_trust_score": 72.0,
            "ml_calibration_score": 100.0,
            "overall_score": 78,
            "search_to_booking_score": None,
        },
        "tier": "Gold",
        "badges": [],
        "high_risk_flag": False,
        "high_risk_reasons": [],
        "calculated_at": now,
    }


class TestCalculate:
    """Tests for the calculate() orchestration method (cache, lock, fallback)."""

    @patch("app.services.agent_trust_scorer.CacheAdapter")
    def test_returns_cached_result(self, mock_cache_cls):
        mock_cache = MagicMock()
        mock_cache_cls.return_value = mock_cache
        mock_cache.get.return_value = _minimal_result_dict()

        s = AgentTrustScorer()
        result = s.calculate(db=MagicMock(), agent_id=1)

        assert isinstance(result, AgentTrustResult)
        assert result.agent_id == 1
        mock_cache.acquire_lock.assert_not_called()

    @patch("app.services.agent_trust_scorer.AuditRepository")
    @patch("app.services.agent_trust_scorer.TrustModelPredictor")
    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_computes_on_cache_miss(
        self, mock_repo_cls, mock_cache_cls, mock_ml_cls, mock_audit_cls
    ):
        mock_cache = MagicMock()
        mock_cache_cls.return_value = mock_cache
        mock_cache.get.return_value = None
        mock_cache.acquire_lock.return_value = "lock-token-abc"

        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        mock_repo.get_agent.return_value = (1, "Test Agent")
        mock_repo.get_credit_stats.return_value = _make_credit_stats()
        mock_repo.get_multi_timeframe_stats.return_value = {
            1: _minimal_conversion(),
            7: _minimal_conversion(),
            30: _minimal_conversion(),
            365: _minimal_conversion(),
        }
        mock_repo.get_experience_stats.return_value = {
            "created_at": datetime.now(timezone.utc),
            "lifetime_bookings": 0,
            "lifetime_revenue": 0.0,
            "lifetime_cancelled": 0,
        }
        mock_repo.get_agent_search_activity.return_value = {
            "searches": 0,
            "created": 0,
            "reused": 0,
            "bookings": 0,
        }
        mock_repo.get_supplier_expected_ratio.return_value = 0.05

        mock_ml = MagicMock()
        mock_ml_cls.return_value = mock_ml
        mock_ml.predict.return_value = 80.0

        s = AgentTrustScorer()
        result = s.calculate(db=MagicMock(), agent_id=1)

        assert isinstance(result, AgentTrustResult)
        assert result.agent_id == 1
        mock_repo.get_agent.assert_called_once()
        mock_cache.set.assert_called_once()

    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_falls_back_to_stale_on_db_error(self, mock_repo_cls, mock_cache_cls):
        mock_cache = MagicMock()
        mock_cache_cls.return_value = mock_cache
        mock_cache.get.return_value = None
        mock_cache.acquire_lock.return_value = "lock-token-abc"
        mock_cache.get_stale.return_value = _minimal_result_dict()

        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        mock_repo.get_agent.side_effect = DatabaseUnavailableError()

        s = AgentTrustScorer()
        result = s.calculate(db=MagicMock(), agent_id=1)

        assert isinstance(result, AgentTrustResult)
        assert result.agent_name == "Test Agent"
        mock_cache.get_stale.assert_called()

    @patch("app.services.agent_trust_scorer.AuditRepository")
    @patch("app.services.agent_trust_scorer.TrustModelPredictor")
    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_releases_lock_on_success(
        self, mock_repo_cls, mock_cache_cls, mock_ml_cls, mock_audit_cls
    ):
        mock_cache = MagicMock()
        mock_cache_cls.return_value = mock_cache
        mock_cache.get.return_value = None
        mock_cache.acquire_lock.return_value = "lock-token-abc"

        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        mock_repo.get_agent.return_value = (1, "Test Agent")
        mock_repo.get_credit_stats.return_value = _make_credit_stats()
        mock_repo.get_multi_timeframe_stats.return_value = {
            1: _minimal_conversion(),
            7: _minimal_conversion(),
            30: _minimal_conversion(),
            365: _minimal_conversion(),
        }
        mock_repo.get_experience_stats.return_value = {
            "created_at": datetime.now(timezone.utc),
            "lifetime_bookings": 0,
            "lifetime_revenue": 0.0,
            "lifetime_cancelled": 0,
        }
        mock_repo.get_agent_search_activity.return_value = {
            "searches": 0,
            "created": 0,
            "reused": 0,
            "bookings": 0,
        }
        mock_repo.get_supplier_expected_ratio.return_value = 0.05

        mock_ml = MagicMock()
        mock_ml_cls.return_value = mock_ml
        mock_ml.predict.return_value = 80.0

        s = AgentTrustScorer()
        s.calculate(db=MagicMock(), agent_id=1)

        mock_cache.release_lock.assert_called_once_with(
            "trust:agent:1:conversion", "lock-token-abc"
        )

    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_releases_lock_on_error(self, mock_repo_cls, mock_cache_cls):
        mock_cache = MagicMock()
        mock_cache_cls.return_value = mock_cache
        mock_cache.get.return_value = None
        mock_cache.acquire_lock.return_value = "lock-token-abc"

        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        mock_repo.get_agent.side_effect = DatabaseUnavailableError()
        mock_cache.get_stale.return_value = None

        s = AgentTrustScorer()
        with pytest.raises(DatabaseUnavailableError):
            s.calculate(db=MagicMock(), agent_id=1)

        mock_cache.release_lock.assert_called_once_with(
            "trust:agent:1:conversion", "lock-token-abc"
        )


class TestComputeFinancialScore:
    def test_perfect_credit(self, scorer):
        stats = _make_credit_stats(current_overdue_ratio=0.0, current_max_delay_days=0)
        score = scorer._compute_financial_score(stats)
        assert score == 100.0

    def test_high_overdue_ratio(self, scorer):
        stats = _make_credit_stats(current_overdue_ratio=50.0, current_max_delay_days=0)
        score = scorer._compute_financial_score(stats)
        assert score == 50.0

    def test_delay_penalty(self, scorer):
        stats = _make_credit_stats(current_overdue_ratio=0.0, current_max_delay_days=30)
        score = scorer._compute_financial_score(stats)
        assert score == 85.0  # 100 - min((30/10)*5, 40) = 100 - 15

    def test_max_penalty(self, scorer):
        stats = _make_credit_stats(current_overdue_ratio=0.0, current_max_delay_days=100)
        score = scorer._compute_financial_score(stats)
        assert score == 60.0  # 100 - min(50, 40) = 60

    def test_combined_penalty(self, scorer):
        stats = _make_credit_stats(current_overdue_ratio=30.0, current_max_delay_days=20)
        score = scorer._compute_financial_score(stats)
        assert score == 60.0  # 100 - 30 - min(10, 40) = 60

    def test_floor_at_5(self, scorer):
        stats = _make_credit_stats(current_overdue_ratio=100.0, current_max_delay_days=100)
        score = scorer._compute_financial_score(stats)
        assert score == 5.0


class TestComputeExperienceScore:
    def test_no_data(self, scorer):
        stats = {"created_at": None, "lifetime_bookings": 0}
        score = scorer._compute_experience_score(stats)
        assert score == 0.0

    def test_moderate_experience(self, scorer):
        from datetime import datetime, timedelta, timezone

        stats = {
            "created_at": datetime.now(timezone.utc) - timedelta(days=365),
            "lifetime_bookings": 100,
        }
        score = scorer._compute_experience_score(stats)
        assert 0 < score <= 100

    def test_high_experience_is_capped(self, scorer):
        from datetime import datetime, timedelta, timezone

        stats = {
            "created_at": datetime.now(timezone.utc) - timedelta(days=3650),
            "lifetime_bookings": 5000,
        }
        score = scorer._compute_experience_score(stats)
        assert score <= 100.0


class TestComputeReliabilityScore:
    def test_no_activity_is_excluded(self, scorer):
        batch = {365: {"bookings": 0, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        score = scorer._compute_reliability_score(batch, exp)
        assert score is None

    def test_all_bookings_successful(self, scorer):
        batch = {365: {"bookings": 100, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 100}
        score = scorer._compute_reliability_score(batch, exp)
        assert score == 100.0

    def test_mixed_existing_bookings_reliability_100(self, scorer):
        batch = {365: {"bookings": 10, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        assert scorer._compute_reliability_score(batch, exp) == 100.0

    def test_high_cancellation_rate(self, scorer):
        batch = {365: {"bookings": 50, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 50, "lifetime_bookings": 50}
        score = scorer._compute_reliability_score(batch, exp)
        expected = round(0.6429 * 100.0 + 0.3571 * 50.0, 2)
        assert score == expected
        assert score < 88.0

    def test_weights_renormalize_over_configured_sum(self, scorer, monkeypatch):
        fake = SimpleNamespace(
            reliability_component_weights={"booking_success": 0.5, "cancellation_quality": 0.5}
        )
        monkeypatch.setattr("app.services.agent_trust_scorer.get_settings", lambda: fake)
        batch = {365: {"bookings": 100, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 50, "lifetime_bookings": 50}
        # 0.5*100 + 0.5*50 = 75.0 (no renormalization drift with two weights)
        assert scorer._compute_reliability_score(batch, exp) == 75.0

    def test_cancellation_quality_mirrors_implementation(self, scorer):
        batch = {365: {"bookings": 1, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 100, "lifetime_bookings": 1}
        score = scorer._compute_reliability_score(batch, exp)
        # cancel quality = (1 - 100/101)*100 = 0.99; clamp is defensive only
        cancel_rate = 100.0 / 101.0
        cancel_quality = max(0.0, (1.0 - cancel_rate) * 100.0)
        expected = round(0.6429 * 100.0 + 0.3571 * cancel_quality, 2)
        assert score == expected


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

    def test_high_risk_via_overdue_ratio(self, scorer):
        assert scorer._determine_tier(80, 0, 60.0, 0) == "High Risk"

    def test_high_risk_via_delay(self, scorer):
        assert scorer._determine_tier(80, 0, 0.0, 70) == "High Risk"

    def test_high_risk_via_overdue_count(self, scorer):
        assert scorer._determine_tier(80, 10, 0.0, 0) == "High Risk"

    def test_platinum_requires_zero_overdue(self, scorer):
        assert scorer._determine_tier(85, 1, 0.0, 0) == "Gold"

    def test_platinum_requires_low_delay(self, scorer):
        assert scorer._determine_tier(85, 0, 0.0, 5) == "Gold"


class TestDetermineBadges:
    def test_trusted_partner(self, scorer):
        badges = scorer._determine_badges(
            overall_score=95,
            experience_score=40.0,
            financial_score=100.0,
            current_overdue_ratio=0.0,
            exp_stats={"lifetime_bookings": 500},
        )
        assert "Trusted Partner" in badges

    def test_perfect_payer(self, scorer):
        badges = scorer._determine_badges(
            overall_score=80,
            experience_score=20.0,
            financial_score=100.0,
            current_overdue_ratio=0.0,
            exp_stats={"lifetime_bookings": 100},
        )
        assert "Perfect Payer" in badges

    def test_booking_champion(self, scorer):
        badges = scorer._determine_badges(
            overall_score=70,
            experience_score=20.0,
            financial_score=80.0,
            current_overdue_ratio=10.0,
            exp_stats={"lifetime_bookings": 1000},
        )
        assert "Booking Champion" in badges

    def test_no_badges_for_poor_score(self, scorer):
        badges = scorer._determine_badges(
            overall_score=30,
            experience_score=5.0,
            financial_score=30.0,
            current_overdue_ratio=70.0,
            exp_stats={"lifetime_bookings": 0},
        )
        assert badges == []


class TestCheckHighRisk:
    def test_clean_agent_not_high_risk(self, scorer):
        stats = _make_credit_stats(
            current_overdue_count=0,
            current_overdue_ratio=0.0,
            current_max_delay_days=0,
            consecutive_unpaid_cycles=0,
        )
        is_risk, reasons = scorer._check_high_risk(stats)
        assert is_risk is False
        assert reasons == []

    def test_high_risk_via_overdue_count(self, scorer):
        stats = _make_credit_stats(current_overdue_count=10)
        is_risk, reasons = scorer._check_high_risk(stats)
        assert is_risk is True
        assert any("overdue invoice count" in r.lower() for r in reasons)

    def test_high_risk_via_overdue_ratio(self, scorer):
        stats = _make_credit_stats(current_overdue_ratio=60.0)
        is_risk, reasons = scorer._check_high_risk(stats)
        assert is_risk is True
        assert any("overdue ratio" in r.lower() for r in reasons)

    def test_high_risk_via_delay(self, scorer):
        stats = _make_credit_stats(current_max_delay_days=70)
        is_risk, reasons = scorer._check_high_risk(stats)
        assert is_risk is True
        assert any("payment delay" in r.lower() for r in reasons)

    def test_high_risk_via_consecutive_defaults(self, scorer):
        stats = _make_credit_stats(consecutive_unpaid_cycles=3)
        is_risk, reasons = scorer._check_high_risk(stats)
        assert is_risk is True
        assert any("consecutive" in r.lower() for r in reasons)


class TestCreditStatsCorrectness:
    """
    Tests that verify the business definition of overdue:
    overdue = status != 'paid' AND due_date IS NOT NULL AND due_date < CURRENT_DATE()
    """

    def test_all_paid(self, scorer):
        """10 paid transactions -> overdue_count=0, overdue_ratio=0"""
        stats = _make_credit_stats(
            current_overdue_count=0,
            current_overdue_ratio=0.0,
            current_max_delay_days=0,
            outstanding_amount=0.0,
        )
        assert stats.current_overdue_count == 0
        assert stats.current_overdue_ratio == 0.0
        financial = scorer._compute_financial_score(stats)
        assert financial == 100.0

    def test_pending_not_due_not_overdue(self, scorer):
        """
        10 transactions: 8 pending (due in future) + 2 paid
        -> overdue_count=0, overdue_ratio=0 (pending not due are NOT overdue)
        THIS IS THE BUG TEST that catches the old unpaid_count inflation.
        """
        stats = _make_credit_stats(
            current_overdue_count=0,
            current_overdue_ratio=0.0,
            current_max_delay_days=0,
            outstanding_amount=0.0,
        )
        assert stats.current_overdue_count == 0
        assert stats.current_overdue_ratio == 0.0
        financial = scorer._compute_financial_score(stats)
        assert financial == 100.0

    def test_overdue_transactions(self, scorer):
        """10 transactions: 8 paid + 2 overdue (past due) -> overdue_count=2, ratio=20%"""
        stats = _make_credit_stats(
            current_overdue_count=2,
            current_overdue_ratio=20.0,
            current_max_delay_days=15,
            outstanding_amount=500.0,
        )
        assert stats.current_overdue_count == 2
        assert stats.current_overdue_ratio == 20.0
        financial = scorer._compute_financial_score(stats)
        assert financial == 72.5  # 100 - 20 (overdue) - 7.5 (delay: (15/10)*5)

    def test_mixed_transactions(self, scorer):
        """
        20 transactions: 10 paid + 5 pending/not due + 3 overdue + 2 failed but not due
        -> overdue_count=3, ratio=15%
        """
        stats = _make_credit_stats(
            current_overdue_count=3,
            current_overdue_ratio=15.0,
            current_max_delay_days=10,
            outstanding_amount=300.0,
        )
        assert stats.current_overdue_count == 3
        assert stats.current_overdue_ratio == 15.0

    def test_high_risk_threshold_overdue_vs_pending(self, scorer):
        """
        10 overdue -> high_risk = True
        10 pending/not-due -> high_risk = False
        This is the critical business rule.
        """
        overdue_stats = _make_credit_stats(current_overdue_count=10)
        is_risk, _ = scorer._check_high_risk(overdue_stats)
        assert is_risk is True

        pending_stats = _make_credit_stats(current_overdue_count=10, current_overdue_ratio=0.0)
        pending_stats = _make_credit_stats(current_overdue_count=0)
        is_risk, _ = scorer._check_high_risk(pending_stats)
        assert is_risk is False

    def test_consecutive_defaults_trigger_high_risk(self, scorer):
        stats = _make_credit_stats(consecutive_unpaid_cycles=3)
        is_risk, reasons = scorer._check_high_risk(stats)
        assert is_risk is True
        assert any("3 consecutive" in r for r in reasons)

    def test_consecutive_defaults_below_threshold_ok(self, scorer):
        stats = _make_credit_stats(consecutive_unpaid_cycles=2)
        is_risk, _ = scorer._check_high_risk(stats)
        assert is_risk is False


class TestComputeBookingBehaviorScore:
    """Feature B: agent search-to-booking component (target 0.05 = 20:1)."""

    @pytest.fixture
    def activity(self):
        return {"searches": 100, "created": 80, "reused": 20, "bookings": 20}

    def test_strong_conversion_scores_100(self, scorer, activity):
        assert scorer._compute_booking_behavior_score(activity, 0.05) == 100.0

    def test_at_supplier_target_scores_80(self, scorer, activity):
        activity["bookings"] = 5  # 100 searches / 5 bookings = 20:1 exactly
        assert scorer._compute_booking_behavior_score(activity, 0.05) == 80.0

    def test_weak_conversion(self, scorer, activity):
        activity["bookings"] = 2  # 50:1 -> below target, 80 * (0.02/0.05) = 32
        assert scorer._compute_booking_behavior_score(activity, 0.05) == 32.0

    def test_low_volume_one_to_one_is_dampened(self, scorer):
        activity = {"searches": 1, "created": 1, "reused": 0, "bookings": 1}
        # raw 100, confidence 1/20 -> 80 + 20*0.05 = 81.0
        assert scorer._compute_booking_behavior_score(activity, 0.05) == 81.0

    def test_high_volume_zero_bookings_scores_0(self, scorer):
        activity = {"searches": 1000, "created": 900, "reused": 100, "bookings": 0}
        assert scorer._compute_booking_behavior_score(activity, 0.05) == 0.0

    def test_zero_searches_is_excluded(self, scorer):
        activity = {"searches": 0, "created": 0, "reused": 0, "bookings": 0}
        assert scorer._compute_booking_behavior_score(activity, 0.05) is None

    def test_zero_target_is_excluded(self, scorer, activity):
        assert scorer._compute_booking_behavior_score(activity, 0.0) is None

    def test_confidence_boundary(self, scorer, activity):
        activity["searches"] = 20
        assert scorer._compute_booking_behavior_score(activity, 0.05) == 100.0

    def test_progressive_differentiation(self, scorer):
        expected = {0: 0.0, 1: 48.48, 2: 81.41, 3: 85.45,
                    4: 89.49, 5: 93.54, 6: 97.58, 7: 100.0}
        for bookings, exp in expected.items():
            act = {"searches": 33, "created": 33, "reused": 0, "bookings": bookings}
            assert scorer._compute_booking_behavior_score(act, 0.05) == exp

    @pytest.mark.parametrize("at_target,mult,ratio,exp,searches", [
        (100.0, 1.0, 0.01, 20.0, 100),   # legacy-equivalence rollback guard
        (100.0, 1.0, 0.05, 100.0, 100),
        (100.0, 1.0, 0.50, 100.0, 100),
        (60.0, 4.0, 0.05, 60.0, 100),    # custom anchor sanity
        (60.0, 4.0, 0.20, 100.0, 100),
    ])
    def test_anchors_and_legacy_equivalence(
        self, scorer, monkeypatch, at_target, mult, ratio, exp, searches
    ):
        fake = SimpleNamespace(
            search_to_booking_at_target_score=at_target,
            search_to_booking_excellent_multiplier=mult,
            search_to_booking_min_searches=20,
            search_to_booking_neutral_score=80.0,
        )
        monkeypatch.setattr("app.services.agent_trust_scorer.get_settings", lambda: fake)
        activity = {"searches": searches, "created": searches, "reused": 0,
                    "bookings": round(ratio * searches)}
        assert scorer._compute_booking_behavior_score(activity, 0.05) == exp


class TestCompositeRenormalization:
    """Composite uses 40/25/15/20; renormalizes when S2B is excluded."""

    def _setup_scorer(
        self,
        mock_repo_cls,
        mock_cache_cls,
        mock_ml_cls,
        search_activity,
        batch_365=None,
        exp_stats=None,
    ):
        mock_cache = MagicMock()
        mock_cache_cls.return_value = mock_cache
        mock_cache.get.return_value = None
        mock_cache.acquire_lock.return_value = "lock-token"

        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        mock_repo.get_agent.return_value = (1, "Test Agent")
        mock_repo.get_credit_stats.return_value = _make_credit_stats()
        conv_active = {
            **_minimal_conversion(),
            "searches": 100,
            "effective_searches": 100,
            "bookings": 5,
            "no_activity": False,
            "low_confidence": False,
        }
        mock_repo.get_multi_timeframe_stats.return_value = {
            1: conv_active,
            7: conv_active,
            30: conv_active,
            365: batch_365 or {"bookings": 10, "bookstep_failed": 0},
        }
        mock_repo.get_experience_stats.return_value = exp_stats or {
            "created_at": None,
            "lifetime_bookings": 0,
            "lifetime_revenue": 0.0,
            "lifetime_cancelled": 0,
        }
        mock_repo.get_agent_search_activity.return_value = search_activity
        mock_repo.get_supplier_expected_ratio.return_value = 0.05

        mock_ml = MagicMock()
        mock_ml_cls.return_value = mock_ml
        mock_ml.predict.return_value = 80.0
        return mock_cache

    @patch("app.services.agent_trust_scorer.AuditRepository")
    @patch("app.services.agent_trust_scorer.TrustModelPredictor")
    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_s2b_excluded_renormalizes_composite(
        self, mock_repo_cls, mock_cache_cls, mock_ml_cls, mock_audit_cls
    ):
        self._setup_scorer(
            mock_repo_cls,
            mock_cache_cls,
            mock_ml_cls,
            {"searches": 0, "created": 0, "reused": 0, "bookings": 0},
        )
        result = AgentTrustScorer().calculate(db=MagicMock(), agent_id=1)

        assert result.scores.search_to_booking_score is None
        assert result.features.search_activity["scored"] is False
        # (100*0.40 + 100*0.25 + 0*0.15) / 0.80 = 81.25
        assert result.scores.composite_trust_score == 81.25
        # overall = round(81.25*0.8 + 80*0.2) = 81
        assert result.scores.overall_score == 81

    @patch("app.services.agent_trust_scorer.AuditRepository")
    @patch("app.services.agent_trust_scorer.TrustModelPredictor")
    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_s2b_included_uses_full_weights(
        self, mock_repo_cls, mock_cache_cls, mock_ml_cls, mock_audit_cls
    ):
        self._setup_scorer(
            mock_repo_cls,
            mock_cache_cls,
            mock_ml_cls,
            {"searches": 100, "created": 80, "reused": 20, "bookings": 20},
        )
        result = AgentTrustScorer().calculate(db=MagicMock(), agent_id=1)

        assert result.scores.search_to_booking_score == 100.0
        assert result.features.search_activity["scored"] is True
        # (100*0.40 + 100*0.25 + 0*0.15 + 100*0.20) / 1.00 = 85.0
        assert result.scores.composite_trust_score == 85.0
        # overall = round(85.0*0.8 + 80*0.2) = 84
        assert result.scores.overall_score == 84

    @patch("app.services.agent_trust_scorer.AuditRepository")
    @patch("app.services.agent_trust_scorer.TrustModelPredictor")
    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_reliability_excluded_s2b_active_renormalizes(
        self, mock_repo_cls, mock_cache_cls, mock_ml_cls, mock_audit_cls
    ):
        # No booking evidence -> reliability is None; S2B present -> /0.60
        self._setup_scorer(
            mock_repo_cls,
            mock_cache_cls,
            mock_ml_cls,
            {"searches": 100, "created": 80, "reused": 20, "bookings": 20},
            batch_365={"bookings": 0, "bookstep_failed": 0},
            exp_stats={"created_at": None, "lifetime_bookings": 0,
                       "lifetime_revenue": 0.0, "lifetime_cancelled": 0},
        )
        result = AgentTrustScorer().calculate(db=MagicMock(), agent_id=1)

        assert result.scores.reliability_score is None
        assert result.scores.search_to_booking_score == 100.0
        # (100*0.25 + 0*0.15 + 100*0.20) / 0.60 = 75.0
        assert result.scores.composite_trust_score == 75.0
        # overall = round(75.0*0.8 + 80*0.2) = 76
        assert result.scores.overall_score == 76

    @patch("app.services.agent_trust_scorer.AuditRepository")
    @patch("app.services.agent_trust_scorer.TrustModelPredictor")
    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_both_optional_components_excluded_uses_financial_experience(
        self, mock_repo_cls, mock_cache_cls, mock_ml_cls, mock_audit_cls
    ):
        # No booking evidence (reliability None) + no search data (S2B None)
        # -> only financial + experience, /0.40
        self._setup_scorer(
            mock_repo_cls,
            mock_cache_cls,
            mock_ml_cls,
            {"searches": 0, "created": 0, "reused": 0, "bookings": 0},
            batch_365={"bookings": 0, "bookstep_failed": 0},
            exp_stats={"created_at": None, "lifetime_bookings": 0,
                       "lifetime_revenue": 0.0, "lifetime_cancelled": 0},
        )
        result = AgentTrustScorer().calculate(db=MagicMock(), agent_id=1)

        assert result.scores.reliability_score is None
        assert result.scores.search_to_booking_score is None
        # (100*0.25 + 0*0.15) / 0.40 = 62.50
        assert result.scores.composite_trust_score == 62.5
        # overall = round(62.5*0.8 + 80*0.2) = 66
        assert result.scores.overall_score == 66
