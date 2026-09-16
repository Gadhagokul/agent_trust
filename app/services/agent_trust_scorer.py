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
)
from app.infra.db.audit_repository import AuditRepository
from app.infra.db.repository import AgentRepository, CreditStats
from app.infra.settings import get_settings
from app.ml.trust_model import TrustModelPredictor
from app.observability.metrics import AGENT_TRUST_DURATION, AGENT_TRUST_REQUESTS
from app.services.cache_adapter import CacheAdapter

logger = logging.getLogger(__name__)


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
        current_overdue_count: int,
        current_overdue_ratio: float,
        current_max_delay_days: int,
    ) -> str:
        """
        Maps the final blended overall_score to a tier label.

        Platinum : >= 80, zero overdue and delay < 5 days
        Gold     : >= 65
        Silver   : >= 50
        Bronze   : >= 35
        High Risk: < 35 OR overdue_ratio > max OR delay > max OR overdue_count >= max
        """
        settings = get_settings()
        if (
            current_overdue_ratio > settings.credit_max_overdue_ratio
            or current_max_delay_days > settings.credit_max_delay_days
            or current_overdue_count >= settings.credit_max_overdue_count
        ):
            return "High Risk"

        if overall >= 80 and current_overdue_count == 0 and current_max_delay_days < 5:
            return "Platinum"
        elif overall >= 65:
            return "Gold"
        elif overall >= 50:
            return "Silver"
        elif overall >= 35:
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
        if credit_stats.consecutive_unpaid_cycles >= 3:
            reasons.append("Defaulted on the last 3 consecutive credit cycles")

        return len(reasons) > 0, reasons

    def _compute_reliability_score(self, batch_stats: dict, exp_stats: dict) -> float | None:
        """
        Reliability V3:
        64.3% Booking Success Rate
        35.7% Cancellation Quality (Inverse)

        Refund, Supplier, and SLA were removed as placeholders (no real data);
        weights in reliability_component_weights are renormalized when re-added.
        Returns None (component excluded) when the agent has no booking
        behavior evidence, so the composite renormalizes over active parts.
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
            return None

        weights = settings.reliability_component_weights
        weight_sum = sum(weights.values())
        component_scores = {
            "booking_success": success_score,
            "cancellation_quality": cancel_quality_score,
        }
        reliability = sum(component_scores[k] * w for k, w in weights.items()) / weight_sum
        return round(reliability, 2)

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

    def _compute_booking_behavior_score(self, activity: dict, target_ratio: float) -> float | None:
        """
        Agent Search-to-Booking component (Feature B).

        Searches = every search intent the agent used (created + reused access
        rows), and bookings = successful bookings, both over the same 365-day
        window. The target ratio is derived from suppliers (Σ minimum_booking /
        Σ search_limit, currently 0.05 = 20:1). Meeting the target ratio equals
        the minimum acceptable score (default 80); the score rises linearly to
        100 at `excellent_multiplier` x the target ratio (default 4x = 20%).

        Returns None (component excluded) when the agent has no search behavior
        data or no executable target — in that case the composite is
        renormalized over the active components.
        """
        settings = get_settings()
        searches = activity.get("searches", 0)
        bookings = activity.get("bookings", 0)

        if not searches or target_ratio <= 0:
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
            target_ratio = self.repository.get_supplier_expected_ratio(db)

            # --- 1. Compute Transparent Business Components ---
            reliability_score = self._compute_reliability_score(batch_stats, exp_stats)
            financial_score = self._compute_financial_score(credit_stats)
            experience_score = self._compute_experience_score(exp_stats)
            booking_behavior_score = self._compute_booking_behavior_score(
                search_activity, target_ratio
            )

            # --- 2. Composite Trust Score (Weights: Rel 40%, Fin 25%, Exp 15%, S2B 20%) ---
            # Components with no evidence (reliability / booking behavior returning
            # None) are excluded and the remaining weights renormalized so the
            # composite stays on the 0-100 scale and remains comparable.
            weights = get_settings().composite_weights
            active_parts: dict[str, float] = {}
            active_weight_sum = 0.0
            if reliability_score is not None:
                active_parts["reliability"] = reliability_score
                active_weight_sum += weights["reliability"]
            active_parts["financial"] = financial_score
            active_parts["experience"] = experience_score
            active_weight_sum += weights["financial"] + weights["experience"]
            if booking_behavior_score is not None:
                active_parts["search_to_booking"] = booking_behavior_score
                active_weight_sum += weights["search_to_booking"]

            composite_trust = round(
                sum(active_parts[k] * weights[k] for k in active_parts) / active_weight_sum,
                2,
            )

            # --- 3. ML Calibration Layer ---
            stats_7d = batch_stats.get(7, {})
            stats_30d = batch_stats.get(30, {})
            ml_features = {
                "eff_searches_7d": stats_7d.get("effective_searches", 0),
                "bookings_7d": stats_7d.get("bookings", 0),
                "eff_searches_30d": stats_30d.get("effective_searches", 0),
                "bookings_30d": stats_30d.get("bookings", 0),
                "current_overdue_count": credit_stats.current_overdue_count,
                "current_overdue_ratio": credit_stats.current_overdue_ratio,
                "current_max_delay_days": credit_stats.current_max_delay_days,
            }

            is_inactive_overall = (
                ml_features["eff_searches_7d"] == 0 and ml_features["eff_searches_30d"] == 0
            )

            if is_inactive_overall:
                ml_calibration_score = financial_score
            else:
                try:
                    predictor = TrustModelPredictor()
                    ml_calibration_score = predictor.predict(ml_features)
                except ModelUnavailableError:
                    logger.warning(
                        "ML model unavailable -- falling back to financial score for agent %s",
                        agent_id,
                    )
                    ml_calibration_score = financial_score

            # --- 4. Final Trust Score (80% Composite, 20% ML) ---
            overall = round((composite_trust * 0.8) + (ml_calibration_score * 0.2))
            overall = max(5, min(overall, 100))

            # --- 5. High Risk Overrides ---
            is_high_risk, high_risk_reasons = self._check_high_risk(credit_stats)
            settings = get_settings()
            if is_high_risk:
                overall = min(overall, settings.high_risk_score_cap)

            tier = self._determine_tier(
                overall,
                credit_stats.current_overdue_count,
                credit_stats.current_overdue_ratio,
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
                    metadata={"trigger": "composite_recalculation"},
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
                ),
                tier=tier,
                badges=badges,
                high_risk_flag=is_high_risk,
                high_risk_reasons=high_risk_reasons,
                calculated_at=datetime.now(timezone.utc),
            )

            score_elapsed_ms = (perf_counter() - score_start) * 1000
            AGENT_TRUST_DURATION.observe(score_elapsed_ms)
            AGENT_TRUST_REQUESTS.labels(status="success").inc()

            self.cache.set(cache_key, result.model_dump())
            return result

        except (DatabaseUnavailableError, SchemaChangedError) as err:
            AGENT_TRUST_REQUESTS.labels(status="error").inc()
            logger.warning("DB failure -> trying stale cache: %s", err.code)
            stale = self.cache.get_stale(cache_key)
            stale_result = self._deserialize_or_clear(cache_key, stale)
            if stale_result:
                return stale_result
            raise

        finally:
            self.cache.release_lock(cache_key, lock_token)
