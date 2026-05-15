# Audit: mediastore Adapter

**Adapter**: `services/adapters/mediastore.py` (59 lines)
**ALL_MODULES index**: 24
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 |
| L2 Calculation | **WARN** | 3 (1 MEDIUM, 2 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **WARN** — S3-equivalent pricing acceptable proxy; missing ingest + API fees; $20 fallback invented |

**Note**: MediaStore is being deprecated by AWS (announced 2024) — most accounts won't have new MediaStore resources to optimize. Audit findings still apply for legacy accounts.

---

## Pre-flight facts

- **Required clients**: `("mediastore", "cloudwatch")` ✓
- **Sources**: `{"enhanced_checks"}`
- **Pricing**: `get_s3_monthly_price_per_gb("STANDARD")` (live) — used as proxy for MediaStore storage

---

## L1 — Technical findings

### L1-001 Adapter print  [LOW]
### L1-002 No try/except around shim call  [MEDIUM]

---

## L2 — Calculation findings

### L2-001 $20/month fallback when `EstimatedStorageGB` missing  [MEDIUM]
- **Evidence**: line 49 — invented constant
- **Recommended fix**: emit 0 + `pricing_warning`

### L2-002 Ingest ($0.0085/GB) + API request fees ($0.0050/10K PUT, $0.0004/10K GET) not modeled  [MEDIUM]
- **Evidence**: adapter only models storage ($/GB-mo). MediaStore charges three distinct dimensions
- **Recommended fix**: shim should query CW `IngressBytes` + `RequestCount` metrics; adapter multiplies by published rates

### L2-003 S3 STANDARD as MediaStore proxy  [LOW]
- **Evidence**: line 43 — MediaStore lists at $0.023/GB-month, same as S3 STANDARD ✓
- **Status**: PASS (matches AWS list)

---

## L3 — Reporting findings

**None**.

---

## Recommended next steps

1. **[MEDIUM] L2-001** — Drop $20 fallback.
2. **[MEDIUM] L2-002** — Model ingest + API request costs.
3. **[MEDIUM] L1-002** — Wrap shim with try/except.
4. **[LOW] L1-001** — Drop print.
