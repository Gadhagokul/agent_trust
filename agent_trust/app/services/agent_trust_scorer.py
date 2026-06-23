# app/services/agent_trust_scorer.py

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.errors import (
    DatabaseUnavailableError,
    SchemaChangedError,
)
from app.domain.models import (
    AgentTrustFeatures,
    AgentTrustResult,
    AgentTrustScores,
    ConversionMetrics,
)
from app.infra.db.audit_repository import AuditRepository
from app.infra.db.repository import AgentRepository
from app.infra.settings import get_settings
from app.ml.trust_model import TrustModelPredictor
from app.services.cache_adapter import CacheAdapter

logger = logging.getLogger(__name__)


class AgentTrustScorer:

    def __init__(self):
        self.repository = AgentRepository()
        self.cache = CacheAdapter()

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

    def _calculate_credit_score(self, stats: dict) -> tuple:
        """
        Derive operational and credit-weighted scores from credit transaction stats.

        Returns:
            (operational_score, credit_overall, raw_delay_penalty)

        Scoring logic:
          - Heavily penalizes high unpaid ratios and overdue balances.
          - avg_delay_days drives a soft exponential decay in the credit component.
          - operational_score is a direct inverse of unpaid_ratio (0–100 range).
        """
        current_delay = stats.get("current_credit_delay_days", 0)
        unpaid_ratio = stats.get("unpaid_ratio", 0.0)
        unpaid_count = stats.get("unpaid_count", 0)

        # Operational score: purely how clean the unpaid ratio is
        operational = max(5, int(100 - unpaid_ratio))

        # Delay penalty: gentle decay — every 10 days overdue costs ~5 points
        delay_penalty = min(int(current_delay / 10) * 5, 40)

        # Extra penalty for accumulation of overdue invoices
        count_penalty = min(unpaid_count * 3, 20)

        credit_overall = max(5, operational - delay_penalty - count_penalty)

        return operational, credit_overall, delay_penalty

    def _determine_tier(
        self,
        overall: int,
        unpaid_count: int,
        unpaid_ratio: float,
        current_credit_delay_days: int,
    ) -> str:
        """
        Maps the final blended overall_score to a tier label.

        Platinum : ≥ 80, zero unpaid and avg_delay < 5 days
        Gold     : ≥ 65
        Silver   : ≥ 50
        Bronze   : ≥ 35
        High Risk: < 35 OR unpaid_ratio > max_unpaid_ratio OR avg_delay > max_delay_days
        """
        settings = get_settings()
        # Hard override: automatic High Risk if debt signals are extreme
        if (
            unpaid_ratio > settings.credit_max_unpaid_ratio
            or current_credit_delay_days > settings.credit_max_delay_days
            or unpaid_count >= settings.credit_max_unpaid_count
        ):
            return "High Risk"

        if overall >= 80 and unpaid_count == 0 and current_credit_delay_days < 5:
            return "Platinum"
        elif overall >= 65:
            return "Gold"
        elif overall >= 50:
            return "Silver"
        elif overall >= 35:
            return "Bronze"
        else:
            return "High Risk"

    def _check_high_risk(
        self,
        db: Session,
        agent_id: int,
        credit_stats: dict,
    ) -> tuple[bool, list[str]]:
        """
        Returns (is_high_risk, list_of_reasons).
        """
        settings = get_settings()
        reasons = []
        unpaid_ratio = credit_stats.get("unpaid_ratio", 0.0)
        delay_days = credit_stats.get("current_credit_delay_days", 0)
        unpaid_count = credit_stats.get("unpaid_count", 0)

        if unpaid_ratio > settings.credit_max_unpaid_ratio:
            reasons.append(f"Unpaid ratio ({unpaid_ratio}%) exceeds {settings.credit_max_unpaid_ratio}%")
        if delay_days > settings.credit_max_delay_days:
            reasons.append(f"Overdue payment delay ({delay_days} days) exceeds {settings.credit_max_delay_days} days")
        if unpaid_count >= settings.credit_max_unpaid_count:
            reasons.append(f"Outstanding unpaid invoice count ({unpaid_count}) exceeds {settings.credit_max_unpaid_count}")

        recent_statuses = self.repository.get_recent_credit_cycles(db, agent_id, limit=3)
        if len(recent_statuses) >= 3 and all(s != "paid" for s in recent_statuses):
            reasons.append("Defaulted on the last 3 consecutive credit cycles")

        return len(reasons) > 0, reasons

    def _compute_reliability_score(self, batch_stats: dict, exp_stats: dict) -> float:
        """
        Reliability V2:
        45% Booking Success Rate
        25% Cancellation Quality (Inverse)
        15% Refund Behavior (Stubbed to 100%)
        10% Supplier Failure Adjusted Score (Stubbed to 100%)
        5%  SLA Adherence (Stubbed to 100%)
        """
        stats = batch_stats.get(365, {})
        bookings = stats.get("bookings", 0)
        bookstep_failed = stats.get("bookstep_failed", 0)
        
        # 1. Booking Success Rate (45%)
        total_attempts = bookings + bookstep_failed
        if total_attempts == 0:
            success_score = 100.0  # Safe default
        else:
            success_score = (bookings / total_attempts) * 100.0
            
        # 2. Cancellation Quality (25%)
        total_cancelled = exp_stats.get("lifetime_cancelled", 0)
        lifetime_bookings = exp_stats.get("lifetime_bookings", 0)
        total_transactions = total_cancelled + lifetime_bookings
        
        if total_transactions == 0:
            cancel_quality_score = 100.0 # Safe default
        else:
            cancel_rate = total_cancelled / total_transactions
            cancel_quality_score = max(0, (1.0 - cancel_rate) * 100.0)
            
        # 3. Future Components (Stubbed)
        refund_score = 100.0
        supplier_score = 100.0
        sla_score = 100.0
        
        # Weighted Total
        reliability = (
            (success_score * 0.45) +
            (cancel_quality_score * 0.25) +
            (refund_score * 0.15) +
            (supplier_score * 0.10) +
            (sla_score * 0.05)
        )
        
        # If absolutely no activity, return baseline 80
        if total_attempts == 0 and total_transactions == 0:
            return 80.0
            
        return round(reliability, 2)

    def _compute_financial_score(self, credit_stats: dict) -> float:
        unpaid_ratio = credit_stats.get("unpaid_ratio", 0.0)
        delay_days = credit_stats.get("current_credit_delay_days", 0)
        score = 100.0 - unpaid_ratio
        delay_penalty = min((delay_days / 10) * 5, 40)
        score -= delay_penalty
        return max(5.0, round(score, 2))

    def _compute_experience_score(self, exp_stats: dict) -> float:
        import math
        from datetime import date, datetime
        created_at = exp_stats.get("created_at")
        lifetime_bookings = exp_stats.get("lifetime_bookings", 0)
        
        days_active = 0
        if created_at:
            if isinstance(created_at, datetime):
                days_active = (datetime.utcnow() - created_at).days
            elif isinstance(created_at, date):
                days_active = (datetime.utcnow().date() - created_at).days
                    
        booking_score = min(50, math.log1p(lifetime_bookings) * 7.2) 
        age_score = min(50, math.log1p(max(0, days_active)) * 6.7)
        return round(booking_score + age_score, 2)

    def _determine_badges(self, overall_score: int, experience_score: float, financial_score: float, unpaid_ratio: float, exp_stats: dict) -> list[str]:
        badges = []
        
        # Trusted Partner: Overall Trust > 90 and high experience
        if overall_score >= 90 and experience_score >= 30:
            badges.append("Trusted Partner")
            
        # Perfect Payer: Financial Score = 100 and no unpaid ratio
        if financial_score == 100.0 and unpaid_ratio == 0.0:
            badges.append("Perfect Payer")
            
        # Booking Champion: Lifetime bookings >= 1000
        if exp_stats.get("lifetime_bookings", 0) >= 1000:
            badges.append("Booking Champion")
            
        # Fraud Free is disabled for now per user instruction
        return badges

    def calculate(self, db: Session, agent_id: int) -> AgentTrustResult:
        cache_key = f"trust:agent:{agent_id}:conversion"
        
        cached = self.cache.get(cache_key)
        if cached:
            return AgentTrustResult(**cached)

        lock_acquired = self.cache.acquire_lock(cache_key)
        if not lock_acquired:
            stale = self.cache.get_stale(cache_key)
            if stale:
                logger.warning("Serving stale cache for %s", cache_key)
                return AgentTrustResult(**stale)
            waited = self.cache.wait_for_cache(cache_key)
            if waited:
                return AgentTrustResult(**waited)
            raise DatabaseUnavailableError("Service busy. Try again.")

        try:
            agent_id_val, agent_name = self.repository.get_agent(db, agent_id)
            credit_stats = self.repository.get_credit_stats(db, agent_id)
            batch_stats = self.repository.get_multi_timeframe_stats(db, agent_id)
            exp_stats = self.repository.get_experience_stats(db, agent_id)
            
            # --- 1. Compute Transparent Business Components ---
            reliability_score = self._compute_reliability_score(batch_stats, exp_stats)
            financial_score = self._compute_financial_score(credit_stats)
            experience_score = self._compute_experience_score(exp_stats)
            
            # --- 2. Composite Trust Score (Weights: Rel 50%, Fin 30%, Exp 20%) ---
            composite_trust = (
                (reliability_score * 0.50) +
                (financial_score * 0.30) +
                (experience_score * 0.20)
            )
            composite_trust = round(composite_trust, 2)
            
            # --- 3. ML Calibration Layer ---
            stats_7d = batch_stats.get(7, {})
            stats_30d = batch_stats.get(30, {})
            ml_features = {
                "eff_searches_7d": stats_7d.get("effective_searches", 0),
                "bookings_7d": stats_7d.get("bookings", 0),
                "eff_searches_30d": stats_30d.get("effective_searches", 0),
                "bookings_30d": stats_30d.get("bookings", 0),
                "unpaid_count": credit_stats.get("unpaid_count", 0),
                "unpaid_ratio": credit_stats.get("unpaid_ratio", 0.0),
                "delay_days": credit_stats.get("current_credit_delay_days", 0)
            }
            
            is_inactive_overall = (ml_features["eff_searches_7d"] == 0 and ml_features["eff_searches_30d"] == 0)
            
            if is_inactive_overall:
                ml_calibration_score = financial_score
            else:
                predictor = TrustModelPredictor()
                ml_calibration_score = predictor.predict(ml_features)

            # --- 4. Final Trust Score (80% Composite, 20% ML) ---
            overall = int(round((composite_trust * 0.8) + (ml_calibration_score * 0.2)))
            overall = max(5, min(overall, 100))

            # --- 5. High Risk Overrides ---
            is_high_risk, high_risk_reasons = self._check_high_risk(db, agent_id, credit_stats)
            settings = get_settings()
            if is_high_risk:
                overall = min(overall, settings.high_risk_score_cap)

            tier = self._determine_tier(
                overall,
                credit_stats.get("unpaid_count", 0),
                credit_stats.get("unpaid_ratio", 0),
                credit_stats.get("current_credit_delay_days", 0)
            )

            # Legacy operational score computation (kept for backward schema compatibility)
            operational = int(financial_score)
            if is_high_risk:
                operational = min(operational, settings.high_risk_score_cap)

            try:
                last_log = AuditRepository.get_last_audit(db, agent_id_val)
                prev_score = float(last_log.new_score) if last_log else None
                prev_tier = last_log.new_tier if last_log else None
                AuditRepository.append_audit_log(
                    db=db, agent_id=agent_id_val, old_score=prev_score, new_score=overall, 
                    old_tier=prev_tier, new_tier=tier, event_type="score_shift",
                    metadata={"trigger": "composite_recalculation"}
                )
            except Exception as e:
                logger.error("Audit logging skipped: %s", str(e))

            badges = self._determine_badges(
                overall_score=overall,
                experience_score=experience_score,
                financial_score=financial_score,
                unpaid_ratio=credit_stats.get("unpaid_ratio", 0.0),
                exp_stats=exp_stats
            )

            result = AgentTrustResult(
                agent_id=agent_id_val,
                agent_name=agent_name,
                features=AgentTrustFeatures(
                    current_credit_delay_days=credit_stats.get("current_credit_delay_days", 0),
                    unpaid_ratio=credit_stats.get("unpaid_ratio", 0),
                    unpaid_count=credit_stats.get("unpaid_count", 0),
                    no_activity=is_inactive_overall,  
                    daily=ConversionMetrics(**self._safe_metrics(batch_stats.get(1, {}))),
                    weekly=ConversionMetrics(**self._safe_metrics(batch_stats.get(7, {}))),
                    monthly=ConversionMetrics(**self._safe_metrics(batch_stats.get(30, {}))),
                    yearly=ConversionMetrics(**self._safe_metrics(batch_stats.get(365, {}))),
                ),
                scores=AgentTrustScores(
                    operational_score=operational,
                    reliability_score=reliability_score,
                    financial_score=financial_score,
                    experience_score=experience_score,
                    composite_trust_score=composite_trust,
                    ml_calibration_score=ml_calibration_score,
                    overall_score=overall,
                ),
                tier=tier,
                badges=badges,
                high_risk_flag=is_high_risk,
                high_risk_reasons=high_risk_reasons,
                calculated_at=datetime.utcnow(),
            )

            self.cache.set(cache_key, result.model_dump())
            return result

        
        except (DatabaseUnavailableError, SchemaChangedError) as err:
            logger.warning("DB failure → using stale cache: %s", err.code)
            stale = self.cache.get_stale(cache_key)
            if stale:
                return AgentTrustResult(**stale)
            raise

        finally:
            self.cache.release_lock(cache_key)
