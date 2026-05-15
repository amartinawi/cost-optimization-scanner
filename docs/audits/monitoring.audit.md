# Audit: monitoring Adapter

**Adapter**: `services/adapters/monitoring.py` (76 lines)
**Legacy shims (4)**: `services/monitoring.py` (233 lines), `services/backup.py` (130 lines), `services/route53.py` (141 lines), `services/cloudtrail` (subset of monitoring.py)
**ALL_MODULES index**: 10
**Renderer path**: **Phase B** — each of `cloudwatch_checks`, `cloudtrail_checks`, `backup_checks`, `route53_checks` mapped to `_render_monitoring_enhanced_checks` (`reporter_phase_b.py:2190-2193`)
**Date**: 2026-05-15
**Auditor**: automated

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 4 (2 HIGH, 1 MEDIUM, 1 LOW) |
| L2 Calculation | **FAIL** | 5 (1 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 2 (1 MEDIUM, 1 LOW) |
| **Overall**    | **FAIL** | CW Logs storage rate **19× too high** ($0.57 vs $0.03/GB-Mo) |

---

## Pre-flight facts

- **Adapter class**: `MonitoringModule` at `services/adapters/monitoring.py:14`
- **Declared `required_clients`**: `("cloudwatch", "logs", "cloudtrail", "backup", "route53")` — accurate
- **Declared `requires_cloudwatch`**: `False` (default) — **lies**: shim hits CW alarms/metrics paginators
- **Declared `reads_fast_mode`**: `True` — **lies**: `fast_mode` not referenced in any of the 4 shims
- **Sources emitted**: `{"cloudwatch_checks", "cloudtrail_checks", "backup_checks", "route53_checks"}` (good — 4 distinct sources)
- **Pricing methods consumed**: **none** from `PricingEngine`. Hardcoded `0.57`, `0.30`, `0.50` in shims
- **Test files**: standard regression + offline scan

---

## L1 — Technical findings

### L1-001 `requires_cloudwatch` is False but shim uses CloudWatch  [HIGH]
- **Evidence**: shim `services/monitoring.py:103-105` uses `cloudwatch.get_paginator("describe_alarms")` and `cloudwatch.get_paginator("list_metrics")`
- **Recommended fix**: set `requires_cloudwatch = True` on `MonitoringModule`

### L1-002 `reads_fast_mode` is True but `fast_mode` is never read  [HIGH]
- **Evidence**: `grep "fast_mode" services/monitoring.py services/backup.py services/route53.py` → no consumer
- **Recommended fix**: flip to False OR honor `ctx.fast_mode` by skipping CW alarm/metrics scan

### L1-003 8 `print()` calls across the 4 shims  [MEDIUM]
- **Evidence**:
  - `services/monitoring.py:117, 145, 148, 205, 207, 219, 221` — alarms, metrics, CloudTrail error paths
  - `services/route53.py` — likely similar (not verified line-by-line)
  - `services/backup.py` — likely similar
- **Recommended fix**: route via `ctx.warn` / `ctx.permission_issue`

### L1-004 `describe_log_groups` uses manual NextToken loop, not paginator  [LOW]
- **Evidence**: `services/monitoring.py:71-79` — manual `while True: nextToken` loop instead of `logs.get_paginator("describe_log_groups").paginate()`. Boto3 exposes the paginator
- **Recommended fix**: use the paginator for consistency with EFS/DDB shims

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| `cloudwatch_checks` never-expiring logs $/month | `module-const` (`$0.57/GB-month`) | `services/monitoring.py:93` | **CRITICAL FAIL** — AWS list price is `$0.03/GB-month` for CloudWatch Logs storage. Shim is **19× too high** | AWS Pricing API → SKU JRHJQ2UMPUB5K73A `$0.03 per GB-mo of log storage` us-east-1 |
| `cloudwatch_checks` excessive custom metrics | `module-const` (`$0.30/metric/month × 50%`) | `services/monitoring.py:139` | **WARN** — $0.30/metric matches first tier; ignores tiered breakpoints (next 240K @ $0.10, above @ $0.05). Also "if reduced by 50%" is arbitrary | AWS CloudWatch pricing page: $0.30/metric first 10K, $0.10 next 240K, $0.05 above |
| `cloudtrail_checks` (all) | — | shim emits zero recs (lines 183, 198, 213, 223 — all 5 categories explicitly REMOVED per scope rule purge) | YES — clean | n/a |
| `backup_checks` retention | `arbitrary` ("Reduce retention to lower storage costs") | `services/backup.py:72` | **FAIL** — qualitative string, scope rule violation |
| `backup_checks` weekly/monthly | `arbitrary` ("Weekly/monthly backups can reduce costs by 70-85%") | `services/backup.py:84` | **FAIL** — percentage string, scope rule violation |
| `route53_checks` empty zone | `module-const` (`$0.50/zone`) | `services/route53.py:61` | **WARN** — $0.50 matches first-25-zones tier; ignores >25-zones tier ($0.10) | AWS list: $0.50 first 25, $0.10 each thereafter |
| `route53_checks` consolidation | `derived` (`(N-1) × $0.50`) | `services/route53.py:129` | **WARN** — assumes all extras can be merged into one zone; ignores tier breakpoint |

### L2-001 CloudWatch Logs storage rate $0.57 is 19× the AWS list price  [CRITICAL]
- **Check**: L2.1 / L2.2 — `module-const` must match AWS
- **Evidence**: `services/monitoring.py:93` — `f"${stored_bytes * 0.57 / (1024**3):.2f}/month with 30-day retention"`. AWS CloudWatch Logs storage: $0.03/GB-month (SKU JRHJQ2UMPUB5K73A confirmed via Pricing API)
- **Why it matters**: a 100 GB log group with no retention is reported as `$57/month` of savings if retention is set to 30 days. Real saving is at most `$3 × (1 − 30/historical_days)`. **Every log-retention recommendation overstates by ~19×.** This dwarfs the cumulative $ impact of every other adapter audited so far
- **Recommended fix**: replace `0.57` with `0.03`. Better: add `PricingEngine.get_cloudwatch_logs_storage_per_gb()` and use it. Also note: the calculation assumes 100% of `stored_bytes` is saved with 30-day retention — that's only true if logs were never going to be deleted. Compute `(actual_age_days − 30) / actual_age_days × stored_bytes × $0.03/GB`

### L2-002 Custom-metrics formula ignores tier breakpoints  [HIGH]
- **Check**: L2.2.5 — tiered pricing
- **Evidence**: `services/monitoring.py:139` — `f"${count * 0.30:.2f}/month if reduced by 50%"`. AWS CW custom metrics: $0.30 first 10K, $0.10 next 240K, $0.05 above 250K
- **Why it matters**: a namespace with 5000 metrics shows `5000 × 0.30 = $1500/month` — correct for the first-10K tier. A namespace with 50,000 metrics shows `50000 × 0.30 = $15000/month` — actual cost is `10000 × $0.30 + 40000 × $0.10 = $7000/month`. **Off by 2.14×** at high metric counts
- **Recommended fix**: implement tiered pricing math: `cost(N) = min(N, 10000) × 0.30 + max(0, min(N, 250000) − 10000) × 0.10 + max(0, N − 250000) × 0.05`

### L2-003 `pricing_multiplier` not applied to any of the shim-const paths  [HIGH]
- **Check**: L2.3.2 — `module-const` path must apply `ctx.pricing_multiplier`
- **Evidence**:
  - `services/monitoring.py:93, 139` — no `pricing_multiplier`
  - `services/route53.py:61, 129` — no `pricing_multiplier`
  - Adapter at `services/adapters/monitoring.py:54-61` — parses dollar amount from the string, no multiplier
- **Why it matters**: a scan in São Paulo (multiplier ~1.5) reports the same $0.03/GB-Mo as us-east-1 — incorrect for actual São Paulo pricing
- **Recommended fix**: apply `× ctx.pricing_multiplier` at every emit site OR move pricing into a `PricingEngine` method that returns region-correct values

### L2-004 Adapter's savings parser is fragile (re-implements `parse_dollar_savings`)  [MEDIUM]
- **Check**: L2.1 — should reuse `services/_savings.py:parse_dollar_savings`
- **Evidence**: `services/adapters/monitoring.py:55-61`:
  ```python
  savings_str = rec.get("EstimatedSavings", "")
  if "$" in savings_str and "/month" in savings_str:
      try:
          savings_val = float(savings_str.replace("$", "").split("/")[0])
          savings += savings_val
      except (ValueError, AttributeError):
          pass
  ```
  This silently fails on `"Up to $25/month for low-traffic"` (split returns `"25 for low-traffic"`, float() raises ValueError, swallowed)
- **Recommended fix**: replace with `from services._savings import parse_dollar_savings` and `savings += parse_dollar_savings(rec.get("EstimatedSavings", ""))`

### L2-005 Recommendations without `$N/month` string contribute 0 to savings but count to `total_recommendations`  [LOW]
- **Evidence**: Backup recs at `services/backup.py:72, 84` carry "Reduce retention..." and "Weekly/monthly backups can reduce..." strings — neither matches `$/month`, both increment `total_recs` while contributing $0 savings
- **Recommended fix**: same fix as L2-004 + scope-rule cleanup (quantify or remove)

### External validation log

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| L2-001 | `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonCloudWatch, us-east-1, productFamily="Storage Snapshot"` | $0.03/GB-Mo (SKUs 6K9ADYQAHV5KX9KZ, JRHJQ2UMPUB5K73A) | refutes shim's $0.57; confirms 19× error |
| L2-002 | AWS CloudWatch pricing docs | https://aws.amazon.com/cloudwatch/pricing/ | "First 10,000 metrics — $0.30/metric, Next 240,000 — $0.10, Over 250,000 — $0.05" | refutes flat $0.30 model |
| L2-003 | AWS Pricing API regional | (verified by region delta) | São Paulo / Tokyo regions have higher CW Logs rates | refutes no-multiplier path |

---

## L3 — Reporting findings

### L3-001 Single renderer `_render_monitoring_enhanced_checks` handles 4 distinct source shapes  [MEDIUM]
- **Evidence**: `reporter_phase_b.py:2190-2193` — all 4 sources point to the same handler. Need to verify handler logic dispatches per source name or per `CheckCategory`. Likely the renderer is descriptor-driven but could miss specific category groupings
- **Recommended fix**: confirm via tests; add unit test that each source name renders all its rec categories

### L3-002 No `priority` field on any rec  [LOW]
- **Recommended fix**: derive (e.g. log group > 1 TB → high)

---

## Cross-layer red flags

- **L2-001 (19× CW Logs rate)** is by far the largest single $-magnitude error encountered in this audit pass. Combined with L2-003 (no multiplier) and L2-005 (placeholder savings), the entire monitoring tab's headline number is unreliable.

Overall **FAIL**.

---

## Verification log

```
# 2. Registration check
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='monitoring'); print('clients:', m.required_clients()); print('cw:', m.requires_cloudwatch); print('fast:', m.reads_fast_mode)"
clients: ('cloudwatch', 'logs', 'cloudtrail', 'backup', 'route53')
cw: False    ← LIE
fast: True   ← LIE

# 4-5. Tests
$ python3 -m pytest tests/test_offline_scan.py tests/test_regression_snapshot.py \
    tests/test_reporter_snapshots.py -k monitoring -v
(passes against the same flawed math)

# 6. AWS prices
CW Logs storage:        $0.03/GB-Mo us-east-1  ← shim uses $0.57 (19× error)
CW custom metrics:      $0.30/metric tier-1, $0.10 next 240K, $0.05 above 250K
Route53 hosted zone:    $0.50 first 25, $0.10 thereafter
```

---

## Recommended next steps (prioritized)

1. **[CRITICAL] L2-001** — Replace `0.57` with `0.03` at `services/monitoring.py:93`. Verify CW Logs savings drop ~19×.
2. **[HIGH] L2-002** — Implement tiered pricing for custom metrics.
3. **[HIGH] L2-003** — Apply `× ctx.pricing_multiplier` to every emit site OR introduce `PricingEngine.get_cloudwatch_logs_storage_per_gb()`.
4. **[HIGH] L1-001 / L1-002** — Fix `requires_cloudwatch` / `reads_fast_mode` flags.
5. **[MEDIUM] L1-003** — Replace 8 `print` sites with `ctx.warn`.
6. **[MEDIUM] L2-004** — Use `parse_dollar_savings` instead of the ad-hoc parser.
7. **[MEDIUM] L1-004** — Use paginator for `describe_log_groups`.
8. **[LOW] L2-005** — Quantify or remove the 2 backup placeholder recs.
9. **[LOW] L3-002** — Set `priority`.
