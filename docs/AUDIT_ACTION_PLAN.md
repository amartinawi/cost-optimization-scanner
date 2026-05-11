# Audit Action Plan — AUDIT_REPORT.md Findings

> **STATUS: ALL REMEDIATIONS COMPLETE** — 46 wave fixes + 4 post-fix corrections + 2 overlap fixes = **52 changes applied**. Test suite: 152 passed, 1 skipped, 0 failures.

Validation status based on direct code inspection. Findings marked FALSE POSITIVE were rejected after reading the actual code.

---

## Validation Verdicts

### FALSE POSITIVES (rejected after code inspection)

| Finding | Reason |
|---------|--------|
| `commitment_analysis.py:176` multiplier on waste | `TotalHourlyCommitment` is CE API real USD; `waste = hourly * (1-rate) * 730` is already in real USD. Multiplier would double-apply. |
| `lambda_svc.py` cloudwatch missing from required_clients | Adapter at `services/adapters/lambda_svc.py` calls no CloudWatch; legacy shim `services/lambda_svc.py` does. Adapter's `required_clients()` is correct. |
| `cost_optimization_hub.py` `_ROUTED_TYPES` | Code shows no double-counting bug in context; `_ROUTED_TYPES` is used to skip, not match — still needs verification against live COH API type strings. Mark as **NEEDS INVESTIGATION**. |

---

## Wave 1 — CRITICAL: Crash & Data Accuracy

These directly cause report crashes or produce wrong dollar values.

### W1-1 · `html_report_generator.py:2010` — KeyError crash
**File:** `html_report_generator.py`
**Fix:** `filtered_service_data["total_monthly_savings"]` → `.get("total_monthly_savings", 0.0)`

### W1-2 · `cost_anomaly.py` — optimization_descriptions keys all wrong
**File:** `services/adapters/cost_anomaly.py:129-154`
**Problem:** Sources are `active_anomalies`, `anomaly_monitors`, `billing_alarms`, `recommendations`.
Descriptions are `cost_anomaly`, `anomaly_monitor`, `billing_alarm`, `anomaly_subscription`, `anomaly_summary`, `cost_governance` — zero keys match.
**Fix:** Rename description keys to exactly match source keys:
```python
optimization_descriptions={
    "active_anomalies": {"title": "Active Cost Anomalies", ...},
    "anomaly_monitors": {"title": "Anomaly Monitor Coverage", ...},
    "billing_alarms": {"title": "Billing Alarm Gaps", ...},
    "recommendations": {"title": "Recommendations", ...},
}
```

### W1-3 · `opensearch.py` — pricing_multiplier missing (2 locations)
**File:** `services/adapters/opensearch.py:58,69`
**Fix:**
- Line 58: `savings += monthly * instance_count * ri_rate * ctx.pricing_multiplier`
- Line 70: `savings += storage_monthly * ri_rate * ctx.pricing_multiplier`

### W1-4 · `ebs.py` — GP2→GP3 savings missing pricing_multiplier (2 locations)
**File:** `services/adapters/ebs.py:63,65`
**Fix:**
- Line 63: `savings += size * max(gp2_price - gp3_price, 0) * ctx.pricing_multiplier`
- Line 65: `savings += size * 0.10 * 0.20 * ctx.pricing_multiplier`

### W1-5 · `file_systems.py` — enhanced_recs savings not included in total
**File:** `services/adapters/file_systems.py:79`
**Problem:** `total_recs` counts `enhanced_recs` but savings loop never sums their savings.
**Fix:** Add savings loop for enhanced_recs after the existing loops:
```python
for rec in enhanced_recs:
    savings += rec.get("EstimatedMonthlySavings", rec.get("monthly_savings", 0))
```

### W1-6 · `lambda_svc.py` — pricing_multiplier applied to CE API values
**File:** `services/adapters/lambda_svc.py:68`
**Problem:** `savings *= ctx.pricing_multiplier` at line 68 multiplies the entire savings including CE API `estimatedMonthlySavings` which are already in real USD.
**Fix:** Separate CE API savings from formula savings:
```python
hub_savings = sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)
formula_savings = 0.0
for rec in enhanced_recs:
    # ... existing formula calculations writing to formula_savings
formula_savings *= ctx.pricing_multiplier
savings = hub_savings + formula_savings
```

### W1-7 · `network_cost.py` — pricing_multiplier applied to real CE spend
**File:** `services/adapters/network_cost.py:246,283`
**Problem:** `monthly_est` is real CE API spend in USD. `potential_savings = monthly_est * 0.30 * multiplier` inflates savings by the multiplier for data already in real USD.
**Fix:** Remove the `* multiplier` from CE-sourced calculations. The 30% savings factor is already applied; regional pricing is reflected in the actual spend.
```python
potential_savings = monthly_est * 0.30  # CE data is already real USD
```

---

## Wave 2 — HIGH: Contract Violations (optimization_descriptions key mismatches)

These break description rendering in the report — titles and descriptions for service tabs are silent no-ops.

### W2-1 · `ebs.py` — descriptions 3/4 keys mismatched
**File:** `services/ebs.py` (`EBS_OPTIMIZATION_DESCRIPTIONS`)
**Problem:** Description keys: `unattached_volumes`, `gp2_to_gp3`, `io1_to_io2`, `volume_rightsizing`.
Source keys: `compute_optimizer`, `unattached_volumes`, `gp2_migration`, `enhanced_checks`.
**Fix:** Add matching keys to `EBS_OPTIMIZATION_DESCRIPTIONS`:
```python
EBS_OPTIMIZATION_DESCRIPTIONS = {
    "compute_optimizer": {...},
    "unattached_volumes": {...},  # already exists
    "gp2_migration": {...},       # rename from gp2_to_gp3
    "enhanced_checks": {...},     # rename from volume_rightsizing
    # keep old keys too if referenced elsewhere
}
```

### W2-2 · `ami.py` — optimization_descriptions is None
**File:** `services/adapters/ami.py`
**Fix:** Add `optimization_descriptions` dict with at least an `old_amis` key.

### W2-3 · `dynamodb.py` — all description keys orphaned
**File:** `services/adapters/dynamodb.py:72`
**Problem:** Sources are `dynamodb_table_analysis`, `enhanced_checks`. Descriptions use completely different keys.
**Fix:** Rename description keys to match source keys.

### W2-4 · `eks.py` — descriptions 4/5 keys mismatched
**File:** `services/adapters/eks.py`
**Fix:** Align description keys to match source keys.

### W2-5 · `dms.py` — Multi-AZ recs not in sources
**File:** `services/adapters/dms.py:60-69`
**Problem:** Multi-AZ recommendations appended to `recs` but `sources` only built from `checks`. `total_recommendations` overcounts.
**Fix:** Add Multi-AZ recs to a named source block.

### W2-6 · `s3.py` — enhanced_recs savings excluded
**File:** `services/adapters/s3.py:42`
**Problem:** `total_monthly_savings` only sums `opt_opps` savings; enhanced S3 recs contribute $0.
**Fix:** Sum `enhanced_recs` savings into `total_monthly_savings`.

### W2-7 · `cloudfront.py` — optimization_descriptions is None
**File:** `services/adapters/cloudfront.py`
**Fix:** Provide `optimization_descriptions` dict.

### W2-8 · `compute_optimizer.py` — summary SourceBlock count mismatch
**File:** `services/adapters/compute_optimizer.py`
**Problem:** `summary` SourceBlock has `count=total_recs` but `recommendations=()`.
**Fix:** Either remove the summary block or populate it with actual recommendations.

---

## Wave 3 — HIGH: Missing Client Declarations

These prevent the orchestrator from pre-validating AWS clients, causing runtime failures in strict mode.

### W3-1 · `elasticache.py` — cloudwatch not in required_clients
**File:** `services/adapters/elasticache.py:19-21`
**Fix:** `return ("elasticache", "cloudwatch")`

### W3-2 · `mediastore.py` — cloudwatch not in required_clients
**File:** `services/adapters/mediastore.py:19-21`
**Fix:** `return ("mediastore", "cloudwatch")`

### W3-3 · `network.py` — elb client not declared
**File:** `services/adapters/network.py:26`
**Fix:** `return ("ec2", "elasticloadbalancingv2", "autoscaling", "elb")`

---

## Wave 4 — HIGH: Missing reads_fast_mode Declarations

Adapters calling CloudWatch without declaring `reads_fast_mode = True` may execute expensive CW calls during fast mode.

**Files to add `reads_fast_mode: bool = True` class attribute:**
- `services/adapters/apprunner.py`
- `services/adapters/athena.py`
- `services/adapters/opensearch.py`
- `services/adapters/monitoring.py`
- `services/adapters/elasticache.py`
- `services/adapters/mediastore.py`
- `services/adapters/step_functions.py`
- `services/adapters/dms.py`
- `services/adapters/ec2.py`
- `services/adapters/dynamodb.py`

---

## Wave 5 — HIGH: Pagination Gaps

Truncated results for large accounts.

### W5-1 · `commitment_analysis.py` — 4 CE APIs missing pagination
**File:** `services/adapters/commitment_analysis.py:169,208,261,310,361`
**Fix:** Wrap each call with a `while nextToken` pagination loop.

### W5-2 · `cost_anomaly.py` — 2 APIs missing pagination
**File:** `services/adapters/cost_anomaly.py:239` (`get_anomaly_monitors`), `line 321` (`describe_alarms`)
**Fix:** Add `NextToken`/`NextPageToken` pagination loops.

### W5-3 · `monitoring.py` — describe_log_groups not paginated
**File:** `services/adapters/monitoring.py:40`
**Fix:** Add `nextToken` pagination loop.

### W5-4 · `aurora.py` — describe_db_cluster_snapshots not paginated
**File:** `services/adapters/aurora.py:218`
**Fix:** Add `Marker`-based pagination loop.

### W5-5 · `sagemaker.py` — list_notebook_instances and list_training_jobs not paginated
**File:** `services/adapters/sagemaker.py:170,211`
**Fix:** Paginate both calls.

---

## Wave 6 — MEDIUM: Isolation / Error Handling

Silent exception swallowing hides auth errors, throttling, and data issues.

### W6-1 · Silent `except Exception: pass` blocks
**Files:** `services/adapters/apprunner.py:51-52`, `athena.py:54-55`, `aurora.py:399-400`
**Fix:** Log warning instead of silently passing:
```python
except Exception as e:
    print(f"Warning: [{adapter}] metric check failed: {e}")
```

### W6-2 · No try/except on legacy module calls
**Files:** `services/adapters/containers.py:38-39`, `cloudfront.py:36`
**Fix:** Wrap each legacy call in individual try/except with warning log.

### W6-3 · Blanket except catches all checks in rds.py
**File:** `services/adapters/rds.py:43`
**Fix:** Narrow the try/except scope so snapshot and Aurora checks run independently even if instance listing fails.

---

## Wave 7 — HIGH/MEDIUM: HTML Report Generator Fixes

### W7-1 · XSS via unsanitized HTML interpolation (CRITICAL)
**File:** `html_report_generator.py:1273,1274,1275,1351,1974`
**Fix:** Wrap all adapter-sourced values in `html.escape()` before interpolating into f-strings. Import `html` at the top.

### W7-2 · Shallow copy mutates caller's sources dict
**File:** `html_report_generator.py:1803`
**Fix:** Deep copy or reconstruct filtered data without mutating `sources` nested dicts:
```python
import copy
filtered_data = copy.deepcopy(service_data)
```

### W7-3 · Fragile savings string parsing (comma causes ValueError)
**File:** `html_report_generator.py:1443-1454,1509-1514,1547-1551`
**Fix:** Add `.replace(",", "")` before the strip chain, or use a helper that handles commas.

### W7-4 · `print()` for error output in production path
**File:** `html_report_generator.py:1516,1553`
**Fix:** Replace `print()` with `logging.warning()`.

### W7-5 · Services Scanned card shows recommendation count not service count
**File:** `html_report_generator.py:1294-1298`
**Fix:** Either rename the card label to "Services With Findings" or change the count to `len(scan_results.get("services", {}))`.

### W7-6 · chartData not serialized via json.dumps
**File:** `html_report_generator.py:2335`
**Fix:** `const chartData = {json.dumps(chart_data)};`

---

## Wave 8 — MEDIUM: Pricing & Date Fixes

### W8-1 · `transfer_svc.py:64` — datetime.utcnow() deprecated
**File:** `services/transfer_svc.py:64`
**Fix:** `from datetime import datetime, timezone` → `datetime.now(timezone.utc)`

### W8-2 · `commitment_analysis.py:33` — date.today() timezone-naive
**File:** `services/adapters/commitment_analysis.py:33`
**Fix:** `from datetime import datetime, timezone` → `datetime.now(timezone.utc).date()`

### W8-3 · `rds.py` — parse_dollar_savings returns $0 for percentage strings
**File:** `services/adapters/rds.py:49-53`
**Problem:** Strings like `"~50% of instance cost"` return $0 from `parse_dollar_savings()`.
**Fix:** Add a fallback heuristic when the string contains `%` and no dollar amount.

### W8-4 · Hardcoded magic-number fallbacks without named constants
**Files:** `redshift.py:49` ($200), `step_functions.py:85` ($150), `athena.py:62` ($50), `batch.py:59` ($150)
**Fix:** Promote to named module-level constants with a comment citing the AWS pricing source or rationale:
```python
REDSHIFT_NODE_MONTHLY_FALLBACK: float = 200.0  # ~dc1.large on-demand list price
STEP_FUNCTIONS_IDLE_MONTHLY_FALLBACK: float = 150.0  # estimated from workflow execution data
```

---

## Investigation Results (RESOLVED)

| Item | File | Verdict | Resolution |
|------|------|---------|------------|
| Aurora/RDS double-counting | `services/adapters/rds.py` + `aurora.py` | **FALSE POSITIVE** | Both adapters process complementary sub-populations: rds.py targets clusters WITHOUT ServerlessV2; aurora.py targets clusters WITH ServerlessV2. No dollar-savings overlap. |
| cost_optimization_hub.py `_ROUTED_TYPES` | `services/adapters/cost_optimization_hub.py:18-25` | **CONFIRMED BUG — FIXED** | `_ROUTED_TYPES` used fabricated strings (`Ec2InstanceRightsizing`) that don't exist in the AWS API. Renamed to `_ROUTED_RESOURCE_TYPES` with correct `currentResourceType` values (`Ec2Instance`, `EbsVolume`, `LambdaFunction`, `RdsDbInstance`). Filter now checks `currentResourceType` instead of `recommendationType`. |
| EKS nodes triple-counted vs EC2 | `services/ec2.py` + `services/adapters/eks.py` + `services/adapters/containers.py` | **CONFIRMED OVERLAP — FIXED** | `get_enhanced_ec2_checks()` and `get_advanced_ec2_checks()` iterated ALL instances with zero EKS tag filtering. Added `_is_eks_managed_instance()` helper and skip in both loops. EKS instances are now handled only by eks.py and containers.py adapters. |

---

## Summary by Priority

| Wave | Count | Effort |
|------|-------|--------|
| W1 — Critical crashes & wrong numbers | 7 fixes | Low–Medium |
| W2 — Contract key mismatches | 8 fixes | Low |
| W3 — Missing client declarations | 3 fixes | Trivial |
| W4 — Missing reads_fast_mode | 10 fixes | Trivial |
| W5 — Pagination | 5 fixes | Medium |
| W6 — Isolation / error handling | 3 fixes | Low |
| W7 — HTML report bugs | 6 fixes | Low–Medium |
| W8 — Pricing & dates | 4 fixes | Low |
| **Total confirmed** | **46 fixes** | |
| Post-fix corrections | 4 fixes | Trivial |
| Overlap fixes (investigation) | 2 fixes | Low–Medium |
| Investigation false positives | 1 item | N/A |
| **Grand total applied** | **52 changes** | |
