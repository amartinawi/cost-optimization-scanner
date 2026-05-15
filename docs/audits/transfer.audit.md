# Audit: transfer Adapter

**Adapter**: `services/adapters/transfer.py` (57 lines)
**ALL_MODULES index**: 21
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 |
| L2 Calculation | **FAIL** | 4 (2 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **WARN** (was already WARN in SUMMARY) — protocol $/hr correct but missing data transfer, connectors, Web App cost categories |

---

## Pre-flight facts

- **Required clients**: `("transfer",)`
- **Sources**: `{"enhanced_checks"}`
- **Pricing**: `$0.30/protocol/hour × 730` = $219/protocol/month — matches AWS list

---

## L1 — Technical findings

### L1-001 Adapter banner `print()`  [LOW]
### L1-002 No try/except around shim call  [MEDIUM]

---

## L2 — Calculation findings

### L2-001 `removable = num_protocols - 1` assumption  [HIGH]
- **Evidence**: line 45 — assumes the user wants to consolidate to 1 protocol. The shim's recommendation may actually be "delete entire server" or "specific protocol unused" — the adapter doesn't know which
- **Recommended fix**: shim should emit `RemovableProtocols` explicitly per rec

### L2-002 Data transfer, AS2, connector, Web App pricing omitted  [HIGH]
- **Evidence**: AWS Transfer Family has several major cost components beyond endpoint-protocol-hours:
  - Data uploaded: $0.04/GB
  - Data downloaded: $0.04/GB
  - AS2 message processing: $0.001/message (first 1M)
  - Connectors: $0.05/connector/hour
  - Web App: $0.30/Web App-hour + $0.05/active user
- Adapter only models endpoint-protocol-hours; other categories invisible
- **Recommended fix**: extend shim to emit recs per cost component

### L2-003 No fallback for missing `Protocols`  [MEDIUM]
- **Evidence**: line 43 — `protocols = rec.get("Protocols", ["SFTP"])`. If rec is for a non-endpoint resource (e.g., connector), `Protocols` is missing and adapter assumes 1 SFTP protocol → savings = $0 (correct by accident)
- **Recommended fix**: only apply protocol-based calc when rec is endpoint-shaped

### L2-004 `pricing_multiplier` correctly applied to module-const path  [LOW / POSITIVE]
- **Status**: PASS

---

## L3 — Reporting findings

**None**.

---

## Recommended next steps

1. **[HIGH] L2-001** — Shim emits explicit `RemovableProtocols` (or specific protocol to remove); adapter uses it.
2. **[HIGH] L2-002** — Add data-transfer + connector + Web App cost models.
3. **[MEDIUM] L2-003** — Only apply protocol calc to endpoint-shape recs.
4. **[LOW] L1-001 / L1-002** — Drop print; add try/except.
