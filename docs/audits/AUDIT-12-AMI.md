# AUDIT-12 · AMI (Amazon Machine Images)

**Adapter**: `services/adapters/ami.py` (46 lines)
**Supporting**: `services/ami.py` (129 lines), `services/_base.py` (34 lines), `core/contracts.py` (188 lines)
**Date**: 2026-05-01
**Auditor**: Sisyphus Audit Agent
**Pricing Data Source**: AWS Price List API, queried 2026-05-01

---

## 1. Code Analysis

### 1.1 Architecture

The AMI adapter is a thin field-extraction wrapper. The adapter (`services/adapters/ami.py`) extends `BaseServiceModule` and delegates all scanning logic to `services/ami.py::compute_ami_checks()`.

**Call chain**:
```
AmiModule.scan(ctx) → compute_ami_checks(ctx, ctx.pricing_multiplier)
  → returns {recommendations: list, total_count: int}
  → adapter sums EstimatedMonthlySavings per-recommendation
  → returns ServiceFindings(sources={"old_amis": SourceBlock(...)})
```

### 1.2 Unused AMI Detection

**Code path**: `services/ami.py:28–58`

The function builds a `running_amis` set from three sources:
1. **Running/stopped EC2 instances** — `ec2.describe_instances()` paginator, extracting `ImageId` from instances in `running` or `stopped` state.
2. **Launch templates** — `ec2.describe_launch_templates()` → `ec2.describe_launch_template_versions()`, extracting `ImageId` from `LaunchTemplateData`.
3. **Auto Scaling Groups** — `autoscaling.describe_auto_scaling_groups()` → `autoscaling.describe_launch_configurations()`, extracting `ImageId` from launch configs.

An AMI is flagged as **unused** if:
- `ami_id not in running_amis` AND
- `age_days > 30`

### 1.3 Old AMI Detection (90-Day Threshold)

**Code path**: `services/ami.py:67–119`

The constant `OLD_SNAPSHOT_DAYS: int = 90` defines the age threshold. For AMIs older than 90 days, the function:

1. Iterates over `BlockDeviceMappings` to collect snapshot IDs.
2. Calls `ec2.describe_snapshots(SnapshotIds=[...])` per snapshot to get `VolumeSize`.
3. Falls back to `block_device["Ebs"].get("VolumeSize", 8)` if snapshot lookup fails.
4. Falls back to 8 GB total if no block device mappings found.

**Pricing formula**:
```python
monthly_snapshot_cost = total_snapshot_size_gb * 0.05 * pricing_multiplier
```

### 1.4 Adapter Aggregation

**Code path**: `services/adapters/ami.py:35–46`

The adapter:
- Calls `compute_ami_checks(ctx, ctx.pricing_multiplier)`
- Extracts `recommendations` list (which are the `old_amis` entries only)
- Sums `EstimatedMonthlySavings` across all recommendations
- Returns `ServiceFindings` with a single source block `"old_amis"`

**Critical observation**: The adapter only returns `old_amis` recommendations in the output. The `unused_amis` list computed in `services/ami.py` is **discarded** — it's never returned to the adapter.

### 1.5 Required Clients

The adapter declares `required_clients() → ("ec2",)` but the underlying `compute_ami_checks()` also uses `autoscaling`. This is a mismatch.

---

## 2. Pricing Validation

### 2.1 Live AWS Pricing API Results

**Queried 2026-05-01** via `aws-pricing-mcp-server get_pricing`:

| Region | Usage Type | Price (USD/GB-Mo) | Source |
|--------|------------|-------------------|--------|
| eu-west-1 | `EU-EBS:SnapshotUsage` | **$0.0500** | AWS Price List API |
| us-east-1 | `EBS:SnapshotUsage` | **$0.0500** | AWS Price List API |
| eu-west-1 | `EU-EBS:SnapshotArchiveStorage` | **$0.0125** | AWS Price List API |

### 2.2 Code Constant vs Live Pricing

| Constant | Code Value | Live API Value (eu-west-1) | Live API Value (us-east-1) | Match? |
|----------|-----------|--------------------------|--------------------------|--------|
| `0.05` (line 106 of `services/ami.py`) | $0.05/GB-Mo | $0.05/GB-Mo | $0.05/GB-Mo | **YES** |

The hardcoded `$0.05/GB-Mo` is accurate for both eu-west-1 and us-east-1 standard snapshot storage.

### 2.3 Pricing Accuracy Concerns

| Issue | Detail |
|-------|--------|
| **No regional variation** | The $0.05 constant is used regardless of region. Some regions have different snapshot pricing (e.g., ap-south-1 = $0.055, sa-east-1 = $0.06). |
| **Full volume size, not incremental** | EBS snapshots are incremental — only changed blocks are stored. The code uses `VolumeSize` (full provisioned size), which overstates actual snapshot storage cost. |
| **No archive tier consideration** | EBS Snapshots Archive ($0.0125/GB-Mo) is a cheaper alternative not mentioned. |

---

## 3. Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Snapshot storage cost uses correct per-GB rate | ✅ PASS | $0.05/GB-Mo matches us-east-1 and eu-west-1 |
| 2 | pricing_multiplier applied correctly | ✅ PASS | Multiplied into monthly_snapshot_cost |
| 3 | AMI age threshold (90 days) is sensible | ✅ PASS | Industry standard for stale AMIs |
| 4 | Unused AMI detection checks all references | ✅ PASS | Checks instances, launch templates, ASG launch configs |
| 5 | Unused AMIs are included in output | ❌ FAIL | `unused_amis` list is computed but **discarded** — never returned |
| 6 | required_clients() declares all needed clients | ❌ FAIL | Only declares `ec2`, but code also uses `autoscaling` |
| 7 | Snapshot size fallback is reasonable | ⚠️ WARN | Falls back to 8 GB which may under/overestimate |
| 8 | Pricing is region-aware | ❌ FAIL | Hardcoded $0.05 regardless of region |
| 9 | Incremental vs full snapshot cost documented | ⚠️ WARN | Uses full volume size, not actual incremental snapshot size |
| 10 | Error handling is graceful | ✅ PASS | All API calls wrapped in try/except |
| 11 | CreationDate parsing is robust | ⚠️ WARN | Only handles `%Y-%m-%dT%H:%M:%S.%fZ` format; may fail on `+00:00` suffix |
| 12 | Adapter follows ServiceModule protocol | ✅ PASS | Correctly extends BaseServiceModule and returns ServiceFindings |
| 13 | Savings aggregation is correct | ✅ PASS | Sums `EstimatedMonthlySavings` from all recommendations |
| 14 | No security issues (IAM, secrets) | ✅ PASS | Read-only operations, no credential handling |

---

## 4. Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| AMI-01 | **HIGH** | Unused AMI recommendations computed but never returned | Users never see unused AMI cleanup recommendations — potentially significant savings missed |
| AMI-02 | **MEDIUM** | `required_clients()` missing `autoscaling` | Scan may fail if ScanContext only provisions clients declared in `required_clients()`. Launch config checks would silently fail. |
| AMI-03 | **MEDIUM** | Pricing not region-aware — hardcoded $0.05/GB-Mo | In regions like sa-east-1 ($0.06), ap-south-1 ($0.055), costs are understated by 10-20% |
| AMI-04 | **LOW** | Snapshot cost uses full volume size, not actual incremental storage | Overestimates savings — real snapshot storage is typically much smaller than volume size due to incremental nature |
| AMI-05 | **LOW** | CreationDate parsing only handles one format | `datetime.strptime(ami["CreationDate"], "%Y-%m-%dT%H:%M:%S.%fZ")` will fail if AMI has no microseconds (e.g., `2024-01-01T00:00:00Z`) |
| AMI-06 | **INFO** | Default fallback of 8 GB for empty block device mappings | May be inaccurate for AMIs with unusual storage configurations |

---

## 5. Detailed Findings

### 5.1 AMI-01: Unused AMIs Discarded (HIGH)

In `services/ami.py`, the function `compute_ami_checks()` builds two lists:
- `checks["unused_amis"]` — AMIs not referenced by any instance/template/ASG and older than 30 days
- `checks["old_amis"]` — AMIs older than 90 days with snapshot cost estimates

However, the return statement at line 128 only returns `old_amis`:
```python
return {"recommendations": checks["old_amis"], "total_count": len(checks["old_amis"])}
```

The `unused_amis` list is computed (with potentially expensive API calls) but never surfaced to the user. The adapter at `services/adapters/ami.py:38` reads `result.get("recommendations", [])` which only gets the old_amis.

**Recommendation**: Return both categories, or merge unused_amis into the recommendations with a distinct `CheckCategory`.

### 5.2 AMI-02: Missing `autoscaling` Client Declaration (MEDIUM)

`services/adapters/ami.py:24`:
```python
def required_clients(self) -> tuple[str, ...]:
    return ("ec2",)
```

But `services/ami.py:38` calls `ctx.client("autoscaling")`. If the scan orchestrator only pre-provisions clients listed in `required_clients()`, the autoscaling call will fail. The error is silently caught (line 60: `except Exception: pass`), meaning ASG launch config references are missed — leading to false positives for unused AMIs.

### 5.3 AMI-03: Region-Agnostic Pricing (MEDIUM)

The snapshot cost formula at `services/ami.py:106`:
```python
monthly_snapshot_cost = total_snapshot_size_gb * 0.05 * pricing_multiplier
```

The $0.05 constant is correct for eu-west-1 and us-east-1 but wrong for other regions. The `pricing_multiplier` parameter is intended as a scalar adjustment but doesn't encode the actual regional price difference.

Sample regional deviations (from AWS pricing page):
- sa-east-1: $0.060/GB-Mo (+20%)
- ap-south-1: $0.055/GB-Mo (+10%)
- us-west-1: $0.055/GB-Mo (+10%)

### 5.4 AMI-05: Date Parsing Fragility (LOW)

The `CreationDate` format from `describe_images` is typically `2024-01-15T10:30:00.000Z` but the `%f` directive in `strptime` may fail if the microseconds part is missing or has a different number of digits. A more robust approach would be to use `datetime.fromisoformat()` (Python 3.11+) or handle the trailing `Z` explicitly.

---

## 6. AWS Documentation Cross-Reference

Per AWS EBS Pricing documentation (queried 2026-05-01):

> "Snapshot storage is based on the amount of space the data consumes in Amazon Simple Storage Service (Amazon S3)."

Key points from docs that affect this adapter:
1. **Snapshots are incremental** — only changed blocks are stored. The code uses full `VolumeSize`, not actual snapshot storage consumed.
2. **Standard snapshot: $0.05/GB-Mo** in eu-west-1 — matches code constant.
3. **Archive tier: $0.0125/GB-Mo** — 75% cheaper, not referenced in recommendations.
4. **Deleting a snapshot doesn't reduce cost** if other snapshots reference its data — the code doesn't account for snapshot sharing.

---

## 7. Verdict

**⚠️ WARN — 62/100**

The adapter correctly identifies old AMIs and produces accurate savings estimates for eu-west-1/us-east-1. However, two significant issues reduce the score:

1. **Unused AMI recommendations are silently discarded** (AMI-01) — the most impactful finding, as it represents completely wasted computation and missed savings opportunities.
2. **Missing `autoscaling` client declaration** (AMI-02) — may cause false positives in unused AMI detection if the orchestrator respects `required_clients()`.

The pricing constant is accurate for major regions but not region-aware. The snapshot cost model overestimates by using full volume size instead of actual incremental storage.

### Score Breakdown

| Category | Weight | Score | Weighted |
|----------|--------|-------|----------|
| Pricing Accuracy | 25% | 75/100 | 18.75 |
| Logic Correctness | 25% | 40/100 | 10.00 |
| Unused Detection | 20% | 50/100 | 10.00 |
| Error Handling | 15% | 85/100 | 12.75 |
| Protocol Compliance | 15% | 70/100 | 10.50 |
| **Total** | **100%** | | **62.00** |
