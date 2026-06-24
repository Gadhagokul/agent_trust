import logging

from sqlalchemy.orm import Session

from app.domain.audit_models import AgentTrustAuditLog

# Table is managed externally by Laravel/MySQL migrations.
# FastAPI does NOT auto-migrate this schema.

logger = logging.getLogger(__name__)


class AuditRepository:
    """Appends agent trust score shift records into the centralized MySQL ledger."""

    @staticmethod
    def get_last_audit(db: Session, agent_id: int) -> AgentTrustAuditLog:
        return db.query(AgentTrustAuditLog)\
            .filter(AgentTrustAuditLog.agent_id == agent_id)\
            .order_by(AgentTrustAuditLog.id.desc())\
            .first()

    @staticmethod
    def append_audit_log(
        db: Session,
        agent_id: int,
        old_score: int,
        new_score: int,
        old_tier: str,
        new_tier: str,
        event_type: str = "score_shift",
        metadata: dict = None,
    ):
        try:
            # Final defensive check against duplicates
            last = AuditRepository.get_last_audit(db, agent_id)
            if last and last.new_score == new_score and last.new_tier == new_tier:
                logger.info("Skipping duplicate audit log for Agent %s", agent_id)
                return

            delta = round(new_score - (old_score or 0), 2)
            log_entry = AgentTrustAuditLog(
                agent_id=agent_id,
                old_score=old_score,
                new_score=new_score,
                score_delta=delta,
                old_tier=old_tier,
                new_tier=new_tier,
                event_type=event_type,
                audit_context=metadata or {},
            )

            db.add(log_entry)
            db.commit()   # Finalize the audit entry for centralized MySQL

            logger.info(
                "Audit log saved → Agent %s | %s→%s (%+.2f) | %s→%s",
                agent_id, old_score, new_score, delta, old_tier, new_tier,
            )
        except Exception:
            db.rollback()
            logger.exception("Failed to append Audit Log for Agent %s", agent_id)
