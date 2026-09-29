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
    def _read_tail(path: str, max_bytes: int) -> list[str]:
        """
        Return at most the last `max_bytes` bytes of `path` as complete lines.

        Bounding the read keeps the per-request cost of a previous-score lookup
        flat as the log grows. When the file is larger than the budget the read
        starts mid-line, so the leading fragment is discarded: it is a partial
        JSON object and would only ever raise or be mis-parsed.
        """
        try:
            size = os.path.getsize(path)
        except OSError:
            return []

        try:
            with open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(-max_bytes, os.SEEK_END)
                chunk = f.read()
        except OSError:
            return []

        text = chunk.decode("utf-8", errors="ignore")
        if not text:
            return []

        lines = text.split("\n")
        # When a partial read happened the first element is a line fragment.
        if size > max_bytes and lines:
            lines = lines[1:]
        return [ln for ln in lines if ln.strip()]

    @staticmethod
    def _candidate_paths(settings) -> list[str]:
        """The active log followed by its rotated generations, newest first."""
        path = settings.audit_log_path
        candidates = [path]
        for i in range(1, settings.audit_backup_count + 1):
            candidates.append(f"{path}.{i}")
        return candidates

    @staticmethod
    def get_last_audit(agent_id: int) -> dict | None:
        """
        Return the most recent audit entry for the given agent_id, or None.

        Scans the tail of the active log, then walks rotated generations newest
        first, so a previous score that has already rotated out of the active
        file is still found.
        """
        settings = get_settings()
        for candidate in AuditRepository._candidate_paths(settings):
            if not os.path.exists(candidate):
                continue
            lines = AuditRepository._read_tail(candidate, settings.audit_tail_read_bytes)
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("agent_id") == agent_id:
                    return entry
        return None

    @staticmethod
    def _rotate_if_needed(path: str, incoming_bytes: int) -> None:
        """
        Shift generations down and start a fresh active log when it is full.

        Best effort: a filesystem problem here must never block a score write,
        so failures are logged and the caller appends to the existing file.
        """
        settings = get_settings()
        try:
            if not os.path.exists(path):
                return
            if os.path.getsize(path) + incoming_bytes <= settings.audit_rotate_max_bytes:
                return

            oldest = f"{path}.{settings.audit_backup_count}"
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(settings.audit_backup_count - 1, 0, -1):
                src = f"{path}.{i}"
                if os.path.exists(src):
                    os.replace(src, f"{path}.{i + 1}")
            os.replace(path, f"{path}.1")
        except OSError:
            logger.exception("Audit log rotation failed for %s", path)

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
            serialized = json.dumps(entry) + "\n"
            # Rotate only on a real write, so a duplicate skip never rotates.
            AuditRepository._rotate_if_needed(log_path, len(serialized.encode("utf-8")))
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(serialized)

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
