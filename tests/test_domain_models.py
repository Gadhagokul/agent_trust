from datetime import datetime

import pytest
from pydantic import ValidationError

from app.domain.models import (
    AgentTrustFeatures,
    AgentTrustResult,
    AgentTrustScores,
    ConversionMetrics,
)


class TestConversionMetrics:
    def test_defaults(self):
        m = ConversionMetrics(
            searches=10,
            bookstep_failed=1,
            adjusted_bookstep_failed=1,
            other_step_failed=0,
            effective_searches=9,
            bookings=3,
            booking_volume=1500.0,
            avg_booking_value=500.0,
            revenue_consistency=100.0,
        )
        assert m.searches == 10
        assert m.no_activity is False
        assert m.low_confidence is False

    def test_no_activity_flag(self):
        m = ConversionMetrics(
            searches=0,
            bookstep_failed=0,
            adjusted_bookstep_failed=0,
            other_step_failed=0,
            effective_searches=0,
            bookings=0,
            booking_volume=0.0,
            avg_booking_value=0.0,
            revenue_consistency=0.0,
            no_activity=True,
        )
        assert m.no_activity is True


class TestAgentTrustScores:
    def test_minimal_valid(self):
        s = AgentTrustScores(overall_score=50)
        assert s.overall_score == 50
        assert s.reliability_score is None
        assert s.composite_trust_score == 0.0

    def test_overall_score_bounds(self):
        with pytest.raises(ValidationError):
            AgentTrustScores(overall_score=150)
        with pytest.raises(ValidationError):
            AgentTrustScores(overall_score=-1)

    def test_full_scores(self):
        s = AgentTrustScores(
            operational_score=80,
            reliability_score=90.0,
            financial_score=85.0,
            experience_score=70.0,
            composite_trust_score=84.5,
            ml_calibration_score=78.0,
            overall_score=82,
        )
        assert s.reliability_score == 90.0
        assert s.composite_trust_score == 84.5


class TestAgentTrustFeatures:
    def test_valid_features(self):
        daily = ConversionMetrics(
            searches=1,
            bookstep_failed=0,
            adjusted_bookstep_failed=0,
            other_step_failed=0,
            effective_searches=1,
            bookings=1,
            booking_volume=500.0,
            avg_booking_value=500.0,
            revenue_consistency=0.0,
        )
        f = AgentTrustFeatures(
            current_max_delay_days=0,
            current_overdue_ratio=0.0,
            current_overdue_count=0,
            outstanding_amount=0.0,
            historical_late_payment_count=0,
            historical_late_payment_ratio=0.0,
            average_payment_delay_days=0.0,
            daily=daily,
            weekly=daily,
            monthly=daily,
            yearly=daily,
        )
        assert f.current_max_delay_days == 0
        assert f.current_overdue_ratio == 0.0
        assert f.current_overdue_count == 0
        assert f.outstanding_amount == 0.0

    def test_overdue_ratio_bounds(self):
        daily = ConversionMetrics(
            searches=0,
            bookstep_failed=0,
            adjusted_bookstep_failed=0,
            other_step_failed=0,
            effective_searches=0,
            bookings=0,
            booking_volume=0.0,
            avg_booking_value=0.0,
            revenue_consistency=0.0,
        )
        with pytest.raises(ValidationError):
            AgentTrustFeatures(
                current_max_delay_days=0,
                current_overdue_ratio=150.0,
                current_overdue_count=0,
                daily=daily,
                weekly=daily,
                monthly=daily,
                yearly=daily,
            )


class TestAgentTrustResult:
    def test_valid_result(self):
        daily = ConversionMetrics(
            searches=0,
            bookstep_failed=0,
            adjusted_bookstep_failed=0,
            other_step_failed=0,
            effective_searches=0,
            bookings=0,
            booking_volume=0.0,
            avg_booking_value=0.0,
            revenue_consistency=0.0,
        )
        result = AgentTrustResult(
            agent_id=1,
            agent_name="Test Agent",
            features=AgentTrustFeatures(
                current_max_delay_days=0,
                current_overdue_ratio=0.0,
                current_overdue_count=0,
                daily=daily,
                weekly=daily,
                monthly=daily,
                yearly=daily,
            ),
            scores=AgentTrustScores(overall_score=75),
            tier="Gold",
            high_risk_flag=False,
            high_risk_reasons=[],
        )
        assert result.agent_id == 1
        assert result.tier == "Gold"
        assert isinstance(result.calculated_at, datetime)
        assert result.badges == []

    def test_high_risk_defaults(self):
        daily = ConversionMetrics(
            searches=0,
            bookstep_failed=0,
            adjusted_bookstep_failed=0,
            other_step_failed=0,
            effective_searches=0,
            bookings=0,
            booking_volume=0.0,
            avg_booking_value=0.0,
            revenue_consistency=0.0,
        )
        result = AgentTrustResult(
            agent_id=1,
            agent_name="High Risk Agent",
            features=AgentTrustFeatures(
                current_max_delay_days=30,
                current_overdue_ratio=80.0,
                current_overdue_count=5,
                outstanding_amount=5000.0,
                daily=daily,
                weekly=daily,
                monthly=daily,
                yearly=daily,
            ),
            scores=AgentTrustScores(overall_score=20),
            tier="High Risk",
            high_risk_flag=True,
            high_risk_reasons=["Overdue ratio exceeds threshold"],
        )
        assert result.high_risk_flag is True
        assert len(result.high_risk_reasons) == 1
