# Live Pricing Migration Plan
## AWS Cost Optimization Scanner — Replacing Hardcoded Constants with Live AWS Data

> **Problem**: 20/28 adapters use hardcoded prices or flat-rates that go stale and ignore resource size.
> **Goal**: Every savings estimate reflects the actual resource, actual instance type, and live AWS pricing.

---

## Root Cause Analysis

The scanner has three distinct accuracy problems, each requiring a different fix:

| Problem | Affected Adapters | Current Error | Fix |
|---|---|---|---|
| Hardcoded prices go stale | EBS, RDS, S3, EFS, FSx | ±5-15% annually | AWS Pricing API |
| Flat-rate ignores resource size | 14 adapters | 2-20× error range | Resource-aware pricing |
| No regional pricing for 20 adapters | All keyword/flat adapters | 5-25% error in non-us-east-1 | Pricing API returns regional prices directly |
| Single global multiplier for all services | All adapters | 3-8% systematic error | Service-specific regional lookup |

---

## Architecture: New Pricing Layer

```
┌─────────────────────────────────────────────────────────┐
│                     ScanContext                          │
│  region, clients, warnings, pricing_engine  ← NEW       │
└──────────────────────┬──────────────────────────────────┘
                       │
              ┌────────▼────────┐
              │  PricingEngine  │  core/pricing_engine.py  (NEW)
              ├─────────────────┤
              │ LivePricingClient│  Wraps boto3 pricing API
              │ CostExplorerClient│ Wraps boto3 ce API
              │ PricingCache    │  Session-scoped, no TTL
              │ FallbackTable   │  Hardcoded constants (last resort)
              └────────┬────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
  Formula         Flat-rate      Keyword-rate
  adapters        adapters       adapters
  (EBS, RDS,      (MSK, Redshift (ElastiCache,
  S3, EFS/FSx)    WorkSpaces...) OpenSearch...)
```

### Key Design Decisions

1. **Pricing API is always `us-east-1`** — the API is global; you filter by region display name
2. **Cache at session scope** — one API call per (service, region, instance_type) per scan
3. **Graceful fallback** — if Pricing API fails or returns no result, fall back to hardcoded constants with a warning
4. **No breaking changes to `ServiceFindings` shape** — savings number changes, structure stays identical
5. **`pricing_multiplier` stays in ScanContext** — used as fallback only; `pricing_engine` is the primary path

---

## Phase 1: PricingEngine Core Module

### New file: `core/pricing_engine.py`

```python
"""
Session-scoped pricing engine that fetches live AWS prices via the Pricing API.
Falls back to hardcoded constants if the API is unavailable.
"""
```

**Key implementation points:**

#### 1a. Region Code → Display Name Mapping
The AWS Pricing API filters by display name ("EU (Ireland)"), not region code ("eu-west-1").
This mapping must be maintained as a constant in `pricing_engine.py`:

```python
REGION_DISPLAY_NAMES = {
    "us-east-1":      "US East (N. Virginia)",
    "us-east-2":      "US East (Ohio)",
    "us-west-1":      "US West (N. California)",
    "us-west-2":      "US West (Oregon)",
    "eu-west-1":      "EU (Ireland)",
    "eu-west-2":      "Europe (London)",
    "eu-west-3":      "Europe (Paris)",
    "eu-central-1":   "Europe (Frankfurt)",
    "eu-north-1":     "Europe (Stockholm)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-south-1":     "Asia Pacific (Mumbai)",
    "ap-east-1":      "Asia Pacific (Hong Kong)",
    "sa-east-1":      "South America (Sao Paulo)",
    "af-south-1":     "Africa (Cape Town)",
    "me-south-1":     "Middle East (Bahrain)",
    "me-central-1":   "Middle East (UAE)",
    "ca-central-1":   "Canada (Central)",
    "il-central-1":   "Israel (Tel Aviv)",
    # ... all 38 regions
}
```

#### 1b. PricingEngine class interface

```python
class PricingEngine:
    def __init__(self, region: str, pricing_client, fallback_multiplier: float = 1.0):
        ...

    # EC2
    def get_ec2_hourly_price(self, instance_type: str, os: str = "Linux") -> float:
        """Returns On-Demand hourly price for instance in self.region. Falls back to 0.0."""

    # EBS
    def get_ebs_monthly_price_per_gb(self, volume_type: str) -> float:
        """Returns $/GB/month for volume_type in self.region."""

    def get_ebs_iops_monthly_price(self, volume_type: str) -> float:
        """Returns $/IOPS-month for provisioned IOPS volumes."""

    def get_ebs_throughput_monthly_price(self) -> float:
        """Returns $/MB/s-month for gp3 provisioned throughput above 125 MB/s."""

    # RDS
    def get_rds_monthly_storage_price_per_gb(self, storage_type: str) -> float:
        """Returns $/GB/month for RDS storage type (gp2, gp3, io1) in self.region."""

    def get_rds_backup_storage_price_per_gb(self) -> float:
        """Returns $/GB/month for RDS backup storage beyond free tier."""

    # S3
    def get_s3_monthly_price_per_gb(self, storage_class: str) -> float:
        """Returns $/GB/month for S3 storage class in self.region."""

    # Compute: instance price lookup (for keyword-rate adapters)
    def get_instance_monthly_price(self, service_code: str, instance_type: str) -> float:
        """Returns On-Demand monthly price for any instance type (ElastiCache, OpenSearch, MSK, etc.)"""

    # Regional ratio helper (replaces REGIONAL_PRICING for fallback)
    def get_regional_ratio_vs_useast1(self, service_code: str) -> float:
        """Returns price(region) / price(us-east-1) for a representative instance."""
```

#### 1c. Pricing API call pattern

```python
def _fetch_ec2_price(self, instance_type: str, os: str) -> float | None:
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType",   "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "location",       "Value": self._display_name},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os},
        {"Type": "TERM_MATCH", "Field": "tenancy",        "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "capacityStatus", "Value": "Used"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
    ]
    resp = self._pricing.get_products(ServiceCode="AmazonEC2", Filters=filters, MaxResults=1)
    if not resp["PriceList"]:
        return None
    price_item = json.loads(resp["PriceList"][0])
    # Navigate: terms → OnDemand → <key> → priceDimensions → <key> → pricePerUnit → USD
    return _extract_usd(price_item)
```

#### 1d. Cache layer

```python
class PricingCache:
    """Simple dict cache — one entry per (service_code, params_tuple) per session."""
    _data: dict[tuple, float] = {}

    def get(self, key: tuple) -> float | None: ...
    def set(self, key: tuple, value: float) -> None: ...
```

#### 1e. Fallback constants (moved from scattered files into one place)

```python
# core/pricing_engine.py — fallback constants (used only when Pricing API fails)
FALLBACK_EBS_PRICES = {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.025}
FALLBACK_S3_PRICES  = {"STANDARD": 0.023, "STANDARD_IA": 0.0125, "ONEZONE_IA": 0.01, ...}
FALLBACK_REGIONAL_MULTIPLIERS = {"us-east-1": 1.0, "eu-west-1": 1.10, ...}  # existing dict
```

### ScanContext changes (`core/scan_context.py`)

```python
@dataclass
class ScanContext:
    region: str
    clients: ClientRegistry
    warnings: list[str]
    pricing_multiplier: float = 1.0          # KEEP — used as fallback
    pricing_engine: PricingEngine | None = None  # NEW — primary pricing path
    ...
```

`pricing_multiplier` is retained for backward compatibility and as fallback; adapters should prefer `ctx.pricing_engine` when available.

### `cost_optimizer.py` changes

```python
# In CostOptimizer.__init__ or scan():
pricing_client = session.client("pricing", region_name="us-east-1")
pricing_engine = PricingEngine(
    region=self.region,
    pricing_client=pricing_client,
    fallback_multiplier=self.pricing_multiplier,
)
ctx = ScanContext(
    region=self.region,
    clients=...,
    warnings=...,
    pricing_multiplier=self.pricing_multiplier,
    pricing_engine=pricing_engine,
)
```

**New IAM permission required**: `pricing:GetProducts`, `pricing:DescribeServices`

---

## Phase 2: Migrate Formula Adapters (EBS, RDS, S3, EFS/FSx)

These already apply `pricing_multiplier`. Replace hardcoded constants with `pricing_engine` calls.

### EBS (`services/ebs.py`)

**Before:**
```python
pricing = {"gp2": 0.10, "gp3": 0.08, ...}
base_cost = size_gb * pricing[volume_type] * pricing_multiplier
```

**After:**
```python
price_per_gb = ctx.pricing_engine.get_ebs_monthly_price_per_gb(volume_type)
base_cost = size_gb * price_per_gb
# pricing_multiplier NOT applied — Pricing API already returns regional price
```

**EBS gp2→gp3 savings formula** (`services/adapters/ebs.py:44`):
```python
# Before: size * 0.10 * 0.20
gp2_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp2")
gp3_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp3")
savings += size * (gp2_price - gp3_price)
```

### RDS (`services/rds.py`)

**Before:**
```python
monthly_cost = allocated_storage * 0.115 * pricing_multiplier
savings = monthly_cost * 0.20
```

**After:**
```python
gp2_price = ctx.pricing_engine.get_rds_monthly_storage_price_per_gb("gp2")
gp3_price = ctx.pricing_engine.get_rds_monthly_storage_price_per_gb("gp3")
monthly_cost = allocated_storage * gp2_price
savings = allocated_storage * (gp2_price - gp3_price)
```

### S3 (`services/s3.py`)

Replace `S3_STORAGE_COSTS` dict lookups with `pricing_engine.get_s3_monthly_price_per_gb(storage_class)`.
Remove `S3_REGIONAL_MULTIPLIERS` entirely — Pricing API returns per-region prices directly.

### EFS/FSx (`services/efs_fsx.py`)

Replace `_estimate_efs_cost` and `_estimate_fsx_cost` hardcoded dicts with Pricing API calls.
For EFS: ServiceCode = `AmazonEFS`; for FSx: ServiceCode = `AmazonFSx`.

---

## Phase 3: Resource-Aware Flat-Rate Adapters

This is the highest-impact change. Instead of `$X × count`, query the actual instance type for each resource and price it specifically.

### Pattern for instance-based adapters

```python
# Generic pattern (to be applied per adapter)
def _compute_ri_savings(instance_type: str, pricing_engine, ri_discount_rate: float) -> float:
    """Estimate monthly RI savings = on_demand_monthly * ri_discount_rate."""
    hourly = pricing_engine.get_instance_monthly_price(service_code, instance_type)
    return hourly * ri_discount_rate
```

### Per-adapter migration

| Adapter | Current | New approach | Instance type source |
|---|---|---|---|
| **Redshift** | $200/rec | `node_type` from `describe_clusters` → Pricing API → × 0.35 (1yr RI discount) | `Cluster.NodeType` |
| **MSK** | $150/rec | `InstanceType` from `list_clusters` → EC2 Pricing API → × brokers × 0.30 | `BrokerNodeGroupInfo.InstanceType` |
| **ElastiCache** | keyword → $80-$200 | `CacheNodeType` → ElastiCache Pricing API → keyword determines discount rate | `CacheCluster.CacheNodeType` |
| **OpenSearch** | keyword → $50-$300 | `InstanceType` from `describe_domain` → OpenSearch Pricing API → keyword determines discount rate | `ClusterConfig.InstanceType` |
| **WorkSpaces** | $35/rec | Bundle ID from `describe_workspaces` → WorkSpaces bundle monthly price delta (AlwaysOn vs AutoStop) | `WorkspaceProperties.ComputeTypeName` |
| **Batch** | $150/rec | Job queue compute env instance types → spot discount applied to On-Demand price | `ComputeEnvironment.ComputeResources.InstanceTypes` |
| **Glue** | $100/rec | DPU count from job config → `$0.44/DPU-hour × running_hours` (Pricing API for DPU rate) | `Job.MaxCapacity` |
| **API Gateway** | $15/rec | REST API call volume from CloudWatch → `($3.50 - $1.00)/million × monthly_calls` | CloudWatch `Count` metric |
| **CloudFront** | $25/rec | Distribution monthly data transfer from CloudWatch → price class delta | CloudWatch `BytesDownloaded` |
| **MSK** | $150/rec | Broker instance type → On-Demand price × broker count × 0.30 | `BrokerNodeGroupInfo.InstanceType` |
| **Athena** | $50/rec | Bytes scanned from CloudWatch → `$5/TB × monthly_scanned_TB × optimization_ratio` | CloudWatch `ProcessedBytes` |
| **Lightsail** | $12/rec | Instance bundle ID → Lightsail monthly price | `Instance.BundleId` |
| **MediaStore** | $20/rec | Container storage GB → `$0.023/GB/month` (S3-equivalent storage) | `Container.ARN` → S3 pricing |
| **QuickSight** | $30/rec | SPICE capacity GB from `describe_account_settings` → `$0.38/GB/month` (SPICE pricing) | `AccountSettings.Edition` |
| **Transfer** | $40/rec | Protocol endpoints × `$0.30/endpoint-hour × 730` | `Server.Protocols` |
| **DMS** | $50/rec | Replication instance class → EC2 Pricing API equivalent | `ReplicationInstance.ReplicationInstanceClass` |
| **AppRunner** | $25/rec | vCPU + memory config → AppRunner pricing | `Service.InstanceConfiguration` |
| **Step Functions** | keyword → $150-$200 | State transition count from CloudWatch → Standard vs Express price delta | CloudWatch `ExecutionsStarted`, `ExecutionTime` |

### Containers keyword-rate upgrade

```python
# Current: "spot" keyword → $150 flat
# New:
if "spot" in recommendation:
    # Get ECS task definition CPU/memory → find equivalent EC2 price → apply spot discount
    task_vcpu = task_def.get("cpu", "256") / 1024  # vCPUs
    task_mem_gb = task_def.get("memory", "512") / 1024
    fargate_price = pricing_engine.get_fargate_price(task_vcpu, task_mem_gb)
    spot_savings = fargate_price * monthly_task_hours * 0.70  # Spot typically 70% cheaper
```

---

## Phase 4: Upgrade Percentage-Based Adapters

### DynamoDB (30% assumption)

Replace with actual RCU/WCU pricing:
- Get `ProvisionedThroughput.ReadCapacityUnits` and `WriteCapacityUnits` from scan
- Pricing API: RCU = $0.00013/RCU-hour, WCU = $0.00065/WCU-hour (us-east-1, on-demand mode)
- Actual savings = (current RCU/WCU cost) × ri_discount_rate (Reserved: ~23% 1yr, 48% 3yr)
- The DynamoDB MCP Server (`mcp__dynamodb`) already has `compute_performances_and_costs` — use it

### File Systems (30% assumption)

Replace with actual EFS tier pricing delta:
- Current: `EstimatedMonthlyCost × 0.3`
- New: `(current_size_gb × standard_price) - (size_gb × 0.2 × standard_price + size_gb × 0.8 × ia_price)`
- This is the actual savings from enabling lifecycle management (standard 80/20 split assumption still valid but now uses live prices)

---

## Phase 5: Network String-Parse Upgrade

The network adapter parses hardcoded strings from legacy shim files. Replace the strings with computed values:

### In `services/elastic_ip.py`
```python
# Before: "EstimatedSavings": "$3.65/month per EIP"
# After: compute from Pricing API
eip_hourly = pricing_engine.get_eip_hourly_price()  # pricing:GetProducts, ServiceCode=AmazonEC2, volumeType=EIP
eip_monthly = eip_hourly * 730
"EstimatedSavings": f"${eip_monthly:.2f}/month per EIP"
```

### In `services/nat_gateway.py`
```python
nat_hourly = pricing_engine.get_nat_gateway_hourly_price()
nat_monthly = nat_hourly * 730
"EstimatedSavings": f"${nat_monthly:.2f}/month base + data processing fees"
```

Similarly for VPC endpoints, ALB, NLB.

**Important**: ALB/NLB use LCU-based pricing — flat monthly estimate is fundamentally wrong.
Replace with: `CloudWatch LCU metrics × LCU price/hour × 730` or acknowledge the limitation.

---

## Phase 6: IAM Permissions Audit

Add to documentation / README:

```
New permissions required for live pricing:

pricing:GetProducts          # AWS Pricing API — all service lookups
pricing:DescribeServices     # List available services and attributes
ce:GetCostAndUsage           # Cost Explorer — actual spend data (optional)
ce:GetReservationPurchaseRecommendation  # RI recommendations
application-autoscaling:DescribeScalableTargets  # DynamoDB capacity lookup
cloudwatch:GetMetricStatistics  # Athena/API Gateway/CloudFront usage metrics
```

---

## AWS MCPs to Add to This Project

### Currently in use
- `aws-knowledge-mcp-server` — AWS documentation validation ✓

### Recommended additions

#### MUST ADD: AWS Pricing MCP Server
```json
{
  "mcpServers": {
    "aws-pricing": {
      "command": "uvx",
      "args": ["awslabs.aws-pricing-mcp-server@latest"],
      "env": { "AWS_REGION": "us-east-1" }
    }
  }
}
```
**Use for**: Validating live prices during development/audit, querying pricing for new adapters before implementation, cross-checking the `PricingEngine` output against documentation.

#### MUST ADD: AWS Billing and Cost Management MCP Server
```json
{
  "mcpServers": {
    "aws-cost": {
      "command": "uvx",
      "args": ["awslabs.cost-analysis-mcp-server@latest"],
      "env": { "AWS_PROFILE": "<your-profile>" }
    }
  }
}
```
**Use for**: Cost Explorer queries during development, validating Cost Hub savings estimates, Compute Optimizer recommendation accuracy checks, RI/Savings Plans coverage analysis.

#### RECOMMENDED: Amazon CloudWatch MCP Server
```json
{
  "mcpServers": {
    "aws-cloudwatch": {
      "command": "uvx",
      "args": ["awslabs.cloudwatch-mcp-server@latest"]
    }
  }
}
```
**Use for**: Pulling actual usage metrics for Athena (bytes scanned), API Gateway (call count), CloudFront (data transfer) to make Phase 3 resource-aware adapters accurate.

#### OPTIONAL: AWS IAM MCP Server
**Use for**: Validating that the IAM policy templates in the docs match what the new `pricing:GetProducts` and `ce:GetCostAndUsage` calls actually require.

### MCPs in the repo that are NOT needed for this project

| MCP | Reason not needed |
|---|---|
| Aurora/RDS/DocumentDB MCPs | We read from RDS via boto3, not query it |
| Lambda Tool MCP | We analyze Lambda costs, not invoke Lambda |
| Step Functions Tool MCP | Same reason |
| AI/ML MCPs (SageMaker, Bedrock) | Not relevant to cost scanning |
| IoT, Health, SAP MCPs | Out of scope |

---

## Implementation Order & Effort

| Phase | Work | Effort | Impact |
|---|---|---|---|
| 1. PricingEngine core | New `core/pricing_engine.py`, update `ScanContext`, update `cost_optimizer.py` | 2 days | Enables all other phases |
| 2. Formula adapters | EBS, RDS, S3, EFS/FSx — update 4 shim files | 1 day | Fixes price staleness |
| 3a. High-impact flat-rate | Redshift, MSK, ElastiCache, OpenSearch (biggest $$) | 2 days | Largest accuracy gain |
| 3b. Remaining flat-rate | WorkSpaces, Batch, Glue, API Gateway, CloudFront, Athena | 2 days | Completes resource-awareness |
| 3c. Minor flat-rate | Lightsail, MediaStore, QuickSight, Transfer, DMS, AppRunner | 1 day | Long tail |
| 4. Percentage adapters | DynamoDB, File Systems | 1 day | Fixes 30% assumption |
| 5. Network strings | elastic_ip, nat_gateway, vpc_endpoints, load_balancer | 1 day | Fixes hardcoded strings |
| 6. Testing | Update aws_stubs/, update snapshot golden files, add pricing mock | 2 days | Regression safety |
| **Total** | | **~12 days** | |

---

## Testing Strategy

### New test fixture: `tests/aws_stubs/pricing_stub.py`

```python
STUB_PRICING_RESPONSES = {
    ("AmazonEC2", "m5.large", "us-east-1"): 0.096,      # $/hour
    ("AmazonEC2", "gp3",      "us-east-1"): 0.08,       # $/GB/month
    ("AmazonEC2", "gp2",      "us-east-1"): 0.10,
    ("AmazonRDS", "gp2",      "us-east-1"): 0.115,      # $/GB/month
    ("AmazonS3",  "STANDARD", "us-east-1"): 0.023,
    # ... etc
}
```

### Pricing API mock in `conftest.py`

```python
@pytest.fixture
def mock_pricing_engine(monkeypatch):
    engine = MockPricingEngine(STUB_PRICING_RESPONSES)
    monkeypatch.setattr("core.pricing_engine.PricingEngine", lambda *a, **kw: engine)
    return engine
```

### New test: `tests/test_pricing_engine.py`

```python
def test_ebs_gp3_price_live(real_aws_credentials):
    """Integration test — only runs with --live flag."""
    engine = PricingEngine("us-east-1", ...)
    price = engine.get_ebs_monthly_price_per_gb("gp3")
    assert 0.07 <= price <= 0.10, f"gp3 price {price} outside expected range"

def test_fallback_on_api_failure(mock_pricing_client_that_raises):
    engine = PricingEngine("eu-west-1", mock_pricing_client_that_raises, fallback_multiplier=1.10)
    price = engine.get_ebs_monthly_price_per_gb("gp3")
    assert price == 0.08 * 1.10  # fallback constant × multiplier
    assert "pricing API unavailable" in engine.warnings
```

### Update snapshot golden files

After Phase 2 (formula adapter migration), run:
```bash
pytest tests/test_regression_snapshot.py --update-snapshots
pytest tests/test_reporter_snapshots.py --update-snapshots
```
The savings numbers will change; the JSON structure must not.

---

## Fallback Strategy (Non-Negotiable)

The Pricing API requires:
- Network access to `pricing.us-east-1.amazonaws.com`
- `pricing:GetProducts` IAM permission
- Response latency: ~200-500ms per call (mitigated by caching)

If ANY of these fail, the scanner must still work. Fallback chain:

```
1. Try: pricing_engine.get_ebs_monthly_price_per_gb("gp3")  → $0.08 (live)
2. Miss cache → call Pricing API → parse → cache → return
3. API failure → log warning to ctx.warnings → return FALLBACK_EBS_PRICES["gp3"] * fallback_multiplier
4. Unknown volume type → return FALLBACK_EBS_PRICES.get(volume_type, 0.10) * fallback_multiplier
```

Warnings surface in the HTML report under a "Pricing Accuracy" section so users know when estimates are approximate.

---

## Migration Compatibility

- `ServiceFindings.total_monthly_savings` value will change — this is expected and correct
- `ServiceFindings` structure stays identical — no JSON shape change
- `pricing_multiplier` stays in `ScanContext` — adapters not yet migrated continue to work
- `REGIONAL_PRICING` dict stays in `cost_optimizer.py` — used as fallback, not removed
- All 131 existing tests continue to pass (via mock pricing engine in fixtures)
- New golden files generated after Phase 2 with updated savings values

---

## Summary: What This Fixes

| Before | After |
|---|---|
| EBS gp2→gp3 savings: `$0.10 × 0.20` (hardcoded, stale) | EBS gp2→gp3 savings: `(live_gp2 - live_gp3) × size_gb` |
| MSK savings: `$150` regardless of cluster size | MSK savings: `instance_type_price × broker_count × 0.30` |
| eu-west-1 flat-rate savings underreported by 10% | eu-west-1 savings correct — Pricing API returns regional price |
| Single multiplier for EC2, S3, EBS in same region | Service-specific regional prices from Pricing API |
| Prices stale from date of last code edit | Prices live at scan time, cached for session |
| EIP: hardcoded `$3.65/month` | EIP: `pricing_engine.get_eip_hourly() × 730` |
