# Audit: msk Adapter

**Adapter**: `services/adapters/msk.py` (65 lines)
**ALL_MODULES index**: 22
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 |
| L2 Calculation | **FAIL** | 5 (1 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | live-pricing × pricing_multiplier double-multiply on broker rate + 30% factor + $150 invented fallback + MSK storage $0.10/GB-mo |

---

## Pre-flight facts

- **Required clients**: `("kafka",)`
- **Sources**: `{"enhanced_checks"}`
- **Pricing**: `get_msk_broker_hourly_price(instance_type)` (live), `$0.10/GB-month` MSK storage (module-const)

---

## L1 — Technical findings

### L1-001 Adapter print  [LOW]
### L1-002 No try/except around shim call  [MEDIUM]
- **Recommended fix**: wrap with `ctx.warn` on failure

---

## L2 — Calculation findings

### L2-001 Live `hourly × 730 × num_brokers` THEN `× pricing_multiplier`  [CRITICAL]
- **Evidence**: line 47-51 — `monthly_cluster = hourly × 730 × num_brokers` where `hourly` is live region-correct; then `savings += monthly_cluster × 0.30 × ctx.pricing_multiplier`
- **Why it matters**: live path double-applies multiplier (L2.3.1). Same bug as elasticache, opensearch, dms
- **Recommended fix**: split — broker savings (live, no multiplier) + storage savings (module-const, with multiplier), then × 0.30

### L2-002 MSK storage $0.10/GB-month flat  [HIGH]
- **Evidence**: line 49 — `volume_size * 0.10 * num_brokers`. AWS MSK storage pricing: gp3 = $0.10/GB-month, gp2 = $0.10/GB-month for managed MSK (MSK adds a markup over raw EBS rates). The $0.10 is correct for managed MSK storage
- **Status**: PASS on the rate; would benefit from live PricingEngine method `get_msk_storage_price_per_gb()` for regional accuracy

### L2-003 30% uniform savings factor  [HIGH]
- **Evidence**: line 51. Right-sizing savings vary by actual utilization
- **Recommended fix**: derive from CW broker CPU/disk metrics

### L2-004 $150 fallback constant in two places  [MEDIUM]
- **Evidence**: lines 53, 55
- **Recommended fix**: emit 0 + warning

### L2-005 Serverless migration comparison missing  [MEDIUM]
- **Evidence**: TODO line 35-36 — Serverless MSK ($0.06/DCU-hr) not modeled

---

## L3 — Reporting findings

**None**.

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Apply `pricing_multiplier` only to storage portion; remove from broker.
2. **[HIGH] L2-003** — Per-rec factor from CW utilization.
3. **[MEDIUM] L2-004** — Drop $150 fallback.
4. **[MEDIUM] L2-005** — Model Serverless MSK comparison.
5. **[LOW] L1-001 / L1-002** — Drop print; wrap shim.
