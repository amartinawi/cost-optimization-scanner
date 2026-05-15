# Audit: elasticache Adapter

**Adapter**: `services/adapters/elasticache.py` (77 lines)
**Legacy shim**: `services/elasticache.py` (168 lines)
**ALL_MODULES index**: 11
**Renderer path**: **Phase B** — `("elasticache", "enhanced_checks"): _render_elasticache_enhanced_checks` (`reporter_phase_b.py:2187`)
**Date**: 2026-05-15
**Auditor**: automated

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 (1 HIGH, 1 MEDIUM, 1 LOW) |
| L2 Calculation | **FAIL** | 6 (1 CRITICAL, 3 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 2 (1 MEDIUM, 1 LOW) |
| **Overall**    | **FAIL** | live-pricing × pricing_multiplier double-multiply + 4 arbitrary ri_rate factors + 4 invented fallback constants |

---

## Pre-flight facts

- **Adapter class**: `ElasticacheModule` at `services/adapters/elasticache.py:12`
- **Declared `required_clients`**: `("elasticache", "cloudwatch")` ✓ accurate
- **Declared `requires_cloudwatch`**: `False` (default) — **lies** (shim uses CW at `services/elasticache.py:56, 123`)
- **Declared `reads_fast_mode`**: `True` — **lies** (no consumer in shim)
- **Sources emitted**: `{"enhanced_checks"}`
- **Pricing methods consumed**: `ctx.pricing_engine.get_instance_monthly_price("AmazonElastiCache", node_type)` (live!)

---

## L1 — Technical findings

### L1-001 `requires_cloudwatch` False but shim uses CW; `reads_fast_mode` True but fast_mode never read  [HIGH]
- Same pattern as DynamoDB / Lambda / Containers
- **Recommended fix**: set `requires_cloudwatch=True`, set `reads_fast_mode=False`

### L1-002 Adapter banner `print()` at line 37  [MEDIUM]
- **Recommended fix**: `logger.debug`

### L1-003 String-token dispatch on `EstimatedSavings`  [LOW]
- Same anti-pattern as containers — uses keyword grep on `est = rec.get("EstimatedSavings","")` to select savings factor. Should dispatch on `CheckCategory` (shim already emits canonical category names like "Valkey Migration", "Graviton Migration", "Reserved Nodes Opportunity", "Underutilized Cluster")

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| Reserved Nodes savings | `derived` (`live_monthly × num_nodes × 0.35 × multiplier`) | line 45, 58-60 | **CRITICAL FAIL** — double-multiply: `get_instance_monthly_price` is live (region-correct) but adapter multiplies by `ctx.pricing_multiplier` again | per `core/CLAUDE.md`: "Multiply by `pricing_multiplier` **only** for module-constant or fallback paths" |
| Graviton Migration | `derived` (`live × 0.05`) | line 46-47, 58-60 | **FAIL** — 5% factor contradicts the rec text "20-40% price-performance improvement" | n/a |
| Valkey Migration | `derived` (`live × 0.20`) | line 48-49 | **WARN** — 20% is mid-band of AWS-published "up to 20% cheaper than Redis OSS"; defensible if documented |
| Underutilized Cluster | `derived` (`live × 0.40`) | line 50-51, 58-60 | **FAIL** — flat 40% has no basis; shim collects CPU utilization (line 123-150) but does not pass it through |
| Generic else branch | `derived` (`live × 0.20`) | line 53, 58-60 | **WARN** — undocumented |
| Fallback (no pricing_engine OR no node_type) | `module-const` ($60 / $80 / $50 / $100 × multiplier) | line 62-69 | **WARN** — invented constants but multiplier correctly applied per L2.3.2 |

### L2-001 Double `pricing_multiplier` on live-pricing path  [CRITICAL]
- **Check**: L2.3.1
- **Evidence**: `services/adapters/elasticache.py:58-60`:
  ```python
  if ctx.pricing_engine is not None and node_type:
      monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonElastiCache", node_type)
      savings += monthly * num_nodes * ri_rate * ctx.pricing_multiplier
  ```
  `get_instance_monthly_price` calls the AWS Pricing API for the resource's region — value is **already region-correct**. Multiplying by `ctx.pricing_multiplier` again over-counts by the regional factor
- **Why it matters**: in eu-west-1 (multiplier ≈ 1.08), every ElastiCache savings figure is 8% inflated. Same for São Paulo (~50% inflated)
- **Recommended fix**: drop `* ctx.pricing_multiplier` from line 60. Apply it only on the fallback `else` branch (lines 62-69), where it's correct (module-const path per L2.3.2)

### L2-002 Reserved-nodes 35% factor contradicts AWS published 30-60%  [HIGH]
- **Evidence**: line 45 — `ri_rate = 0.35` for Reserved. AWS ElastiCache Reserved Nodes: 30-60% off On-Demand depending on term (1yr / 3yr) and payment (no/partial/all upfront). Shim emits `"30-60% vs On-Demand for committed usage"` text but adapter applies flat 35%
- **Recommended fix**: surface Reserved-term selection (1yr-partial = ~38%, 3yr-all-upfront = ~60%) per rec, or document the 35% as conservative midpoint of 1yr-no-upfront

### L2-003 Graviton 5% factor contradicts AWS published 20-40%  [HIGH]
- **Evidence**: line 47 — `ri_rate = 0.05`. AWS publishes "up to 20% better price-performance" for Graviton; the *cost* delta on m6g vs m5 is ~10% lower for the same memory, but performance per dollar improves up to 40%
- **Recommended fix**: use 0.10 as the price delta (conservative); document the price-vs-performance distinction in the rec text

### L2-004 Underutilized cluster 40% factor invented  [HIGH]
- **Evidence**: line 51 — `ri_rate = 0.40`. Shim's underutilized-cluster path at `services/elasticache.py:123-150` collects actual CPU utilization but the value never reaches the adapter as a numeric field
- **Recommended fix**: shim should emit `EstimatedMonthlySavings = monthly × (1 - utilization × 1.2)`; adapter reads numeric

### L2-005 4 invented `else` fallback constants ($60/$80/$50/$100)  [MEDIUM]
- **Evidence**: line 62-69
- **Why it matters**: triggered when `node_type` is missing from rec. Hides shim emission gaps behind arbitrary numbers
- **Recommended fix**: emit 0 + pricing_warning when node_type missing

### L2-006 `pricing_multiplier` correctly applied on fallback path  [LOW / POSITIVE CONTROL]
- **Evidence**: lines 63, 65, 67, 69 — `× ctx.pricing_multiplier` on each invented constant. Per L2.3.2 this is correct (fallback is module-const)
- **Status**: PASS

### External validation log

| Finding ID | Source | Reference | Confirms / Refutes |
|---|---|---|---|
| L2-001 | core/CLAUDE.md | "Multiply by `pricing_multiplier` only for module-constant or fallback paths" | refutes double-multiply on live path |
| L2-002 | AWS ElastiCache Reserved Nodes docs | https://aws.amazon.com/elasticache/reserved-cache-nodes/ | 30-60% range refutes flat 35% |
| L2-003 | AWS Graviton ElastiCache | https://aws.amazon.com/elasticache/features/ | up to 20% performance gain, ~10% list-price delta refutes 5% |

---

## L3 — Reporting findings

### L3-001 Single `enhanced_checks` source — no separation by check category  [MEDIUM]
- Recommendation: 5 distinct sources (`valkey_migration`, `graviton_migration`, `reserved_nodes`, `underutilized_clusters`, `engine_version_review`)

### L3-002 No `priority` set; underutilized + reserved should be high  [LOW]

---

## Verification log

```
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='elasticache'); print('cw:', m.requires_cloudwatch, 'fast:', m.reads_fast_mode)"
cw: False ← LIE
fast: True ← LIE

$ python3 -m pytest tests/test_offline_scan.py tests/test_regression_snapshot.py \
    tests/test_reporter_snapshots.py -k elasticache -v
(passes against flawed math)
```

---

## Recommended next steps (prioritized)

1. **[CRITICAL] L2-001** — Drop `* ctx.pricing_multiplier` from line 60 (live path).
2. **[HIGH] L2-002 / L2-003 / L2-004** — Use measured / documented per-category savings factors; pass measured CPU utilization through for underutilized recs.
3. **[HIGH] L1-001** — Fix `requires_cloudwatch` / `reads_fast_mode` flags.
4. **[MEDIUM] L2-005** — Replace 4 invented fallback constants with 0 + `pricing_warning`.
5. **[MEDIUM] L1-003** — Dispatch on `CheckCategory` not `EstimatedSavings`-string.
6. **[LOW] L1-002** — Drop adapter print.
7. **[LOW] L3-001 / L3-002** — Split sources by category; set priority.
