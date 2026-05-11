# Audit: ec2 Adapter (v4 — FINAL, all severities cleared)

**Adapter**: `services/adapters/ec2.py` (74 lines)
**Legacy shim**: `services/ec2.py` (~850 lines after fixes)
**ALL_MODULES index**: 0
**Renderer path**: Phase B — 4 dedicated handlers
**Date**: 2026-05-12
**Auditor**: automated re-audit (final pass over all 13 v2 findings)

## Verdict

| Layer | v1 | v2 | v3 | **v4** |
|-------|----|----|----|--------|
| L1 Technical   | WARN | WARN | WARN | **PASS** |
| L2 Calculation | FAIL | FAIL | CONDITIONAL-PASS | **PASS** |
| L3 Reporting   | WARN | WARN | CONDITIONAL-PASS | **PASS** |
| **Overall**    | FAIL | FAIL | CONDITIONAL-PASS | **PASS** |

All 13 findings raised in v2 are resolved. No open items for the EC2 adapter.

---

## Finding-by-finding closeout

### CRITICAL (cleared in commit d72be5c, re-verified in v3)

| ID | Title | Status | Verification |
|----|-------|--------|-------------|
| L2-002 | Compute Optimizer field path | ✅ FIXED | `compute_optimizer_savings` reads `recommendationOptions[rank=1].savingsOpportunity.estimatedMonthlySavings.value` — matches AWS docs |
| L2-004 | Arbitrary $50 percentage fallback | ✅ FIXED | `% → $50.0` branch deleted from `parse_dollar_savings`; 8 EC2 categories now emit real `$X.YY/month` via `_compute_ec2_savings` |

### HIGH (cleared in commit 79f0bdb, re-verified in v3)

| ID | Title | Status | Verification |
|----|-------|--------|-------------|
| L1-001 | `requires_cloudwatch=False` lie | ✅ FIXED | `EC2Module.requires_cloudwatch = True` |
| L2-005 | Hardcoded $0.10/GB EBS root volume | ✅ FIXED | Uses `ctx.pricing_engine.get_ebs_monthly_price_per_gb(volume_type)` with regional + type-aware pricing; `VolumeType` in rec payload |
| L2-006 | Duplicate `underutilized_instance_store` append | ✅ FIXED | Second append removed |
| L2-002 in EBS + RDS siblings | Same field-path pattern | ✅ FIXED | `_CO_OPTION_KEYS` covers all 4 documented arrays (EC2 / EBS / RDS instance / RDS storage) |
| L3-001 | Renderer drops Spot recs | ✅ FIXED | 3 Spot filters removed from `reporter_phase_b.py` |

### MEDIUM (cleared in commit a38faca)

| ID | Title | Status | Fix |
|----|-------|--------|-----|
| **L1-002** | `--fast` not honored | ✅ FIXED | `get_enhanced_ec2_checks(ctx, multiplier, fast_mode=False)` — short-circuits CloudWatch + AutoScaling. `get_advanced_ec2_checks` skips `describe_volumes` enrichment. Adapter plumbs `ctx.fast_mode`. |
| **L1-003** | Silent `except: pass` | ✅ FIXED | `describe_auto_scaling_instances` AccessDenied → `ctx.permission_issue`; other errors → `logger.debug`. `get_ec2_instance_count` returns 0 with `ctx.warn` or `ctx.permission_issue` based on error code. |
| **L2-001** | Test fixtures wrong schema | ✅ FIXED | `list_recommendations.json` + `stub_responses.py` now use flat `estimatedMonthlySavings: float` + sibling `currencyCode`, matching documented AWS API |
| **L2-007** | `services/adapters/CLAUDE.md` stale | ✅ FIXED | Live-Pricing table now lists `get_ec2_hourly_price × 730 × EC2_SAVINGS_FACTORS` for EC2; added "Consuming AWS Compute Optimizer" section; corrected the multiplier guidance |
| **L3-002** | `recs[0]` collapse hides per-instance data | ✅ FIXED | `_render_ec2_enhanced_checks` now renders InstanceType + AvgCPU + MaxCPU + EstimatedSavings inline per-`<li>` |

### LOW (cleared in commit a38faca)

| ID | Title | Status | Fix |
|----|-------|--------|-----|
| **L1-004** | `print()` in scan path | ✅ FIXED | All 11 prints in `services/ec2.py`, 1 in `services/adapters/ec2.py`, and 5 in `services/advisor.py` replaced with `logging` calls or routed through `ctx.warn` / `ctx.permission_issue`. `grep "print(" services/{adapters/ec2.py,ec2.py,advisor.py}` returns empty. |
| **L1-005** | `CPUCreditBalance` wrong Period | ✅ FIXED | Changed `Period=3600` → `Period=300` for `CPUCreditBalance` (AWS publishes at 5-min frequency only). `CPUUtilization` kept at 3600s. |
| **L2-003** | Opt-in placeholder key inconsistency | ✅ FIXED | New `_compute_optimizer_opt_in_rec(label, action)` helper in `services/advisor.py` emits `estimatedMonthlySavings: 0.0` (flat float, matching Cost Hub schema) instead of `EstimatedMonthlySavings: "Variable - up to N%"` (prose with capital E). |
| **L3-003** | Residual % strings in `get_auto_scaling_checks` | ✅ FIXED | 4 percentage-only `EstimatedSavings` strings rewritten to `$0.00/month - …` format (or `_compute_ec2_savings(…)` where instance_type was in scope for the "Oversized ASG Instances" check) |

---

## Verification

```
# Module probe
$ python3 -c "from services import ALL_MODULES; m = next(x for x in ALL_MODULES if x.key=='ec2'); print(m.requires_cloudwatch, m.reads_fast_mode, m.required_clients())"
True True ('ec2', 'compute-optimizer', 'autoscaling')

# Print-free
$ grep -n "print(" services/adapters/ec2.py services/ec2.py services/advisor.py
(empty)

# Fixture schema (post-fix)
$ python3 -c "import json; d=json.load(open('tests/fixtures/recorded_aws_responses/cost-optimization-hub/list_recommendations.json'))['items'][0]; print(type(d['estimatedMonthlySavings']).__name__, '=', d['estimatedMonthlySavings'], '|', 'currencyCode:', d.get('currencyCode'))"
float = 150.0 | currencyCode: USD

# --fast plumbing
$ grep -n "fast_mode" services/adapters/ec2.py services/ec2.py | head
services/adapters/ec2.py:23: reads_fast_mode: bool = True
services/adapters/ec2.py:48: get_enhanced_ec2_checks(ctx, ctx.pricing_multiplier, ctx.fast_mode)
services/adapters/ec2.py:50: get_advanced_ec2_checks(ctx, ctx.pricing_multiplier, ctx.fast_mode)
services/ec2.py:146: fast_mode: bool = False,
services/ec2.py:156: cloudwatch = ctx.client("cloudwatch", ctx.region) if not fast_mode else None
services/ec2.py:157: autoscaling = ctx.client("autoscaling", ctx.region) if not fast_mode else None

# Lint
$ ruff check services/adapters/ec2.py services/advisor.py services/_savings.py services/ec2.py
All checks passed!

# Full regression gate
$ pytest tests/test_offline_scan.py tests/test_regression_snapshot.py tests/test_reporter_snapshots.py tests/test_result_builder.py tests/test_orchestrator.py tests/test_pricing_engine.py
152 passed, 1 skipped in 5.06s
```

---

## External validation (cumulative across v1→v4)

| Round | Tool | Used for |
|---|---|---|
| v2/v3 | `mcp__aws-pricing-mcp-server__get_pricing` | gp2 us-east-1 = $0.10/GB-mo, gp3 = $0.08/GB-mo, t3.nano/small/m5.large/m6i.32xlarge hourly prices — quantified L2-004 error magnitudes (13×–89× off) and verified L2-005 fix |
| v2/v3 | `WebFetch` boto3 docs | Confirmed real AWS schema for `cost-optimization-hub.list_recommendations` (flat float), `get_recommendation` (flat float), `compute-optimizer.get_ec2_instance_recommendations` (nested in `recommendationOptions[].savingsOpportunity`), `get_ebs_volume_recommendations` (nested in `volumeRecommendationOptions`), `get_rds_database_recommendations` (split into `instanceRecommendationOptions` + `storageRecommendationOptions`) |
| v2 | `WebSearch` | AWS Pricing Calculator standardizes on 730 hours/month (365×24/12) |
| v2 | `mcp__aws-cloudwatch-mcp-server__get_metric_metadata` | Confirmed `AWS/EC2 CPUUtilization` recommended stats and `AWS/EC2 CPUCreditBalance` 5-min publication frequency — drove L1-005 |

---

## Source-classification table (final state)

Every EC2 recommendation now traces to a real cost source:

| Recommendation category | Source class | Backing |
|---|---|---|
| Cost Optimization Hub recs | `aws-api` | AWS Cost Hub API (flat float, defensive coercion via fixture parity) |
| Compute Optimizer recs | `aws-api` | `compute_optimizer_savings` reads nested AWS schema |
| Idle Instances | `live` | `_compute_ec2_savings(…, "Idle Instances", factor=1.00)` |
| Rightsizing Opportunities | `live` | `_compute_ec2_savings(…, factor=0.40)` |
| Burstable Instance Optimization | `live` | `_compute_ec2_savings(…, factor=0.30)` |
| Previous Generation Migration | `live` | `_compute_ec2_savings(…, factor=0.10)` |
| Dedicated Hosts | `live` | `_compute_ec2_savings(…, factor=0.30)` |
| Cron Job Instances | `live` | `_compute_ec2_savings(…, factor=0.85)` |
| Batch Job Instances | `live` | `_compute_ec2_savings(…, factor=0.75)` |
| Underutilized Instance Store | `live` | `_compute_ec2_savings(…, factor=0.15)` |
| Oversized Root Volumes | `live` | `get_ebs_monthly_price_per_gb(volume_type) × (size_gb − 20)` |
| Auto Scaling Missing / Monitoring-Only / Stopped Instances / Monitoring Required | `informational` | Honest `$0.00/month` with explanatory follow-up |

No `arbitrary` or unbacked `module-const` classifications remain.

---

## Cross-layer red flags

**None remaining.** Every dollar in `total_monthly_savings` is now traceable to either an AWS API call or a documented per-check factor applied to a live instance price.

---

## Closeout

The EC2 adapter section is **complete**. CRITICAL, HIGH, MEDIUM, and LOW findings are all resolved with external validation against AWS Pricing API and boto3 documentation. The sibling adapters (EBS, RDS) inherited the L2-002 schema fix.

If the same audit methodology is run against the other 36 service adapters, the same patterns are likely to recur — chiefly:

- `parse_dollar_savings` still returning $0 for many sources (previously $50) — adapters that emitted percentage strings should adopt the live-pricing pattern shown in `services/ec2.py::_compute_ec2_savings`.
- `compute_optimizer_savings` is wired into EC2/EBS/RDS only — Lambda and any future Compute Optimizer integration should adopt it too.
- The `_compute_optimizer_opt_in_rec` helper in `services/advisor.py` is the canonical pattern for opt-in placeholders.

Recommended next step: pick the highest-spend service after EC2 (likely **RDS**, **S3**, or **Lambda**) and run the same audit prompt.
