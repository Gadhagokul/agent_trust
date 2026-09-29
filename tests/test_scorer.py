from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import REGISTRY

from app.domain.errors import DatabaseUnavailableError, ModelUnavailableError
from app.domain.models import AgentTrustResult
from app.infra.db.repository import CreditStats
from app.ml.trust_model import (
    TRAINING_OUTCOME_PROMOTED,
    TRAINING_OUTCOME_REJECTED,
    TRAINING_OUTCOME_SKIPPED,
    TRAINING_STATUS_COMPLETED,
    TRAINING_STATUS_NOT_READY,
    TrainedCandidate,
    TrainingDataset,
    TrainingState,
    classify_trigger,
    drift_trigger_status,
    evaluate_classifier_candidate,
    model_version_dir,
    read_training_state,
    rollback_champion,
    run_training_controller,
    stage_challenger,
    validate_candidate_metrics,
    verify_artifact_integrity,
)
from app.services.agent_trust_scorer import AgentTrustScorer


@pytest.fixture
def scorer():
    return AgentTrustScorer()


def _make_credit_stats(
    current_overdue_count: int = 0,
    current_overdue_ratio: float = 0.0,
    current_max_delay_days: int = 0,
    outstanding_amount: float = 0.0,
    historical_late_payment_count: int = 0,
    historical_late_payment_ratio: float = 0.0,
    average_payment_delay_days: float = 0.0,
    maximum_payment_delay_days: int = 0,
    consecutive_unpaid_cycles: int = 0,
) -> CreditStats:
    return CreditStats(
        current_overdue_count=current_overdue_count,
        current_overdue_ratio=current_overdue_ratio,
        current_max_delay_days=current_max_delay_days,
        outstanding_amount=outstanding_amount,
        historical_late_payment_count=historical_late_payment_count,
        historical_late_payment_ratio=historical_late_payment_ratio,
        average_payment_delay_days=average_payment_delay_days,
        maximum_payment_delay_days=maximum_payment_delay_days,
        consecutive_unpaid_cycles=consecutive_unpaid_cycles,
    )


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
            "historical_max_payment_delay_days": 0,
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
        mock_repo.get_supplier_l2b_targets.return_value = []
        mock_repo.get_agent_supplier_searches.return_value = []
        mock_repo.get_agent_booking_counts_by_provider.return_value = []

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
        mock_repo.get_supplier_l2b_targets.return_value = []
        mock_repo.get_agent_supplier_searches.return_value = []
        mock_repo.get_agent_booking_counts_by_provider.return_value = []

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
        score, detail = scorer._compute_reliability_score(batch, exp)
        assert score is None
        assert detail is None

    def test_all_bookings_successful(self, scorer):
        batch = {365: {"bookings": 100, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 100}
        score, _ = scorer._compute_reliability_score(batch, exp)
        assert score == 100.0

    def test_mixed_existing_bookings_reliability_100(self, scorer):
        batch = {365: {"bookings": 10, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        score, _ = scorer._compute_reliability_score(batch, exp)
        assert score == 100.0

    def test_high_cancellation_rate(self, scorer):
        batch = {365: {"bookings": 50, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 50, "lifetime_bookings": 50}
        score, _ = scorer._compute_reliability_score(batch, exp)
        expected = round(0.6429 * 100.0 + 0.3571 * 50.0, 2)
        assert score == expected
        assert score < 88.0

    def test_weights_renormalize_over_configured_sum(self, scorer, monkeypatch):
        fake = SimpleNamespace(
            reliability_component_weights={"booking_success": 0.5, "cancellation_quality": 0.5},
            reliability_confidence_enabled=False,
            reliability_cancellation_confidence_enabled=False,
            reliability_wilson_z=1.96,
            reliability_min_observations=30,
            reliability_success_prior_rate=0.5,
            reliability_cancellation_prior_rate=0.5,
        )
        monkeypatch.setattr("app.services.agent_trust_scorer.get_settings", lambda: fake)
        batch = {365: {"bookings": 100, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 50, "lifetime_bookings": 50}
        # 0.5*100 + 0.5*50 = 75.0 (no renormalization drift with two weights)
        score, _ = scorer._compute_reliability_score(batch, exp)
        assert score == 75.0

    def test_cancellation_quality_mirrors_implementation(self, scorer):
        batch = {365: {"bookings": 1, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 100, "lifetime_bookings": 1}
        score, _ = scorer._compute_reliability_score(batch, exp)
        # cancel quality = (1 - 100/101)*100 = 0.99; clamp is defensive only
        cancel_rate = 100.0 / 101.0
        cancel_quality = max(0.0, (1.0 - cancel_rate) * 100.0)
        expected = round(0.6429 * 100.0 + 0.3571 * cancel_quality, 2)
        assert score == expected


class TestDetermineTier:
    def test_platinum(self, scorer):
        assert scorer._determine_tier(85, False, 0, 0) == "Platinum"

    def test_gold(self, scorer):
        assert scorer._determine_tier(70, False, 0, 0) == "Gold"

    def test_silver(self, scorer):
        assert scorer._determine_tier(55, False, 0, 0) == "Silver"

    def test_bronze(self, scorer):
        assert scorer._determine_tier(40, False, 0, 0) == "Bronze"

    def test_high_risk_low_score(self, scorer):
        # Below the bronze floor is the High Risk sentinel even with a clean
        # payment record -- a deliberate, spec-sanctioned naming conflation.
        assert scorer._determine_tier(20, False, 0, 0) == "High Risk"

    def test_high_risk_flag_forces_sentinel(self, scorer):
        assert scorer._determine_tier(85, True, 0, 0) == "High Risk"
        assert scorer._determine_tier(85, True, 10, 70) == "High Risk"

    def test_platinum_requires_zero_overdue(self, scorer):
        assert scorer._determine_tier(85, False, 1, 0) == "Gold"

    def test_platinum_requires_low_delay(self, scorer):
        assert scorer._determine_tier(85, False, 0, 5) == "Gold"

    @pytest.mark.parametrize(
        ("overall", "expected"),
        [
            (79.9, "Gold"),
            (80.0, "Platinum"),
            (64.9, "Silver"),
            (50.0, "Silver"),
            (49.9, "Bronze"),
            (35.0, "Bronze"),
            (34.9, "High Risk"),
        ],
    )
    def test_boundaries(self, scorer, overall, expected):
        assert scorer._determine_tier(overall, False, 0, 0) == expected

    def _override_settings(self, monkeypatch, **overrides):
        base = {
            "tier_thresholds": {
                "platinum": 80.0,
                "gold": 65.0,
                "silver": 50.0,
                "bronze": 35.0,
            },
            "platinum_max_overdue_count": 0,
            "platinum_max_delay_days": 5,
        }
        fake = SimpleNamespace(**{**base, **overrides})
        monkeypatch.setattr(
            "app.services.agent_trust_scorer.get_settings", lambda: fake
        )
        return fake

    def test_thresholds_come_from_config(self, scorer, monkeypatch):
        # The regression guard for A2: this fails if anyone re-hard-codes the
        # 80/65/50/35 literals back into _determine_tier.
        self._override_settings(
            monkeypatch,
            tier_thresholds={
                "platinum": 95.0,
                "gold": 75.0,
                "silver": 55.0,
                "bronze": 30.0,
            },
        )
        assert scorer._determine_tier(95, False, 0, 0) == "Platinum"
        assert scorer._determine_tier(92, False, 0, 0) == "Gold"
        assert scorer._determine_tier(76, False, 0, 0) == "Gold"
        assert scorer._determine_tier(56, False, 0, 0) == "Silver"
        assert scorer._determine_tier(31, False, 0, 0) == "Bronze"
        assert scorer._determine_tier(30, False, 0, 0) == "Bronze"
        assert scorer._determine_tier(29.9, False, 0, 0) == "High Risk"

    def test_platinum_conditions_come_from_config(self, scorer, monkeypatch):
        self._override_settings(monkeypatch, platinum_max_delay_days=30)
        assert scorer._determine_tier(85, False, 0, 20) == "Platinum"
        assert scorer._determine_tier(85, False, 0, 31) == "Gold"


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

    def test_consecutive_threshold_configurable(self, scorer, monkeypatch):
        fake = SimpleNamespace(
            credit_max_overdue_ratio=50.0,
            credit_max_delay_days=60,
            credit_max_overdue_count=10,
            credit_max_consecutive_overdue_cycles=4,
        )
        monkeypatch.setattr("app.services.agent_trust_scorer.get_settings", lambda: fake)
        stats = _make_credit_stats(consecutive_unpaid_cycles=3)
        is_risk, _ = scorer._check_high_risk(stats)
        assert is_risk is False

    def test_consecutive_defaults_below_threshold_ok(self, scorer):
        stats = _make_credit_stats(consecutive_unpaid_cycles=2)
        is_risk, _ = scorer._check_high_risk(stats)
        assert is_risk is False


class TestScoringConfiguration:
    """Configuration defaults confirm the senior's 40/25/15/20 component weights."""

    def _settings(self):
        from app.infra.settings import Settings

        return Settings(_env_file=None)

    def test_composite_weights_default_matches_senior_spec(self):
        weights = self._settings().composite_weights
        assert weights == {
            "reliability": 0.40,
            "financial": 0.25,
            "experience": 0.15,
            "search_to_booking": 0.20,
        }

    def test_overdue_boundary_defaults_to_exclusive(self):
        assert self._settings().credit_overdue_boundary == "<"

    def test_high_risk_thresholds_are_configurable(self):
        s = self._settings()
        assert s.credit_max_delay_days == 60
        assert s.credit_max_overdue_ratio == 50.0
        assert s.credit_max_overdue_count == 10
        assert s.credit_max_consecutive_overdue_cycles == 3
        assert s.high_risk_score_cap == 30

    def test_attribution_lists_default_to_empty_open(self):
        s = self._settings()
        assert s.non_agent_failure_reasons == []
        assert s.non_agent_cancellation_reasons == []


class TestTierThresholdValidation:
    """A2: tier config is validated like every other settings group."""

    def _settings(self, **overrides):
        from app.infra.settings import Settings

        return Settings(_env_file=None, **overrides)

    def test_defaults_pass(self):
        self._settings()._validate_startup()

    def test_valid_override_passes(self):
        self._settings(
            tier_thresholds={
                "platinum": 95.0,
                "gold": 75.0,
                "silver": 55.0,
                "bronze": 30.0,
            },
            platinum_max_overdue_count=1,
            platinum_max_delay_days=10,
        )._validate_startup()

    def test_missing_key_rejected(self):
        with pytest.raises(RuntimeError, match="tier_thresholds must define"):
            self._settings(tier_thresholds={"platinum": 80.0, "gold": 65.0,
                                            "silver": 50.0})._validate_startup()

    def test_extra_key_rejected(self):
        with pytest.raises(RuntimeError, match="tier_thresholds must define"):
            self._settings(tier_thresholds={"platinum": 80.0, "gold": 65.0,
                                            "silver": 50.0, "bronze": 35.0,
                                            "diamond": 90.0})._validate_startup()

    def test_high_risk_key_rejected_as_sentinel(self):
        # "High Risk" must stay a sentinel, not a fifth configurable band.
        with pytest.raises(RuntimeError, match="tier_thresholds must define"):
            self._settings(tier_thresholds={"platinum": 80.0, "gold": 65.0,
                                            "silver": 50.0, "bronze": 35.0,
                                            "high_risk": 35.0})._validate_startup()

    def test_out_of_range_rejected(self):
        with pytest.raises(RuntimeError, match="within 0-100"):
            self._settings(tier_thresholds={"platinum": 120.0, "gold": 65.0,
                                            "silver": 50.0, "bronze": 35.0})._validate_startup()
        with pytest.raises(RuntimeError, match="within 0-100"):
            self._settings(tier_thresholds={"platinum": -1.0, "gold": 65.0,
                                            "silver": 50.0, "bronze": 35.0})._validate_startup()

    def test_non_descending_rejected(self):
        with pytest.raises(RuntimeError, match="strictly descending"):
            self._settings(tier_thresholds={"platinum": 60.0, "gold": 65.0,
                                            "silver": 50.0, "bronze": 35.0})._validate_startup()
        with pytest.raises(RuntimeError, match="strictly descending"):
            self._settings(tier_thresholds={"platinum": 80.0, "gold": 65.0,
                                            "silver": 50.0, "bronze": 50.0})._validate_startup()

    def test_negative_platinum_scalar_rejected(self):
        with pytest.raises(RuntimeError, match="must be non-negative"):
            self._settings(platinum_max_overdue_count=-1)._validate_startup()
        with pytest.raises(RuntimeError, match="must be non-negative"):
            self._settings(platinum_max_delay_days=-1)._validate_startup()


class TestCombineComposite:
    """Missing-data redistribution exposes the effective weights (senior §5 / §14)."""

    WEIGHTS: ClassVar[dict[str, float]] = {
        "reliability": 0.40,
        "financial": 0.25,
        "experience": 0.15,
        "search_to_booking": 0.20,
    }

    def test_all_available_uses_full_weights(self, scorer):
        comp, weights_used, avail, unavail = scorer._combine_composite(
            {
                "reliability": 100.0,
                "financial": 100.0,
                "experience": 0.0,
                "search_to_booking": 100.0,
            },
            self.WEIGHTS,
        )
        assert comp == 85.0
        assert weights_used == self.WEIGHTS
        assert avail == ["reliability", "financial", "experience", "search_to_booking"]
        assert unavail == []

    def test_s2b_unavailable_redistributes_per_senior_example(self, scorer):
        """Senior §5 example: reliability 50%, financial 31.25%, experience 18.75%."""
        comp, weights_used, avail, unavail = scorer._combine_composite(
            {
                "reliability": 100.0,
                "financial": 100.0,
                "experience": 0.0,
                "search_to_booking": None,
            },
            self.WEIGHTS,
        )
        assert comp == 81.25
        assert weights_used == {
            "reliability": 0.5,
            "financial": 0.3125,
            "experience": 0.1875,
        }
        assert avail == ["reliability", "financial", "experience"]
        assert unavail == ["search_to_booking"]

    def test_reliability_and_s2b_unavailable(self, scorer):
        comp, weights_used, avail, unavail = scorer._combine_composite(
            {
                "reliability": None,
                "financial": 100.0,
                "experience": 0.0,
                "search_to_booking": None,
            },
            self.WEIGHTS,
        )
        assert comp == 62.5
        assert weights_used == {"financial": 0.625, "experience": 0.375}
        assert avail == ["financial", "experience"]
        assert unavail == ["reliability", "search_to_booking"]


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


class TestScoreL2bForTarget:
    """Shared scoring curve used by aggregate + supplier L2B (Feature B)."""

    def test_exact_target_scores_80(self, scorer):
        assert scorer._score_l2b_for_target({"searches": 100, "bookings": 5}, 0.05) == 80.0

    def test_none_target_is_excluded(self, scorer):
        assert scorer._score_l2b_for_target({"searches": 100, "bookings": 5}, None) is None

    def test_access_count_not_per_row_inflation(self, scorer):
        # 2 bookings / 5 accesses = 40% ratio -> 85.0, never inflated to 100.0.
        # Guards SUM(access_count) semantics: a few accesses must not read as
        # perfect conversion just because the row counts are small.
        assert scorer._score_l2b_for_target({"searches": 5, "bookings": 2}, 0.05) == 85.0


class TestComputeSupplierL2bComponent:
    """Supplier-specific L2B: per-supplier ratios vs site benchmarks + cap."""

    def _call(self, scorer, targets, searches, bookings):
        return scorer._compute_supplier_l2b_component(targets, searches, bookings)

    def test_cap_redistribution_supplier_example(self, scorer):
        # Shares 80/15/5, scores 40/80/90. Emirates (80%) is capped at 50% and
        # the 30% excess is redistributed proportionally -> 61.25 (senior §13).
        targets = [
            {"code": "EK", "name": "Emirates", "target": 0.05},
            {"code": "QR", "name": "Qatar", "target": 0.2},
            {"code": "EY", "name": "Etihad", "target": 0.16},
        ]
        searches = [
            {"code": "EK", "searches": 320},
            {"code": "QR", "searches": 60},
            {"code": "EY", "searches": 20},
        ]
        bookings = [
            {"provider": "Emirates", "bookings": 8},
            {"provider": "Qatar", "bookings": 12},
            {"provider": "Etihad", "bookings": 8},
        ]
        component, detail = self._call(scorer, targets, searches, bookings)

        assert component == 61.25
        scores = {s["code"]: s["score"] for s in detail["supplier_scores"]}
        assert scores == {"EK": 40.0, "QR": 80.0, "EY": 90.0}
        shares = {s["code"]: s["share"] for s in detail["supplier_scores"]}
        assert shares["EK"] == 0.5 and shares["QR"] == 0.375 and shares["EY"] == 0.125
        assert detail["unconfigured_suppliers"] == []

    def test_two_supplier_redistribution(self, scorer):
        # A: 84, B: 64, shares 2/3-1/3 -> capped 50/50 -> 74.0
        targets = [
            {"code": "A", "name": "Sup A", "target": 0.05},
            {"code": "B", "name": "Sup B", "target": 0.05},
        ]
        searches = [
            {"code": "A", "searches": 600},
            {"code": "B", "searches": 300},
        ]
        bookings = [
            {"provider": "Sup A", "bookings": 48},
            {"provider": "Sup B", "bookings": 12},
        ]
        component, _ = self._call(scorer, targets, searches, bookings)
        assert component == 74.0

    def test_unconfigured_supplier_excluded(self, scorer):
        targets = [
            {"code": "A", "name": "Sup A", "target": 0.05},
            {"code": "C", "name": "Sup C", "target": None},
        ]
        searches = [
            {"code": "A", "searches": 200},
            {"code": "C", "searches": 100},
        ]
        bookings = [{"provider": "Sup A", "bookings": 10}]
        component, detail = self._call(scorer, targets, searches, bookings)
        assert component == 80.0
        assert detail["unconfigured_suppliers"] == ["C"]
        assert detail["component"] == 80.0

    def test_all_unconfigured_returns_none(self, scorer):
        targets = [{"code": "C", "name": "Sup C", "target": None}]
        searches = [{"code": "C", "searches": 100}]
        bookings = []
        component, detail = self._call(scorer, targets, searches, bookings)
        assert component is None
        assert detail["unconfigured_suppliers"] == ["C"]

    def test_missing_target_entry_excluded(self, scorer):
        component, detail = self._call(scorer, [], [{"code": "X", "searches": 100}], [])
        assert component is None
        assert detail["unconfigured_suppliers"] == ["X"]

    def test_single_supplier_keeps_full_share(self, scorer):
        targets = [{"code": "SUP", "name": "Alpha Air", "target": 0.05}]
        searches = [{"code": "SUP", "searches": 100}]
        bookings = [{"provider": "Alpha Air", "bookings": 5}]
        component, detail = self._call(scorer, targets, searches, bookings)
        assert component == 80.0
        assert detail["supplier_scores"][0]["share"] == 1.0

    def test_zero_bookings_scores_zero_not_none(self, scorer):
        targets = [{"code": "SUP", "name": "Alpha Air", "target": 0.05}]
        searches = [{"code": "SUP", "searches": 100}]
        bookings = []
        component, _ = self._call(scorer, targets, searches, bookings)
        assert component == 0.0


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
        supplier_targets=None,
        supplier_searches=None,
        booking_counts=None,
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
        mock_repo.get_supplier_l2b_targets.return_value = supplier_targets or []
        mock_repo.get_agent_supplier_searches.return_value = supplier_searches or []
        mock_repo.get_agent_booking_counts_by_provider.return_value = booking_counts or []

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
            supplier_targets=[{"code": "SUP", "name": "Alpha Air", "target": 0.05}],
            supplier_searches=[{"code": "SUP", "searches": 100}],
            booking_counts=[{"provider": "Alpha Air", "bookings": 20}],
        )
        result = AgentTrustScorer().calculate(db=MagicMock(), agent_id=1)

        assert result.scores.search_to_booking_score == 100.0
        assert result.features.search_activity["scored"] is True
        # (100*0.40 + 100*0.25 + 0*0.15 + 100*0.20) / 1.00 = 85.0
        assert result.scores.composite_trust_score == 85.0
        # Sprint 5: ML programme ships DISABLED by default (senior 15.1 gate);
        # overall = round(composite_trust) = round(85.0) = 85 -- never a blend.
        assert result.scores.overall_score == 85

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
            supplier_targets=[{"code": "SUP", "name": "Alpha Air", "target": 0.05}],
            supplier_searches=[{"code": "SUP", "searches": 100}],
            booking_counts=[{"provider": "Alpha Air", "bookings": 20}],
        )
        result = AgentTrustScorer().calculate(db=MagicMock(), agent_id=1)

        assert result.scores.reliability_score is None
        assert result.scores.search_to_booking_score == 100.0
        # (100*0.25 + 0*0.15 + 100*0.20) / 0.60 = 75.0
        assert result.scores.composite_trust_score == 75.0
        # Sprint 5: ML programme ships DISABLED by default (senior 15.1 gate);
        # overall = round(composite_trust) = round(75.0) = 75 -- never a blend.
        assert result.scores.overall_score == 75

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
        # (100*0.25 + 100*0.15) / 0.40 = 62.50
        assert result.scores.composite_trust_score == 62.5
        # Sprint 5: ML programme ships DISABLED by default (senior 15.1 gate);
        # overall = round(composite_trust) = round(62.5) = 62 -- never a blend.
        assert result.scores.overall_score == 62

    @patch("app.services.agent_trust_scorer.AuditRepository")
    @patch("app.services.agent_trust_scorer.TrustModelPredictor")
    @patch("app.services.agent_trust_scorer.CacheAdapter")
    @patch("app.services.agent_trust_scorer.AgentRepository")
    def test_high_risk_evaluated_once_per_calculate(
        self, mock_repo_cls, mock_cache_cls, mock_ml_cls, mock_audit_cls
    ):
        # A2: the credit_max_* comparisons must run exactly once -- the old
        # _determine_tier duplicate block is gone.
        self._setup_scorer(
            mock_repo_cls,
            mock_cache_cls,
            mock_ml_cls,
            {"searches": 0, "created": 0, "reused": 0, "bookings": 0},
        )
        scorer = AgentTrustScorer()
        original = scorer._check_high_risk
        calls: list[int] = []

        def counting(credit_stats):
            calls.append(1)
            return original(credit_stats)

        scorer._check_high_risk = counting
        scorer.calculate(db=MagicMock(), agent_id=1)
        assert len(calls) == 1


# ── Sprint 6: Training Controller & automated model lifecycle ─────────────────


def _s6_settings(**overrides):
    base = {
        "ml_enabled": True,
        "ml_targets": ["severe_default"],
        "ml_horizon_days": 30,
        "ml_model_registry_dir": "unused",
        "ml_trigger_increment": 100,
        "ml_training_interval_days": 7,
        "ml_promotion_headroom": 0.002,
        "ml_classification_min_pr_auc": 0.30,
        "ml_classification_min_f1": 0.40,
        "ml_classification_max_brier": 0.25,
        "ml_segment_max_recall_drop": 0.05,
        "ml_readiness_min_samples": 2,
        "ml_readiness_min_positive": 1,
        "ml_readiness_min_negative": 1,
        "ml_readiness_min_agents": 1,
        "ml_readiness_max_positive_ratio": 0.95,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _counter(name: str) -> float:
    value = REGISTRY.get_sample_value(name)
    return 0.0 if value is None else float(value)


def _s6_dataset(target, *, as_of, db, samples=4, positive=2):
    return TrainingDataset(
        target=target,
        feature_rows=[[float(index)] for index in range(samples)],
        labels=[1] * positive + [0] * (samples - positive),
        stats={
            "samples": samples,
            "positive": positive,
            "negative": samples - positive,
            "agents": samples,
        },
        labeled_records=1000,
    )


def _s6_trainer(actual, probabilities, artifact=b"candidate-artifact"):
    def _train(dataset):
        return TrainedCandidate(
            target=dataset.target,
            actual=list(actual),
            probabilities=list(probabilities),
            artifact=artifact,
            segments={"high_volume": list(range(len(actual)))},
        )

    return _train


def test_controller_is_silent_noop_while_disabled(tmp_path):
    settings = _s6_settings(ml_enabled=False, ml_model_registry_dir=str(tmp_path))
    runs_before = _counter("agent_trust_training_runs_total")
    success_before = _counter("agent_trust_training_success_total")
    failure_before = _counter("agent_trust_training_failures_total")
    promotions_before = _counter("agent_trust_model_promotions_total")

    result = run_training_controller(
        settings,
        build_dataset=lambda *a, **k: pytest.fail("dataset must never be built while off"),
        train_candidate=lambda *a, **k: pytest.fail("trainer must never run while off"),
    )

    assert result == {
        "status": TRAINING_STATUS_NOT_READY,
        "reason": "ml_programme_disabled",
        "targets": {},
    }
    assert _counter("agent_trust_training_runs_total") == runs_before
    assert _counter("agent_trust_training_success_total") == success_before
    assert _counter("agent_trust_training_failures_total") == failure_before
    assert _counter("agent_trust_model_promotions_total") == promotions_before
    assert list(tmp_path.iterdir()) == []


def test_controller_not_ready_without_approved_targets(tmp_path):
    settings = _s6_settings(ml_targets=[], ml_model_registry_dir=str(tmp_path))

    result = run_training_controller(
        settings,
        build_dataset=lambda *a, **k: pytest.fail("dataset must never be built"),
        train_candidate=lambda *a, **k: pytest.fail("trainer must never run"),
    )

    assert result["status"] == TRAINING_STATUS_NOT_READY
    assert result["reason"] == "ml_targets_empty"
    assert list(tmp_path.iterdir()) == []


def test_classify_trigger_growth_uses_labelled_records_not_total_bookings():
    state = TrainingState(
        target="severe_default",
        last_successful_training_at="2026-01-01T00:00:00+00:00",
        last_training_labeled_records=500,
    )
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)

    not_due = classify_trigger(
        state=state, labeled_records=550, increment=100, interval_days=7, now=now
    )
    due = classify_trigger(
        state=state, labeled_records=600, increment=100, interval_days=7, now=now
    )

    assert not_due.triggered is False
    assert not_due.reason == "none"
    assert due.triggered is True
    assert due.reason == "data_growth"
    assert due.detail == "growth=100"


def test_classify_trigger_interval_and_cold_start():
    cold = classify_trigger(
        state=TrainingState(target="severe_default"),
        labeled_records=0,
        increment=100,
        interval_days=7,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    stale = classify_trigger(
        state=TrainingState(
            target="severe_default",
            last_successful_training_at="2025-12-01T00:00:00+00:00",
            last_training_labeled_records=10,
        ),
        labeled_records=10,
        increment=100,
        interval_days=7,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert cold.triggered is True
    assert cold.reason == "interval"
    assert cold.detail == "no_successful_training_yet"
    assert stale.triggered is True
    assert stale.reason == "interval"
    assert "elapsed_days" in stale.detail


def test_drift_trigger_is_deferred_not_evaluated():
    decision = drift_trigger_status()

    assert decision.triggered is False
    assert decision.reason == "drift_not_evaluated"
    assert decision.detail == "drift_trigger_deferred"


def test_no_trigger_is_skipped_and_is_not_a_training_success(tmp_path):
    settings = _s6_settings(ml_model_registry_dir=str(tmp_path))
    state = TrainingState(
        target="severe_default",
        last_successful_training_at="2026-01-01T00:00:00+00:00",
        last_training_labeled_records=1000,
    )
    from app.ml.trust_model import write_training_state

    write_training_state(str(tmp_path), state)
    runs_before = _counter("agent_trust_training_runs_total")
    success_before = _counter("agent_trust_training_success_total")

    result = run_training_controller(
        settings,
        build_dataset=lambda *a, **k: pytest.fail("dataset must not be built"),
        train_candidate=lambda *a, **k: pytest.fail("trainer must not run"),
        labeled_count_reader=lambda db, target, as_of: 1000,
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    target_result = result["targets"]["severe_default"]
    assert target_result["outcome"] == TRAINING_OUTCOME_SKIPPED
    assert target_result["reason"] == "no_trigger"
    assert _counter("agent_trust_training_runs_total") == runs_before
    assert _counter("agent_trust_training_success_total") == success_before
    persisted = read_training_state(str(tmp_path), "severe_default")
    assert persisted.last_run_outcome == TRAINING_OUTCOME_SKIPPED
    assert persisted.last_run_reason == "no_trigger"


def test_readiness_not_ready_is_skipped_and_not_a_success(tmp_path):
    settings = _s6_settings(ml_model_registry_dir=str(tmp_path))
    success_before = _counter("agent_trust_training_success_total")
    runs_before = _counter("agent_trust_training_runs_total")

    result = run_training_controller(
        settings,
        build_dataset=lambda target, *, as_of, db: _s6_dataset(
            target, as_of=as_of, db=db, samples=1, positive=1
        ),
        train_candidate=_s6_trainer([0, 1], [0.1, 0.9]),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    target_result = result["targets"]["severe_default"]
    assert target_result["outcome"] == TRAINING_OUTCOME_SKIPPED
    assert target_result["reason"] == "readiness_not_ready"
    assert _counter("agent_trust_training_runs_total") == runs_before + 1
    assert _counter("agent_trust_training_success_total") == success_before


def test_first_candidate_is_promoted_as_first_production(tmp_path):
    settings = _s6_settings(ml_model_registry_dir=str(tmp_path))
    promotions_before = _counter("agent_trust_model_promotions_total")
    success_before = _counter("agent_trust_training_success_total")

    result = run_training_controller(
        settings,
        build_dataset=_s6_dataset,
        train_candidate=_s6_trainer([0, 1], [0.1, 0.9]),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    target_result = result["targets"]["severe_default"]
    assert result["status"] == TRAINING_STATUS_COMPLETED
    assert target_result["outcome"] == TRAINING_OUTCOME_PROMOTED
    assert target_result["decision"] == "first_production"
    assert target_result["version"] == "v001"
    assert _counter("agent_trust_model_promotions_total") == promotions_before + 1
    assert _counter("agent_trust_training_success_total") == success_before + 1
    state = read_training_state(str(tmp_path), "severe_default")
    assert state.current_production_version == "v001"
    assert state.previous_known_good_production_version is None
    assert state.current_production_score == 1.0
    artifact = model_version_dir(str(tmp_path), "severe_default", "v001") / "model.joblib"
    assert artifact.read_bytes() == b"candidate-artifact"


def test_challenger_that_does_not_beat_headroom_keeps_champion(tmp_path):
    registry = str(tmp_path)
    settings = _s6_settings(ml_model_registry_dir=registry)
    run_training_controller(
        settings,
        build_dataset=_s6_dataset,
        train_candidate=_s6_trainer([0, 1], [0.4, 0.6]),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    rejections_before = _counter("agent_trust_model_rejections_total")

    result = run_training_controller(
        settings,
        build_dataset=_s6_dataset,
        train_candidate=_s6_trainer([0, 1], [0.45, 0.55]),
        labeled_count_reader=lambda db, target, as_of: 5000,
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    target_result = result["targets"]["severe_default"]
    assert target_result["outcome"] == TRAINING_OUTCOME_REJECTED
    assert target_result["decision"] in {"keep_champion", "reject_challenger"}
    assert _counter("agent_trust_model_rejections_total") == rejections_before + 1
    state = read_training_state(registry, "severe_default")
    assert state.current_production_version == "v001"


def test_promotion_then_rollback_targets_previous_known_good(tmp_path):
    registry = str(tmp_path)
    settings = _s6_settings(ml_model_registry_dir=registry)
    # Champion candidate: passes every gate (pr_auc 0.5833, f1 0.5, brier 0.2456)
    # but is beatable, so the next run can genuinely win the comparison.
    run_training_controller(
        settings,
        build_dataset=_s6_dataset,
        train_candidate=_s6_trainer([0, 0, 1, 1], [0.6, 0.4, 0.6, 0.45]),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    run_training_controller(
        settings,
        build_dataset=_s6_dataset,
        train_candidate=_s6_trainer([0, 1], [0.1, 0.9]),
        labeled_count_reader=lambda db, target, as_of: 5000,
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    promoted = read_training_state(registry, "severe_default")
    assert promoted.current_production_version == "v002"
    assert promoted.previous_known_good_production_version == "v001"
    rollbacks_before = _counter("agent_trust_model_rollbacks_total")

    rolled_back = rollback_champion(registry_dir=registry, state=promoted)

    assert _counter("agent_trust_model_rollbacks_total") == rollbacks_before + 1
    assert rolled_back.current_production_version == "v003"
    assert rolled_back.previous_known_good_production_version == "v002"
    restored = model_version_dir(registry, "severe_default", "v003") / "model.joblib"
    assert restored.read_bytes() == b"candidate-artifact"
    assert rolled_back.last_run_outcome == "ROLLED_BACK"


def test_rollback_without_previous_known_good_is_refused():
    state = TrainingState(target="severe_default", current_production_version="v001")

    with pytest.raises(ModelUnavailableError):
        rollback_champion(registry_dir="unused", state=state)


def test_artifact_integrity_detects_tampering(tmp_path):
    version, artifact_path, digest = stage_challenger(
        registry_dir=str(tmp_path),
        target="severe_default",
        artifact=b"original-bytes",
    )

    assert version == "v001"
    assert verify_artifact_integrity(artifact_path, digest) is True
    artifact_path.write_bytes(b"tampered")
    assert verify_artifact_integrity(artifact_path, digest) is False


def test_classifier_metrics_match_hand_computed_values():
    perfect = evaluate_classifier_candidate(
        [0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9], segments={"high_volume": [2, 3]}
    )
    mixed = evaluate_classifier_candidate([0, 1, 1, 0], [0.6, 0.7, 0.8, 0.2])
    degenerate = evaluate_classifier_candidate([1, 1, 1], [0.2, 0.5, 0.9])

    assert perfect["roc_auc"] == 1.0
    assert perfect["pr_auc"] == 1.0
    assert perfect["precision"] == 1.0
    assert perfect["recall"] == 1.0
    assert perfect["f1"] == 1.0
    assert perfect["brier"] == 0.025
    assert perfect["segment_recall_high_volume"] == 1.0
    assert mixed["precision"] == 0.6667
    assert mixed["recall"] == 1.0
    assert mixed["f1"] == 0.8
    assert mixed["fp"] == 1.0
    assert mixed["tp"] == 2.0
    assert degenerate["degenerate"] == 1.0
    assert degenerate["pr_auc"] == 0.0


def test_validation_gate_rejects_low_quality_and_segment_regression():
    low_quality = {"degenerate": 0.0, "pr_auc": 0.10, "f1": 0.90, "brier": 0.10}
    regressed = {
        "degenerate": 0.0,
        "pr_auc": 0.80,
        "f1": 0.90,
        "brier": 0.10,
        "segment_recall_low_volume": 0.40,
    }

    quality_ok, quality_reason = validate_candidate_metrics(
        low_quality, min_pr_auc=0.30, min_f1=0.40, max_brier=0.25
    )
    segment_ok, segment_reason = validate_candidate_metrics(
        regressed,
        min_pr_auc=0.30,
        min_f1=0.40,
        max_brier=0.25,
        champion_segment_recall={"low_volume": 0.90},
        max_segment_recall_drop=0.05,
    )
    passing, passing_reason = validate_candidate_metrics(
        {"degenerate": 0.0, "pr_auc": 0.80, "f1": 0.90, "brier": 0.10},
        min_pr_auc=0.30,
        min_f1=0.40,
        max_brier=0.25,
    )

    assert quality_ok is False
    assert "pr_auc_below_minimum" in quality_reason
    assert segment_ok is False
    assert "segment_regression" in segment_reason
    assert passing is True
    assert passing_reason == "passed"


def test_supervisor_is_not_enabled_with_shipped_defaults():
    from app.infra.settings import Settings
    from app.main import training_supervisor_enabled

    settings = Settings(
        db_host="localhost",
        db_database="db",
        db_username="user",
        db_password="password",
        laravel_service_token="x" * 40,
    )

    assert settings.ml_enabled is False
    assert settings.ml_targets == []
    assert training_supervisor_enabled(settings) is False
    assert training_supervisor_enabled(
        _s6_settings(ml_enabled=True, ml_targets=["severe_default"])
    ) is True
