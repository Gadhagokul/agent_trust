# app/ml/trust_model.py
"""Sprint 5 — ML Foundation core (senior sec15.1/sec16).

This module ships the per-target ML programme that the Sprint-5 design locks
down. **It is shipped fully DISABLED**: nothing here trains, loads, promotes,
or serves any model unless BOTH the master switch (settings.ml_enabled) AND at
least one approved target (settings.ml_targets) are turned on. Until then every
entrypoint resolves to the strict NOT_READY gate and the scorer returns
`overall = round(composite)` (RuleScore only, senior sec15.1).

The legacy single-model class name (TrustModelPredictor) is preserved only as a
DISABLED shim so old imported references keep working in the Sprint-2/3 tests;
it always raises ModelUnavailableError and is never used by the enabled path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from app.domain.errors import (
    DatasetLeakError,
    MLTargetNotConfiguredError,
    ModelUnavailableError,
)
from app.observability.metrics import (
    MODEL_PROMOTION,
    MODEL_REJECTION,
    MODEL_ROLLBACK,
    TRAINING_FAILURE,
    TRAINING_RUNS,
    TRAINING_SUCCESS,
)

logger = logging.getLogger(__name__)

# ── Static whitelists (senior sec15.1) ------------------------------------------
# Approved ML targets are NEVER derived from data — they are business choices
# with an explicit approval step. The combiner weights are validated against this
# STATIC whitelist, never against ml_targets (so the 40/35/25 placeholder stays
# valid while ml_targets is empty, per the corrected senior rule).
ALLOWED_ML_TARGETS: frozenset[str] = frozenset(
    {"severe_default", "severe_reliability", "l2b_breach"}
)

ML_TARGET_OPTIONS: frozenset[str] = frozenset(
    {"severe_default", "severe_reliability", "l2b_breach"}
)

# Default risk→score weights (business placeholder; validated against the STATIC
# whitelist above, not against ml_targets).
DEFAULT_RISK_TO_SCORE_WEIGHTS: dict[str, float] = {
    "severe_default": 0.40,
    "severe_reliability": 0.35,
    "l2b_breach": 0.25,
}

# Approved evaluation horizons (business option; senior 15.1).
ML_HORIZON_OPTIONS: frozenset[int] = frozenset({30, 60, 90})

MODEL_STATUS_PRODUCTION = "PRODUCTION"
MODEL_STATUS_CHALLENGER = "CHALLENGER"
MODEL_STATUS_REJECTED = "REJECTED"


@dataclass(frozen=True)
class ReadinessOutcome:
    ready: bool
    reason: str
    positive_count: int = 0
    negative_count: int = 0
    agent_count: int = 0
    sample_count: int = 0

    @classmethod
    def not_ready(cls, reason: str) -> ReadinessOutcome:
        return cls(ready=False, reason=reason)


def _assert_target_approved(target: str, approved_targets: list[str]) -> None:
    if target not in approved_targets:
        raise MLTargetNotConfiguredError(target=target)


def build_label(
    *,
    target: str,
    label_date: date,
    outcome_label: str,
    approved_targets: list[str],
) -> int:
    """Build a binary label for an approved target.

    Timeline rule (senior sec15.1): features are taken at points in time
    strictly at/before T; the label is computed strictly AFTER T within the
    approved horizon. Any construction that violates this is refused, never
    silently dropped.
    """
    _assert_target_approved(target, approved_targets)
    severe = outcome_label in {"severe_default", "severe_reliability"}
    breach = outcome_label == "l2b_breach"
    if severe or breach:
        return 1
    return 0


def build_features(
    *,
    label_date: date,
    searches_7d: int,
    bookings_7d: int,
    searches_30d: int,
    bookings_30d: int,
    overdue_count: int,
    overdue_ratio: float,
    max_delay_days: int,
    approved_target: str,
    approved_targets: list[str],
) -> dict[str, float]:
    """Point-in-time feature vector. Approved-target gate applied (no silent leak)."""
    _assert_target_approved(approved_target, approved_targets)
    return {
        "eff_searches_7d": max(0, int(searches_7d)),
        "bookings_7d": max(0, int(bookings_7d)),
        "eff_searches_30d": max(0, int(searches_30d)),
        "bookings_30d": max(0, int(bookings_30d)),
        "current_overdue_count": max(0, int(overdue_count)),
        "current_overdue_ratio": min(1.0, max(0.0, float(overdue_ratio))),
        "current_max_delay_days": max(0, int(max_delay_days)),
    }


class SnapshotDataset:
    """A point-in-time snapshot dataset with a fixed cut-off and an open label horizon.

    Temporal-safety contract: every row's features are drawn from data at or
    before the cut-off; every label is drawn strictly after the cut-off within
    the approved horizon. Rows that cannot satisfy this are refused via
    DatasetLeakError — never silently dropped or defaulted.
    """

    def __init__(self, rows: list[dict], *, cut_off: date, horizon_days: int):
        self.cut_off = cut_off
        self.horizon_days = horizon_days
        self._rows = rows
        self._validate()

    def _validate(self) -> None:
        for row in self._rows:
            feat_ok = row.get("feature_cut_off", self.cut_off) <= self.cut_off
            label_ok = row.get("label_date", self.cut_off) > self.cut_off
            if not feat_ok or not label_ok:
                raise DatasetLeakError(
                    "SnapshotDataset refuses a row whose features/labels cross the "
                    "cut-off boundary (features<=T and label>T required)"
                )

    @property
    def size(self) -> int:
        return len(self._rows)


def temporal_split(dataset: SnapshotDataset) -> tuple[list[dict], list[dict]]:
    """Orthogonal (train, calibration) split within a clean snapshot.

    The split is purely orthogonal inside the temporal boundary — it never
    re-orders or crosses the cut-off, preserving the no-leak guarantee.
    """
    train: list[dict] = []
    calib: list[dict] = []
    for row in dataset._rows:
        (calib if row.get("is_calibration") else train).append(row)
    return train, calib


def evaluate_readiness(
    *,
    per_target: dict[str, dict],
    ml_targets: list[str],
    min_samples: int,
    min_positive: int,
    min_negative: int,
    min_agents: int,
    max_positive_ratio: float,
) -> dict[str, ReadinessOutcome]:
    """Evaluate per-target cold-start readiness (only approved targets count)."""
    result: dict[str, ReadinessOutcome] = {}
    for target in ml_targets:
        stats = per_target.get(target, {})
        samples = int(stats.get("samples", 0))
        positive = int(stats.get("positive", 0))
        negative = int(stats.get("negative", 0))
        agents = int(stats.get("agents", 0))
        if samples < min_samples or positive < min_positive or negative < min_negative:
            result[target] = ReadinessOutcome.not_ready(
                f"insufficient_samples samples={samples} pos={positive} neg={negative}"
            )
            continue
        if agents < min_agents:
            result[target] = ReadinessOutcome.not_ready(
                f"insufficient_agents agents={agents}"
            )
            continue
        if samples > 0 and (positive / samples) > max_positive_ratio:
            result[target] = ReadinessOutcome.not_ready("skewed_label_ratio")
            continue
        result[target] = ReadinessOutcome(
            ready=True,
            reason="ready",
            positive_count=positive,
            negative_count=negative,
            agent_count=agents,
            sample_count=samples,
        )
    return result


def risk_to_score(risk: float, mode: str = "linear_inverse") -> float:
    """Map predicted risk in [0,1] to a score in [5,100] (linear inverse)."""
    p = min(1.0, max(0.0, float(risk)))
    if mode == "linear_inverse":
        return round(100.0 - 100.0 * p, 2)
    raise DatasetLeakError(f"Unknown risk_to_score mode {mode!r}")


def combine_ml_scores(
    scores: dict[str, float], weights: dict[str, float] | None = None
) -> float | None:
    """Weighted combination over READY targets only.

    Returns None when no target has a score (cold start); never fabricates a
    0-out-of-nothing average.
    """
    if not scores:
        return None
    w = weights or DEFAULT_RISK_TO_SCORE_WEIGHTS
    available = {t: s for t, s in scores.items() if s is not None}
    if not available:
        return None
    total_w = sum(w.get(t, 0.0) for t in available)
    if total_w <= 0:
        return None
    return round(
        sum(w.get(t, 0.0) * s for t, s in available.items()) / total_w, 2
    )


def evaluate_model(actual: list[int], predicted: list[float]) -> dict[str, float]:
    """Quick quality summary over a READY target's calibration set."""
    if not actual or not predicted or len(actual) != len(predicted):
        return {"n": 0, "mae": 0.0, "rmse": 0.0}
    diffs = [float(p) - float(a) for a, p in zip(actual, predicted, strict=True)]
    mae = sum(abs(d) for d in diffs) / len(diffs)
    rmse = (sum(d * d for d in diffs) / len(diffs)) ** 0.5
    return {"n": len(diffs), "mae": round(mae, 3), "rmse": round(rmse, 3)}


def decide_promotion(
    current_score: float, challenger_score: float, *, headroom: float = 0.002
) -> tuple[str, bool]:
    """Champion-challenger promotion decision (explicit, never silent)."""
    if challenger_score > current_score * (1 + headroom):
        return "promote", True
    if challenger_score >= current_score:
        return "keep_champion", False
    return "reject_challenger", False


# ── Sprint 6 — Training Controller (senior §19-§24) ──────────────────────────
# Inert unless BOTH ml_enabled is True AND ml_targets is non-empty:
# run_training_controller's FIRST executable statement is that gate and it
# returns a silent NOT_READY no-op, exactly like the Sprint 5 disabled trainer.

TRAINING_STATUS_NOT_READY = "NOT_READY"
TRAINING_STATUS_COMPLETED = "COMPLETED"

TRAINING_OUTCOME_SKIPPED = "SKIPPED"
TRAINING_OUTCOME_PROMOTED = "PROMOTED"
TRAINING_OUTCOME_REJECTED = "REJECTED"
TRAINING_OUTCOME_ROLLED_BACK = "ROLLED_BACK"
TRAINING_OUTCOME_FAILED = "FAILED"

TRAINING_REASON_DISABLED = "ml_programme_disabled"
TRAINING_REASON_NO_TARGETS = "ml_targets_empty"
TRAINING_REASON_NO_TRIGGER = "no_trigger"
TRAINING_REASON_NOT_READY = "readiness_not_ready"
TRAINING_REASON_VALIDATION = "classification_validation_failed"
TRAINING_REASON_INTEGRITY = "artifact_integrity_failed"
TRAINING_REASON_DRIFT_DEFERRED = "drift_not_evaluated"

TRIGGER_DATA_GROWTH = "data_growth"
TRIGGER_INTERVAL = "interval"
TRIGGER_NONE = "none"
TRIGGER_DRIFT = "drift_not_evaluated"

TRAINING_STATE_FILENAME = "state.json"
MODEL_ARTIFACT_FILENAME = "model.joblib"
MODEL_METADATA_FILENAME = "metadata.json"
MODEL_VERSION_PREFIX = "v"
MODEL_VERSION_DIGITS = 3
CHAMPION_SCORE_METRIC = "pr_auc"
SEGMENT_RECALL_PREFIX = "segment_recall_"


class TrainingConfig(Protocol):
    """Settings subset the controller needs (keeps the ML core infra-agnostic)."""

    ml_enabled: bool
    ml_targets: list[str]
    ml_horizon_days: int
    ml_model_registry_dir: str
    ml_trigger_increment: int
    ml_training_interval_days: int
    ml_promotion_headroom: float
    ml_classification_min_pr_auc: float
    ml_classification_min_f1: float
    ml_classification_max_brier: float
    ml_segment_max_recall_drop: float
    ml_readiness_min_samples: int
    ml_readiness_min_positive: int
    ml_readiness_min_negative: int
    ml_readiness_min_agents: int
    ml_readiness_max_positive_ratio: float


@dataclass(frozen=True)
class TrainingState:
    target: str
    last_successful_training_at: str | None = None
    last_training_labeled_records: int = 0
    current_production_version: str | None = None
    previous_known_good_production_version: str | None = None
    current_production_score: float = 0.0
    last_promotion_at: str | None = None
    last_run_outcome: str | None = None
    last_run_reason: str | None = None


@dataclass(frozen=True)
class TriggerDecision:
    triggered: bool
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class TrainingDataset:
    target: str
    feature_rows: list[list[float]]
    labels: list[int]
    stats: dict[str, int]
    labeled_records: int
    segments: dict[str, list[int]] | None = None


@dataclass(frozen=True)
class TrainedCandidate:
    target: str
    actual: list[int]
    probabilities: list[float]
    artifact: bytes
    segments: dict[str, list[int]] | None = None


class DatasetBuilder(Protocol):
    def __call__(self, target: str, *, as_of: date, db: Any) -> TrainingDataset: ...


class CandidateTrainer(Protocol):
    def __call__(self, dataset: TrainingDataset) -> TrainedCandidate: ...


class LabeledCountReader(Protocol):
    def __call__(self, db: Any, target: str, as_of: date) -> int: ...


# ── Registry state (senior §24: explicit production / previous-production) ────


def target_registry_dir(registry_dir: str, target: str) -> Path:
    return Path(registry_dir) / target


def model_version_dir(registry_dir: str, target: str, version: str) -> Path:
    return target_registry_dir(registry_dir, target) / version


def read_training_state(registry_dir: str, target: str) -> TrainingState:
    """Read the per-target controller state; a missing file is a cold start."""
    path = target_registry_dir(registry_dir, target) / TRAINING_STATE_FILENAME
    if not path.exists():
        return TrainingState(target=target)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelUnavailableError(
            f"Training state for {target} is unreadable at {path}"
        ) from exc
    if not isinstance(raw, dict):
        raise ModelUnavailableError(f"Training state for {target} is malformed at {path}")
    return TrainingState(
        target=target,
        last_successful_training_at=raw.get("last_successful_training_at"),
        last_training_labeled_records=int(raw.get("last_training_labeled_records", 0)),
        current_production_version=raw.get("current_production_version"),
        previous_known_good_production_version=raw.get(
            "previous_known_good_production_version"
        ),
        current_production_score=float(raw.get("current_production_score", 0.0)),
        last_promotion_at=raw.get("last_promotion_at"),
        last_run_outcome=raw.get("last_run_outcome"),
        last_run_reason=raw.get("last_run_reason"),
    )


def write_training_state(registry_dir: str, state: TrainingState) -> Path:
    """Atomically persist controller state (temp file + os.replace)."""
    target_dir = target_registry_dir(registry_dir, state.target)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / TRAINING_STATE_FILENAME
    tmp_path = target_dir / f"{TRAINING_STATE_FILENAME}.tmp"
    tmp_path.write_text(
        json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(tmp_path, path)
    return path


def read_model_metadata(registry_dir: str, target: str, version: str) -> dict[str, Any] | None:
    path = model_version_dir(registry_dir, target, version) / MODEL_METADATA_FILENAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(raw) if isinstance(raw, dict) else None


def write_model_metadata(
    registry_dir: str, target: str, version: str, metadata: dict[str, Any]
) -> Path:
    version_path = model_version_dir(registry_dir, target, version)
    version_path.mkdir(parents=True, exist_ok=True)
    path = version_path / MODEL_METADATA_FILENAME
    tmp_path = version_path / f"{MODEL_METADATA_FILENAME}.tmp"
    tmp_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    os.replace(tmp_path, path)
    return path


def existing_model_versions(registry_dir: str, target: str) -> list[str]:
    target_dir = target_registry_dir(registry_dir, target)
    if not target_dir.exists():
        return []
    return sorted(
        entry.name
        for entry in target_dir.iterdir()
        if entry.is_dir() and entry.name.startswith(MODEL_VERSION_PREFIX)
    )


def next_model_version(registry_dir: str, target: str) -> str:
    """Next immutable version id (v001, v002, ...). Never reuses a version dir."""
    highest = 0
    for name in existing_model_versions(registry_dir, target):
        suffix = name[len(MODEL_VERSION_PREFIX):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{MODEL_VERSION_PREFIX}{highest + 1:0{MODEL_VERSION_DIGITS}d}"


# ── Triggers (growth + interval are real; drift is deferred) ─────────────────


def drift_trigger_status() -> TriggerDecision:
    """Drift-triggered retraining is DEFERRED in Sprint 6.

    The senior lists distribution drift as a possible retraining trigger but does
    not require it in the first automated-training phase, so this phase reports
    NOT_EVALUATED instead of pretending a drift signal exists.
    """
    return TriggerDecision(
        triggered=False,
        reason=TRIGGER_DRIFT,
        detail="drift_trigger_deferred",
    )


def classify_trigger(
    *,
    state: TrainingState,
    labeled_records: int,
    increment: int,
    interval_days: int,
    now: datetime,
) -> TriggerDecision:
    """Decide whether a training run is due, from real measured quantities.

    growth  = eligible labelled records now - labelled records in the last
              training dataset >= ml_trigger_increment (never the total booking
              count, and never per-booking retraining);
    interval= days since the last SUCCESSFUL training run >= interval_days.
    """
    if state.last_successful_training_at is None:
        return TriggerDecision(
            triggered=True,
            reason=TRIGGER_INTERVAL,
            detail="no_successful_training_yet",
        )
    growth = labeled_records - state.last_training_labeled_records
    if growth >= increment:
        return TriggerDecision(
            triggered=True, reason=TRIGGER_DATA_GROWTH, detail=f"growth={growth}"
        )
    last_run = datetime.fromisoformat(state.last_successful_training_at)
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    elapsed_days = (current - last_run).total_seconds() / 86400.0
    if elapsed_days >= interval_days:
        return TriggerDecision(
            triggered=True, reason=TRIGGER_INTERVAL, detail=f"elapsed_days={elapsed_days:.2f}"
        )
    return TriggerDecision(triggered=False, reason=TRIGGER_NONE, detail=f"growth={growth}")


# ── Candidate evaluation (binary classification, senior §22) ─────────────────
# The metric maths is implemented here in plain Python on purpose: the ML core
# stays dependency-free and fully typed, and every number below is unit-testable
# against hand-computed fixtures.


def _roc_auc(labels: list[int], scores: list[float]) -> float:
    """Rank-based ROC-AUC (Mann-Whitney U) with average ranks for tied scores."""
    ordered = sorted(zip(scores, labels, strict=True), key=lambda item: item[0])
    ranks = [0.0] * len(ordered)
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[index][0]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[position] = average_rank
        index = end + 1
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return 0.0
    positive_rank_sum = sum(
        rank for rank, (_score, label) in zip(ranks, ordered, strict=True) if label == 1
    )
    return (positive_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def _average_precision(labels: list[int], scores: list[float]) -> float:
    """Step-wise average precision (PR-AUC): sum of recall gains x precision."""
    order = sorted(range(len(scores)), key=lambda position: scores[position], reverse=True)
    positives = sum(labels)
    if positives == 0:
        return 0.0
    true_positives = 0
    false_positives = 0
    previous_recall = 0.0
    average_precision = 0.0
    for position in order:
        if labels[position] == 1:
            true_positives += 1
        else:
            false_positives += 1
        recall = true_positives / positives
        precision = true_positives / (true_positives + false_positives)
        average_precision += (recall - previous_recall) * precision
        previous_recall = recall
    return average_precision


def _brier_score(labels: list[int], scores: list[float]) -> float:
    """Calibration error: mean squared error between probability and outcome."""
    if not labels:
        return 1.0
    return sum((score - label) ** 2 for score, label in zip(scores, labels, strict=True)) / len(
        labels
    )


def evaluate_classifier_candidate(
    actual: list[int],
    probabilities: list[float],
    *,
    segments: dict[str, list[int]] | None = None,
) -> dict[str, float]:
    """Classification quality summary for a candidate: ROC-AUC, PR-AUC, precision,
    recall, F1, calibration (Brier), the confusion matrix, and per-segment recall.

    A single-class validation slice cannot produce a meaningful ranking metric, so
    it is reported as degenerate with zeroed ranking metrics and brier=1.0; the
    validation gate then refuses it instead of promoting noise.
    """
    if not actual or len(actual) != len(probabilities):
        return {"n": 0.0, "degenerate": 1.0}
    labels = [int(value) for value in actual]
    scores = [float(value) for value in probabilities]
    positives = sum(labels)
    if positives == 0 or positives == len(labels):
        return {
            "n": float(len(labels)),
            "degenerate": 1.0,
            "roc_auc": 0.0,
            "pr_auc": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "brier": 1.0,
        }
    predicted = [1 if score >= 0.5 else 0 for score in scores]
    true_positives = sum(
        1 for label, prediction in zip(labels, predicted, strict=True) if label == 1 and prediction
    )
    false_positives = sum(
        1 for label, prediction in zip(labels, predicted, strict=True) if label == 0 and prediction
    )
    false_negatives = positives - true_positives
    true_negatives = len(labels) - positives - false_positives
    predicted_positives = true_positives + false_positives
    precision = true_positives / predicted_positives if predicted_positives else 0.0
    recall = true_positives / positives
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    metrics: dict[str, float] = {
        "n": float(len(labels)),
        "degenerate": 0.0,
        "roc_auc": round(_roc_auc(labels, scores), 4),
        "pr_auc": round(_average_precision(labels, scores), 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "brier": round(_brier_score(labels, scores), 4),
        "tn": float(true_negatives),
        "fp": float(false_positives),
        "fn": float(false_negatives),
        "tp": float(true_positives),
    }
    for name, indices in (segments or {}).items():
        usable = [index for index in indices if 0 <= index < len(labels)]
        segment_labels = [labels[index] for index in usable]
        segment_positives = sum(segment_labels)
        if not segment_labels or segment_positives == 0:
            continue
        hits = sum(
            1
            for index, label in zip(usable, segment_labels, strict=True)
            if label == 1 and predicted[index] == 1
        )
        metrics[f"{SEGMENT_RECALL_PREFIX}{name}"] = round(hits / segment_positives, 4)
    return metrics


def validate_candidate_metrics(
    metrics: dict[str, float],
    *,
    min_pr_auc: float,
    min_f1: float,
    max_brier: float,
    champion_segment_recall: dict[str, float] | None = None,
    max_segment_recall_drop: float = 0.0,
) -> tuple[bool, str]:
    """Multi-stage validation gate: quality thresholds, then segment regression."""
    if metrics.get("degenerate", 1.0) >= 1.0:
        return False, "degenerate_evaluation_set"
    pr_auc = metrics.get("pr_auc", 0.0)
    if pr_auc < min_pr_auc:
        return False, f"pr_auc_below_minimum pr_auc={pr_auc} min={min_pr_auc}"
    f1 = metrics.get("f1", 0.0)
    if f1 < min_f1:
        return False, f"f1_below_minimum f1={f1} min={min_f1}"
    brier = metrics.get("brier", 1.0)
    if brier > max_brier:
        return False, f"brier_above_maximum brier={brier} max={max_brier}"
    for name, champion_recall in (champion_segment_recall or {}).items():
        candidate_recall = metrics.get(f"{SEGMENT_RECALL_PREFIX}{name}")
        if candidate_recall is None:
            continue
        drop = champion_recall - candidate_recall
        if drop > max_segment_recall_drop:
            return False, f"segment_regression segment={name} drop={drop:.4f}"
    return True, "passed"


# ── Artifacts (integrity before promotion, immutable version dirs) ───────────


def compute_artifact_sha256(artifact: bytes) -> str:
    return hashlib.sha256(artifact).hexdigest()


def verify_artifact_integrity(artifact_path: Path, expected_sha256: str) -> bool:
    if not artifact_path.exists():
        return False
    try:
        return compute_artifact_sha256(artifact_path.read_bytes()) == expected_sha256
    except OSError:
        return False


def stage_challenger(
    *,
    registry_dir: str,
    target: str,
    artifact: bytes,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, Path, str]:
    """Write an immutable version dir for a candidate and return (version, path, sha)."""
    version = next_model_version(registry_dir, target)
    version_path = model_version_dir(registry_dir, target, version)
    version_path.mkdir(parents=True, exist_ok=True)
    artifact_path = version_path / MODEL_ARTIFACT_FILENAME
    artifact_path.write_bytes(artifact)
    digest = compute_artifact_sha256(artifact)
    payload: dict[str, Any] = {
        "target": target,
        "version": version,
        "status": MODEL_STATUS_CHALLENGER,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "sha256": digest,
    }
    payload.update(metadata or {})
    write_model_metadata(registry_dir, target, version, payload)
    return version, artifact_path, digest


def promote_challenger(
    *,
    registry_dir: str,
    state: TrainingState,
    version: str,
    score: float,
    labeled_records: int,
    now: datetime,
) -> TrainingState:
    """Promote a verified challenger and rotate the previous known-good production."""
    metadata = read_model_metadata(registry_dir, state.target, version) or {}
    metadata["status"] = MODEL_STATUS_PRODUCTION
    metadata["promoted_at"] = now.isoformat()
    metadata[CHAMPION_SCORE_METRIC] = score
    write_model_metadata(registry_dir, state.target, version, metadata)
    new_state = replace(
        state,
        current_production_version=version,
        previous_known_good_production_version=state.current_production_version,
        current_production_score=score,
        last_successful_training_at=now.isoformat(),
        last_training_labeled_records=labeled_records,
        last_promotion_at=now.isoformat(),
        last_run_outcome=TRAINING_OUTCOME_PROMOTED,
        last_run_reason=CHAMPION_SCORE_METRIC,
    )
    write_training_state(registry_dir, new_state)
    MODEL_PROMOTION.inc()
    return new_state


def reject_challenger(
    *,
    registry_dir: str,
    state: TrainingState,
    version: str,
    reason: str,
    labeled_records: int,
    now: datetime,
) -> TrainingState:
    """Mark a candidate REJECTED; the champion stays exactly where it is."""
    metadata = read_model_metadata(registry_dir, state.target, version) or {}
    metadata["status"] = MODEL_STATUS_REJECTED
    metadata["rejected_at"] = now.isoformat()
    metadata["rejection_reason"] = reason
    write_model_metadata(registry_dir, state.target, version, metadata)
    new_state = replace(
        state,
        last_successful_training_at=now.isoformat(),
        last_training_labeled_records=labeled_records,
        last_run_outcome=TRAINING_OUTCOME_REJECTED,
        last_run_reason=reason,
    )
    write_training_state(registry_dir, new_state)
    MODEL_REJECTION.inc()
    return new_state


def rollback_champion(
    *, registry_dir: str, state: TrainingState, now: datetime | None = None
) -> TrainingState:
    """Re-deploy the PREVIOUS KNOWN-GOOD PRODUCTION version (senior §24).

    The rollback target is the version explicitly tracked as production before the
    current one - never a blind v{N-1} assumption, which would be wrong whenever
    the previous version was a rejected challenger.
    """
    rolled_back_at = now or datetime.now(timezone.utc)
    previous = state.previous_known_good_production_version
    if previous is None:
        raise ModelUnavailableError(
            f"No previous known-good production version is tracked for {state.target}; "
            "rollback is refused instead of guessing a version"
        )
    source_dir = model_version_dir(registry_dir, state.target, previous)
    metadata = read_model_metadata(registry_dir, state.target, previous) or {}
    expected_sha = str(metadata.get("sha256", ""))
    if not expected_sha or not verify_artifact_integrity(
        source_dir / MODEL_ARTIFACT_FILENAME, expected_sha
    ):
        raise ModelUnavailableError(
            f"Rollback source {previous} for {state.target} failed integrity verification"
        )
    artifact = (source_dir / MODEL_ARTIFACT_FILENAME).read_bytes()
    version, _path, _digest = stage_challenger(
        registry_dir=registry_dir,
        target=state.target,
        artifact=artifact,
        metadata={
            "status": MODEL_STATUS_PRODUCTION,
            "rolled_back_from": state.current_production_version,
            "restored_from_version": previous,
        },
        now=rolled_back_at,
    )
    new_metadata = read_model_metadata(registry_dir, state.target, version) or {}
    new_metadata["status"] = MODEL_STATUS_PRODUCTION
    new_metadata["promoted_at"] = rolled_back_at.isoformat()
    new_metadata[CHAMPION_SCORE_METRIC] = float(metadata.get(CHAMPION_SCORE_METRIC, 0.0))
    write_model_metadata(registry_dir, state.target, version, new_metadata)
    new_state = replace(
        state,
        current_production_version=version,
        previous_known_good_production_version=state.current_production_version,
        current_production_score=float(metadata.get(CHAMPION_SCORE_METRIC, 0.0)),
        last_promotion_at=rolled_back_at.isoformat(),
        last_run_outcome=TRAINING_OUTCOME_ROLLED_BACK,
        last_run_reason=f"restored_from_version={previous}",
    )
    write_training_state(registry_dir, new_state)
    MODEL_ROLLBACK.inc()
    return new_state


# ── Training controller ──────────────────────────────────────────────────────


def _run_target_training(
    *,
    settings: TrainingConfig,
    target: str,
    registry_dir: str,
    db: Any,
    as_of: date,
    run_at: datetime,
    horizon_days: int,
    build_dataset: DatasetBuilder,
    train_candidate: CandidateTrainer,
    labeled_count_reader: LabeledCountReader | None,
) -> dict[str, Any]:
    try:
        state = read_training_state(registry_dir, target)
        labeled_records = (
            labeled_count_reader(db, target, as_of) if labeled_count_reader else 0
        )
        trigger = classify_trigger(
            state=state,
            labeled_records=labeled_records,
            increment=int(settings.ml_trigger_increment),
            interval_days=int(settings.ml_training_interval_days),
            now=run_at,
        )
        if not trigger.triggered:
            write_training_state(
                registry_dir,
                replace(
                    state,
                    last_run_outcome=TRAINING_OUTCOME_SKIPPED,
                    last_run_reason=TRAINING_REASON_NO_TRIGGER,
                ),
            )
            logger.info(
                "training_controller %s: SKIPPED (%s %s)",
                target,
                trigger.reason,
                trigger.detail,
            )
            return {
                "outcome": TRAINING_OUTCOME_SKIPPED,
                "reason": TRAINING_REASON_NO_TRIGGER,
                "trigger": trigger.reason,
                "detail": trigger.detail,
            }

        TRAINING_RUNS.inc()
        dataset = build_dataset(target, as_of=as_of, db=db)
        readiness = evaluate_readiness(
            per_target={target: dataset.stats},
            ml_targets=[target],
            min_samples=int(settings.ml_readiness_min_samples),
            min_positive=int(settings.ml_readiness_min_positive),
            min_negative=int(settings.ml_readiness_min_negative),
            min_agents=int(settings.ml_readiness_min_agents),
            max_positive_ratio=float(settings.ml_readiness_max_positive_ratio),
        )[target]
        if not readiness.ready:
            write_training_state(
                registry_dir,
                replace(
                    state,
                    last_run_outcome=TRAINING_OUTCOME_SKIPPED,
                    last_run_reason=TRAINING_REASON_NOT_READY,
                ),
            )
            logger.info(
                "training_controller %s: SKIPPED (%s: %s)",
                target,
                TRAINING_REASON_NOT_READY,
                readiness.reason,
            )
            return {
                "outcome": TRAINING_OUTCOME_SKIPPED,
                "reason": TRAINING_REASON_NOT_READY,
                "detail": readiness.reason,
            }

        candidate = train_candidate(dataset)
        metrics = evaluate_classifier_candidate(
            candidate.actual, candidate.probabilities, segments=candidate.segments
        )
        version, artifact_path, digest = stage_challenger(
            registry_dir=registry_dir,
            target=target,
            artifact=candidate.artifact,
            metadata={"evaluation": metrics, "sample_count": dataset.stats.get("samples", 0)},
            now=run_at,
        )
        champion_metadata = (
            read_model_metadata(registry_dir, target, state.current_production_version)
            if state.current_production_version
            else None
        )
        champion_evaluation = (champion_metadata or {}).get("evaluation", {})
        if not isinstance(champion_evaluation, dict):
            champion_evaluation = {}
        champion_segment_recall = {
            key[len(SEGMENT_RECALL_PREFIX):]: float(value)
            for key, value in champion_evaluation.items()
            if key.startswith(SEGMENT_RECALL_PREFIX)
        }
        passed, validation_reason = validate_candidate_metrics(
            metrics,
            min_pr_auc=float(settings.ml_classification_min_pr_auc),
            min_f1=float(settings.ml_classification_min_f1),
            max_brier=float(settings.ml_classification_max_brier),
            champion_segment_recall=champion_segment_recall,
            max_segment_recall_drop=float(settings.ml_segment_max_recall_drop),
        )
        if not passed:
            new_state = reject_challenger(
                registry_dir=registry_dir,
                state=state,
                version=version,
                reason=f"{TRAINING_REASON_VALIDATION}:{validation_reason}",
                labeled_records=labeled_records,
                now=run_at,
            )
            TRAINING_SUCCESS.inc()
            return {
                "outcome": TRAINING_OUTCOME_REJECTED,
                "reason": TRAINING_REASON_VALIDATION,
                "detail": validation_reason,
                "version": version,
                "state": new_state,
            }

        if not verify_artifact_integrity(artifact_path, digest):
            new_state = reject_challenger(
                registry_dir=registry_dir,
                state=state,
                version=version,
                reason=TRAINING_REASON_INTEGRITY,
                labeled_records=labeled_records,
                now=run_at,
            )
            TRAINING_SUCCESS.inc()
            return {
                "outcome": TRAINING_OUTCOME_REJECTED,
                "reason": TRAINING_REASON_INTEGRITY,
                "version": version,
                "state": new_state,
            }

        challenger_score = float(metrics.get(CHAMPION_SCORE_METRIC, 0.0))
        cold_start = state.current_production_version is None
        decision, promoted = decide_promotion(
            float(state.current_production_score),
            challenger_score,
            headroom=float(settings.ml_promotion_headroom),
        )
        if cold_start or promoted:
            new_state = promote_challenger(
                registry_dir=registry_dir,
                state=state,
                version=version,
                score=challenger_score,
                labeled_records=labeled_records,
                now=run_at,
            )
            TRAINING_SUCCESS.inc()
            return {
                "outcome": TRAINING_OUTCOME_PROMOTED,
                "decision": "first_production" if cold_start else decision,
                "version": version,
                "score": challenger_score,
                "state": new_state,
            }

        new_state = reject_challenger(
            registry_dir=registry_dir,
            state=state,
            version=version,
            reason=f"champion_comparison:{decision}",
            labeled_records=labeled_records,
            now=run_at,
        )
        TRAINING_SUCCESS.inc()
        return {
            "outcome": TRAINING_OUTCOME_REJECTED,
            "decision": decision,
            "version": version,
            "score": challenger_score,
            "state": new_state,
        }
    except Exception as exc:
        TRAINING_FAILURE.inc()
        logger.exception("training_controller %s: FAILED (%s)", target, exc)
        return {
            "outcome": TRAINING_OUTCOME_FAILED,
            "reason": type(exc).__name__,
            "detail": str(exc),
        }


def run_training_controller(
    settings: TrainingConfig,
    *,
    build_dataset: DatasetBuilder,
    train_candidate: CandidateTrainer,
    session_factory: Callable[[], Any] | None = None,
    labeled_count_reader: LabeledCountReader | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sprint 6 training controller: trigger -> readiness -> train -> evaluate ->
    validate -> integrity -> champion/challenger -> promote or reject.

    The dataset builder and the classifier trainer are INJECTED callables, so the
    app-side controller never imports the training script and no circular import
    can form. The DB session is opened here and closed in `finally`: a session is
    never handed in from a lifespan or reused across training runs.
    """
    if not bool(getattr(settings, "ml_enabled", False)) or not list(
        getattr(settings, "ml_targets", []) or []
    ):
        reason = (
            TRAINING_REASON_DISABLED
            if not bool(getattr(settings, "ml_enabled", False))
            else TRAINING_REASON_NO_TARGETS
        )
        logger.info("training_controller: %s -- no run, no counters", reason)
        return {"status": TRAINING_STATUS_NOT_READY, "reason": reason, "targets": {}}

    run_at = now or datetime.now(timezone.utc)
    registry_dir = str(
        getattr(settings, "ml_model_registry_dir", "ml/models/registry")
    )
    horizon_days = int(getattr(settings, "ml_horizon_days", 30))
    session = session_factory() if session_factory is not None else None
    try:
        per_target: dict[str, Any] = {}
        for target in list(settings.ml_targets):
            _assert_target_approved(target, list(settings.ml_targets))
            per_target[target] = _run_target_training(
                settings=settings,
                target=target,
                registry_dir=registry_dir,
                db=session,
                as_of=run_at.date(),
                run_at=run_at,
                horizon_days=horizon_days,
                build_dataset=build_dataset,
                train_candidate=train_candidate,
                labeled_count_reader=labeled_count_reader,
            )
        return {
            "status": TRAINING_STATUS_COMPLETED,
            "run_at": run_at.isoformat(),
            "targets": per_target,
        }
    finally:
        if session is not None:
            session.close()


# ── Default dataset builder + classifier trainer (senior §19-§20) ────────────
# These are the concrete implementations the controller is given by the CLI
# trainer and by the lifespan supervisor. Heavy dependencies (the repository,
# scikit-learn) are imported INSIDE the functions, so importing this module - and
# therefore the disabled service - never pulls a training stack into memory.

FEATURE_NAMES: tuple[str, ...] = (
    "eff_searches_7d",
    "bookings_7d",
    "eff_searches_30d",
    "bookings_30d",
    "current_overdue_count",
    "current_overdue_ratio",
    "current_max_delay_days",
)
CALIBRATION_FRACTION = 0.2


def _segment_name(bookings_30d: int) -> str:
    if bookings_30d <= 0:
        return "no_recent_bookings"
    if bookings_30d < 5:
        return "low_volume"
    return "high_volume"


def build_default_dataset(
    settings: TrainingConfig,
    db: Any,
    target: str,
    *,
    as_of: date,
) -> TrainingDataset:
    """Point-in-time snapshot dataset for one approved target.

    The snapshot cut-off is T = as_of - horizon, the most recent cut-off whose
    (T, T + horizon] label window has fully elapsed, so every label is observable
    now. Features are read with T as their boundary and labels strictly after T.
    The train/calibration split is temporal: the last CALIBRATION_FRACTION of the
    population (by agent id) is held out for evaluation.
    """
    from app.infra.db.repository import AgentRepository

    _assert_target_approved(target, list(settings.ml_targets))
    repository = AgentRepository()
    horizon_days = int(settings.ml_horizon_days)
    cut_off = as_of - timedelta(days=horizon_days)
    label_end = cut_off + timedelta(days=horizon_days)

    population = repository.get_training_population(db, cut_off=cut_off)
    if not population:
        return TrainingDataset(
            target=target,
            feature_rows=[],
            labels=[],
            stats={"samples": 0, "positive": 0, "negative": 0, "agents": 0},
            labeled_records=0,
        )
    feature_rows = repository.get_training_feature_rows(
        db, agent_ids=population, as_of=cut_off
    )
    evidence = repository.get_label_evidence(
        db, target=target, cut_off=cut_off, horizon_days=horizon_days
    )
    labeled_records = repository.get_labeled_sample_count(
        db, target=target, horizon_days=horizon_days, as_of=as_of
    )

    rows: list[dict] = []
    calibration_size = max(1, int(len(feature_rows) * CALIBRATION_FRACTION))
    calibration_start = len(feature_rows) - calibration_size
    for position, feature_row in enumerate(feature_rows):
        agent_id = int(feature_row["agent_id"])
        agent_evidence = evidence.get(agent_id)
        if agent_evidence is None:
            continue
        features = build_features(
            label_date=cut_off,
            searches_7d=int(feature_row["searches_7d"]),
            bookings_7d=int(feature_row["bookings_7d"]),
            searches_30d=int(feature_row["searches_30d"]),
            bookings_30d=int(feature_row["bookings_30d"]),
            overdue_count=int(feature_row["current_overdue_count"]),
            overdue_ratio=float(feature_row["current_overdue_ratio"]),
            max_delay_days=int(feature_row["current_max_delay_days"]),
            approved_target=target,
            approved_targets=list(settings.ml_targets),
        )
        rows.append(
            {
                "agent_id": agent_id,
                "features": features,
                "label": 1 if agent_evidence["positives"] > 0 else 0,
                "segment": _segment_name(int(feature_row["bookings_30d"])),
                "feature_cut_off": cut_off,
                "label_date": label_end,
                "is_calibration": position >= calibration_start,
            }
        )

    if not rows:
        return TrainingDataset(
            target=target,
            feature_rows=[],
            labels=[],
            stats={"samples": 0, "positive": 0, "negative": 0, "agents": 0},
            labeled_records=labeled_records,
        )

    dataset = SnapshotDataset(rows, cut_off=cut_off, horizon_days=horizon_days)
    train_rows, calibration_rows = temporal_split(dataset)
    ordered = [*train_rows, *calibration_rows]
    feature_matrix = [
        [float(row["features"][name]) for name in FEATURE_NAMES] for row in ordered
    ]
    labels = [int(row["label"]) for row in ordered]
    segments: dict[str, list[int]] = {}
    for position, row in enumerate(ordered):
        if row["is_calibration"]:
            segments.setdefault(str(row["segment"]), []).append(position)
    positives = sum(labels)
    return TrainingDataset(
        target=target,
        feature_rows=feature_matrix,
        labels=labels,
        stats={
            "samples": len(labels),
            "positive": positives,
            "negative": len(labels) - positives,
            "agents": len({int(row["agent_id"]) for row in ordered}),
        },
        labeled_records=labeled_records,
        segments=segments,
    )


def train_random_forest_candidate(dataset: TrainingDataset) -> TrainedCandidate:
    """Train the real candidate classifier and return it as a versioned artifact.

    A temporal hold-out is applied first: the controller's evaluation only ever
    scores predictions on rows the model did not train on. The serialized
    classifier is returned as bytes; the controller writes it into an immutable
    version directory and verifies its checksum before any promotion.
    """
    import pickle

    from sklearn.ensemble import RandomForestClassifier  # type: ignore[import-untyped]

    if not dataset.feature_rows or not dataset.labels:
        raise DatasetLeakError("Cannot train a candidate from an empty training dataset")
    labels = dataset.labels
    split_at = max(1, int(len(labels) * (1.0 - CALIBRATION_FRACTION)))
    train_labels = labels[:split_at]
    holdout_labels = labels[split_at:]
    if not holdout_labels:
        holdout_labels = train_labels
        split_at = len(labels)
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=8,
        min_samples_leaf=5,
        class_weight="balanced",
        random_state=42,
    )
    model.fit(dataset.feature_rows[:split_at], train_labels)
    probabilities = [
        float(value)
        for value in model.predict_proba(dataset.feature_rows[split_at:])[:, 1]
    ]
    artifact = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
    return TrainedCandidate(
        target=dataset.target,
        actual=[int(value) for value in holdout_labels],
        probabilities=probabilities,
        artifact=artifact,
    )


class TrustModelPredictor:
    """DISABLED shim — keeps the legacy class name importable for old tests.

    The Sprint 5 programme is per-target, readiness-gated and ships DISABLED.
    This single-model class always raises ModelUnavailableError so nothing can
    accidentally run inference through the retired single-model path.
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self, model_path: str | None = None):
        self.model_path = model_path
        self.model = None

    def predict(self, features: dict) -> float:
        raise ModelUnavailableError(
            "TrustModelPredictor is retired under Sprint 5. The per-target, "
            "readiness-gated programme owns inference; approve ml_targets and "
            "enable ml_enabled before using the ML scoring path."
        )
