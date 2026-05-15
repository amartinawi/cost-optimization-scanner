# Audit: aurora Adapter

**Adapter**: `services/adapters/aurora.py` (431 lines — self-contained, no shim)
**ALL_MODULES index**: 28
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 (1 HIGH, 2 MEDIUM) |
| L2 Calculation | **WARN** | 4 (1 HIGH, 2 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **WARN** — substantially better than most adapters; live ACU pricing, scope-rule-compliant removal of 3 categories. Main issues: fallback-path double-multiply + CW Period invalid + dead code branches |

**Notable**: this adapter is **best-in-class** for live-pricing usage and scope-rule compliance. 3 of 5 originally-emitted recs (`clone_sprawl`, `global_db`, `backtrack`) explicitly REMOVED per scope rule, with the helper functions retained but returning empty.

---

## Pre-flight facts

- **Required clients**: `("rds", "cloudwatch")` ✓
- **Flags**: `requires_cloudwatch=True` ✓, `reads_fast_mode=True` ✓ (honored multiple places)
- **Sources**: 5 distinct sources (`serverless_v2`, `io_tier_analysis`, `clone_sprawl` empty, `global_db` empty, `backtrack` empty)
- **Pricing**: `get_aurora_acu_hourly()` (live, with $0.06 fallback)

---

## L1 — Technical findings

### L1-001 Two `print()` statements (banner + per-cluster error)  [MEDIUM]
- Lines 330, 372
- **Recommended fix**: `logger.debug` / `ctx.warn`

### L1-002 CloudWatch `Period = days * 86400` exceeds maximum  [HIGH]
- **Evidence**: lines 57, 81 — `period = days * 86400` (= 1,209,600 for 14 days). CloudWatch `get_metric_statistics` Period max is 86,400 seconds (1 day) for queries within 15 days, or 3600 for queries beyond 15 days. Period > 86,400 returns an error. Adapter swallows the error and returns `None` → check silently skipped
- **Why it matters**: every CW lookup in this adapter silently fails; `_get_cloudwatch_avg` returns `None` → `_check_serverless_v2` returns no recs → `_check_io_tier` returns no recs. The adapter currently emits ZERO Serverless v2 recommendations on any account because the CW call always fails
- **Recommended fix**: set `Period=86400` and average across the returned datapoints manually (or use `Period=3600` for hourly granularity)

### L1-003 Dead helper functions still issue RDS/CW API calls  [MEDIUM]
- **Evidence**: `_check_clone_sprawl` (line 207) calls `describe_db_cluster_snapshots` paginated, then ignores the result (line 232-235). `_check_global_db` (line 239) calls `describe_global_clusters` + `get_metric_statistics`, then ignores (line 281-283). `_check_backtrack` (line 287) inspects cluster, then ignores
- **Why it matters**: wasted API calls on every scan; risk of throttling. The scope-rule removal should also remove the API calls
- **Recommended fix**: replace each helper body with `return []` immediately, or delete the functions

---

## L2 — Calculation findings

### Source-classification table

| Recommendation | Source | Evidence | Acceptable? |
|---|---|---|---|
| `serverless_v2_acu_waste` | `live` (`get_aurora_acu_hourly`) × CW utilization | line 28, 129 | YES (modulo L1-002 making it unreachable) |
| `io_tier_optimization` | `module-const` ($0.20/M IO + $0.025/GB premium) × CW IOPS | line 20-21, 185-198 | YES — constants match AWS list |
| `clone_sprawl` / `global_db` / `backtrack` | scope-rule REMOVED | line 232, 281, 297 | YES — exemplary |

### L2-001 ACU `pricing_multiplier` double-applied on fallback path  [HIGH]
- **Evidence**:
  - Line 33 — `return ACU_HOURLY_FALLBACK * ctx.pricing_multiplier` (multiplier applied)
  - Line 129 — `wasted_acu * acu_hourly * HOURS_PER_MONTH * pricing_multiplier` (multiplier applied AGAIN)
- **Why it matters**: when live pricing fails and fallback is used, multiplier is applied twice → savings inflated by `multiplier²`. In eu-west-1 (multiplier ≈ 1.08), savings 1.166× actual
- **Recommended fix**: drop `ctx.pricing_multiplier` from line 33 (return raw fallback); rely solely on the multiplication at line 129

### L2-002 `acu_hourly` correctly NOT multiplied on live path  [LOW / POSITIVE]
- **Evidence**: line 28-33 — live `get_aurora_acu_hourly()` returns raw value (no multiplier); fallback path applies multiplier
- **Status**: PASS on live path; conflicts with L2-001 on fallback

### L2-003 `AURORA_ENGINES` includes deprecated `"aurora"` engine  [MEDIUM]
- **Evidence**: line 22 — `("aurora", "aurora-mysql", "aurora-postgresql")`. AWS retired the legacy `aurora` engine (Aurora MySQL 1.x); only `aurora-mysql` and `aurora-postgresql` exist for new clusters
- **Recommended fix**: drop `"aurora"` from the tuple

### L2-004 `_check_serverless_v2` requires `avg_util > 0` AND `monthly_savings > 1.0` minimums  [MEDIUM]
- **Evidence**: lines 124-131. Reasonable noise-filtering, but a cluster with avg_util = 0 (truly idle Serverless) gets NO recommendation — should be the strongest case
- **Recommended fix**: emit rec when avg_util == 0 with `monthly_savings = max_acu × acu_hourly × 730 × multiplier`

---

## L3 — Reporting findings

**None** — adapter declares `stat_cards` and `grouping`, sets `total_count` extras correctly, descriptions cover all 5 sources.

---

## Recommended next steps

1. **[HIGH] L1-002** — Fix CW `Period` to ≤86400. Currently EVERY serverless_v2 / io_tier rec is silently dropped.
2. **[HIGH] L2-001** — Drop `ctx.pricing_multiplier` from line 33 fallback; rely on line 129.
3. **[MEDIUM] L1-003** — Replace dead helper bodies with `return []` to eliminate wasted API calls.
4. **[MEDIUM] L2-003** — Drop legacy `"aurora"` engine.
5. **[MEDIUM] L2-004** — Cover `avg_util == 0` idle-cluster case.
6. **[MEDIUM] L1-001** — Drop the 2 prints.
