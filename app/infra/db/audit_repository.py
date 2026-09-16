import json
import logging
import os
from datetime import datetime, timezone

from app.infra.settings import get_settings

logger = logging.getLogger(__name__)


class AuditRepository:
    """Appends agent trust score shift records to a local JSONL audit log file."""

    @staticmethod
    def _get_log_path() -> str:
        settings = get_settings()
        path = settings.audit_log_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    @staticmethod
    def get_last_audit(agent_id: int) -> dict | None:
        """
        Read the audit log file from the end and return the most recent entry
        for the given agent_id. Returns None if no entry exists.
        """
        log_path = AuditRepository._get_log_path()
        if not os.path.exists(log_path):
            return None

        try:
            with open(log_path, encoding="utf-8") as f:
                lines = f.readlines()

            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("agent_id") == agent_id:
                        return entry
                except json.JSONDecodeError:
                    continue

            return None
        except Exception:
            logger.exception("Failed to read audit log for agent %s", agent_id)
            return None

    @staticmethod
    def append_audit_log(
        agent_id: int,
        old_score: float | None,
        new_score: int,
        old_tier: str | None,
        new_tier: str,
        event_type: str = "score_shift",
        metadata: dict | None = None,
    ):
        try:
            last = AuditRepository.get_last_audit(agent_id)
            if last and last.get("new_score") == new_score and last.get("new_tier") == new_tier:
                logger.info("Skipping duplicate audit log for Agent %s", agent_id)
                return

            delta = round(new_score - (old_score or 0), 2)
            entry = {
                "agent_id": agent_id,
                "old_score": old_score,
                "new_score": new_score,
                "score_delta": delta,
                "old_tier": old_tier,
                "new_tier": new_tier,
                "event_type": event_type,
                "metadata": metadata or {},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            log_path = AuditRepository._get_log_path()
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

            logger.info(
                "Audit log saved → Agent %s | %s→%s (%+.2f) | %s→%s",
                agent_id,
                old_score,
                new_score,
                delta,
                old_tier,
                new_tier,
            )
        except Exception:
            logger.exception("Failed to append Audit Log for Agent %s", agent_id)
