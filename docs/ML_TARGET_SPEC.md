# ML Target Specification — Proposed (NOT yet approved)

Status: **PROPOSED — pending business/data-science sign-off.**
Until approved, `ml_targets = []` and `ml_enabled = false`, so ML is always
`NOT_READY` and the published score is the pure rule composite
(`Final = round(Composite)`).

## Purpose & anti-circularity rule

The ML programme **does not predict the current Trust Score**. Doing so would
be circular: the score is computed from the same rule features, so a model
trained to "reproduce the Trust Score" only memorizes the rule engine.

Instead each target is a **binary classifier of a defined future outcome**:

```
features at T  →  outcome in (T, T + horizon]
```

- Features use only data at/before `T`.
- Labels use only data strictly after `T` within the horizon — the future
  outcome, **not** today's score.
- No leakage: the trust score and its rule-derived features are never used as a
  feature pointing at the label.

## Proposed targets (PENDING SIGN-OFF — not "approved")

| Proposed target id | ML prediction (binary) | Horizon proposal |
|--------------------|------------------------|------------------|
| `severe_default` | Probability the agent develops a severe payment/default event (a credit transaction ≥30 days past-due/unpaid, or a §27 high-risk condition) | T+30 (60/90 evaluated) |
| `severe_reliability` | Probability of a severe booking failure/cancellation event (≥50% of eligible attempts fail or cancel) | T+30 (60/90 evaluated) |
| `l2b_breach` | Probability the agent breaches a supplier's allowed L2B (`observed L2B > allowed L2B`) | T+30 |

The word "proposed" is deliberate: the exact target list, horizon, and combiner
weights are business/data-science decisions. The junior/data-science team must
not choose them arbitrarily (senior review §10). Sign-off happens **before** ML
activation, never after.

## Activation model — two switches, both required

1. `ml_targets = [...]` (business-approved target list + horizon,
   currently `[]` = nothing approved — valid, NOT_READY is the shipping state).
2. Per target: dataset readiness gate must pass, a `PRODUCTION` artifact must be
   registry-verified, and `ml_enabled = true`.

Until both hold the supervisor never starts, no dataset is built, no registry
write happens, and the live score stays `Final = round(Composite)`
(`app/infra/settings.py:112-139`, `app/infra/settings.py:124-127`).

## Per-target guardrails (all shipped, all inert while disabled)

- **Readiness gate** — `evaluate_readiness` (`app/ml/trust_model.py:183`): the
  target needs enough labeled positives/negatives and agents
  (`ml_readiness_min_samples/positive/negative/agents`, `max_positive_ratio`).
- **Dataset** — `build_default_dataset` (`trust_model.py:1142`):
  `T = as_of − horizon`; temporal hold-out split via `temporal_split`
  (`trust_model.py:170`) with `CALIBRATION_FRACTION = 0.2` (`trust_model.py:1131`).
- **No leakage validation** — features ≤ T / labels in `(T, T + horizon]`
  enforced at dataset construction.
- **Registry** — each target owns an immutable version registry with explicit
  status `PRODUCTION | CHALLENGER | REJECTED`; only a checksum-verified
  `PRODUCTION` artifact is ever loaded.
- **Combiner (when READY)** — per-target probabilities → scores
  (`ml_risk_to_score_mode`, default `linear_inverse` = `100 − p`), weighted by
  `ml_risk_to_score_weights` (placeholder proposal `severe_default` 40 /
  `severe_reliability` 35 / `l2b_breach` 25) and renormalized over READY
  targets only. Weights validated against the static allowed-target whitelist.

## Fallback policy (model unavailable)

The published score must never treat a non-ML value as an ML prediction:

- **Disabled phase (current):** `ml_targets = []` ⇒ the gate
  `ml_ready = ml_enabled AND ml_targets AND ml_calibration_score is not None`
  (`agent_trust_scorer.py:677-679`) is always `false`, so `Final = round(Composite)`.
  No model, no registry, no blend.
- **Ready phase (post sign-off):** any model-unavailable condition
  (`ModelUnavailableError` — artifact missing/corrupt, checksum mismatch, load
  failure) must read as `NOT_READY` ⇒ the 20% ML share is dropped ⇒
  `Final = round(Composite)`.
- **Activation constraint (flag for implementation before enabling):** the
  current fallback branches swap in `financial_score` on the stub predictor
  (`agent_trust_scorer.py:658,668`). That is inert today because `ml_targets = []`
  short-circuits the gate, but it **must be removed/replaced** so that an
  unavailable model can never contribute a financial-proxy value as the ML share.
  Activating the programme requires this resolved first — it is NOT part of this
  disabled-phase change.

## Sign-off checklist (business/data-science)

- [ ] Approve the target list (and whether each of the three proposals stands).
- [ ] Approve the horizon (30 / 60 / 90 days).
- [ ] Approve the combiner weights (proposal 40/35/25) and risk→score mapping.
- [ ] Confirm targets describe business-meaningful, measurable future events.

Until all boxes are checked the service ships with `ml_targets = []` and
`ml_enabled = false` => ML is always `NOT_READY`.