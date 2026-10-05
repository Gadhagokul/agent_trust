# Terminology — Supplier L2B & Search-to-Booking

Canonical terminology for the project, per the senior review's terminology
correction. Use these terms everywhere; the legacy field names live only in the
API/audit/settings surface for backward compatibility and must be read against
this glossary.

## Canonical terms

| Term | Definition |
|------|------------|
| **Supplier Search Requests** | Successful supplier search requests made by the agent (created **and** reused intents; `SUM(search_session_accesses.access_count)` with `access_count` summing created + reused). |
| **Successful Bookings** | Confirmed/ticketed bookings attributed to the supplier (`bookings.provider = suppliers.name`, status `confirmed`/`ticketed`). |
| **Supplier L2B** | `Supplier Search Requests / Successful Bookings`, i.e. searches per booking. Written as an *x:1* ratio — a ratio, **never a percentage**. |

```
L2B = searches / bookings            (x:1)
```

Example: 10,000 supplier searches and 10 successful bookings

```
L2B = 10,000 / 10 = 1,000:1
```

- **Lower observed L2B = better** (the agent converts searches efficiently).
- **Higher observed L2B = worse** (the agent consumes searches without booking).

| Term | Definition |
|------|------------|
| **Allowed L2B** (allowed searches per booking) | The supplier's contractual limit derived from the `suppliers` table as `search_limit / minimum_booking` (x:1). If `minimum_booking = 1` and `search_limit = 1000`, the allowed searches per booking is **1000 : 1** — a ratio, never a percentage. |
| **Supplier L2B compliance** | `observed L2B` relative to `allowed L2B`. Compliant when `observed L2B ≤ allowed L2B`; breaching when `observed L2B > allowed L2B`. Do **not** invert this (e.g. "bookings per search") and call it L2B. |

## Internal code representation (mapping)

The scorer compares two internal ratios (both inverted, for numerical safety):

| Code quantity | Formula | Meaning |
|---------------|---------|---------|
| `target_s` | `suppliers.minimum_booking / search_limit` | `1 / allowed L2B` |
| `ratio_s` | `bookings_s / searches_s` | `1 / observed L2B` (both > 0) |

`ratio_s ≥ target_s` ⇔ `observed L2B ≤ allowed L2B` ⇔ compliant. The legacy
`_score_l2b_for_target` curve (`agent_trust_scorer.py:369`) implements exactly
this comparison; the arbitrary internal curve inputs are {searches, bookings,
target} and the documented anchors below.

**Direction contract:** the business rule and the observed metric are **both**
expressed as `searches / bookings` (x:1). The implementation's internal
`bookings / searches` is only a derived inverse used for numerical scoring — it
is **never** the definition of L2B. Do not read `ratio_s = bookings/searches` as
"L2B"; it is `1/observed L2B`.

## Scoring curve contract

For a supplier with allowed L2B `A:1` (target_ratio `t = 1/A`), the L2B score
(`_score_l2b_for_target`, `agent_trust_scorer.py:369-405`) is:

| Observed L2B (searches/bookings) | Internal `ratio_s` (bookings/searches) | Score |
|-----------------------------------|----------------------------------------|-------|
| `= allowed` (`A:1`) | `= target` (`t`) | **80** (neutral, at target) |
| `< allowed` (better) | `> target` | rises **80 → 100**, reaching 100 at `excellent_multiplier × target` |
| `> allowed` (worse) | `< target` | falls **80 → 0** linearly (below-target ramp toward 0) |

**Worked example (senior review):** allowed searches per booking = **1000 : 1**
(`search_limit = 1000`, `minimum_booking = 1`, `t = 0.001`):

| Agent | Searches | Bookings | Observed L2B | Result |
|-------|----------|----------|--------------|--------|
| equal | 10,000 | 10 | 1000:1 (= allowed) | **80** |
| better | 10,000 | 20 | 500:1 (< allowed) | **> 80** |
| worse | 10,000 | 5 | 2000:1 (> allowed) | **< 80** |

Pinned by `tests/test_scorer.py::TestScoreL2bForTarget::test_allowed_l2b_direction_pinned`.

## Supplier aggregation

The L2B component aggregates **configured suppliers only**, weighted by
**search volume**, never by booking share:

- **Share** = `supplier searches / total configured searches`
  (`agent_trust_scorer.py:498`, `share = searches / total_searches`).
- **Unconfigured suppliers** (no `minimum_booking`/`search_limit > 0`) are
  **excluded** — never assigned an invented ratio (`l2b_not_configured_policy = "exclude"`).
- **Dominance cap:** any single supplier's share above `l2b_max_supplier_share`
  (default 0.5) is capped and the excess redistributed proportionally over the
  remaining suppliers; the final shares are renormalized to sum to 1
  (`agent_trust_scorer.py:498-516`).

## Legacy names — map to canonical

| Legacy / ambiguous name | Canonical term |
|-------------------------|----------------|
| `search-to-booking`, `search_to_booking_score`, `S2B` | **Supplier L2B compliance** score (the 20% trust component) |
| `l2b`, `l2b_component`, `l2b_policy` | **Supplier L2B compliance** (audit metadata) |
| `search/booking`, `booking ratio`, `conversion` | **observed L2B** = `searches / bookings` |
| `supplier ratio`, site benchmark | **allowed L2B** = `search_limit / minimum_booking` (x:1) |

## Configuration knobs (settings → senior-review terms)

| Settings field (`app/infra/settings.py`) | Senior-review term | Notes |
|------------------------------------------|--------------------|-------|
| `search_to_booking_min_searches` (default 20) | `L2B_MIN_SEARCHES_BEFORE_BREACH` | Minimum observed searches before sustained L2B behavior can yield a breach score; below it behavior is anchored to the neutral score (zero-booking confidence ramp). Configurable, **not hard-coded**. |
| `search_to_booking_neutral_score` (default 80) | neutral anchor | Score applied under low evidence / at target. |
| `search_to_booking_excellent_multiplier` (default 4.0) | excellent threshold | Score reaches 100 at `excellent_multiplier × target`. |
| `l2b_max_supplier_share` (default 0.5) | supplier dominance cap | Max share any single supplier may drive of the final L2B component; excess redistributed proportionally. Configurable; business-justification required (§8). |
| `l2b_not_configured_policy` (= `exclude`) | NOT_CONFIGURED handling | The only sanctioned policy: a supplier without a usable target is **excluded**, never assigned invented compliance. |
| `l2b_group_by_channel` (default false) | channel dimension | OFF until the business defines the channel dimension (NDC/GDS) in the schema. |

## Supplier L2B business rules

The authoritative per-supplier rules (`allowed search/book ratio`, channel,
effective date, measurement definition, minimum observation) must be formally
confirmed by the business for each supplier (AEGEAN, VERTEIL, GETFARES, ONEFLY,
SABRE) before any ratio enters scoring. V1 sources the allowed L2B from
`suppliers.minimum_booking / search_limit`; undocumented suppliers stay
NULL/NOT_CONFIGURED and are excluded. See README → *Supplier L2B business
rules* and `docs/ML_TARGET_SPEC.md`.