# scripts/train_db_trust_model.py
"""Sprint 5/6 - per-target ML Target Programme trainer (ships DISABLED).

The trainer is part of the disabled-by-default ML Target Programme (senior
15.1 / 16-24). It runs ONLY when the programme is both configured AND the
scorer's ml_ready gate is live. While disabled (the shipped default) the
trainer is a SILENT LOG-ONLY NO-OP:

    * never builds a programme dataset;
    * never evaluates readiness;
    * never trains a model;
    * never writes to the model registry;
    * never increments any ML counter.

This matches the senior 15.1 exit: 'the programme ships disabled; nothing
runs while it is off, and the final score is round(composite_trust) -- never
a blend'. The disabled gate below is the ONLY code path reached while off,
and it does not even import the training stack (no RandomForest / repository on
the active path).

Sprint 6 adds the real ENABLED path: when the programme is on, this entrypoint
delegates to the training controller (app.ml.trust_model), which owns trigger
evaluation, readiness, candidate evaluation, champion/challenger promotion and
rollback. The classifier trainer and the dataset builder are injected into the
controller, keeping the dependency direction one-way (scripts -> app).
"""

import logging

from app.infra.settings import get_settings

logger = logging.getLogger(__name__)

PROGRAMME_STATUS_NOT_READY = "NOT_READY"
PROGRAMME_STATUS_READY = "READY"
PROGRAMME_REASON_DISABLED = "ml_programme_disabled"
PROGRAMME_REASON_GATE_OFF = "ml_ready_gate_off"
PROGRAMME_REASON_ENABLED = "programme_enabled"


def _programme_gate(settings) -> dict:
    """Silent readiness gate. Returns the disabled/not-ready outcome.

    NEVER raises. NEVER builds a dataset. NEVER trains. This is the only
    code path reached while the programme is off, and it is a silent
    log-only no-op (senior 15.1: nothing runs while disabled).
    """
    ml_enabled = bool(getattr(settings, "ml_enabled", False))
    ml_targets = list(getattr(settings, "ml_targets", []) or [])

    if not ml_enabled:
        reason = PROGRAMME_REASON_DISABLED
    elif not ml_targets:
        reason = PROGRAMME_REASON_GATE_OFF
    else:
        logger.info(
            "ml_programme gate: %s (ml_enabled=%s ml_targets=%d)",
            PROGRAMME_REASON_ENABLED,
            ml_enabled,
            len(ml_targets),
        )
        return {
            "status": PROGRAMME_STATUS_READY,
            "reason": PROGRAMME_REASON_ENABLED,
            "targets": ml_targets,
        }

    logger.info(
        "ml_programme gate: %s (ml_enabled=%s ml_targets=%d)",
        reason,
        ml_enabled,
        len(ml_targets),
    )
    return {"status": PROGRAMME_STATUS_NOT_READY, "reason": reason}


def _run_enabled_programme(settings) -> dict:
    """Sprint 6 enabled path: run the real training controller.

    The controller owns the lifecycle (trigger, readiness, candidate evaluation,
    champion/challenger, promotion, rollback) and receives the dataset builder and
    the classifier trainer as injected callables, so the dependency direction stays
    one-way (scripts -> app) and no circular import is possible. The session is
    opened here and closed by the controller's own finally block.
    """
    from app.infra.db.session import SessionLocal
    from app.ml.trust_model import (
        build_default_dataset,
        run_training_controller,
        train_random_forest_candidate,
    )

    def build_dataset(target, *, as_of, db):
        return build_default_dataset(settings, db, target, as_of=as_of)

    def labeled_count_reader(db, target, as_of):
        from app.infra.db.repository import AgentRepository

        return AgentRepository().get_labeled_sample_count(
            db,
            target=target,
            horizon_days=int(settings.ml_horizon_days),
            as_of=as_of,
        )

    return run_training_controller(
        settings,
        build_dataset=build_dataset,
        train_candidate=train_random_forest_candidate,
        session_factory=SessionLocal,
        labeled_count_reader=labeled_count_reader,
    )


def main() -> dict:
    """Trainer entrypoint. Ships DISABLED: this is a silent no-op while off.

    The gate below is the first and ONLY executable statement of the active
    path. While ml_enabled=False (the default) the trainer never builds a
    dataset, never evaluates readiness, never trains, and never writes to the
    model registry. This matches senior 15.1 exactly: 'the programme ships
    disabled and never runs while off'. When the programme IS enabled this
    entrypoint delegates to the Sprint 6 training controller.
    """
    settings = get_settings()
    gate = _programme_gate(settings)
    if gate["status"] != PROGRAMME_STATUS_READY:
        logger.info(
            "ml_programme trainer: status=%s reason=%s -- no dataset, no training, "
            "no registry write (ships disabled, senior 15.1).",
            gate["status"],
            gate["reason"],
        )
        return gate
    result = _run_enabled_programme(settings)
    logger.info("ml_programme trainer: status=%s", result.get("status"))
    return result


if __name__ == "__main__":
    outcome = main()
    print(f"ml_programme trainer: status={outcome.get('status')} reason={outcome.get('reason')}")
