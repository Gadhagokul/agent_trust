"""
Small-sample confidence for reliability (senior sec6.3, A1).

The estimator is Wilson lower bound plus shrinkage toward a prior, and it ships
DISABLED. These tests pin three things:

  1. the arithmetic of the estimator, against hand-computed values;
  2. that a disabled deployment is byte-identical to the pre-A1 behaviour;
  3. that the two attribution lists remain unwired, so nothing here can be
     mistaken for satisfying the attribution requirement.
"""

import math
from types import SimpleNamespace

import pytest

from app.infra.settings import Settings
from app.services.agent_trust_scorer import (
    AgentTrustScorer,
    prior_weight,
    wilson_lower_bound,
)


@pytest.fixture
def scorer():
    return AgentTrustScorer()


def test_weights_are_exactly_two_dimensions():
    """Senior review: unavailable dimensions are None, never artificial 100s.

    The shipped weight set must contain exactly the two evidenced sub-components
    (booking_success, cancellation_quality) and nothing else -- no phantom
    refund/supplier-failure/SLA dimension may appear with a fabricated 100.
    """
    weights = Settings().reliability_component_weights
    assert set(weights) == {"booking_success", "cancellation_quality"}
    assert weights["booking_success"] == 0.6429
    assert weights["cancellation_quality"] == 0.3571
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def _settings(**overrides):
    base = dict(
        reliability_component_weights={
            "booking_success": 0.6429,
            "cancellation_quality": 0.3571,
        },
        reliability_confidence_enabled=False,
        reliability_cancellation_confidence_enabled=False,
        reliability_wilson_z=1.96,
        reliability_min_observations=30,
        reliability_success_prior_rate=0.5,
        reliability_cancellation_prior_rate=0.5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch(monkeypatch, **overrides):
    fake = _settings(**overrides)
    monkeypatch.setattr("app.services.agent_trust_scorer.get_settings", lambda: fake)
    return fake


# --- Estimator arithmetic -------------------------------------------------


class TestWilsonLowerBound:
    def test_no_trials_returns_none(self):
        assert wilson_lower_bound(0, 0, 1.96) is None
        assert wilson_lower_bound(5, 0, 1.96) is None

    def test_perfect_small_sample_is_pulled_below_one(self):
        # The whole point: 1/1 is not certainty.
        lb = wilson_lower_bound(1, 1, 1.96)
        assert lb is not None
        assert lb < 1.0
        assert lb == pytest.approx(0.2065, abs=1e-4)

    def test_perfect_large_sample_approaches_observed_rate(self):
        # p=1, z2=3.8416; (1+0.0019208-1.96*sqrt(0.0019208/500)) / (1+3.8416/500)
        lb = wilson_lower_bound(500, 500, 1.96)
        assert lb is not None
        assert lb == pytest.approx(0.99238, abs=1e-4)

    def test_known_hand_computed_value(self):
        # successes=7, trials=10, z=1.96
        #   p=0.7, denom=1+3.8416/10=1.38416
        #   centre=0.7+3.8416/20=0.89208
        #   margin=1.96*sqrt((0.7*0.3+3.8416/40)/10)=1.96*sqrt(0.030604)
        #   lower=(0.89208-0.342882)/1.38416
        assert wilson_lower_bound(7, 10, 1.96) == pytest.approx(0.39677, abs=1e-5)

    def test_clamped_at_zero(self):
        # 0/10 -> the Wilson lower bound is negative and must be clamped.
        assert wilson_lower_bound(0, 10, 1.96) == 0.0

    def test_matches_direct_derivation(self):
        successes, trials, z = 40, 50, 1.96
        p = successes / trials
        z2 = z * z
        expected = max(
            0.0,
            (
                p + z2 / (2 * trials)
                - z * math.sqrt((p * (1 - p) + z2 / (4 * trials)) / trials)
            )
            / (1 + z2 / trials),
        )
        assert wilson_lower_bound(successes, trials, z) == pytest.approx(expected, abs=1e-12)

    def test_lower_bound_never_exceeds_observed_rate(self):
        for successes, trials in [(1, 1), (1, 3), (5, 10), (9, 10), (30, 50), (500, 500)]:
            lb = wilson_lower_bound(successes, trials, 1.96)
            assert lb is not None
            assert lb <= successes / trials + 1e-12

    def test_increases_with_evidence_at_constant_rate(self):
        # Hold the observed rate at 0.8 and grow the sample: the lower bound
        # rises toward it. (Holding successes FIXED while n grows would fall,
        # because each added observation is a failure -- also correct.)
        values = [wilson_lower_bound(8 * k, 10 * k, 1.96) for k in (1, 3, 10, 50)]
        assert values == sorted(values)
        assert values[0] < 0.8
        assert values[-1] < 0.8


class TestPriorWeight:
    def test_zero_trials_is_full_shrinkage(self):
        assert prior_weight(0, 30) == 1.0

    def test_exactly_zero_at_threshold(self):
        assert prior_weight(30, 30) == 0.0
        assert prior_weight(31, 30) == 0.0

    def test_monotone_decreasing(self):
        weights = [prior_weight(n, 30) for n in (0, 1, 5, 15, 29, 30)]
        assert weights == sorted(weights, reverse=True)

    def test_confidence_is_its_complement(self):
        for n in (0, 1, 7, 29, 30, 100):
            assert 1.0 - prior_weight(n, 30) == pytest.approx(min(1.0, n / 30))


# --- Legacy behaviour must be untouched -----------------------------------


class TestDefaultIsByteIdenticalToLegacy:
    def test_ships_disabled(self):
        s = Settings(_env_file=None)
        assert s.reliability_confidence_enabled is False
        assert s.reliability_cancellation_confidence_enabled is False

    def test_single_success_no_failure_still_scores_100(self, scorer):
        # The legacy quirk A1 deliberately does NOT fix: one booking, no
        # failures, 100.0. Confidence is what will eventually change this, and
        # only when an operator enables it.
        batch = {365: {"bookings": 1, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        score, detail = scorer._compute_reliability_score(batch, exp)
        assert score == 100.0
        assert detail["booking_success"].confidence_applied is False

    def test_uses_raw_bookstep_failed_key(self, scorer):
        # adjusted_bookstep_failed is int(min(bookstep_failed, searches * 0.7)),
        # a search-volume cap -- NOT an attribution adjustment. Feeding the
        # attempts count must never come from that key.
        batch = {365: {"bookings": 8, "bookstep_failed": 2, "adjusted_bookstep_failed": 2}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        _, detail = scorer._compute_reliability_score(batch, exp)
        assert detail["booking_success"].n == 10

    def test_detail_reports_legacy_raw_rate(self, scorer):
        batch = {365: {"bookings": 7, "bookstep_failed": 3}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        _, detail = scorer._compute_reliability_score(batch, exp)
        booking = detail["booking_success"]
        assert booking.n == 10
        assert booking.successes == 7
        assert booking.raw_rate == pytest.approx(0.7)
        assert booking.score == pytest.approx(70.0)
        assert booking.prior_weight == prior_weight(10, 30)
        assert booking.confidence == pytest.approx(1.0 - booking.prior_weight)

    def test_no_evidence_subcomponent_keeps_100_and_flags(self, scorer):
        batch = {365: {"bookings": 0, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 5}
        _, detail = scorer._compute_reliability_score(batch, exp)
        booking = detail["booking_success"]
        assert booking.no_evidence is True
        assert booking.n == 0
        assert booking.raw_rate is None
        assert booking.adjusted_rate is None
        assert booking.confidence == 0.0
        assert booking.score == 100.0


def _legacy_reliability(bookings, bookstep_failed, total_cancelled, lifetime_bookings):
    """The pre-A1 implementation, transcribed verbatim as an oracle."""
    total_attempts = bookings + bookstep_failed
    if total_attempts == 0:
        success_score = 100.0
    else:
        success_score = (bookings / total_attempts) * 100.0

    total_transactions = total_cancelled + lifetime_bookings
    if total_transactions == 0:
        cancel_quality_score = 100.0
    else:
        cancel_rate = total_cancelled / total_transactions
        cancel_quality_score = max(0.0, (1.0 - cancel_rate) * 100.0)

    if total_attempts == 0 and total_transactions == 0:
        return None

    weights = {"booking_success": 0.6429, "cancellation_quality": 0.3571}
    weight_sum = sum(weights.values())
    component_scores = {
        "booking_success": success_score,
        "cancellation_quality": cancel_quality_score,
    }
    reliability = sum(component_scores[k] * w for k, w in weights.items()) / weight_sum
    return round(reliability, 2)


class TestDisabledPathIsArithmeticallyIdenticalToLegacy:
    """
    The single most important A1 guarantee. Because the legacy expressions are
    kept verbatim and only replaced on a flag, the disabled score must equal the
    pre-A1 oracle bit for bit -- not approximately, exactly. In particular the
    cancellation score is NOT recomputed as lifetime_bookings/total, which could
    differ in the last ULP from the legacy max(0, (1 - cancel_rate) * 100).
    """

    @pytest.mark.parametrize(
        "bookings,failed,cancelled,lifetime_bookings",
        [
            (0, 0, 0, 0),
            (0, 0, 0, 1),
            (0, 0, 1, 0),
            (1, 0, 0, 0),
            (1, 0, 0, 1),
            (1, 0, 3, 7),
            (1, 1, 1, 1),
            (2, 1, 1, 2),
            (7, 3, 4, 6),
            (10, 0, 0, 0),
            (10, 0, 0, 10),
            (10, 5, 5, 5),
            (50, 0, 50, 50),
            (100, 0, 0, 100),
            (100, 100, 100, 300),
            (333, 7, 71, 29),
            (1000, 3, 999, 1),
        ],
    )
    def test_exact_match_with_legacy_oracle(
        self, scorer, bookings, failed, cancelled, lifetime_bookings
    ):
        batch = {365: {"bookings": bookings, "bookstep_failed": failed}}
        exp = {"lifetime_cancelled": cancelled, "lifetime_bookings": lifetime_bookings}
        score, _ = scorer._compute_reliability_score(batch, exp)
        expected = _legacy_reliability(bookings, failed, cancelled, lifetime_bookings)
        assert score == expected

    def test_detail_scores_match_legacy_components(self, scorer):
        batch = {365: {"bookings": 1, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 100, "lifetime_bookings": 1}
        _, detail = scorer._compute_reliability_score(batch, exp)
        # The exact legacy expression, including the defensive clamp.
        cancel_rate = 100.0 / 101.0
        expected_cancel = max(0.0, (1.0 - cancel_rate) * 100.0)
        assert detail["cancellation_quality"].score == expected_cancel

    def test_none_only_when_both_components_have_no_evidence(self, scorer):
        # Never exclude a component that has evidence in one dimension.
        for bookings, failed, cancelled, lifetime_bookings in [
            (0, 0, 0, 1),
            (0, 0, 1, 0),
            (1, 0, 0, 0),
        ]:
            score, detail = scorer._compute_reliability_score(
                {365: {"bookings": bookings, "bookstep_failed": failed}},
                {"lifetime_cancelled": cancelled, "lifetime_bookings": lifetime_bookings},
            )
            assert score is not None
            assert detail is not None


# --- Confidence behaviour when enabled -------------------------------------


class TestBookingSuccessConfidence:
    def test_single_booking_no_longer_perfect(self, scorer, monkeypatch):
        _patch(monkeypatch, reliability_confidence_enabled=True)
        batch = {365: {"bookings": 1, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        score, detail = scorer._compute_reliability_score(batch, exp)
        assert detail["booking_success"].confidence_applied is True
        # Cancellation has no evidence, so it stays 100.0; the booking component
        # is dragged down by shrinkage and the composite must fall below 100.
        assert detail["booking_success"].score < 100.0
        assert score < 100.0

    def test_established_agent_not_shrunk(self, scorer, monkeypatch):
        _patch(monkeypatch, reliability_confidence_enabled=True)
        batch = {365: {"bookings": 400, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        _, detail = scorer._compute_reliability_score(batch, exp)
        booking = detail["booking_success"]
        assert booking.prior_weight == 0.0
        assert booking.confidence == 1.0
        assert booking.adjusted_rate == pytest.approx(booking.wilson_lower_bound)

    def test_wilson_bound_never_above_raw_rate(self, scorer, monkeypatch):
        _patch(monkeypatch, reliability_confidence_enabled=True)
        batch = {365: {"bookings": 3, "bookstep_failed": 2}}
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        _, detail = scorer._compute_reliability_score(batch, exp)
        assert detail["booking_success"].adjusted_rate < detail["booking_success"].raw_rate

    def test_perfect_record_converges_toward_one(self, scorer, monkeypatch):
        _patch(monkeypatch, reliability_confidence_enabled=True)
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        rates = []
        for b in (2, 5, 15, 30, 200, 5000):
            _, d = scorer._compute_reliability_score(
                {365: {"bookings": b, "bookstep_failed": 0}}, exp
            )
            rates.append(d["booking_success"].adjusted_rate)
        assert all(r is not None for r in rates)
        # Monotone non-decreasing and bounded by the perfect observed rate.
        assert rates == sorted(rates)
        assert all(r <= 1.0 for r in rates)

    def test_adjusted_never_exceeds_raw_rate(self, scorer, monkeypatch):
        _patch(monkeypatch, reliability_confidence_enabled=True)
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        for b, f in ((1, 0), (2, 1), (10, 10), (400, 0), (30, 3)):
            _, d = scorer._compute_reliability_score(
                {365: {"bookings": b, "bookstep_failed": f}}, exp
            )
            booking = d["booking_success"]
            assert booking.adjusted_rate <= booking.raw_rate + 1e-12

    def test_known_n1_inversion_is_pinned(self, scorer, monkeypatch):
        # With a 0.5 prior, a 1/1 record scores marginally HIGHER than a 2/2
        # record (0.4902 vs 0.4895): shrinkage toward 0.5 dominates the rising
        # Wilson bound until the sample is a few observations wide. Pinned here
        # so the inversion is a known, deliberate property rather than a
        # surprise -- and so it is revisited if the prior ever changes.
        _patch(monkeypatch, reliability_confidence_enabled=True)
        exp = {"lifetime_cancelled": 0, "lifetime_bookings": 0}
        _, one = scorer._compute_reliability_score(
            {365: {"bookings": 1, "bookstep_failed": 0}}, exp
        )
        _, two = scorer._compute_reliability_score(
            {365: {"bookings": 2, "bookstep_failed": 0}}, exp
        )
        a1 = one["booking_success"].adjusted_rate
        a2 = two["booking_success"].adjusted_rate
        assert a1 > a2
        assert a1 - a2 < 0.01

    def test_priors_are_isolated_between_subcomponents(self, scorer, monkeypatch):
        # Both components must be small enough that the prior still has weight
        # (n < reliability_min_observations), otherwise prior_weight is 0 and
        # the prior is correctly irrelevant.
        exp = {"lifetime_cancelled": 2, "lifetime_bookings": 4}
        batch = {365: {"bookings": 5, "bookstep_failed": 0}}
        _patch(
            monkeypatch,
            reliability_confidence_enabled=True,
            reliability_cancellation_confidence_enabled=True,
            reliability_success_prior_rate=0.1,
        )
        _, low = scorer._compute_reliability_score(batch, exp)
        _patch(
            monkeypatch,
            reliability_confidence_enabled=True,
            reliability_cancellation_confidence_enabled=True,
            reliability_success_prior_rate=0.5,
        )
        _, base = scorer._compute_reliability_score(batch, exp)
        assert base["booking_success"].prior_weight > 0
        # Moving the booking prior must not touch the cancellation component.
        assert (
            low["cancellation_quality"].adjusted_rate
            == base["cancellation_quality"].adjusted_rate
        )
        assert low["booking_success"].adjusted_rate != base["booking_success"].adjusted_rate


class TestCancellationConfidenceIsGated:
    def test_cancellation_flag_off_leaves_cancellation_untouched(self, scorer, monkeypatch):
        # booking confidence on, cancellation off: only one component moves.
        _patch(monkeypatch, reliability_confidence_enabled=True)
        batch = {365: {"bookings": 1, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 1, "lifetime_bookings": 0}
        _, detail = scorer._compute_reliability_score(batch, exp)
        cancel = detail["cancellation_quality"]
        assert cancel.confidence_applied is False
        assert cancel.score == pytest.approx(0.0)
        assert detail["booking_success"].confidence_applied is True

    def test_cancellation_flag_on_does_apply(self, scorer, monkeypatch):
        _patch(monkeypatch, reliability_cancellation_confidence_enabled=True)
        batch = {365: {"bookings": 1, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 1, "lifetime_bookings": 0}
        _, detail = scorer._compute_reliability_score(batch, exp)
        cancel = detail["cancellation_quality"]
        assert cancel.confidence_applied is True
        # Shrunk away from a hard 0.0 toward the prior.
        assert 0.0 < cancel.adjusted_rate < 0.5

    def test_cancellation_successes_are_non_cancellations(self, scorer, monkeypatch):
        _patch(monkeypatch, reliability_cancellation_confidence_enabled=True)
        batch = {365: {"bookings": 0, "bookstep_failed": 0}}
        exp = {"lifetime_cancelled": 1, "lifetime_bookings": 9}
        _, detail = scorer._compute_reliability_score(batch, exp)
        cancel = detail["cancellation_quality"]
        assert cancel.n == 10
        assert cancel.successes == 9
        assert cancel.raw_rate == pytest.approx(0.9)


# --- Attribution must not be implied ---------------------------------------


class TestAttributionRemainsOpen:
    def test_reason_lists_are_declared_but_unwired(self):
        s = Settings(_env_file=None)
        assert s.non_agent_failure_reasons == []
        assert s.non_agent_cancellation_reasons == []

    def test_confidence_does_not_claim_attribution(self, scorer, monkeypatch):
        # A confidence number must never be presented as an attribution fix.
        # Documented as an open data-layer item; this test exists so the day
        # someone wires a reason column, the flag interaction is revisited.
        _patch(
            monkeypatch,
            reliability_confidence_enabled=True,
            reliability_cancellation_confidence_enabled=True,
        )
        batch = {365: {"bookings": 9, "bookstep_failed": 1}}
        exp = {"lifetime_cancelled": 4, "lifetime_bookings": 6}
        _, detail = scorer._compute_reliability_score(batch, exp)
        # Cancellation detail is computed, but the underlying cancellation count
        # is still every cancelled booking, agent-attributable or not.
        assert detail["cancellation_quality"].n == 10
        assert detail["cancellation_quality"].confidence_applied is True


class TestSettingsValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("reliability_wilson_z", 0.0),
            ("reliability_wilson_z", -1.0),
            ("reliability_min_observations", 0),
            ("reliability_success_prior_rate", 0.0),
            ("reliability_success_prior_rate", 1.0),
            ("reliability_cancellation_prior_rate", 0.0),
            ("reliability_cancellation_prior_rate", 1.0),
        ],
    )
    def test_invalid_values_raise_runtime_error(self, field, value):
        # Follows the existing Settings convention (settings.py): RuntimeError
        # from _validate_startup(), not a pydantic ValidationError, and not from
        # the constructor -- the constructor does no validation.
        with pytest.raises(RuntimeError):
            Settings(**{field: value})._validate_startup()

    def test_valid_boundaries_accepted(self):
        Settings(reliability_wilson_z=0.5, reliability_min_observations=1)._validate_startup()
        Settings(
            reliability_success_prior_rate=0.001,
            reliability_cancellation_prior_rate=0.999,
        )._validate_startup()
