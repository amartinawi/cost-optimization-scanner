# Audit: opensearch Adapter

**Adapter**: `services/adapters/opensearch.py` (80 lines)
**Legacy shim**: `services/opensearch.py`
**ALL_MODULES index**: 12
**Renderer path**: **Phase B** — `("opensearch", "enhanced_checks"): _render_opensearch_enhanced_checks` (`reporter_phase_b.py:2188`)
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 |
| L2 Calculation | **FAIL** | 6 (1 CRITICAL, 3 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 2 |
| **Overall**    | **FAIL** | live-pricing × multiplier double-multiply + arbitrary `ri_rate` factors + 3 invented fallback constants |

---

## Pre-flight facts

- **Adapter class**: `OpensearchModule` at `services/adapters/opensearch.py:12`
- **Required clients**: `("opensearch",)`
- **Flags**: `requires_cloudwatch=False`, `reads_fast_mode=True` (probably lying — need shim verification)
- **Sources**: `{"enhanced_checks"}`
- **Pricing methods**: `get_instance_monthly_price("AmazonES", instance_type)` (live, AmazonES confirmed as valid OpenSearch service code)

---

## L1 — Technical findings

### L1-001 Adapter banner `print()` at line 36  [MEDIUM]
- **Recommended fix**: `logger.debug`

### L1-002 String-token dispatch on `EstimatedSavings`  [MEDIUM]
- Same anti-pattern as containers/elasticache — keyword grep against `est = rec.get("EstimatedSavings","")`. Should dispatch on `CheckCategory`

### L1-003 `reads_fast_mode=True` likely lies  [LOW]
- Need to verify `fast_mode` consumed in `services/opensearch.py`. If not consumed, flip to False

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| Reserved | `derived` (`live × 0.35 × multiplier`) | line 46, 57-59 | **CRITICAL FAIL** — double-multiplier on live path | AWS RI savings 30-60% |
| Graviton | `derived` (`live × 0.25`) | line 47-48 | **WARN** — 25% near AWS-published "up to 30% better price-performance" |
| storage | `derived` (`live × 0.20`) | line 49-50 | **FAIL** — "storage" keyword applies 20% to instance price, conceptually mismatched (storage savings should come from `ebs_volume_size × gp3_rate × ri_rate`, which the code does separately at line 68-71) — risks double-counting |
| else | `derived` (`live × 0.30`) | line 52 | **WARN** — undocumented |
| Fallback (no engine) | `module-const` ($300/$120/$50 × multiplier) | line 60-66 | **WARN** — invented constants but multiplier correctly applied |
| EBS volume gp3 | `module-const` (`$0.11/GB-month × multiplier`) | line 40, 68-71 | **WARN** — $0.11 close but **not** AWS gp3 list ($0.08/GB-month); applies `ri_rate` to STORAGE savings which is nonsensical (storage savings don't get RI discount) |

### L2-001 Live-pricing × `pricing_multiplier` double-multiply  [CRITICAL]
- **Evidence**: line 59 — `savings += monthly * instance_count * ri_rate * ctx.pricing_multiplier` where `monthly` is already region-correct from PricingEngine
- **Recommended fix**: drop `* ctx.pricing_multiplier` from line 59

### L2-002 `ri_rate` applied to storage savings is conceptually wrong  [HIGH]
- **Evidence**: line 71 — `storage_monthly * ri_rate * ctx.pricing_multiplier`. Reserved Instances cover instance hours, not EBS storage. Applying `ri_rate = 0.35` to gp3 storage produces a meaningless number
- **Recommended fix**: drop `* ri_rate` from line 71 OR compute storage savings as `(current_storage_class_rate - target_storage_class_rate) × ebs_volume_size × multiplier`

### L2-003 gp3 rate $0.11/GB-month doesn't match AWS list  [HIGH]
- **Evidence**: line 40 — `GP3_PRICE_PER_GB_MONTH = 0.11`. AWS list price for gp3 EBS storage in us-east-1 is $0.08/GB-month. OpenSearch may apply a managed-service markup ($0.135/GB-month for OpenSearch-managed gp3 storage in some regions per OS pricing page) but $0.11 is neither — appears mid-range invented
- **Recommended fix**: replace with `ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp3")` or use the actual OpenSearch-managed storage price from AWS docs

### L2-004 Arbitrary `ri_rate` factors  [HIGH]
- **Evidence**: lines 46, 48, 50, 52 — 0.35, 0.25, 0.20, 0.30. Each unsourced
- **Recommended fix**: per-category factors from AWS docs

### L2-005 Invented fallback constants $300/$120/$50  [MEDIUM]
- **Recommended fix**: emit 0 + `pricing_warning` when `instance_type` missing

### L2-006 No `EBSVolumeSize` aggregation across multiple recs  [LOW]
- **Evidence**: each rec contributes its own storage savings line independently. If the shim emits 2 recs for the same domain (e.g., reserved + graviton + storage), the EBS savings counts 2-3×
- **Recommended fix**: dedupe storage savings by `DomainName`

---

## L3 — Reporting findings

### L3-001 Single `enhanced_checks` source  [MEDIUM]
- Split into `reserved_nodes`, `graviton_migration`, `storage_optimization`, etc.

### L3-002 No `priority`  [LOW]

---

## Verification log

```
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='opensearch'); print(m.required_clients(), m.requires_cloudwatch, m.reads_fast_mode)"
('opensearch',) False True
```

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Drop double-multiply on live path (line 59).
2. **[HIGH] L2-002** — Drop `ri_rate` from storage-savings calculation.
3. **[HIGH] L2-003** — Use PricingEngine for gp3 rate.
4. **[HIGH] L2-004** — Source per-category factors.
5. **[MEDIUM] L2-005** — Replace fallback constants with 0 + warning.
6. **[MEDIUM] L1-002** — Dispatch on `CheckCategory`.
7. **[LOW] L2-006** — Dedupe storage savings by domain.
