# Audit: redshift Adapter

**Adapter**: `services/adapters/redshift.py` (63 lines)
**ALL_MODULES index**: 17
**Renderer path**: Phase B (generic) — `_PHASE_B_SKIP_PER_REC` does NOT include redshift, so falls through to `_render_generic_other_rec`. However `should_skip_section_header` includes `"redshift"` (`reporter_phase_b.py:2236`) → section header omitted
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 |
| L2 Calculation | **FAIL** | 5 (1 HIGH, 3 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 1 |
| **Overall**    | **FAIL** | arbitrary 24% factor + $200 fallback inconsistency + RA3 managed storage not modeled |

---

## Pre-flight facts

- **Required clients**: `("redshift",)`
- **Flags**: defaults
- **Sources**: `{"enhanced_checks"}`
- **Pricing**: `get_instance_monthly_price("AmazonRedshift", node_type)` — live; `REDSHIFT_NODE_MONTHLY_FALLBACK=200.0` invented constant

---

## L1 — Technical findings

### L1-001 Adapter banner `print()`  [LOW]

### L1-002 No try/except around shim call  [MEDIUM]
- **Recommended fix**: route shim exceptions via `ctx.warn`

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| Live-pricing rec (node_type known) | `live × 0.24 × num_nodes` | line 48-49 | **FAIL** — 24% factor uniform across rec types (Reserved, Pause, Spectrum, Concurrency Scaling) — each has different real savings |
| Missing-node-type fallback | `module-const` ($200 × multiplier) | line 51 | **WARN** — invented constant; correctly applies multiplier per L2.3.2 |
| `pricing_engine is None` fallback | `module-const` ($200 × len(recs), **no multiplier**) | line 53 | **HIGH FAIL** — applies $200 per rec without multiplier; double-bug: invented constant AND missing regional adjustment |

### L2-001 Flat 24% savings factor uniform across heterogeneous rec types  [HIGH]
- **Evidence**: line 49 — `savings += monthly * num_nodes * 0.24`. AWS Redshift savings vary by opportunity:
  - Reserved instances: 30-75% off On-Demand
  - Pause/resume (unused clusters): 100% of compute, but RMS storage still charged on RA3
  - Spectrum: $5/TB scanned, no node savings
  - Concurrency Scaling: free for first 1 hr/day per node, then $/sec
- 24% applied to everything — every category is mis-priced
- **Recommended fix**: per-`CheckCategory` factor dict

### L2-002 $200/month fallback when node_type missing  [MEDIUM]
- **Evidence**: line 51. AWS Redshift node prices range from $0.25/hr (dc2.large = $182.50/mo) to $13/hr (ra3.16xlarge = $9,490/mo)
- **Recommended fix**: emit 0 + `pricing_warning` when node_type missing

### L2-003 `pricing_engine is None` path drops `pricing_multiplier`  [MEDIUM]
- **Evidence**: line 53 — `savings = REDSHIFT_NODE_MONTHLY_FALLBACK * len(recs)` — no multiplier
- **Recommended fix**: apply `× ctx.pricing_multiplier` (the live-path branch correctly omits it because live is region-correct, but the fallback should apply it)

### L2-004 RA3 managed storage not modeled  [MEDIUM]
- **Evidence**: adapter TODO line 38-39. RA3 nodes charge $0.024/GB/month for Redshift Managed Storage. Adapter only models compute (node hours)
- **Recommended fix**: shim should query `redshift.describe_clusters().Clusters[].TotalStorageCapacityInMegaBytes` × $0.024/GB

### L2-005 `pricing_multiplier` correctly omitted on live path  [LOW / POSITIVE]
- **Evidence**: line 49 — no multiplier on `get_instance_monthly_price` result. Per L2.3.1 this is correct
- **Status**: PASS

---

## L3 — Reporting findings

### L3-001 Generic per-rec renderer + section header suppressed  [WARN]
- **Evidence**: `redshift` is in `should_skip_section_header` allowlist (`reporter_phase_b.py:2236`) but not in `_PHASE_B_SKIP_PER_REC` (which would skip per-rec rendering). Falls through to `_render_generic_other_rec` (line 1188). Field `NodeType` is not in the resource-id resolver list — recs likely render as "Resource"
- **Recommended fix**: add `ClusterIdentifier` to resource-id resolver chain, or add a dedicated `_render_redshift_*` handler

---

## Recommended next steps

1. **[HIGH] L2-001** — Per-category savings factors with documented sources.
2. **[MEDIUM] L2-002 / L2-003 / L2-004** — Drop $200 fallback; add multiplier on fallback path; model RMS storage.
3. **[MEDIUM] L1-002** — Wrap shim call with try/except → ctx.warn.
4. **[MEDIUM] L3-001** — Dedicated renderer or add `ClusterIdentifier` resolver.
5. **[LOW] L1-001** — Drop adapter print.
