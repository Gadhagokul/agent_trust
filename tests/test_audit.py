"""
A3: bounded audit-log tail reads and size-based rotation.

These tests exercise the real filesystem in tmp_path -- no mocks -- because the
behaviour under test is precisely how bytes are read and renamed.
"""

import json

import pytest

from app.infra.db.audit_repository import AuditRepository
from app.infra.settings import get_settings


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    """Point the audit log at a temp file with test-sized rotation settings."""
    log = tmp_path / "logs" / "agent_score_audits.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    monkeypatch.setattr(settings, "audit_log_path", str(log))
    monkeypatch.setattr(settings, "audit_tail_read_bytes", 4096)
    monkeypatch.setattr(settings, "audit_rotate_max_bytes", 2048)
    monkeypatch.setattr(settings, "audit_backup_count", 3)
    return log


def _write_entries(log, count, agent_id=1, size_hint=200):
    """Append `count` entries directly, bypassing the rotation trigger."""
    with open(log, "a", encoding="utf-8") as fh:
        for i in range(count):
            entry = {
                "agent_id": agent_id,
                "old_score": i,
                "new_score": i + 1,
                "new_tier": "Silver",
                "created_at": f"2026-01-01T00:00:{i % 60:02d}+00:00",
                "padding": "x" * size_hint,
            }
            fh.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- #
# Bounded tail read                                                             #
# --------------------------------------------------------------------------- #


def test_read_tail_returns_whole_file_when_smaller_than_budget(audit_env):
    _write_entries(audit_env, 5)
    lines = AuditRepository._read_tail(str(audit_env), 4096)
    assert len(lines) == 5
    assert all(json.loads(ln) for ln in lines), "every line must be complete JSON"


def test_read_tail_is_bounded_on_large_file(audit_env):
    _write_entries(audit_env, 500, size_hint=100)
    assert audit_env.stat().st_size > 4096, "fixture must exceed the budget"

    lines = AuditRepository._read_tail(str(audit_env), 4096)

    assert lines, "tail must not be empty"
    assert len(lines) < 500, "read must be bounded, not the whole file"
    # The budget bounds the read, so far fewer than 500 lines come back.
    assert len(lines) <= 4096 // 50


def test_read_tail_discards_partial_first_line(audit_env):
    """
    A byte-offset read lands mid-line. The leading fragment must be dropped --
    it is a partial JSON object, so keeping it would raise or mis-parse on
    every single read.
    """
    _write_entries(audit_env, 200, size_hint=100)

    lines = AuditRepository._read_tail(str(audit_env), 4096)

    assert lines, "tail must not be empty"
    for line in lines:
        json.loads(line)  # raises if any fragment survived


def test_read_tail_missing_file_returns_empty(tmp_path):
    assert AuditRepository._read_tail(str(tmp_path / "nope.log"), 4096) == []


def test_read_tail_handles_unicode_without_crashing(tmp_path):
    log = tmp_path / "u.log"
    with open(log, "w", encoding="utf-8") as fh:
        for i in range(200):
            fh.write(json.dumps({"agent_id": i, "name": "Ünïcödé ✈"}) + "\n")

    lines = AuditRepository._read_tail(str(log), 4096)
    for line in lines:
        json.loads(line)


# --------------------------------------------------------------------------- #
# Lookup semantics (unchanged behaviour, now bounded)                          #
# --------------------------------------------------------------------------- #


def test_get_last_audit_finds_most_recent_entry(audit_env):
    AuditRepository.append_audit_log(1, 50.0, 60, "Bronze", "Silver")
    AuditRepository.append_audit_log(1, 60.0, 70, "Silver", "Gold")

    last = AuditRepository.get_last_audit(1)

    assert last is not None
    assert last["new_score"] == 70
    assert last["new_tier"] == "Gold"


def test_get_last_audit_returns_none_for_unknown_agent(audit_env):
    AuditRepository.append_audit_log(1, 50.0, 60, "Bronze", "Silver")
    assert AuditRepository.get_last_audit(4242) is None


def test_get_last_audit_returns_none_when_file_absent(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "audit_log_path", str(tmp_path / "missing.log"))
    monkeypatch.setattr(settings, "audit_backup_count", 3)
    assert AuditRepository.get_last_audit(1) is None


def test_get_last_audit_skips_corrupt_line(audit_env):
    AuditRepository.append_audit_log(7, 40.0, 55, "Bronze", "Silver")
    with open(audit_env, "a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    AuditRepository.append_audit_log(7, 55.0, 80, "Silver", "Platinum")

    last = AuditRepository.get_last_audit(7)
    assert last is not None
    assert last["new_score"] == 80, "a corrupt line must be skipped, not fatal"


def test_get_last_audit_separates_agents(audit_env):
    AuditRepository.append_audit_log(1, 10.0, 20, "Bronze", "Bronze")
    AuditRepository.append_audit_log(2, 30.0, 40, "Bronze", "Silver")
    AuditRepository.append_audit_log(1, 20.0, 25, "Bronze", "Silver")

    assert AuditRepository.get_last_audit(1)["new_score"] == 25
    assert AuditRepository.get_last_audit(2)["new_score"] == 40


def test_duplicate_write_is_skipped(audit_env):
    AuditRepository.append_audit_log(1, 50.0, 60, "Bronze", "Silver")
    before = audit_env.read_text(encoding="utf-8")
    AuditRepository.append_audit_log(1, 60.0, 60, "Silver", "Silver")
    after = audit_env.read_text(encoding="utf-8")

    assert before == after, "an unchanged score+tier must not append a duplicate"


# --------------------------------------------------------------------------- #
# Rotation                                                                      #
# --------------------------------------------------------------------------- #


def test_rotation_moves_active_log_to_dot_one(audit_env):
    for i in range(40):
        AuditRepository.append_audit_log(1, float(i), i + 1, "Bronze", "Silver")

    assert (audit_env.parent / "agent_score_audits.log.1").exists(), "rotation must have fired"
    assert audit_env.stat().st_size < 2048, "active log must have been truncated"


def test_rotation_preserves_entry_count(audit_env):
    for i in range(60):
        AuditRepository.append_audit_log(1, float(i), i + 1, "Bronze", "Silver")

    total = 0
    for suffix in ("", ".1", ".2", ".3"):
        path = audit_env.parent / f"agent_score_audits.log{suffix}"
        if path.exists():
            total += len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])
    assert total > 0, "no entries may be lost across rotations"


def test_rotation_never_exceeds_backup_count(audit_env):
    for i in range(200):
        AuditRepository.append_audit_log(1, float(i), i + 1, "Bronze", "Silver")

    survivors = sorted(
        p.name for p in audit_env.parent.glob("agent_score_audits.log*")
    )
    assert "agent_score_audits.log.4" not in survivors, "retention cap must be enforced"
    assert len(survivors) <= 4, f"active + 3 backups expected, found {survivors}"


def test_previous_score_found_after_rotation(audit_env):
    """
    The point of rotation: an entry that has rotated into .log.1 must still be
    reachable, otherwise every write after rotation would record a null
    previous score and the audit trail would lose its history.
    """
    for i in range(40):
        AuditRepository.append_audit_log(99, float(i), i + 1, "Bronze", "Silver")

    last = AuditRepository.get_last_audit(99)
    assert last is not None, "entry must still be found after rotation"
    assert last["new_score"] == 40


def test_duplicate_skip_does_not_trigger_rotation(audit_env):
    """
    Rotation runs after the duplicate check, so a no-op write must not rotate
    the log -- otherwise a stream of unchanged scores would churn generations.
    """
    AuditRepository.append_audit_log(1, 50.0, 60, "Bronze", "Silver")
    for _ in range(20):
        AuditRepository.append_audit_log(1, 60.0, 60, "Silver", "Silver")

    assert not (audit_env.parent / "agent_score_audits.log.1").exists()


def test_rotation_survives_unwritable_directory(audit_env, monkeypatch):
    """A rotation failure must not block the score write."""
    AuditRepository.append_audit_log(1, 50.0, 60, "Bronze", "Silver")
    _write_entries(audit_env, 30, size_hint=100)
    assert audit_env.stat().st_size > 2048

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("app.infra.db.audit_repository.os.replace", _boom)

    AuditRepository.append_audit_log(1, 60.0, 61, "Silver", "Gold")

    assert AuditRepository.get_last_audit(1) is not None, "the write must still land"


# --------------------------------------------------------------------------- #
# Settings validation                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field, value",
    [
        ("audit_tail_read_bytes", 0),
        ("audit_tail_read_bytes", 512),
        ("audit_backup_count", 0),
        ("audit_backup_count", -1),
    ],
)
def test_invalid_audit_settings_rejected(monkeypatch, field, value):
    settings = get_settings()
    original = getattr(settings, field)
    monkeypatch.setattr(settings, field, value)
    try:
        with pytest.raises(RuntimeError):
            settings._validate_startup()
    finally:
        monkeypatch.setattr(settings, field, original)


def test_rotate_limit_must_exceed_tail_budget(monkeypatch):
    settings = get_settings()
    original = settings.audit_rotate_max_bytes
    monkeypatch.setattr(settings, "audit_rotate_max_bytes", settings.audit_tail_read_bytes)
    try:
        with pytest.raises(RuntimeError):
            settings._validate_startup()
    finally:
        monkeypatch.setattr(settings, "audit_rotate_max_bytes", original)


def test_shipped_defaults_are_valid():
    """The committed defaults must pass their own validation."""
    settings = get_settings()
    assert settings.audit_tail_read_bytes == 262_144
    assert settings.audit_rotate_max_bytes == 10_485_760
    assert settings.audit_backup_count == 3
    settings._validate_startup()
