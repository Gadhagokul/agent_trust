# ML Promotion Pipeline — Documented Gate

The promotion pipeline below is **already implemented** and ships **disabled**
(`ml_enabled = false`, `ml_targets = []`). This page documents the existing
code so the gate is explicit and auditable; it is not a request for new code.

## The gate (no "just because it's better" promotion)

A challenger is promoted only if it passes **every** stage. A candidate that
fails any stage is recorded as `REJECTED` (auditable, never loaded).

| # | Stage | Implementation | Where |
|---|-------|----------------|-------|
| 1 | **Data validation / readiness** | Per-target readiness gate before any training | `evaluate_readiness` — `app/ml/trust_model.py:183` |
| 2 | **Temporal validation** | Chronological train/calibration split; labels always in `(T, T+horizon]`, features at/before `T` | `temporal_split` — `trust_model.py:170`; `build_default_dataset` — `trust_model.py:1142`; `CALIBRATION_FRACTION = 0.2` — `trust_model.py:1131`; train split — `trust_model.py:1262-1267` |
| 3 | **Minimum performance thresholds** | PR-AUC, F1, Brier, confusion matrix, per-segment recall on the temporal hold-out | `evaluate_classifier_candidate` — `trust_model.py:597`; `validate_candidate_metrics` — `trust_model.py:668` |
| 4 | **Champion comparison** | Challenger must beat the champion by the configured headroom, not merely "be better" | `decide_promotion` — `trust_model.py:265`; headroom applied at `trust_model.py:1010` (`ml_promotion_headroom`) |
| 5 | **Segment safety checks** | Reject when any segment's recall drops more than allowed vs the champion (e.g. new-agent recall drop) | `ml_segment_max_recall_drop` vs `champion_segment_recall` — `trust_model.py:689-694`, `957-968` |
| 6 | **Artifact integrity** | SHA-256 digest computed at staging and verified on load; promote only after re-verification | `compute_artifact_sha256` — `trust_model.py:702`; `verify_artifact_integrity` — `trust_model.py:706`; staged digest `trust_model.py:729`; pre-promotion re-check `trust_model.py:988`; load-time verify `trust_model.py:819` |
| 7 | **Promotion / rollback** | Registry rotation to `PRODUCTION`, explicit previous-good version, rollback never guesses `v{N-1}` | `stage_challenger` — `trust_model.py:715`; `promote_challenger` — `trust_model.py:742`; `reject_challenger` — `trust_model.py:773`; `rollback_champion` — `trust_model.py:800` |

## Gate thresholds (configurable project defaults)

| Setting | Default | Meaning |
|---------|---------|---------|
| `ml_classification_min_pr_auc` | 0.30 | Minimum PR-AUC on the temporal hold-out |
| `ml_classification_min_f1` | 0.40 | Minimum F1 |
| `ml_classification_max_brier` | 0.25 | Maximum Brier score (calibration) |
| `ml_segment_max_recall_drop` | 0.05 | Max per-segment recall drop vs champion |
| `ml_promotion_headroom` | 0.002 | Required margin over the champion |

## Controller & triggers

`run_training_controller` (`trust_model.py:1056`) drives per-target runs,
guarded by a Redis lock (`ml_training_lock_ttl_seconds`) so only one worker
trains at a time. A run only starts when a trigger fires (`classify_trigger` —
`trust_model.py:499`):

| Trigger | Rule |
|---------|------|
| `data_growth` | ≥ `ml_trigger_increment` new eligible labeled records since the last successful run |
| `interval` | `ml_training_interval_days` elapsed since the last success (first run always due) |
| `drift` | **Deferred** — reported `NOT_EVALUATED`, never a trigger (`drift_trigger_status` — `trust_model.py:485`) |

All ML behaviour is inert while `ml_enabled = false` / `ml_targets = []`; the
live score remains the pure rule composite `Final = round(Composite)`.