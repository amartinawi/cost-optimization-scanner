# Live Pricing Engine — Implementation Prompt (v2 — Updated)

> **Purpose**: Self-contained, phase-by-phase prompt for completing live AWS pricing in the Cost Optimization Scanner.
> Feed this directly to an agent (planner, code-reviewer, or coder).
> All file paths, line numbers, and code snippets are grounded in the actual codebase.

---

## Implementation Status

Run this before starting any work to confirm baseline:
```bash
cd /Users/amartinawi/Desktop/Cost_OptV1_dev
python3 -m pytest tests/ -q --no-header 2>&1 | tail -5
```
All 152 tests must pass. If any fail, stop and fix before proceeding.

### Already Complete — DO NOT re-implement

| Phase | What was done |
|---|---|
| Phase 1 | `core/pricing_engine.py` (430 lines), `ScanContext.pricing_engine` field, `cost_optimizer.py` wiring |
| Phase 2 | `services/ebs.py`, `services/rds.py`, `services/s3.py`, `adapters/ebs.py` (gp2→gp3), `adapters/file_systems.py` (EFS) |
| Phase 3a | `adapters/redshift.py`, `adapters/msk.py`, `adapters/elasticache.py`, `adapters/opensearch.py` |
| Phase 3b partial | `adapters/batch.py`, `adapters/dms.py` |
| Phase 5 | `services/elastic_ip.py`, `services/nat_gateway.py`, `services/load_balancer.py`, `services/vpc_endpoints.py` |
| Phase 6 | `tests/test_pricing_engine.py` (21 tests), `conftest.py` (`--live` flag, `mock_pricing_engine` fixture) |
| Bug fix: RDS Multi-AZ | `pricing_engine.get_rds_monthly_storage_price_per_gb(multi_az=)` + `rds.py` passes actual `MultiAZ` flag. Cache key includes deployment option. |
| Bug fix: network.py | Removed regex string-parsing; now uses `parse_dollar_savings()` from `services/_savings.py`. 152 passed, golden files unchanged. |

**DO NOT re-add `--live` option or `mock_pricing_engine` to `conftest.py` — both already exist.**
**DO NOT re-create `core/pricing_engine.py` or modify `ScanContext` — both already done.**

---

## Project Context

**Repo root**: `/Users/amartinawi/Desktop/Cost_OptV1_dev/` (flat layout — NOT a package)
**Python**: 3.11+, boto3; no new external deps without explicit approval

**Key files**:
- `core/pricing_engine.py` — `PricingEngine` class with all fetch methods
- `core/scan_context.py` — `ScanContext.pricing_engine: PricingEngine | None` field at line 43
- `services/adapters/*.py` — primary modification zone
- `services/elastic_ip.py`, `nat_gateway.py`, `load_balancer.py`, `vpc_endpoints.py` — legacy shim files (produce recommendation dicts with `EstimatedSavings` strings)
- `services/adapters/network.py` — aggregates shim outputs; currently parses savings from strings

**Regression gate** (run after EVERY phase):
```bash
python3 -m pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v
```

Golden files update procedure (after intentional savings-number changes):
```bash
# Re-run the offline scan to regenerate fixtures, then overwrite:
python3 -m pytest tests/test_offline_scan.py -v  # generates new output
# Copy generated output over fixtures/golden_scan_results.json and fixtures/golden_report.html
# Then commit both with: fix: update golden files after live pricing Phase X
```
There is NO `--update-snapshots` pytest flag in this repo — do not use it.

---

## Known Bug: RDS MultiAZ Deployment Option

**File**: `core/pricing_engine.py` → `_fetch_rds_storage_price`
**File**: `services/rds.py` lines 270-280

**Problem**: `_fetch_rds_storage_price(storage_type)` pins `deployment_option = "Multi-AZ"` only for io1. For gp2/gp3 it always uses "Single-AZ" — but RDS instances have a `MultiAZ` attribute. A Multi-AZ gp2 instance is ~2× more expensive than Single-AZ. The scanner underreports savings for Multi-AZ gp2/gp3 instances.

**Fix in `core/pricing_engine.py`**: Add `multi_az: bool = False` parameter:

```python
def get_rds_monthly_storage_price_per_gb(self, storage_type: str, multi_az: bool = False) -> float:
    key = ("rds_storage", storage_type, multi_az)
    if (cached := self._cache.get(key)) is not None:
        return cached
    price = self._fetch_rds_storage_price(storage_type, multi_az)
    if price is None:
        price = FALLBACK_RDS_STORAGE_GB_MONTH.get(storage_type, 0.115) * self._fallback_multiplier
        self.warnings.append(
            f"Pricing API unavailable for RDS {storage_type} storage; using fallback"
        )
    self._cache.set(key, price)
    return price

def _fetch_rds_storage_price(self, storage_type: str, multi_az: bool = False) -> float | None:
    deployment_option = "Multi-AZ" if (multi_az or storage_type == "io1") else "Single-AZ"
    filters = [
        {"Type": "TERM_MATCH", "Field": "location",         "Value": self._display_name},
        {"Type": "TERM_MATCH", "Field": "productFamily",    "Value": "Database Storage"},
        {"Type": "TERM_MATCH", "Field": "volumeType",       "Value": storage_type.upper()},
        {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deployment_option},
    ]
    return self._call_pricing_api("AmazonRDS", filters)
```

**Fix in `services/rds.py`** (around line 270-280 where `multi_az` is already fetched at line 184):
```python
# Already have: multi_az = instance.get("MultiAZ", False)
# Change:
storage_price = (
    ctx.pricing_engine.get_rds_monthly_storage_price_per_gb("gp2", multi_az=multi_az)
    if ctx.pricing_engine
    else 0.115 * pricing_multiplier
)
# And for gp3 comparison:
gp3_price = (
    ctx.pricing_engine.get_rds_monthly_storage_price_per_gb("gp3", multi_az=multi_az)
    if ctx.pricing_engine
    else 0.092 * pricing_multiplier
)
```

**Regression gate after this fix** — savings numbers change for Multi-AZ RDS instances:
```bash
python3 -m pytest tests/test_regression_snapshot.py -v
```
If golden file diff looks correct, update it (see procedure above).

---

## Phase A: Fix `adapters/network.py` String-Parsing

**File**: `services/adapters/network.py` (73 lines)
**Problem**: The adapter collects recommendations from 5 shim functions, then extracts savings by regex-parsing `EstimatedSavings` strings like `"$3.65/month per EIP"`. This is fragile — some strings like `"$0.01/GB data processing + NAT costs"` are intentionally non-parseable, and `"Up to $25/month for low-traffic scenarios"` gives wrong values.

**Root cause**: The 5 shim files (`elastic_ip.py`, `nat_gateway.py`, `vpc_endpoints.py`, `load_balancer.py`, `services/ec2.py` for ASG) return `dict` results where each recommendation has a string `EstimatedSavings`. The shims already compute correct float values internally but only expose them in the formatted string.

**Fix**: Add a numeric `savings_value: float` key to each recommendation dict in the shim files, then read it directly in the adapter.

### Step A.1: Add `savings_value` to shim recommendations

In each shim function, wherever a recommendation dict is appended, add:

**`services/elastic_ip.py`** — each `recommendations.append({...})` block:
```python
# Add alongside "EstimatedSavings": f"${eip_monthly:.2f}/month per EIP"
"savings_value": eip_monthly,
```
For the "reduced to 1 EIP" case: `"savings_value": (eip_count - 1) * eip_monthly`

**`services/nat_gateway.py`** — each recommendations block:
```python
# For parseable amounts: add savings_value = the float
"savings_value": nat_monthly + 0.85,   # base savings line
"savings_value": (count - 1) * nat_monthly,  # consolidation lines
# For non-parseable strings like "$0.01/GB data processing savings": use 0.0
"savings_value": 0.0,
```

**`services/vpc_endpoints.py`**:
```python
"savings_value": vpc_ep_monthly,  # for per-endpoint savings
"savings_value": 0.0,             # for "$0.01/GB data processing + NAT costs" (variable)
```

**`services/load_balancer.py`**:
```python
"savings_value": alb_monthly,     # delete/consolidate recommendations
"savings_value": 0.0,             # for string-only or security-improvement notes
```

**`services/ec2.py`** (ASG recommendations, if applicable): Same pattern.

### Step A.2: Update `services/adapters/network.py`

Replace the string-parsing loop:
```python
# BEFORE:
for rec in all_recs:
    savings_str = rec.get("EstimatedSavings", "")
    if "$" in savings_str and "/month" in savings_str:
        match = re.search(r"\$(\d+\.?\d*)", savings_str)
        if match:
            savings += float(match.group(1))

# AFTER:
for rec in all_recs:
    savings += rec.get("savings_value", 0.0)
```

Remove the `import re` at the top of `network.py` if it's no longer used anywhere else in the file.

**Regression gate**: Savings numbers may change (upward — the "Up to $25/month" lines were being incorrectly parsed as $25). Update golden files if diff looks correct.

---

## Phase B: Remaining Flat-Rate Adapters

These adapters still use flat savings amounts independent of resource size. Apply the `pricing_engine` pattern: try live lookup → fall back to current flat rate.

### B.1 WorkSpaces (`services/adapters/workspaces.py`)

**Current**: `savings = 35 * len(recs)`
**Note**: WorkSpaces instances use `ComputeTypeName` (e.g. `"STANDARD"`, `"PERFORMANCE"`) — NOT EC2 instance types. Do NOT use `get_ec2_hourly_price` here.

WorkSpaces are priced as a flat monthly bundle (AlwaysOn) or per-hour (AutoStop). The savings come from switching AlwaysOn to AutoStop for workspaces used < 125 hours/month.

**Approach**: Use WorkSpaces Pricing API to find the bundle monthly delta.

```python
# In services/adapters/workspaces.py scan():
# The existing get_enhanced_workspaces_checks(ctx) returns recs with workspace details.
# Each rec should have "BundleId" or "ComputeType" from describe_workspaces response.

savings = 0.0
for rec in recs:
    compute_type = rec.get("ComputeType", "STANDARD")
    if ctx.pricing_engine:
        # WorkSpaces AlwaysOn vs AutoStop price delta:
        # AutoStop ~40% cheaper for < 125 hrs/month use
        # Pricing API ServiceCode: AmazonWorkSpaces
        # Fallback: $35/workspace/month is a reasonable average AlwaysOn premium
        monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonWorkSpaces", compute_type)
        savings += monthly * 0.40 if monthly > 0 else 35.0
    else:
        savings += 35.0
```

Add `get_instance_monthly_price("AmazonWorkSpaces", compute_type)` support to the engine's `_fetch_generic_instance_price`. The WorkSpaces Pricing API uses field `"bundleDescription"` or `"instanceType"` — test with `MaxResults=1` first to inspect the actual response fields.

### B.2 Glue (`services/adapters/glue.py`)

**Current**: Flat rate (likely `$100` per recommendation).
**Approach**: Use DPU count from the job configuration.

```python
# From describe_jobs response: job.get("MaxCapacity", 10) or job.get("NumberOfWorkers", 10)
# Glue Standard DPU pricing: ~$0.44/DPU-hour (us-east-1)
# Pricing API: ServiceCode="AWSGlue", filter productFamily="AWS Glue"

dpu_count = job.get("MaxCapacity", job.get("NumberOfWorkers", 10))
# Savings from reducing DPU count by 30% or switching to G.1X workers
if ctx.pricing_engine:
    dpu_hourly = ctx.pricing_engine.get_instance_monthly_price("AWSGlue", "Standard")
    # get_instance_monthly_price × 730 is wrong for Glue — it's per DPU-hour
    # Glue jobs run on-demand; estimate 8 hrs/day, 20 days/month
    monthly_cost = 0.44 * dpu_count * 8 * 20 * ctx.pricing_multiplier
    savings = monthly_cost * 0.30
else:
    savings = 100.0
```

**Note**: For Glue, the `get_instance_monthly_price` × 730 approach is wrong (Glue is not always-on). Hardcode the `0.44/DPU-hour` with fallback to `ctx.pricing_multiplier` scaling, and add a dedicated `get_glue_dpu_hourly_price()` method to `PricingEngine` if needed.

### B.3 Athena (`services/adapters/athena.py`)

**Current**: Flat rate.
**Approach**: CloudWatch metric `ProcessedBytes` for actual data scanned → `$5/TB × bytes_scanned_per_month`.

```python
# Athena pricing: $5 per TB scanned (us-east-1, fixed — rarely changes regionally)
# Savings from partitioning/columnar format: typically 70-90% scan reduction
athena_price_per_tb = 5.0 * ctx.pricing_multiplier

# Get bytes scanned from CloudWatch (last 30 days):
if not ctx.fast_mode:
    cw = ctx.client("cloudwatch")
    # metric: AWS/Athena, ProcessedBytes, workgroup dimension
    # ... fetch and sum
    monthly_tb = total_bytes / (1024 ** 4)
    savings = monthly_tb * athena_price_per_tb * 0.75  # 75% scan reduction
else:
    savings = 50.0  # fallback for fast_mode
```

### B.4 Lightsail (`services/adapters/lightsail.py`)

**Current**: `$12` per recommendation.
**Approach**: Lightsail instances have a bundle with a fixed monthly price — get it from the bundle.

```python
# From get_instances response: instance.get("bundleName", "")
# Lightsail Pricing API: ServiceCode="AmazonLightsail"
bundle_name = instance.get("bundleName", "")
if ctx.pricing_engine and bundle_name:
    monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonLightsail", bundle_name)
    savings = monthly if monthly > 0 else 12.0
else:
    savings = 12.0
```

### B.5 AppRunner (`services/adapters/apprunner.py`)

**Current**: `$25` per recommendation.
**Approach**: vCPU + memory config → AppRunner active pricing.

```python
# From describe_service response:
# service["InstanceConfiguration"]["Cpu"]    e.g. "1 vCPU"
# service["InstanceConfiguration"]["Memory"] e.g. "2 GB"
# AppRunner active pricing: $0.064/vCPU-hr + $0.007/GB-hr (us-east-1)
# Savings: reduce vCPU/memory or switch to inactive (pay-per-request)

cpu_str = instance_config.get("Cpu", "1 vCPU")
vcpus = float(cpu_str.split()[0])
mem_str = instance_config.get("Memory", "2 GB")
mem_gb = float(mem_str.split()[0])

vcpu_hourly = 0.064 * ctx.pricing_multiplier
mem_hourly = 0.007 * ctx.pricing_multiplier
monthly_active = (vcpus * vcpu_hourly + mem_gb * mem_hourly) * 730
savings = monthly_active * 0.30  # 30% from rightsizing or auto-scaling
```

### B.6 Step Functions (`services/adapters/step_functions.py`)

**Current**: Flat rate keyword-based.
**Approach**: State transitions from CloudWatch.

```python
# Standard workflow: $0.025 per 1,000 state transitions
# Express workflow: $1.00 per 1M state transitions + duration cost
# Savings: switching to Express where applicable, or reducing transitions

if not ctx.fast_mode:
    # CloudWatch: AWS/States, ExecutionsStarted metric
    # Savings: ~60% cost reduction switching Standard→Express for high-frequency short runs
    monthly_transitions = fetch_monthly_transitions(ctx)
    savings = (monthly_transitions / 1000) * 0.025 * 0.60 * ctx.pricing_multiplier
else:
    savings = 150.0  # fallback
```

### B.7 Transfer (`services/adapters/transfer.py`)

**Current**: `$40` per recommendation.
**Approach**: Protocol endpoint pricing.

```python
# AWS Transfer Family: $0.30/protocol/hour per server endpoint
# Savings: remove unused protocols or delete idle servers
protocols = server.get("Protocols", ["SFTP"])
num_protocols = len(protocols)
endpoint_monthly = 0.30 * num_protocols * 730 * ctx.pricing_multiplier
savings = endpoint_monthly  # full cost if server is idle
```

### B.8 MediaStore (`services/adapters/mediastore.py`)

**Current**: `$20` per recommendation.
**Approach**: Container storage GB.

```python
# MediaStore pricing is similar to S3: ~$0.023/GB/month for stored objects
# Savings from deleting unused containers
if ctx.pricing_engine:
    price_per_gb = ctx.pricing_engine.get_s3_monthly_price_per_gb("STANDARD")
else:
    price_per_gb = 0.023 * ctx.pricing_multiplier
# Estimate storage size if CloudWatch ObjectSize metric available; else flat fallback
savings = estimated_gb * price_per_gb if estimated_gb > 0 else 20.0
```

### B.9 QuickSight (`services/adapters/quicksight.py`)

**Current**: `$30` per recommendation.
**Approach**: SPICE capacity pricing.

```python
# QuickSight SPICE: $0.38/GB/month (Enterprise edition), $0.25/GB/month (Standard)
# From describe_account_settings: account_settings["Edition"]
edition = account_settings.get("Edition", "ENTERPRISE")
spice_price = 0.38 if edition == "ENTERPRISE" else 0.25
spice_price *= ctx.pricing_multiplier

# From list_ingestions or describe_data_set: size in bytes
# Savings from reducing SPICE capacity (delete unused datasets)
savings = unused_spice_gb * spice_price if unused_spice_gb > 0 else 30.0
```

### B.10 Containers (`services/adapters/containers.py`)

**Current**: Flat keyword-based rate (Fargate Spot keyword → `$150`).
**Approach**: Fargate task CPU + memory pricing.

```python
# Fargate: $0.04048/vCPU-hour + $0.004445/GB-hour (us-east-1)
# Spot: ~70% cheaper than On-Demand
vcpu_hourly = 0.04048 * ctx.pricing_multiplier
mem_hourly = 0.004445 * ctx.pricing_multiplier

task_vcpu = task_def_cpu / 1024   # task_def["cpu"] is in units (e.g. "256" = 0.25 vCPU)
task_mem_gb = task_def_memory / 1024  # task_def["memory"] in MB

monthly_ondemand = (task_vcpu * vcpu_hourly + task_mem_gb * mem_hourly) * 730 * monthly_task_count
# Spot savings: 70% discount
spot_savings = monthly_ondemand * 0.70
savings = spot_savings if spot_savings > 0 else 150.0
```

---

## Phase C: DynamoDB Actual Capacity Pricing

**File**: `services/adapters/dynamodb.py`
**Current**: `savings = cost * 0.30` flat 30% rate.

**Fix**:
```python
rcu = table.get("ProvisionedThroughput", {}).get("ReadCapacityUnits", 0)
wcu = table.get("ProvisionedThroughput", {}).get("WriteCapacityUnits", 0)

if rcu > 0 or wcu > 0:
    # DynamoDB provisioned capacity pricing (us-east-1 On-Demand):
    # $0.00013/RCU-hour, $0.00065/WCU-hour
    # Apply pricing_multiplier for regional adjustment (no Pricing API path yet)
    rcu_hourly = 0.00013 * ctx.pricing_multiplier
    wcu_hourly = 0.00065 * ctx.pricing_multiplier
    monthly_current = (rcu * rcu_hourly + wcu * wcu_hourly) * 730
    # Reserved capacity discount: ~23% savings for 1yr
    savings = monthly_current * 0.23
else:
    # On-Demand mode — fall back to percentage of estimated cost
    savings = cost * 0.30
```

**Note**: DynamoDB capacity pricing can technically be fetched from the Pricing API (`ServiceCode="AmazonDynamoDB"`, `productFamily="Amazon DynamoDB PayPerRequest Throughput"`). Add a dedicated `get_dynamodb_rcu_hourly_price()` method to `PricingEngine` if exact accuracy is needed. For now, the fallback constants above are correct for us-east-1 and `pricing_multiplier` accounts for other regions.

---

## Adapter Lookup Reference (remaining adapters)

| Adapter file | boto3 call | Field to use |
|---|---|---|
| `adapters/workspaces.py` | `describe_workspaces` | `Workspace["WorkspaceProperties"]["ComputeTypeName"]` |
| `adapters/glue.py` | `get_jobs` | `Job["MaxCapacity"]` or `Job["NumberOfWorkers"]` |
| `adapters/athena.py` | CloudWatch `ProcessedBytes` | AWS/Athena namespace, 30-day sum |
| `adapters/lightsail.py` | `get_instances` | `Instance["bundleName"]` |
| `adapters/apprunner.py` | `describe_service` | `Service["InstanceConfiguration"]["Cpu"]`, `["Memory"]` |
| `adapters/step_functions.py` | CloudWatch `ExecutionsStarted` | AWS/States namespace, 30-day sum |
| `adapters/transfer.py` | `list_servers` | `Server["Protocols"]` (list) |
| `adapters/mediastore.py` | `list_containers` | CloudWatch `ObjectCount` or flat fallback |
| `adapters/quicksight.py` | `describe_account_settings` | `AccountSettings["Edition"]` |
| `adapters/containers.py` | `describe_task_definition` | `TaskDefinition["cpu"]`, `["memory"]` |

---

## Pricing Engine — Service Codes

| Service | ServiceCode | Notes |
|---|---|---|
| EC2 instances | `AmazonEC2` | `instanceType`, `operatingSystem`, `tenancy=Shared` |
| EBS storage | `AmazonEC2` | `volumeApiName`, `productFamily=Storage` |
| EBS IOPS | `AmazonEC2` | `volumeApiName`, `productFamily=System Operation` |
| RDS storage | `AmazonRDS` | `volumeType`, `productFamily=Database Storage`, `deploymentOption` |
| S3 | `AmazonS3` | `storageClass`, `volumeType` |
| ElastiCache | `AmazonElastiCache` | `instanceType`, `location` |
| OpenSearch | `AmazonES` | `instanceType`, `location` |
| Redshift | `AmazonRedshift` | `instanceType`, `location` |
| EFS | `AmazonEFS` | Handled by dedicated `get_efs_monthly_price_per_gb()` |
| WorkSpaces | `AmazonWorkSpaces` | `bundleDescription` or `instanceType` — verify field name from live API |
| Glue | `AWSGlue` | `productFamily=AWS Glue` — per DPU-hour, NOT × 730 |
| Lightsail | `AmazonLightsail` | `bundleName` |
| DMS | `AmazonDMS` | `instanceType`, `location` |

---

## New IAM Permissions Required

```
pricing:GetProducts          # All Pricing API lookups (already needed since Phase 1)
pricing:DescribeServices     # Service catalog
```
No additional permissions beyond what Phase 1 added.

---

## Commit Sequence

```
fix: correct RDS storage pricing for Multi-AZ instances
fix: replace network adapter string-parsing with savings_value numeric key
fix: resource-aware pricing for WorkSpaces, Glue, Athena, Lightsail
fix: resource-aware pricing for AppRunner, Step Functions, Transfer, MediaStore
fix: resource-aware pricing for QuickSight and Fargate containers
fix: DynamoDB provisioned capacity actual pricing (replaces 30% flat rate)
```

Run regression gate after each commit:
```bash
python3 -m pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v
```

---

## Stop Conditions — Escalate to Human

- `ServiceFindings` JSON shape changes (keys added/removed in `core/result_builder.py`)
- Pricing API returns prices differing from fallback by more than 30% — investigate before updating golden files
- New pip dependency required
- `_fetch_generic_instance_price` returns 0 for a service where it previously returned data — the filter may be wrong for that service code
- Golden file diff shows savings going to `0.0` for a service — indicates fallback triggered silently

---

## Quick Validation (run after fixing RDS bug)

```bash
python3 -c "
import boto3
from core.pricing_engine import PricingEngine

client = boto3.client('pricing', region_name='us-east-1')
engine = PricingEngine('us-east-1', client)

# RDS MultiAZ vs Single-AZ should differ
single = engine.get_rds_monthly_storage_price_per_gb('gp2', multi_az=False)
multi  = engine.get_rds_monthly_storage_price_per_gb('gp2', multi_az=True)
print(f'RDS gp2 Single-AZ: \${single:.4f}/GB-month')
print(f'RDS gp2 Multi-AZ:  \${multi:.4f}/GB-month')
assert multi > single, 'Multi-AZ must cost more than Single-AZ'
print('RDS pricing fix: PASS')
print('Warnings:', engine.warnings)
"
```

Expected: Multi-AZ price roughly 2× Single-AZ for gp2.
