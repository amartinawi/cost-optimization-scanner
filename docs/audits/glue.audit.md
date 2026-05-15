# Audit: glue Adapter

**Adapter**: `services/adapters/glue.py` (62 lines)
**ALL_MODULES index**: 25
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 1 |
| L2 Calculation | **FAIL** | 4 (2 HIGH, 2 MEDIUM) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **WARN** — DPU rate correct; 160 hr/month + 30% factor invented |

---

## Pre-flight facts

- **Required clients**: `("glue",)`
- **Sources**: dynamic per-shim-checks
- **Pricing**: `GLUE_DPU_HOURLY = 0.44` (matches AWS list)

---

## L1 — Technical findings

### L1-001 Adapter print  [LOW]

---

## L2 — Calculation findings

### L2-001 `160 DPU-hours/month/job` assumption  [HIGH]
- **Evidence**: line 49 — `dpu_count × 0.44 × 160`. Comment says "8 hrs/day × 20 workdays" but real Glue jobs run on schedule (might be 1 hour/day, or 12 hours daily ETL). Actual DPU-hours come from `glue.get_job_runs` aggregate
- **Recommended fix**: shim should query `glue.get_job_runs` for actual DPU-hour history; emit `MonthlyDPUHours` numeric per rec

### L2-002 30% rightsize factor uniform  [HIGH]
- **Evidence**: line 51 — `monthly_cost × 0.30`. Glue rightsizing savings vary by current vs optimal DPU count
- **Recommended fix**: derive from actual job metrics (DPU utilization)

### L2-003 Worker-type variations not modeled  [MEDIUM]
- **Evidence**: Glue has G.1X ($0.44/DPU-hr), G.2X ($0.44/DPU-hr × 2), G.4X, G.8X, G.025X ($0.44/DPU-hr × 0.25), Python shell ($0.44/DPU-hr × 1/16). Adapter treats all as $0.44/DPU-hr
- **Recommended fix**: model per-worker-type DPU pricing

### L2-004 Default `dpu_count = 10` if `MaxCapacity`/`NumberOfWorkers` missing  [MEDIUM]
- **Evidence**: line 43, 47 — invented default
- **Recommended fix**: skip rec if DPU count unknown

---

## L3 — Reporting findings

**None** — shim emits dynamic sources, adapter passes through.

---

## Recommended next steps

1. **[HIGH] L2-001** — Replace 160 hr/month with actual `glue.get_job_runs` aggregate.
2. **[HIGH] L2-002** — Per-rec savings factor from job metrics.
3. **[MEDIUM] L2-003** — Per-worker-type DPU pricing.
4. **[MEDIUM] L2-004** — Skip recs with missing DPU count.
5. **[LOW] L1-001** — Drop print.
