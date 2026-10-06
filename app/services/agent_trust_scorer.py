# app/services/agent_trust_scorer.py

import logging
import math
from datetime import datetime, timezone
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.domain.errors import (
    DatabaseUnavailableError,
    ModelUnavailableError,
    SchemaChangedError,
)
from app.domain.models import (
    AgentTrustFeatures,
    AgentTrustResult,
    AgentTrustScores,
    ConversionMetrics,
    ReliabilityDetail,
)
from app.infra.db.audit_repository import AuditRepository
from app.infra.db.repository import AgentRepository, CreditStats
from app.infra.settings import get_settings
from app.ml.trust_model import TrustModelPredictor
from app.observability.metrics import (
    AGENT_TRUST_DURATION,
    AGENT_TRUST_REQUESTS,
    COMPONENT_UNAVAILABLE,
    DATABASE_FAILURES,
    ML_FALLBACKS,
    ML_NOT_READY,
    ML_PREDICTIONS,
)
from app.services.cache_adapter import CacheAdapter

logger = logging.getLogger(__name__)


def wilson_lower_bound(successes: int, trials: int, z: float) -> float | None:
    """
    Lower bound of the Wilson score interval for a binomial proportion.

    The plain rate is an over-estimate of the true rate when the sample is tiny:
    1/1 successes yields exactly 1.0, which claims certainty from a single
    observation. The Wilson lower bound stays below the observed rate and rises
    toward it as evidence accumulates, so a new agent cannot look perfect on one
    booking.

    z is the normal quantile for the one-sided confidence level; 1.96 ~= 95%.
    Returns None when there is no evidence (trials <= 0).
    """
    if trials <= 0:
        return None
    p = successes / trials
    z2 = z * z
    denom = 1.0 + z2 / trials
    centre = p + z2 / (2 * trials)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * trials)) / trials)
    return max(0.0, (centre - margin) / denom)


def prior_weight(trials: int, min_observations: int) -> float:
    """
    How far the estimate is pulled toward the prior.

    Exactly 0 at or above min_observations, so an established agent's estimate
    is never shrunk. This is the quantity `confidence` is defined against:
    confidence == 1 - prior_weight.
    """
    if min_observations < 1:
        return 0.0
    if trials <= 0:
        return 1.0
    return max(0.0, 1.0 - (trials / min_observations))


class AgentTrustScorer:
    def __init__(self):
        self.repository = AgentRepository()
        self.cache = CacheAdapter()

    def _deserialize_or_clear(self, key: str, data: dict | None) -> AgentTrustResult | None:
        """
        Build an AgentTrustResult from cached JSON, clearing the cache (active +
        stale tiers) if the payload is corrupt or from an older schema so it can
        never crash the request with a validation error.
        """
        if not data:
            return None
        try:
            return AgentTrustResult(**data)
        except ValidationError as exc:
            logger.warning(
                "Cached trust payload for %s is corrupt or from an older schema; "
                "clearing cache entries: %s",
                key,
                exc,
            )
            self.cache.invalidate(key)
            self.cache.invalidate(f"stale:{key}")
            return None

    def _safe_metrics(self, stats: dict) -> dict:
        return {
            "searches": stats.get("searches", 0),
            "effective_searches": stats.get("effective_searches", 0),
            "bookings": stats.get("bookings", 0),
            "other_step_failed": stats.get("other_step_failed", 0),
            "bookstep_failed": stats.get("bookstep_failed", 0),
            "adjusted_bookstep_failed": stats.get("adjusted_bookstep_failed", 0),
            "booking_volume": stats.get("booking_volume", 0),
            "avg_booking_value": stats.get("avg_booking_value", 0.0),
            "revenue_consistency": stats.get("revenue_consistency", 0.0),
            "no_activity": stats.get("no_activity", False),
            "low_confidence": stats.get("low_confidence", False),
        }

    def _determine_tier(
        self,
        overall: int,
        is_high_risk: bool,
        current_overdue_count: int,
        current_max_delay_days: int,
    ) -> str:
        """
        Maps the final blended overall_score to a tier label.

        Platinum : >= tier_thresholds["platinum"], at most
                   platinum_max_overdue_count overdue and delay
                   < platinum_max_delay_days
        Gold     : >= tier_thresholds["gold"]
        Silver   : >= tier_thresholds["silver"]
        Bronze   : >= tier_thresholds["bronze"]
        High Risk: sentinel returned when is_high_risk is set, or the score
                   falls below the bronze floor. It is NOT a configurable band;
                   high-risk detection lives in _check_high_risk.
        """
        if is_high_risk:
            return "High Risk"

        settings = get_settings()
        t = settings.tier_thresholds
        if (
            overall >= t["platinum"]
            and current_overdue_count <= settings.platinum_max_overdue_count
            and current_max_delay_days < settings.platinum_max_delay_days
        ):
            return "Platinum"
        elif overall >= t["gold"]:
            return "Gold"
        elif overall >= t["silver"]:
            return "Silver"
        elif overall >= t["bronze"]:
            return "Bronze"
        else:
            return "High Risk"

    def _check_high_risk(self, credit_stats: CreditStats) -> tuple[bool, list[str]]:
        """Returns (is_high_risk, list_of_reasons)."""
        settings = get_settings()
        reasons = []

        if credit_stats.current_overdue_ratio > settings.credit_max_overdue_ratio:
            reasons.append(
                f"Overdue ratio ({credit_stats.current_overdue_ratio}%) exceeds "
                f"{settings.credit_max_overdue_ratio}%"
            )
        if credit_stats.current_max_delay_days > settings.credit_max_delay_days:
            reasons.append(
                f"Overdue payment delay ({credit_stats.current_max_delay_days} days) exceeds "
                f"{settings.credit_max_delay_days} days"
            )
        if credit_stats.current_overdue_count >= settings.credit_max_overdue_count:
            reasons.append(
                f"Outstanding overdue invoice count ({credit_stats.current_overdue_count}) exceeds "
                f"{settings.credit_max_overdue_count}"
            )
        if credit_stats.consecutive_unpaid_cycles >= settings.credit_max_consecutive_overdue_cycles:
            reasons.append(
                f"Defaulted on the last {settings.credit_max_consecutive_overdue_cycles} "
                "consecutive credit cycles"
            )

        return len(reasons) > 0, reasons

    def _reliability_detail(
        self,
        *,
        successes: int,
        trials: int,
        prior_rate: float,
        z: float,
        min_observations: int,
        score: float,
        confidence_applied: bool,
    ) -> ReliabilityDetail:
        """
        Build the small-sample evidence record for one reliability sub-component.

        This never decides the score -- the caller does. It is computed on every
        request even while confidence is disabled so the activation review has
        the would-be Wilson bound, prior weight and confidence to hand, without
        having to enable anything and re-run production.
        """
        w = prior_weight(trials, min_observations)
        wilson = wilson_lower_bound(successes, trials, z)
        if trials <= 0:
            return ReliabilityDetail(
                n=0,
                successes=0,
                raw_rate=None,
                wilson_lower_bound=None,
                prior_weight=w,
                adjusted_rate=None,
                confidence=max(0.0, 1.0 - w),
                score=score,
                no_evidence=True,
                confidence_applied=confidence_applied,
            )
        raw_rate = successes / trials
        adjusted = wilson if wilson is None else ((1.0 - w) * wilson + w * prior_rate)
        return ReliabilityDetail(
            n=trials,
            successes=successes,
            raw_rate=raw_rate,
            wilson_lower_bound=wilson,
            prior_weight=w,
            adjusted_rate=adjusted,
            confidence=max(0.0, 1.0 - w),
            score=score,
            no_evidence=False,
            confidence_applied=confidence_applied,
        )

    def _compute_reliability_score(
        self, batch_stats: dict, exp_stats: dict
    ) -> tuple[float | None, dict[str, ReliabilityDetail] | None]:
        """
        Reliability V3:
        64.3% Booking Success Rate
        35.7% Cancellation Quality (Inverse)

        Returns (score, detail). The score is None (component excluded) when the
        agent has no booking behavior evidence at all, so the composite
        renormalizes over active parts. Detail is always populated when the
        score is not None, so the raw rate stays auditable either way.

        Attribution (senior sec6.2/7) is NOT satisfied today, and this method
        must not appear to satisfy it. non_agent_failure_reasons and
        non_agent_cancellation_reasons are declared in settings but wired to
        nothing: the booking data has no confirmed failure/cancellation reason
        field, so
          * bookstep_failed counts EVERY BookStep failure (outage, timeout,
            supplier-side and agent-caused alike), and
          * lifetime_cancelled counts EVERY bookings.status = 'cancelled' row
            (repository.get_experience_stats), including airline, supplier,
            schedule-change and involuntary cancellations.
        Both are attributed to the agent. Note `adjusted_bookstep_failed` is NOT
        an attribution adjustment: it is int(min(bookstep_failed, searches * 0.7)),
        a search-volume cap. Do not substitute it here -- that would silently
        change the attempt denominator.

        Small-sample confidence (senior sec6.3) therefore adjusts the two rates
        independently and ships disabled. When a switch is off, the score
        arithmetic below is left byte-identical to the pre-A1 code.
        """
        settings = get_settings()
        stats = batch_stats.get(365, {})
        bookings = stats.get("bookings", 0)
        bookstep_failed = stats.get("bookstep_failed", 0)

        total_attempts = bookings + bookstep_failed
        if total_attempts == 0:
            success_score = 100.0
        else:
            success_score = (bookings / total_attempts) * 100.0

        total_cancelled = exp_stats.get("lifetime_cancelled", 0)
        lifetime_bookings = exp_stats.get("lifetime_bookings", 0)
        total_transactions = total_cancelled + lifetime_bookings

        if total_transactions == 0:
            cancel_quality_score = 100.0
        else:
            cancel_rate = total_cancelled / total_transactions
            cancel_quality_score = max(0.0, (1.0 - cancel_rate) * 100.0)

        if total_attempts == 0 and total_transactions == 0:
            return None, None

        z = settings.reliability_wilson_z
        min_obs = settings.reliability_min_observations

        # Both sub-components are "successes out of trials, higher is better":
        # booking success counts the bookings, cancellation quality counts the
        # NON-cancelled bookings, so a good agent is rewarded either way.
        booking_detail = self._reliability_detail(
            successes=bookings,
            trials=total_attempts,
            prior_rate=settings.reliability_success_prior_rate,
            z=z,
            min_observations=min_obs,
            score=success_score,
            confidence_applied=False,
        )
        cancel_detail = self._reliability_detail(
            successes=lifetime_bookings,
            trials=total_transactions,
            prior_rate=settings.reliability_cancellation_prior_rate,
            z=z,
            min_observations=min_obs,
            score=cancel_quality_score,
            confidence_applied=False,
        )

        # Substitute the confidence-adjusted rate only where a switch is on and
        # there is evidence. The legacy expressions above are never rewritten, so
        # a disabled deployment is arithmetically identical to before A1.
        if settings.reliability_confidence_enabled and booking_detail.adjusted_rate is not None:
            success_score = booking_detail.adjusted_rate * 100.0
            booking_detail = booking_detail.model_copy(
                update={"score": success_score, "confidence_applied": True}
            )
        if (
            settings.reliability_cancellation_confidence_enabled
            and cancel_detail.adjusted_rate is not None
        ):
            cancel_quality_score = cancel_detail.adjusted_rate * 100.0
            cancel_detail = cancel_detail.model_copy(
                update={"score": cancel_quality_score, "confidence_applied": True}
            )

        detail = {
            "booking_success": booking_detail,
            "cancellation_quality": cancel_detail,
        }

        weights = settings.reliability_component_weights
        weight_sum = sum(weights.values())
        component_scores = {
            "booking_success": success_score,
            "cancellation_quality": cancel_quality_score,
        }
        reliability = sum(component_scores[k] * w for k, w in weights.items()) / weight_sum
        return round(reliability, 2), detail

    def _compute_financial_score(self, credit_stats: CreditStats) -> float:
        score = 100.0 - credit_stats.current_overdue_ratio
        delay_penalty = min((credit_stats.current_max_delay_days / 10) * 5, 40)
        score -= delay_penalty
        return max(5.0, round(score, 2))

    def _compute_experience_score(self, exp_stats: dict) -> float:
        created_at = exp_stats.get("created_at")
        lifetime_bookings = exp_stats.get("lifetime_bookings", 0)

        days_active = 0
        if created_at:
            if isinstance(created_at, datetime):
                now_utc = datetime.now(timezone.utc)
                created_utc = created_at.replace(tzinfo=timezone.utc)
                days_active = (now_utc - created_utc).days
            elif hasattr(created_at, "strftime"):
                days_active = (datetime.now(timezone.utc).date() - created_at).days

        booking_score = min(50, math.log1p(lifetime_bookings) * 7.2)
        age_score = min(50, math.log1p(max(0, days_active)) * 6.7)
        return round(booking_score + age_score, 2)

    def _score_l2b_for_target(self, activity: dict, target_ratio: float | None) -> float | None:
        """
        Shared Search-to-Booking scoring curve (senior §13).

        Compares an observed bookings/searches ratio (0-1) against a benchmark
        target_ratio and returns a 0-100 score. Meeting the target ratio equals
        the minimum acceptable score (default 80); the score rises linearly to
        100 at `excellent_multiplier` x the target. Used both for the aggregate
        agent metric (Search-to-Booking component, Feature B) and per-supplier
        compliance under the supplier-specific L2B model.

        Returns None (component excluded) when there is no search activity or no
        executable target - the composite then renormalizes over active parts.
        """
        settings = get_settings()
        searches = activity.get("searches", 0)
        bookings = activity.get("bookings", 0)

        if not searches or target_ratio is None or target_ratio <= 0:
            return None

        ratio = bookings / searches
        at_target = settings.search_to_booking_at_target_score
        mult = settings.search_to_booking_excellent_multiplier
        if ratio <= target_ratio:
            base = at_target * (ratio / target_ratio)
        elif mult <= 1.0:
            base = 100.0
        else:
            base = at_target + (100.0 - at_target) * min(
                1.0, (ratio - target_ratio) / ((mult - 1.0) * target_ratio)
            )
        confidence = min(1.0, searches / settings.search_to_booking_min_searches)
        score = settings.search_to_booking_neutral_score + (
            (base - settings.search_to_booking_neutral_score) * confidence
        )
        return round(max(0.0, min(100.0, score)), 2)

    def _compute_booking_behavior_score(
        self, activity: dict, target_ratio: float | None
    ) -> float | None:
        """
        Agent Search-to-Booking component (Feature B).

        Searches = every search intent the agent used (created + reused access
        rows), and bookings = successful bookings, both over the same 365-day
        window. The benchmark target is derived per supplier from
        suppliers.minimum_booking / search_limit (site-level) and combined per
        supplier; the scoring curve itself lives in _score_l2b_for_target.

        Returns None (component excluded) when the agent has no search behavior
        data or no executable target - in that case the composite is
        renormalized over the active components.
        """
        return self._score_l2b_for_target(activity, target_ratio)

    def _compute_supplier_l2b_component(
        self,
        supplier_targets: list[dict],
        supplier_searches: list[dict],
        booking_counts: list[dict],
    ) -> tuple[float | None, dict]:
        """
        Supplier-specific Search-to-Booking (L2B) component (senior §10-13).

        Every supplier the agent searched gets its own ratio
        (bookings_s / searches_s) benchmarked against the SITE-level target from
        suppliers.minimum_booking / search_limit. The component is the
        share-weighted mean of per-supplier scores, where share = that
        supplier's search volume / total volume across configured suppliers.
        No single supplier may dominate: shares above l2b_max_supplier_share are
        capped and the excess redistributed proportionally over the rest
        (senior §13; single-pass is exact because at most one supplier can
        exceed 50% of a set that sums to 1).

        Aggregation detail confirmed from real data: one session queries all the
        suppliers the agent selected, so the same access_count legitimately
        counts toward every supplier with a search_supplier_runs row.

        Unconfigured suppliers (no executable target) are excluded
        (l2b_not_configured_policy = "exclude", senior §13) - they are never
        assigned an invented compliance score. If no supplier is configured the
        component is None and the composite renormalizes (senior §5).
        """
        settings = get_settings()
        target_by_code = {t["code"]: t for t in supplier_targets}
        bookings_by_provider = {b["provider"]: b["bookings"] for b in booking_counts}
        searched_codes = [s["code"] for s in supplier_searches]

        configured = []
        for item in supplier_searches:
            code = item["code"]
            target = target_by_code.get(code)
            if target is None or target.get("target") is None:
                continue
            if item.get("searches", 0) <= 0:
                continue
            searches = item["searches"]
            bookings = bookings_by_provider.get(target.get("name") or "", 0)
            per_supplier_score = self._score_l2b_for_target(
                {"searches": searches, "bookings": bookings}, target["target"]
            )
            if per_supplier_score is None:
                continue
            configured.append(
                {
                    "code": code,
                    "name": target.get("name"),
                    "searches": searches,
                    "bookings": bookings,
                    "target": target["target"],
                    "ratio": round(bookings / searches, 6),
                    "score": per_supplier_score,
                }
            )

        unconfigured = sorted(
            set(searched_codes) - {c["code"] for c in configured}
        )

        if not configured:
            return None, {
                "supplier_scores": [],
                "unconfigured_suppliers": unconfigured,
                "policy": settings.l2b_not_configured_policy,
                "component": None,
            }

        total_searches = sum(c["searches"] for c in configured)
        shares = [c["searches"] / total_searches for c in configured]
        cap = settings.l2b_max_supplier_share

        if len(configured) > 1:
            over_indices = [i for i, share in enumerate(shares) if share > cap]
            if over_indices:
                excess = sum(shares[i] - cap for i in over_indices)
                kept_indices = [i for i in range(len(shares)) if i not in over_indices]
                kept_total = sum(shares[i] for i in kept_indices)
                for i in over_indices:
                    shares[i] = cap
                if kept_total > 0:
                    for i in kept_indices:
                        shares[i] += excess * (shares[i] / kept_total)

        share_sum = sum(shares)
        normalized = [share / share_sum for share in shares]
        component = round(
            sum(share * c["score"] for share, c in zip(normalized, configured, strict=True)),
            2,
        )

        detail = {
            "supplier_scores": [
                {
                    "code": c["code"],
                    "name": c["name"],
                    "searches": c["searches"],
                    "bookings": c["bookings"],
                    "target": c["target"],
                    "ratio": c["ratio"],
                    "share": round(share, 6),
                    "score": c["score"],
                }
                for share, c in zip(normalized, configured, strict=True)
            ],
            "unconfigured_suppliers": unconfigured,
            "policy": settings.l2b_not_configured_policy,
            "component": component,
        }
        return component, detail

    def _combine_composite(
        self, component_scores: dict[str, float | None], weights: dict[str, float]
    ) -> tuple[float, dict[str, float], list[str], list[str]]:
        """
        Missing-data redistribution (senior §5 / §14). Components that cannot be
        calculated (None) are excluded and their weight is redistributed
        proportionally among the available components. Returns
        (composite, weights_used, available_components, unavailable_components)
        so the effective weights actually used are explicit and auditable.
        """
        parts = {k: v for k, v in component_scores.items() if v is not None}
        unavailable = [k for k in component_scores if k not in parts]
        weight_sum = sum(weights[k] for k in parts)
        weights_used = {k: round(weights[k] / weight_sum, 4) for k in parts}
        composite = sum(weights[k] * parts[k] for k in parts) / weight_sum
        return round(composite, 2), weights_used, list(parts), unavailable

    def _determine_badges(
        self,
        overall_score: int,
        experience_score: float,
        financial_score: float,
        current_overdue_ratio: float,
        exp_stats: dict,
    ) -> list[str]:
        badges = []

        if overall_score >= 90 and experience_score >= 30:
            badges.append("Trusted Partner")

        if financial_score == 100.0 and current_overdue_ratio == 0.0:
            badges.append("Perfect Payer")

        if exp_stats.get("lifetime_bookings", 0) >= 1000:
            badges.append("Booking Champion")

        return badges

    def calculate(self, db: Session, agent_id: int) -> AgentTrustResult:
        cache_key = f"trust:agent:{agent_id}:conversion"

        cached = self.cache.get(cache_key)
        cached_result = self._deserialize_or_clear(cache_key, cached)
        if cached_result:
            return cached_result

        lock_token = self.cache.acquire_lock(cache_key)
        if not lock_token:
            stale = self.cache.get_stale(cache_key)
            stale_result = self._deserialize_or_clear(cache_key, stale)
            if stale_result:
                logger.warning("Serving stale cache for %s", cache_key)
                return stale_result
            waited = self.cache.wait_for_cache(cache_key)
            waited_result = self._deserialize_or_clear(cache_key, waited)
            if waited_result:
                return waited_result
            raise DatabaseUnavailableError("Service busy. Try again.")

        try:
            score_start = perf_counter()
            agent_id_val, agent_name = self.repository.get_agent(db, agent_id)
            credit_stats = self.repository.get_credit_stats(db, agent_id)
            batch_stats = self.repository.get_multi_timeframe_stats(db, agent_id)
            exp_stats = self.repository.get_experience_stats(db, agent_id)
            search_activity = self.repository.get_agent_search_activity(db, agent_id)
            supplier_targets = self.repository.get_supplier_l2b_targets(db)
            supplier_searches = self.repository.get_agent_supplier_searches(db, agent_id)
            supplier_bookings = self.repository.get_agent_booking_counts_by_provider(db, agent_id)

            # --- 1. Compute Transparent Business Components ---
            reliability_score, reliability_detail = self._compute_reliability_score(
                batch_stats, exp_stats
            )
            if reliability_score is None:
                COMPONENT_UNAVAILABLE.labels(component="reliability").inc()
            financial_score = self._compute_financial_score(credit_stats)
            experience_score = self._compute_experience_score(exp_stats)
            booking_behavior_score, l2b_detail = self._compute_supplier_l2b_component(
                supplier_targets, supplier_searches, supplier_bookings
            )

            # --- 2. Composite Trust Score (Weights: Rel 40%, Fin 25%, Exp 15%, S2B 20%) ---
            # Components with no evidence (reliability / booking behavior returning
            # None) are excluded and their weight is redistributed proportionally
            # among the available components (senior §5). The effective weights
            # actually used are recorded for auditability.
            weights = get_settings().composite_weights
            composite_trust, weights_used, available_components, unavailable_components = (
                self._combine_composite(
                    {
                        "reliability": reliability_score,
                        "financial": financial_score,
                        "experience": experience_score,
                        "search_to_booking": booking_behavior_score,
                    },
                    weights,
                )
            )

            # --- 3. ML Calibration Layer ---
            # `is_inactive_overall` also feeds features.no_activity below, so it
            # is derived independently of the ML gate.
            stats_7d = batch_stats.get(7, {})
            stats_30d = batch_stats.get(30, {})
            is_inactive_overall = (
                stats_7d.get("effective_searches", 0) == 0
                and stats_30d.get("effective_searches", 0) == 0
            )

            # The gate is evaluated BEFORE any inference is attempted, so a
            # disabled programme costs nothing and can never reach the blend.
            settings = get_settings()
            ml_gate_on = bool(settings.ml_enabled and settings.ml_targets)
            ml_calibration_score: float | None = None

            if not ml_gate_on:
                ML_NOT_READY.inc()
            elif is_inactive_overall:
                # No behavioural evidence to calibrate against. This is NOT READY,
                # never a substitute score.
                ML_NOT_READY.inc()
            else:
                ml_features = {
                    "eff_searches_7d": stats_7d.get("effective_searches", 0),
                    "bookings_7d": stats_7d.get("bookings", 0),
                    "eff_searches_30d": stats_30d.get("effective_searches", 0),
                    "bookings_30d": stats_30d.get("bookings", 0),
                    "current_overdue_count": credit_stats.current_overdue_count,
                    "current_overdue_ratio": credit_stats.current_overdue_ratio,
                    "current_max_delay_days": credit_stats.current_max_delay_days,
                }
                try:
                    predictor = TrustModelPredictor()
                    ml_calibration_score = predictor.predict(ml_features)
                    ML_PREDICTIONS.inc()
                except ModelUnavailableError:
                    # Model unavailable => NOT_READY => rules-only. The ML share
                    # is dropped; a non-ML value (e.g. the financial score) must
                    # never occupy it (senior review #6).
                    logger.warning(
                        "ML model unavailable -- NOT_READY, serving rules-only for agent %s",
                        agent_id,
                    )
                    ml_calibration_score = None
                    ML_FALLBACKS.inc()

            # --- 4. Final Trust Score (Sprint 5 ML gate) ---
            # The ML layer ships DISABLED. Gate logic (senior 15.1):
            #   * if the programme is not configured OR not ready -> the agent's
            #     overall score is round(composite_trust) -- never a blend and
            #     never a fallback value masquerading as ML.
            #   * only when ready -> 80% composite / 20% combined ML weights.
            ml_ready = bool(ml_gate_on and ml_calibration_score is not None)
            if ml_ready and ml_calibration_score is not None:
                overall = round((composite_trust * 0.8) + (ml_calibration_score * 0.2))
            else:
                overall = round(composite_trust)
            overall = max(5, min(overall, 100))

            # --- 5. High Risk Overrides ---
            is_high_risk, high_risk_reasons = self._check_high_risk(credit_stats)
            settings = get_settings()
            if is_high_risk:
                overall = min(overall, settings.high_risk_score_cap)

            tier = self._determine_tier(
                overall,
                is_high_risk,
                credit_stats.current_overdue_count,
                credit_stats.current_max_delay_days,
            )

            operational = int(financial_score)
            if is_high_risk:
                operational = min(operational, settings.high_risk_score_cap)

            try:
                last_log = AuditRepository.get_last_audit(agent_id_val)
                prev_score = float(last_log["new_score"]) if last_log else None
                prev_tier = last_log["new_tier"] if last_log else None
                AuditRepository.append_audit_log(
                    agent_id=agent_id_val,
                    old_score=prev_score,
                    new_score=overall,
                    old_tier=prev_tier,
                    new_tier=tier,
                    event_type="score_shift",
                    metadata={
                        "trigger": "composite_recalculation",
                        "available_components": available_components,
                        "unavailable_components": unavailable_components,
                        "weights_used": weights_used,
                        "component_scores": {
                            "reliability": reliability_score,
                            "financial": financial_score,
                            "experience": experience_score,
                            "booking_behavior": booking_behavior_score,
                        },
                        "high_risk_flag": is_high_risk,
                        "high_risk_reasons": high_risk_reasons,
                        "high_risk_score_cap": (
                            settings.high_risk_score_cap if is_high_risk else None
                        ),
                        "data_snapshot": {
                            "evaluated_on": datetime.now(timezone.utc).date().isoformat(),
                            "credit_overdue_boundary": get_settings().credit_overdue_boundary,
                        },
                        "model_version": "rules_v1",
                        "calculation_timestamp": datetime.now(timezone.utc).isoformat(),
                        "l2b_policy": get_settings().l2b_not_configured_policy,
                        "l2b_component": booking_behavior_score,
                        "l2b_unconfigured_suppliers": l2b_detail.get(
                            "unconfigured_suppliers", []
                        ),
                        "reliability_detail": (
                            {
                                name: detail.model_dump(mode="json")
                                for name, detail in reliability_detail.items()
                            }
                            if reliability_detail
                            else None
                        ),
                        "reliability_confidence_enabled": (
                            get_settings().reliability_confidence_enabled
                        ),
                        "reliability_cancellation_confidence_enabled": (
                            get_settings().reliability_cancellation_confidence_enabled
                        ),
                    },
                )
            except Exception:
                logger.exception("Audit logging skipped for agent %s", agent_id_val)

            badges = self._determine_badges(
                overall_score=overall,
                experience_score=experience_score,
                financial_score=financial_score,
                current_overdue_ratio=credit_stats.current_overdue_ratio,
                exp_stats=exp_stats,
            )

            result = AgentTrustResult(
                agent_id=agent_id_val,
                agent_name=agent_name,
                features=AgentTrustFeatures(
                    current_max_delay_days=credit_stats.current_max_delay_days,
                    current_overdue_ratio=credit_stats.current_overdue_ratio,
                    current_overdue_count=credit_stats.current_overdue_count,
                    outstanding_amount=credit_stats.outstanding_amount,
                    historical_late_payment_count=credit_stats.historical_late_payment_count,
                    historical_late_payment_ratio=credit_stats.historical_late_payment_ratio,
                    average_payment_delay_days=credit_stats.average_payment_delay_days,
                    historical_max_payment_delay_days=credit_stats.maximum_payment_delay_days,
                    no_activity=is_inactive_overall,
                    daily=ConversionMetrics(**self._safe_metrics(batch_stats.get(1, {}))),
                    weekly=ConversionMetrics(**self._safe_metrics(batch_stats.get(7, {}))),
                    monthly=ConversionMetrics(**self._safe_metrics(batch_stats.get(30, {}))),
                    yearly=ConversionMetrics(**self._safe_metrics(batch_stats.get(365, {}))),
                    search_activity={
                        "created": search_activity.get("created", 0),
                        "reused": search_activity.get("reused", 0),
                        "searches": search_activity.get("searches", 0),
                        "bookings": search_activity.get("bookings", 0),
                        "scored": booking_behavior_score is not None,
                    },
                ),
                scores=AgentTrustScores(
                    operational_score=operational,
                    reliability_score=reliability_score,
                    financial_score=financial_score,
                    experience_score=experience_score,
                    composite_trust_score=composite_trust,
                    ml_calibration_score=ml_calibration_score,
                    overall_score=overall,
                    search_to_booking_score=booking_behavior_score,
                    reliability_detail=reliability_detail,
                ),
                tier=tier,
                badges=badges,
                high_risk_flag=is_high_risk,
                high_risk_reasons=high_risk_reasons,
                calculated_at=datetime.now(timezone.utc),
            )

            score_elapsed_ms = (perf_counter() - score_start) * 1000
            # Default Prometheus histogram buckets are second-based; the metric
            # name keeps the legacy _ms suffix for dashboard compatibility.
            AGENT_TRUST_DURATION.observe(score_elapsed_ms / 1000.0)
            AGENT_TRUST_REQUESTS.labels(status="success").inc()

            self.cache.set(cache_key, result.model_dump())
            return result

        except (DatabaseUnavailableError, SchemaChangedError) as err:
            DATABASE_FAILURES.inc()
            AGENT_TRUST_REQUESTS.labels(status="error").inc()
            logger.warning("DB failure -> trying stale cache: %s", err.code)
            stale = self.cache.get_stale(cache_key)
            stale_result = self._deserialize_or_clear(cache_key, stale)
            if stale_result:
                return stale_result
            raise

        finally:
            self.cache.release_lock(cache_key, lock_token)
