# Audit: ec2 Adapter (v2 — externally validated)

**Adapter**: `services/adapters/ec2.py` (72 lines)
**Legacy shim**: `services/ec2.py` (773 lines)
**ALL_MODULES index**: 0 (first registered)
**Renderer path**: Phase B — 4 dedicated handlers (`enhanced_checks`, `cost_optimization_hub`, `compute_optimizer`, `advanced_ec2_checks`)
**Date**: 2026-05-11
**Auditor**: automated (multi-layer audit prompt v2, external-validation enforced)

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN**   | 5 (1 HIGH, 2 MEDIUM, 2 LOW) |
| L2 Calculation | **FAIL**   | 7 (2 CRITICAL, 2 HIGH, 2 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN**   | 3 (1 HIGH, 2 MEDIUM) |
| **Overall**    | **FAIL**   | 15 findings |

`FAIL` driver: AWS Compute Optimizer savings are read from the wrong field path (L2-002 confirmed via AWS docs — top-level `estimatedMonthlySavings` does **not** exist), and percentage-only `EstimatedSavings` strings inject an arbitrary flat $50 per recommendation (L2-004 confirmed via AWS Pricing API — $50 is 13× too high for a t3.nano and 89× too low for an m6i.32xlarge).

---

## Pre-flight facts

- **Adapter class**: `EC2Module` at `services/adapters/ec2.py:14`
- **Required boto3 clients**: `("ec2", "compute-optimizer", "autoscaling")`
- **`requires_cloudwatch`**: `False` ← incorrect, see L1-001
- **`reads_fast_mode`**: `True` ← but unused downstream, see L1-002
- **CLI alias resolution**: `resolve_cli_keys(ALL_MODULES, {'ec2'}, None) → {'ec2'}` (PASS)
- **Sources emitted**: `cost_optimization_hub`, `compute_optimizer`, `enhanced_checks`, `advanced_ec2_checks`
- **Pricing methods consumed (from `PricingEngine`)**: **none** ← see L2-007
- **Extras emitted**: `instance_count`
- **`stat_cards`**: empty tuple (adapter declares none)
- **`optimization_descriptions`**: not set (None)
- **Test files exercising EC2**: `tests/test_offline_scan.py:28`, `tests/test_regression_snapshot.py:43`, `tests/test_result_builder.py:19,82`, `tests/test_reporter_snapshots.py:40`
- **Golden savings snapshot**: `tests/fixtures/reporter_snapshots/savings/ec2.txt` = `387.500000`

---

## External validation evidence (used throughout L2)

| Tool | Call | Result | Used by |
|---|---|---|---|
| `mcp__aws-pricing-mcp-server__get_pricing_service_codes` | (no filter) | Confirms `AmazonEC2` and `AWSComputeOptimizer` are canonical codes | L2 source classification |
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2 / us-east-1`, filter `volumeApiName=gp2 + productFamily=Storage` | gp2 = **$0.10/GB-month** in us-east-1 (matches hardcoded constant exactly) | L2-005 |
| `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2 / us-east-1`, filter `instanceType ANY_OF [t3.nano, t3.small, m5.large, m6i.32xlarge] + OS=Linux` | t3.nano=$0.0052/h, t3.small=$0.0208/h, m5.large=$0.096/h, m6i.32xlarge=$6.144/h | L2-004 (proves $50 fixed is wrong by factors of 13× and 89×) |
| `WebFetch` boto3 ref → Cost Optimization Hub `list_recommendations` | docs.aws.amazon.com | `estimatedMonthlySavings` is **flat `float`** (`123.0`), `currencyCode` is **separate** sibling field — **NOT** a `{currency, value}` dict | L2-001 (refutes initial draft) |
| `WebFetch` boto3 ref → Cost Optimization Hub `get_recommendation` | docs.aws.amazon.com | Same — flat float at top level | L2-001 |
| `WebFetch` boto3 ref → Compute Optimizer `get_ec2_instance_recommendations` | docs.aws.amazon.com | `estimatedMonthlySavings` does **NOT** exist at top level of `instanceRecommendation`; only inside `recommendationOptions[N].savingsOpportunity.estimatedMonthlySavings` as `{currency, value}` dict | L2-002 (confirms CRITICAL) |
| `WebSearch` → AWS Pricing Calculator FAQ | calculator.aws | AWS standardizes on **730 hours/month** (365×24/12). Confirmed. | L2.2.2 baseline |
| `mcp__aws-cloudwatch-mcp-server__get_metric_metadata` | `AWS/EC2 / CPUUtilization` | Standard CloudWatch metric, Unit=Percent, recommended stats Average/Min/Max | L1-001 |
| `mcp__aws-cloudwatch-mcp-server__get_metric_metadata` | `AWS/EC2 / CPUCreditBalance` | "CPU credit metrics are available at a 5-minute frequency only" | L1-005 (new) |

---

## L1 — Technical findings

### L1-001  `requires_cloudwatch=False` contradicts adapter behavior  [HIGH]

- **Check**: L1.1.5
- **Evidence**:
  - `services/_base.py:24` — `requires_cloudwatch: bool = False` (default, inherited)
  - `services/adapters/ec2.py:14` — `EC2Module` does **not** override
  - `services/ec2.py:108,139,277,292` — `ctx.client("cloudwatch", ctx.region)` + three `cloudwatch.get_metric_statistics` calls inside `get_enhanced_ec2_checks`, which the adapter calls unconditionally at `services/adapters/ec2.py:44`
- **External**: `mcp__aws-cloudwatch-mcp-server__get_metric_metadata("AWS/EC2","CPUUtilization")` confirms this is a CloudWatch metric.
- **Why it matters**: Orchestrator and any future planner treat `requires_cloudwatch=False` as "no CloudWatch needed". Affects IAM gating, `--fast` planning, rate-limit budgeting.
- **Fix**: Set `requires_cloudwatch: bool = True` on `EC2Module`.

### L1-002  `--fast` ignored by EC2 enhanced & advanced checks  [MEDIUM]

- **Check**: L1.1.6
- **Evidence**:
  - `services/adapters/ec2.py:20` — `reads_fast_mode: bool = True`
  - `services/adapters/ec2.py:44` — `get_enhanced_ec2_checks(ctx, ctx.pricing_multiplier)` does **not** receive `fast_mode`
  - `services/adapters/ec2.py:46` — `get_advanced_ec2_checks(ctx, ctx.pricing_multiplier, ctx.fast_mode)` passes the flag, but `services/ec2.py:579-773` never reads it
- **Why it matters**: `--fast` is documented as skipping CloudWatch. EC2 ignores that promise.
- **Fix**: Honor `fast_mode` (skip CloudWatch calls and `describe_volumes` enrichment) or set `reads_fast_mode = False`.

### L1-003  Silent exception swallowing in legacy shim  [MEDIUM]

- **Check**: L1.2.2
- **Evidence**:
  - `services/ec2.py:244-245` — `except Exception: pass` after `describe_auto_scaling_instances`
  - `services/ec2.py:319, 345` — bare `pass` early-exits
  - `services/ec2.py:365-384` — `except Exception:` falls through to a "monitoring required" rec but logs nothing
- **Why it matters**: AccessDenied on `describe_auto_scaling_instances` produces zero ASG recs with no `ctx._warnings` or `ctx._permission_issues` entry. Operators cannot distinguish "no ASGs" from "no permission".
- **Fix**: Route `ClientError` to `ctx.permission_issue` / `ctx.warn`. Replace bare `pass` with explicit `continue` + log.

### L1-004  `print()` statements pollute stdout  [LOW]

- **Check**: L1.5 hygiene
- **Evidence**: `services/adapters/ec2.py:40` plus 11 prints in `services/ec2.py` (lines 27, 39, 41, 43, 46, 79, 403, 544, 566, 569, 695).
- **Why it matters**: Breaks `cli.py … | jq`. Mixes log channels.
- **Fix**: Use `logging.getLogger(__name__)` for diagnostics; route operator-visible messages through `ctx.warn`.

### L1-005  `CPUCreditBalance` queried at `Period=3600` despite 5-minute publication frequency  [LOW]

- **Check**: L1.2 hygiene (CloudWatch query correctness)
- **Evidence**:
  - `services/ec2.py:288` — `Period=3600` for `CPUCreditBalance`
  - External (`get_metric_metadata`): _"CPU credit metrics are available at a 5-minute frequency only."_
- **Why it matters**: 3600s period works (CloudWatch averages the 5-minute samples), but for credit exhaustion detection a 300s period would surface short credit droughts that hourly averaging smooths over.
- **Fix**: Use `Period=300, Statistics=["Minimum"]` for `CPUCreditBalance` queries. `Average` smooths away the very dip the check is trying to detect.

---

## L2 — Calculation findings

### Source classification table

| Recommendation type | Source key | Source class | Evidence | Acceptable? | External validation |
|---|---|---|---|---|---|
| Cost Optimization Hub recs (EC2 bucket) | `cost_optimization_hub` | `aws-api` (real API) | `services/adapters/ec2.py:50` | **YES** (adapter correct vs documented API) | `WebFetch list_recommendations` + `get_recommendation` → flat float — adapter's `rec.get("estimatedMonthlySavings", 0)` works for real API |
| Compute Optimizer recs | `compute_optimizer` | **bug: wrong field path** | `services/adapters/ec2.py:51-53` | **NO** | `WebFetch get_ec2_instance_recommendations` → no top-level `estimatedMonthlySavings`; only nested under `recommendationOptions[N].savingsOpportunity.estimatedMonthlySavings` as `{currency, value}` |
| Compute Optimizer opt-in placeholder | `compute_optimizer` | `module-const string` | `services/advisor.py:87` — uses key `EstimatedMonthlySavings` (capital E) which is NEVER read by the adapter | WARN | n/a — internal data flow |
| Enhanced "Idle Instances" | `enhanced_checks` | `parsed → $50 fallback` | `services/ec2.py:169` | **NO — arbitrary** | `get_pricing(t3.nano)=$3.80/mo, m5.large=$70.08/mo, m6i.32xlarge=$4485/mo` — $50 is wrong for every plausible instance |
| Enhanced "Rightsizing Opportunities" | `enhanced_checks` | `parsed → $50 fallback` | `services/ec2.py:187` | **NO** | same as above |
| Enhanced "Burstable Credits" | `enhanced_checks` | `parsed → $50 fallback` | `services/ec2.py:329` | **NO** | same as above |
| Enhanced "Auto Scaling Missing" | `enhanced_checks` | `parsed → 0` | `services/ec2.py:239` ("Improved availability and potential Spot instance usage") | WARN (silent zero) | n/a |
| Enhanced "Previous Generation Migration" | `enhanced_checks` | `parsed → 0` | `services/ec2.py:257` | WARN | n/a |
| Enhanced "Dedicated Hosts" | `enhanced_checks` | `parsed → 0` | `services/ec2.py:268` | WARN | n/a |
| Enhanced "Stopped Instances" | `enhanced_checks` | `parsed → 0` | `services/ec2.py:398` | WARN | n/a |
| Advanced "Cron Job Instances" | `advanced_ec2_checks` | `parsed → $50 fallback` | `services/ec2.py:631` | **NO** | same |
| Advanced "Batch Job Instances" | `advanced_ec2_checks` | `parsed → $50 fallback` | `services/ec2.py:750` | **NO** | same |
| Advanced "Underutilized Instance Store" | `advanced_ec2_checks` | `parsed → $50 × 2` | `services/ec2.py:727 + 760` (duplicate append) | **NO** | n/a (duplicate counted twice) |
| Advanced "Oversized Root Volumes" | `advanced_ec2_checks` | `module-const` (us-east-1 gp2 price) | `services/ec2.py:686` — `(size_gb - 20) * 0.10` | WARN (region-blind) | `get_pricing(gp2, us-east-1) = $0.10/GB-month` — matches us-east-1 only |
| Advanced "Monitoring-Only Instances" | `advanced_ec2_checks` | `parsed → 0` | `services/ec2.py:653` | WARN | n/a |

### External validation log

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| L2-001 | `WebFetch` | `list_recommendations` boto3 ref | `estimatedMonthlySavings: float (123.0)` + separate `currencyCode: 'string'` | **Refutes** my draft claim that real API returns a dict; the test fixture is wrong, not the adapter |
| L2-001 | `WebFetch` | `get_recommendation` boto3 ref | `estimatedMonthlySavings: float (123.0)` at top level | **Confirms** flat float for real API |
| L2-002 | `WebFetch` | `get_ec2_instance_recommendations` boto3 ref | `instanceRecommendation` top-level fields list — **no** `estimatedMonthlySavings`. Only inside `recommendationOptions[N].savingsOpportunity.estimatedMonthlySavings = {currency, value}` | **Confirms CRITICAL** — adapter reads non-existent field |
| L2-004 | `get_pricing` | EC2 instance prices us-east-1 | t3.nano=$3.80/mo, t3.small=$15.18/mo, m5.large=$70.08/mo, m6i.32xlarge=$4485.12/mo (730h) | **Confirms CRITICAL** — flat $50 is wrong by 13×–89× depending on instance |
| L2-005 | `get_pricing` | gp2 us-east-1 | `$0.10/GB-month` exactly | **Confirms** hardcoded constant matches us-east-1 ONLY; non-us-east-1 regions wrong |
| L2.2.2 | `WebSearch` | AWS Pricing Calculator FAQ | 730 hours/month standard (365×24/12) | **Confirms** scanner uses correct hour basis where 730 is applied |

### L2-001  Test fixtures have wrong schema for `estimatedMonthlySavings`  [MEDIUM] ⬇️ demoted

- **Check**: L2.1 source attribution + L2.6.1 reproducibility
- **Evidence**:
  - `tests/fixtures/recorded_aws_responses/cost-optimization-hub/list_recommendations.json:8-11` and similar — encode `"estimatedMonthlySavings": {"currency": "USD", "value": 150.0}` (dict)
  - `tests/aws_stubs/stub_responses.py:44` — same dict shape `{"currency": "USD", "value": 0.0}` in the stub
  - **External (AWS docs)**: real API returns flat float `'estimatedMonthlySavings': 123.0` with `'currencyCode': 'USD'` as a sibling. The fixture/stub disagree with the API.
- **Why it matters**: Today the bug is dormant because no fixture entry has `currentResourceType == "Ec2Instance"` (orchestrator's `type_map` skips SavingsPlans/Unknown — verified at `core/scan_orchestrator.py:66-77`). If a future fixture adds an EC2-typed item, every adapter using `rec.get("estimatedMonthlySavings", 0)` would crash on `sum(dict, float)` and `safe_scan` would zero the entire service.
- **Note**: Originally drafted as CRITICAL claiming the **adapter** had a schema mismatch. AWS docs (`WebFetch list_recommendations` + `get_recommendation`) **refute** that — the adapter is correct for the real API. Severity downgraded from CRITICAL → MEDIUM and reframed as a fixture bug.
- **Fix**: Update fixtures + stub to match the documented API:
  ```json
  "estimatedMonthlySavings": 150.0,
  "currencyCode": "USD"
  ```

### L2-002  Compute Optimizer savings read from a field AWS does not return  [CRITICAL]

- **Check**: L2.1 source attribution
- **Evidence**:
  - `services/adapters/ec2.py:51-53` — `sum(rec.get("estimatedMonthlySavings", 0) for rec in co_recs)`
  - **External (boto3 ref for `get_ec2_instance_recommendations`)**: every field on `instanceRecommendation` is enumerated; there is **no top-level `estimatedMonthlySavings`**. The value lives at `recommendationOptions[N].savingsOpportunity.estimatedMonthlySavings = {'currency': 'USD'|'CNY', 'value': 123.0}`. Quote: _"`estimatedMonthlySavings` does NOT appear at the top level of `instanceRecommendation`. It only appears inside `recommendationOptions`, specifically within the `savingsOpportunity` and `savingsOpportunityAfterDiscounts` objects."_
- **Why it matters**: Every real Compute Optimizer EC2 finding contributes **$0** to total savings. The number AWS spent compute time calculating is silently discarded. Hidden in tests because the fixture has `instanceRecommendations: []`.
- **Fix**:
  ```python
  def _co_ec2_savings(rec: dict) -> float:
      options = rec.get("recommendationOptions") or []
      # Take the rank-1 (most-recommended) option, fall back to first
      best = next((o for o in options if o.get("rank") == 1), options[0] if options else {})
      ems = best.get("savingsOpportunity", {}).get("estimatedMonthlySavings", {})
      return float(ems.get("value", 0.0)) if isinstance(ems, dict) else float(ems or 0.0)
  co_total = sum(_co_ec2_savings(r) for r in co_recs)
  ```
  Apply the same fix to `services/adapters/ebs.py:66`, `services/adapters/rds.py:63`, `services/adapters/lambda_svc.py` Compute Optimizer paths.

### L2-003  Compute Optimizer opt-in placeholder uses inconsistent field key  [LOW]

- **Check**: L2.1
- **Evidence**: `services/advisor.py:82-92` — synthetic rec uses key `"EstimatedMonthlySavings"` (capital E, a **string**), while real CO recs would (if read correctly) use `estimatedMonthlySavings` (lowercase). The adapter only reads lowercase, so the opt-in placeholder contributes $0 to totals — but the recommendation still renders.
- **Why it matters**: Inconsistency invites future bugs. The placeholder shows in HTML with no dollar value rather than "Compute Optimizer is disabled" being surfaced as an `optimization_status` flag.
- **Fix**: Surface CO disabled-state via `ctx.warn` or a typed `extras["compute_optimizer_status"]` field, not as a fake recommendation.

### L2-004  Percentage-only `EstimatedSavings` strings produce arbitrary flat $50  [CRITICAL]

- **Check**: L2.1 (`arbitrary`), L2.3.3 (`pricing_multiplier` not applied to fallback)
- **Evidence**:
  - `services/_savings.py:19-20`:
    ```python
    if "%" in savings_str:
        return 50.0
    ```
  - 6 EC2 recommendation categories emit percentage-only strings (table above): idle, rightsizing, burstable, cron, batch, instance-store.
  - **External (AWS Pricing API)**: real EC2 instance prices in us-east-1 (Linux on-demand, 730 h/mo):
    | Instance | Hourly | Monthly | $50 wrong by |
    |---|---|---|---|
    | `t3.nano` | $0.0052 | **$3.80** | +1216 % too high |
    | `t3.small` | $0.0208 | **$15.18** | +229 % too high |
    | `m5.large` | $0.096 | **$70.08** | −29 % too low |
    | `m6i.32xlarge` | $6.144 | **$4485.12** | −98.9 % too low |
  - The $50 is not multiplied by `ctx.pricing_multiplier`, so non-us-east-1 regions are doubly wrong.
- **Why it matters**: On a 100-instance scan, the flat-fallback math can be off by `100 × (real_cost − 50)` dollars. The headline `total_monthly_savings` reported to the user is the **least** correlated with reality of all scanner outputs.
- **Fix**:
  1. Delete `if "%" in savings_str: return 50.0` from `services/_savings.py`. Return `0.0` instead.
  2. Compute real savings inline:
     ```python
     hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type, os="Linux")
     monthly = hourly * 730
     factors = {"Idle Instances": 1.0, "Rightsizing Opportunities": 0.40,
                "Burstable Instance Optimization": 0.30, "Previous Generation Migration": 0.10,
                "Cron Job Instances": 0.85, "Batch Job Instances": 0.75}
     savings = monthly * factors[category]
     rec["EstimatedSavings"] = f"${savings:.2f}/month"
     ```
  3. Move the heuristic factors into a single `EC2_SAVINGS_FACTORS` constant module-local so the audit trail is one diff away.

### L2-005  "Oversized Root Volumes" hardcodes us-east-1 gp2 price; ignores PricingEngine  [HIGH]

- **Check**: L2.1 `module-const`, L2.3.2 multiplier missing
- **Evidence**:
  - `services/ec2.py:686`:
    ```python
    "EstimatedSavings": f"${(size_gb - 20) * 0.10:.2f}/month if reduced to 20GB + separate data volume"
    ```
  - **External (`get_pricing` AmazonEC2 us-east-1 volumeApiName=gp2)**: `$0.10/GB-month` exactly — constant is correct for us-east-1.
  - But `core/pricing_engine.py:172-184` exposes `get_ebs_monthly_price_per_gb("gp2")` that returns the **region-correct** price.
  - No `* ctx.pricing_multiplier`, so non-us-east-1 regions get the us-east-1 number.
  - Volume-type-blind: assumes gp2; gp3 root volumes ($0.08/GB-mo) would compute the wrong savings; io1/io2 ($0.125/GB-mo plus IOPS) would also be off.
- **Why it matters**: The adapter advertises a "Live Pricing" approach in `services/adapters/CLAUDE.md` for EC2, but this path is the opposite — drift risk + region-blind + volume-type-blind.
- **Fix**: In `services/ec2.py:670` the `volume` dict is already in scope; use:
  ```python
  vt = volume.get("VolumeType", "gp2")
  rate = ctx.pricing_engine.get_ebs_monthly_price_per_gb(vt)
  monthly_savings = max(size_gb - 20, 0) * rate
  ```

### L2-006  "Underutilized Instance Store" double-counted for batch instances  [HIGH]

- **Check**: L2.5.2 no double-counting, L2.5.3 per-source vs total invariant
- **Evidence**: `services/ec2.py:744-763` — for an instance whose name matches batch/job/worker **and** whose family is `m5d/c5d/r5d/i3/i4i`, both `batch_job_instances` and `underutilized_instance_store` are appended, then `underutilized_instance_store` is **appended a second time** at lines 754-763.
- **Why it matters**: Same instance contributes `$50 (batch) + $50 (store) + $0 (store dupe)` = `$100` while the real saving for the resource is at most one of those checks. `total_recommendations` overstates by 1 per matching instance.
- **Fix**: Remove `services/ec2.py:754-763`. Add unit test:
  ```python
  assert sum(1 for r in advanced_recs if r["CheckCategory"] == "Underutilized Instance Store" and r["InstanceId"] == iid) <= 1
  ```

### L2-007  No PricingEngine integration despite CLAUDE.md claim  [MEDIUM]

- **Check**: L2.1 "live" preference
- **Evidence**:
  - `grep -n "ctx.pricing_engine" services/adapters/ec2.py services/ec2.py` → 0 matches
  - `services/adapters/CLAUDE.md` (pre-cleanup version) listed EC2 as one of the 19 "Live Pricing" adapters using `get_ec2_instance_monthly_price`. Untrue.
  - `core/pricing_engine.py:248-260` exposes `get_ec2_hourly_price(instance_type, os)`; `tests/test_pricing_engine.py:131-133` validates `0.096` for m5.large — i.e. the live path is built and tested but unused.
- **Why it matters**: Documentation drift. EC2 is the largest spend category for most customers; reporting % strings instead of live prices means the dollar headline is the least trustworthy number in the report.
- **Fix**: After applying L2-004 fix, EC2 actually uses PricingEngine. Then update `services/adapters/CLAUDE.md` row to truthfully describe EC2's pricing source.

---

## L3 — Reporting findings

### Field-presence table

| Source | Renderer | Required fields | Missing fields | Notes |
|---|---|---|---|---|
| `enhanced_checks` | `_render_ec2_enhanced_checks` (`reporter_phase_b.py:23`) | `InstanceId`, `ImageId`/`AllocationId`/`ResourceId`, `Name`, `CheckCategory`, `Recommendation`, `EstimatedSavings`, optional `InstanceType` | none | Silently drops "Spot"-categorized recs (line 46) and "spot instance" recs (line 48) — see L3-001 |
| `cost_optimization_hub` | `_render_ec2_cost_hub` (`reporter_phase_b.py:77`) | `actionType`, `resourceId`, `currentResourceDetails.ec2Instance.configuration.instance.type`, `recommendedResourceDetails.ec2Instance...type`, `estimatedMonthlySavings` | nested paths may be missing per action type | `f"${savings:.2f}"` formatting at line 113/134 assumes a number; safe given real API returns float |
| `compute_optimizer` | `_render_ec2_compute_optimizer` (`reporter_phase_b.py:141`) | `instanceArn`, `instanceName`, `finding`, `currentInstanceType`, `recommendationOptions[0].instanceType`, `recommendationOptions[0].estimatedMonthlySavings` | renderer uses **correct** nested path while adapter aggregation uses wrong top-level path | L2-002 mismatch |
| `advanced_ec2_checks` | `_render_ec2_advanced_checks` (`reporter_phase_b.py:1557`) | `InstanceId`, `InstanceType`, `Name`, `Recommendation`, `EstimatedSavings`, `CheckCategory` | none | |

### L3-001  Renderer drops "Spot" recommendations the adapter emitted  [HIGH]

- **Check**: L3.2.1 (every emitted source is rendered)
- **Evidence**:
  - `reporter_phase_b.py:46-49`:
    ```python
    if "CheckCategory" in rec and "Spot" in rec.get("CheckCategory", ""): continue
    if "Recommendation" in rec and "spot instance" in rec.get("Recommendation", "").lower(): continue
    ```
  - `services/ec2.py:239` — `"Improved availability and potential Spot instance usage"` is emitted by `auto_scaling_missing`. Contains "Spot instance" → **dropped before HTML rendering**.
- **Why it matters**: Rec counts toward `total_recommendations` and `total_monthly_savings` (parses to $0 but counted) yet does not appear in HTML. Headline number and the visible list go out of sync.
- **Fix**: Pick one source of truth — either drop at adapter (don't emit) or drop at renderer, not both. Recommended: emit it, render it, let the user see it.

### L3-002  Phase B handler collapses to `recs[0]` per category  [MEDIUM]

- **Check**: L3.2.2
- **Evidence**: `reporter_phase_b.py:62-64` — category-level `<h5>` followed by `recs[0].get("Recommendation", ...)` and `recs[0].get("EstimatedSavings", ...)`. Per-instance `AvgCPU`/`MaxCPU` from `services/ec2.py:160-161,177-178` are never shown.
- **Why it matters**: Per-instance specifics are lost in the HTML; users see only the first instance's text repeated for all.
- **Fix**: Render per-instance specifics inside the `<li>` element instead of collapsing.

### L3-003  `EstimatedSavings` format inconsistency breaks headline ↔ table parity  [MEDIUM]

- **Check**: L3.3.1
- **Evidence**:
  - `parse_dollar_savings` expects `\$(\d+[\d,]*\.?\d*)`.
  - `services/ec2.py:686` emits `"${X.YY}/month if reduced..."` — parses correctly.
  - `services/ec2.py:169,187,257,268,329,398,471,494,515,538,560,631,653,727,750,760` — emit percentage-only strings that produce either $0 or $50 fallback. None display the real per-rec dollar.
  - Headline `total_monthly_savings` cannot be reconstructed by summing the visible per-card `EstimatedSavings` strings — they're prose, not dollars.
- **Why it matters**: Violates principle that headline savings = sum(visible per-card savings).
- **Fix**: After L2-004 is fixed, every `EstimatedSavings` becomes `f"${value:.2f}/month"`. Headline ↔ table parity restored.

---

## Cross-layer red flags

1. **Untraceable dollars in `total_monthly_savings`** — L2-004 ($50 per percentage-only check) and L2-005 (region-blind $0.10/GB). Triggers cross-layer red-flag #2 ("dollar amount the adapter cannot trace to a source"). External validation via AWS Pricing API quantifies the error magnitude (13×–89× off).
2. **Field path Compute Optimizer never returns** — L2-002. External validation via boto3 docs proves the field is not at top level.

No bypass of `ClientRegistry`. No cross-account ARNs. No writes to AWS. No `ALL_MODULES` mutation.

---

## Verification log

```
# 1. Static checks
$ ruff check services/adapters/ec2.py services/ec2.py
All checks passed!

$ python3 -m mypy services/adapters/ec2.py
(success — 18 mypy errors exist in other adapters, none in ec2.py)

# 2. Registration check
idx: 0
class: EC2Module
aliases: ('ec2',)
clients: ('ec2', 'compute-optimizer', 'autoscaling')
cw: False              ← MISMATCH (see L1-001)
fast_mode: True        ← unused (see L1-002)
stat_cards: ()

# 3. CLI resolution
resolve_cli_keys({'ec2'}) → {'ec2'}

# 4. parse_dollar_savings empirical behavior
'30-50% cost reduction with proper rightsizing'           → 50.0
'Up to 100% if terminated, 50-70% if downsized'           → 50.0
'Variable - up to 25% on EC2 instances'                   → 50.0
'Potential cost reduction with better performance per dollar' → 0.0
'EBS storage costs only (compute already saved)'          → 0.0
'$15.20/month'                                            → 15.2

# 5. AWS Pricing API ground-truth (us-east-1, Linux, Shared tenancy)
gp2 EBS storage:   $0.1000/GB-month
t3.nano:           $0.0052/hr → $3.80/month
t3.small:          $0.0208/hr → $15.18/month   ← matches golden $15.20 (rounding)
m5.large:          $0.0960/hr → $70.08/month
m6i.32xlarge:      $6.1440/hr → $4485.12/month

# 6. boto3 ref (AWS docs)
cost-optimization-hub.list_recommendations  →  estimatedMonthlySavings: float
cost-optimization-hub.get_recommendation    →  estimatedMonthlySavings: float
compute-optimizer.get_ec2_instance_recommendations
    → instanceRecommendation has NO top-level estimatedMonthlySavings
    → estimatedMonthlySavings lives in recommendationOptions[N].savingsOpportunity (dict {currency, value})

# 7. CloudWatch metric metadata
AWS/EC2 CPUUtilization     → standard metric, recommended Average/Min/Max
AWS/EC2 CPUCreditBalance   → 5-minute frequency only

# 8. Regression gate
$ pytest tests/test_offline_scan.py tests/test_regression_snapshot.py tests/test_reporter_snapshots.py
126 passed in 9.50s
```

Interpretation: 126 tests pass but **none** exercise L2-002, L2-004, L2-005, or L2-006 against the real AWS schemas. The Compute Optimizer fixture is empty, the Cost Hub fixture has no `Ec2Instance`-typed rows, and the golden snapshot `387.500000` simply pins the broken arithmetic in place.

---

## Recommended next steps (prioritized)

### CRITICAL — fix before next release

1. **L2-002** — Read Compute Optimizer savings from `recommendationOptions[N].savingsOpportunity.estimatedMonthlySavings.value`. Apply identical fix to `ebs.py`, `rds.py`, `lambda_svc.py`. Add a fixture with a non-empty `instanceRecommendations` entry.
2. **L2-004** — Delete the `if "%" in savings_str: return 50.0` branch in `services/_savings.py`. Replace each percentage-only `EstimatedSavings` in `services/ec2.py` with a real `$/month` computed via `ctx.pricing_engine.get_ec2_hourly_price(instance_type) * 730 * factor` where factor depends on check category.

### HIGH

3. **L2-005** — Replace hardcoded `0.10` in `services/ec2.py:686` with `ctx.pricing_engine.get_ebs_monthly_price_per_gb(volume.get("VolumeType", "gp2"))`.
4. **L2-006** — Remove duplicate `underutilized_instance_store` append at `services/ec2.py:754-763`.
5. **L1-001** — Set `requires_cloudwatch: bool = True` on `EC2Module`.
6. **L3-001** — Resolve adapter-emits-vs-renderer-drops drift for Spot recommendations.

### MEDIUM

7. **L1-002** — Honor `fast_mode` in `get_enhanced_ec2_checks` and `get_advanced_ec2_checks` (skip CloudWatch + `describe_volumes` enrichment) — or set `reads_fast_mode = False`.
8. **L1-003** — Route `except: pass` blocks through `ctx.permission_issue` / `ctx.warn`.
9. **L2-001** — Fix the schema of `tests/fixtures/recorded_aws_responses/cost-optimization-hub/list_recommendations.json` and `tests/aws_stubs/stub_responses.py:44` to match the documented AWS API (flat `estimatedMonthlySavings: float`, sibling `currencyCode: str`).
10. **L2-007** — Update `services/adapters/CLAUDE.md` after the L2-004 fix actually wires EC2 into PricingEngine.
11. **L3-002** — Render per-instance CPU values inside `<li>` instead of collapsing to `recs[0]`.
12. **L3-003** — Standardize on `f"${v:.2f}/month"` (drops out of L2-004 fix).

### LOW

13. **L1-004** — Replace `print()` with `logging`.
14. **L1-005** — Use `Period=300, Statistics=["Minimum"]` for `CPUCreditBalance` (AWS publishes at 5-min frequency).

---

## What this audit did **not** cover

- IAM policy completeness for EC2 actions.
- Performance / throttling under > 10,000 instances per region.
- Cross-service double-counting with `aurora` / `eks_cost` / `containers` adapters.
- Cross-account / Organizations scans (single-account assumed).
- AWS Pricing API SLA (only verified the adapter handles a 0.0 return; not that AWS itself is correct).

---

## Sources (external validation)

- AWS Pricing Calculator FAQ (730-hours/month methodology): https://aws.amazon.com/aws-cost-management/aws-pricing-calculator/faqs/
- AWS Pricing Calculator assumptions: https://aws.amazon.com/calculator/calculator-assumptions/
- boto3 `cost-optimization-hub` `list_recommendations`: https://docs.aws.amazon.com/boto3/latest/reference/services/cost-optimization-hub/client/list_recommendations.html
- boto3 `cost-optimization-hub` `get_recommendation`: https://docs.aws.amazon.com/boto3/latest/reference/services/cost-optimization-hub/client/get_recommendation.html
- boto3 `compute-optimizer` `get_ec2_instance_recommendations`: https://docs.aws.amazon.com/boto3/latest/reference/services/compute-optimizer/client/get_ec2_instance_recommendations.html
- AWS CloudWatch viewing metrics: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/viewing_metrics_with_cloudwatch.html
- AWS Pricing API (via MCP server): `mcp__aws-pricing-mcp-server__get_pricing` (AmazonEC2 us-east-1 — gp2 storage, t3.nano, t3.small, m5.large, m6i.32xlarge)
- AWS CloudWatch metric metadata (via MCP server): `mcp__aws-cloudwatch-mcp-server__get_metric_metadata` (AWS/EC2 CPUUtilization, AWS/EC2 CPUCreditBalance)
