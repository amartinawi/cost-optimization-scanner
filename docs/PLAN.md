# AWS Cost Optimization Scanner — Implementation Plan

> **Scope**: Detailed build specification for all roadmap phases (ROADMAP.md).
> **Architecture baseline**: 28 registered adapters, `ServiceModule` Protocol in `core/contracts.py`,
> `PricingEngine` in `core/pricing_engine.py`, `ScanContext` in `core/scan_context.py`.
> **Last Updated**: May 2026

---

## ROADMAP ↔ PLAN Cross-Reference

ROADMAP.md combines some items; this PLAN splits them for implementation detail.

| ROADMAP Item | PLAN Section(s) | Difference |
|---|---|---|
| 1.1 Compute Optimizer | 1.1 | Same |
| 1.2 Cost Optimization Hub | 1.2 | Same |
| 1.3 Aurora | 1.3 | Same |
| 1.4 Savings Plans & RI | 1.4 | Same |
| 2.1 AI/ML (Bedrock + SageMaker) | 2.1 Bedrock, 2.2 SageMaker | Split for per-service API detail |
| 2.2 Network & Data Transfer | 2.3 | Renumbered |
| 2.3 Cost Anomaly | 2.4 | Renumbered |
| 2.4 Kubernetes/EKS | 2.5 | Renumbered |
| 3.1–3.6 | 3.1–3.6 | Same |
| 4.1–4.6 | 4.1–4.6 | Same (vision items, minimal detail) |

---

## How to Read This Document

Each item specifies:
- **AWS APIs used** — exact boto3 calls and client names
- **Pricing calculation** — exact formula, fallbacks, `PricingEngine` method requirements
- **`ServiceFindings` shape** — `sources` dict keys, `extras`, `optimization_descriptions`
- **HTML report integration** — stat cards, grouping strategy, column layout
- **IAM permissions** — minimum IAM actions required
- **Integration points** — how it hooks into `ScanOrchestrator`, `ScanContext`, etc.
- **Validation criteria** — what "done" means before the item ships

---

## Architecture Refresher

```
cost_optimizer.py
  └─ ScanOrchestrator.run()
       ├─ _prefetch_advisor_data()        # Cost Hub pre-fetch for ec2/ebs/lambda/rds
       └─ safe_scan(module, ctx)          # per-adapter, error-isolated
            └─ module.scan(ctx: ScanContext) -> ServiceFindings
                                           # ctx.pricing_engine  → PricingEngine
                                           # ctx.pricing_multiplier → float
                                           # ctx.cost_hub_splits → pre-fetched hub data
                                           # ctx.clients.client("name") → boto3 client
```

Every adapter must:
1. Extend `BaseServiceModule` (`services/_base.py`)
2. Declare `key`, `cli_aliases`, `display_name`
3. Implement `required_clients() -> tuple[str, ...]`
4. Implement `scan(ctx) -> ServiceFindings`
5. Be appended to `ALL_MODULES` in `services/__init__.py`

---

## Phase 1: Quick Wins

---

### 1.1 Compute Optimizer Integration

**File**: `services/adapters/compute_optimizer.py`
**Priority**: 25/25 | **Estimated effort**: 3–5 days

#### What It Does

AWS Compute Optimizer uses CloudWatch metrics and ML models to generate rightsizing recommendations for EC2, EBS, Lambda, ECS, and ASGs. This adapter consumes those recommendations directly rather than deriving them from raw CloudWatch data.

The EC2 adapter (`services/adapters/ec2.py`) already calls `get_ec2_compute_optimizer_recommendations()` from `services/advisor.py`. This new adapter **extends** that pattern to cover the remaining resource types that the EC2 adapter does not (EBS, Lambda, ECS, ASG) and surfaces them as a unified cross-service optimization report.

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("compute-optimizer",)
```

| API Call | Purpose |
|---|---|
| `compute_optimizer.get_recommendation_summaries(accountIds=[ctx.account_id])` | Account-wide savings potential by resource type |
| `compute_optimizer.get_ec2_instance_recommendations()` | EC2 instance type change recommendations |
| `compute_optimizer.get_ebs_volume_recommendations()` | EBS volume type/size recommendations |
| `compute_optimizer.get_lambda_function_recommendations()` | Lambda memory and ARM recommendations |
| `compute_optimizer.get_ecs_service_recommendations()` | ECS task CPU/memory recommendations |
| `compute_optimizer.get_auto_scaling_group_recommendations()` | ASG instance type recommendations |

All calls accept `accountIds=[ctx.account_id]` and use pagination (`nextToken`). Wrap each call in `try/except` and call `ctx.permission_issue(...)` on `ClientError`.

#### Pricing Calculation

Compute Optimizer returns `estimatedMonthlySavings.value` (float, USD) and `estimatedMonthlySavingsPercentage` on every finding. **Use the API values directly** — do not recalculate.

Apply `ctx.pricing_multiplier` as a regional adjustment multiplier on top of API values:

```python
savings = rec["estimatedMonthlySavings"]["value"] * ctx.pricing_multiplier
```

Fallback: if `estimatedMonthlySavings` is missing or zero, set savings to `0.0` (do not estimate).

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="Compute Optimizer",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "ec2_recommendations": SourceBlock(
            count=len(ec2_recs),
            recommendations=tuple(ec2_recs),
            extras={"finding_types": {"OVER_PROVISIONED": n, "UNDER_PROVISIONED": n}},
        ),
        "ebs_recommendations": SourceBlock(count=..., recommendations=...),
        "lambda_recommendations": SourceBlock(count=..., recommendations=...),
        "ecs_recommendations": SourceBlock(count=..., recommendations=...),
        "asg_recommendations": SourceBlock(count=..., recommendations=...),
        "summary": SourceBlock(
            count=len(summaries),
            recommendations=tuple(summaries),
            extras={"total_savings_opportunity": float},
        ),
    },
    extras={
        "opt_in_required": bool,   # True if enhanced mode not opted in
        "resource_counts": {
            "ec2": n, "ebs": n, "lambda": n, "ecs": n, "asg": n
        },
    },
)
```

Each recommendation dict must include: `resource_id`, `resource_type`, `finding` (OVER_PROVISIONED / UNDER_PROVISIONED / OPTIMIZED / NOT_OPTIMIZED), `current_config`, `recommended_config`, `estimatedMonthlySavings`, `lookback_period_days`.

#### De-duplication with EC2 Adapter

The EC2 adapter already calls `get_ec2_compute_optimizer_recommendations()` for EC2 instances. The Compute Optimizer adapter covers **EBS, Lambda, ECS, ASG** only — it must **skip EC2** to avoid double-counting in the total savings summary.

Set `skip_ec2=True` in the adapter and document this in the class docstring.

#### HTML Report Integration

**Tab name**: "Compute Optimizer"

**Stat cards**:
| Label | Source Path | Formatter |
|---|---|---|
| EC2 Recommendations | `sources.ec2_recommendations.count` | int |
| EBS Recommendations | `sources.ebs_recommendations.count` | int |
| Lambda Recommendations | `sources.lambda_recommendations.count` | int |
| Monthly Savings | `total_monthly_savings` | currency |

**Grouping**: `GroupingSpec(by="resource_type")` — groups by the `resource_type` field on each recommendation.

**Column layout** per recommendation row:
- Resource ID / ARN
- Resource Type
- Finding (OVER_PROVISIONED badge, UNDER_PROVISIONED badge)
- Current Config (instance type / memory / volume type)
- Recommended Config
- Est. Monthly Savings ($)
- Lookback Period

**Special rendering**: If `opt_in_required=True` in `extras`, display a yellow banner: "Enhanced Compute Optimizer recommendations require Opt-In — currently showing basic recommendations."

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "compute-optimizer:GetRecommendationSummaries",
    "compute-optimizer:GetEC2InstanceRecommendations",
    "compute-optimizer:GetEBSVolumeRecommendations",
    "compute-optimizer:GetLambdaFunctionRecommendations",
    "compute-optimizer:GetECSServiceRecommendations",
    "compute-optimizer:GetAutoScalingGroupRecommendations"
  ],
  "Resource": "*"
}
```

#### Validation Criteria

- [ ] Returns empty `ServiceFindings` (not exception) when Compute Optimizer is not opted in
- [ ] EC2 recommendations are excluded to avoid double-counting
- [ ] `estimatedMonthlySavings` from API takes precedence over any computed value
- [ ] `pricing_multiplier` applied consistently across all sources
- [ ] Pagination handled for accounts with >100 recommendations per type
- [ ] `pytest tests/test_regression_snapshot.py` passes with new adapter registered

---

### 1.2 Cost Optimization Hub Aggregation

**File**: `services/adapters/cost_optimization_hub.py`
**Priority**: 24/25 | **Estimated effort**: 3–5 days

#### What It Does

AWS Cost Optimization Hub aggregates 18+ recommendation types from Compute Optimizer, Trusted Advisor, Savings Plans analysis, and Reserved Instance analysis into a single API. The scanner already pre-fetches Cost Hub data for EC2/EBS/Lambda/RDS via `ScanOrchestrator._prefetch_advisor_data()`. This adapter:

1. Extends the pre-fetch to cover **all 18 recommendation types**
2. Routes recommendations to the appropriate existing adapters via `ctx.cost_hub_splits`
3. Surfaces the **unrouted recommendations** (EKS, S3, ElastiCache, OpenSearch, Redshift) that existing adapters do not yet consume

#### Changes to `ScanOrchestrator`

Extend `_prefetch_advisor_data()` to populate additional hub splits:

```python
# Current splits: ec2, lambda, ebs, rds
# Add:
splits["elasticache"] = []
splits["opensearch"] = []
splits["redshift"] = []
splits["s3"] = []
splits["eks"] = []
```

Add a new `type_map` entry for each new `RecommendationType`:
```python
"ElastiCacheReservedInstances": "elasticache",
"OpenSearchReservedInstances": "opensearch",
"RedshiftReservedInstances": "redshift",
"S3StorageOptimization": "s3",
"S3GlacierInstantAccess": "s3",
"EksClusterRightsizing": "eks",
```

Each existing adapter (`elasticache.py`, `opensearch.py`, `redshift.py`, `s3.py`) should then check `ctx.cost_hub_splits.get("key", [])` and fold those recommendations into its own `ServiceFindings`, the same pattern EC2 already uses.

#### New Adapter for Residual Recommendations

The `cost_optimization_hub.py` adapter handles recommendation types **not consumed by any existing adapter**. Currently that is: `LambdaEventBridgeIdleRule`, `RdsComputeSavingsPlans`, `ComputeSavingsPlans`, `Ec2InstanceSavingsPlans` (the Savings Plans types — distinct from rightsizing and better surfaced as a cross-service view).

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("cost-optimization-hub",)
```

| API Call | Purpose |
|---|---|
| `cost_optimization_hub.list_recommendations(filter={...})` | Paginated list of all recommendations |
| `cost_optimization_hub.get_recommendation(recommendationId=...)` | Detail for a specific recommendation |

`list_recommendations` accepts `filter.actionTypes`, `filter.resourceTypes`, `filter.regions`. Use `filter.regions=[ctx.region]` to scope to the current region.

Pagination: use `nextToken` until exhausted.

#### Pricing Calculation

Cost Optimization Hub returns `estimatedMonthlySavings` (float, USD) directly on each recommendation. Use as-is, multiplied by `ctx.pricing_multiplier`.

```python
savings = rec.get("estimatedMonthlySavings", 0.0) * ctx.pricing_multiplier
```

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="Cost Optimization Hub",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "savings_plans": SourceBlock(
            count=len(sp_recs),
            recommendations=tuple(sp_recs),
        ),
        "cross_service": SourceBlock(
            count=len(cross_recs),
            recommendations=tuple(cross_recs),
        ),
    },
    extras={
        "recommendation_type_counts": {"ComputeSavingsPlans": n, ...},
        "total_recommendations_in_hub": int,  # before de-dup
    },
    optimization_descriptions={
        "savings_plans": {
            "title": "Savings Plans Opportunities",
            "description": "Commitment-based discounts identified by Cost Optimization Hub",
        },
        "cross_service": {
            "title": "Cross-Service Recommendations",
            "description": "Recommendations not addressed by individual service adapters",
        },
    },
)
```

#### HTML Report Integration

**Tab name**: "Cost Hub"

**Stat cards**:
| Label | Source Path | Formatter |
|---|---|---|
| Savings Plan Opps | `sources.savings_plans.count` | int |
| Cross-Service Recs | `sources.cross_service.count` | int |
| Monthly Savings | `total_monthly_savings` | currency |

**Grouping**: `GroupingSpec(by="check_category")` — group by `recommendationType` field.

**Column layout**:
- Recommendation Type
- Resource ID
- Current Resource
- Recommended Action
- Est. Monthly Savings ($)
- Implementation Effort (from Hub metadata)

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "cost-optimization-hub:ListRecommendations",
    "cost-optimization-hub:GetRecommendation"
  ],
  "Resource": "*"
}
```

#### Validation Criteria

- [ ] `cost_hub_splits` extended with 6 new service keys before adapters run
- [ ] No double-counting: recommendations routed to existing adapters must not appear in Cost Hub tab
- [ ] Pagination tested for accounts with >1000 Hub recommendations
- [ ] Empty findings returned gracefully if Cost Optimization Hub is not enabled in the account
- [ ] Existing EC2/EBS/Lambda/RDS adapter tests still pass after orchestrator changes

---

### 1.3 Aurora-Specific Checks

**File**: `services/adapters/aurora.py`
**Priority**: 20/25 | **Estimated effort**: 5–7 days

#### What It Does

The existing `rds.py` adapter covers generic RDS instances (Multi-AZ, backup, idle detection). Aurora clusters have distinct cost drivers that require separate analysis:
- Serverless v2 min/max ACU misconfiguration
- I/O-Optimized storage tier vs Standard (Aurora I/O-Optimized costs up to 40% more but saves on I/O charges at high I/O workloads)
- Clone sprawl (Aurora fast clones accumulate storage that is billed)
- Global Database replica lag (indicates underutilization)
- Backtrack storage growth

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("rds", "cloudwatch")
```

| API Call | Purpose |
|---|---|
| `rds.describe_db_clusters(Filters=[{"Name":"engine","Values":["aurora","aurora-mysql","aurora-postgresql"]}])` | List all Aurora clusters |
| `rds.describe_db_cluster_snapshots(SnapshotType="manual")` | Manual snapshots (clone-related) |
| `rds.describe_global_clusters()` | Global Database configurations |
| `rds.describe_db_instances(Filters=[{"Name":"db-cluster-id","Values":[cluster_id]}])` | Members of each cluster |
| `cloudwatch.get_metric_data(...)` | `ServerlessV2CapacityUtilization`, `VolumeReadIOPs`, `VolumeWriteIOPs`, `AuroraReplicaLag` |

CloudWatch queries respect `ctx.fast_mode`: if `fast_mode=True`, skip CloudWatch calls and return recommendations based on configuration only.

#### Pricing Calculation

**Serverless v2 misconfiguration**:
```
waste_acu = (max_acu - avg_acu_utilization * max_acu)
hourly_waste = waste_acu * ACU_HOURLY_PRICE  # $0.06/ACU/hr (us-east-1)
monthly_waste = hourly_waste * 730 * ctx.pricing_multiplier
```
ACU price falls back to `PricingEngine` if a method is added; otherwise use module constant `ACU_HOURLY_PRICE = 0.06`.

**I/O-Optimized vs Standard**: Compare monthly I/O cost under Standard tier vs the premium on I/O-Optimized tier:
```
io_ops = VolumeReadIOPs + VolumeWriteIOPs  # from CloudWatch, 30-day average
io_cost_standard = io_ops * 0.20 / 1_000_000  # per million I/O ops
storage_premium = storage_gb * 0.025          # I/O-Optimized premium $/GB/month
if io_cost_standard > storage_premium: recommend Standard
else: recommend I/O-Optimized
```

**Clone sprawl**: Each clone's storage = actual diverged bytes. Estimate: `storage_gb * 0.10` per month (Aurora storage pricing $/GB × 0.10 divergence assumption if not measurable).

**Global Database lag**: No direct savings calculation — flag as a risk/waste indicator with `monthly_savings=0`.

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="Aurora",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "serverless_v2": SourceBlock(count=..., recommendations=...),
        "io_tier_analysis": SourceBlock(count=..., recommendations=...),
        "clone_sprawl": SourceBlock(count=..., recommendations=...),
        "global_db": SourceBlock(count=..., recommendations=...),
        "backtrack": SourceBlock(count=..., recommendations=...),
    },
    extras={
        "cluster_count": int,
        "serverless_cluster_count": int,
        "global_cluster_count": int,
    },
    optimization_descriptions={
        "serverless_v2": {
            "title": "Serverless v2 Capacity Configuration",
            "description": "ACU range (min/max) is wider than observed utilization",
        },
        "io_tier_analysis": {
            "title": "I/O Storage Tier Optimization",
            "description": "Compare I/O-Optimized premium vs per-I/O charges",
        },
        ...
    },
)
```

Each recommendation dict includes: `cluster_id`, `engine`, `engine_version`, `check_type`, `current_value`, `recommended_value`, `monthly_savings`, `reason`.

#### HTML Report Integration

**Tab name**: "Aurora"

**Stat cards**:
| Label | Source Path | Formatter |
|---|---|---|
| Aurora Clusters | `extras.cluster_count` | int |
| Serverless v2 Clusters | `extras.serverless_cluster_count` | int |
| Monthly Savings | `total_monthly_savings` | currency |

**Grouping**: `GroupingSpec(by="check_category")` — group by `check_type` field on each recommendation.

**Column layout**:
- Cluster ID
- Engine / Version
- Check Type
- Current Config
- Recommended Config / Action
- Est. Monthly Savings ($)
- Severity (High / Medium / Low)

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "rds:DescribeDBClusters",
    "rds:DescribeDBClusterSnapshots",
    "rds:DescribeGlobalClusters",
    "rds:DescribeDBInstances",
    "cloudwatch:GetMetricData"
  ],
  "Resource": "*"
}
```

#### Validation Criteria

- [ ] Aurora clusters identified correctly (engine filter excludes non-Aurora RDS)
- [ ] Serverless v2 ACU check skipped for provisioned clusters
- [ ] I/O tier recommendation only surfaced when CloudWatch data is available (skip in fast_mode)
- [ ] `fast_mode=True` path returns config-only checks without CloudWatch
- [ ] Savings formula produces $0 for I/O-Optimized clusters that are already optimal

---

### 1.4 Savings Plans & RI Coverage Analysis

**File**: `services/adapters/commitment_analysis.py`
**Priority**: 20/25 | **Estimated effort**: 5–7 days

#### What It Does

Savings Plans and Reserved Instances are the primary cost levers for most AWS accounts (20–40% savings). No existing adapter covers commitment utilization or coverage. This adapter uses Cost Explorer to:
- Detect under-utilized commitments (utilization < 95%)
- Detect coverage gaps (on-demand spend where commitments could apply)
- Alert on expiring commitments (30/60/90 days)
- Surface purchase recommendations from Cost Explorer

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("ce",)   # ce = Cost Explorer
```

| API Call | Purpose |
|---|---|
| `ce.get_savings_plans_utilization(TimePeriod=last_30_days)` | Overall SP utilization rate |
| `ce.get_savings_plans_utilization_details(TimePeriod=..., Filter=...)` | Per-commitment utilization |
| `ce.get_savings_plans_coverage(TimePeriod=..., GroupBy=[{"Type":"DIMENSION","Key":"SERVICE"}])` | Coverage rate by service |
| `ce.get_reservation_utilization(TimePeriod=..., GroupBy=[{"Type":"DIMENSION","Key":"SERVICE"}])` | RI utilization rate |
| `ce.get_reservation_coverage(TimePeriod=..., GroupBy=...)` | RI coverage rate |
| `ce.get_savings_plans_purchase_recommendation(SavingsPlansType="COMPUTE_SP", TermInYears="ONE_YEAR", PaymentOption="NO_UPFRONT", LookbackPeriodInDays="THIRTY_DAYS")` | Purchase recommendations |
| `ce.get_reservation_purchase_recommendation(Service="Amazon EC2", LookbackPeriodInDays="THIRTY_DAYS")` | RI purchase recommendations |
| `ce.list_savings_plans()` via `savingsplans` client | Expiry dates for active plans |

**Time period**: Last 30 days. Construct as:
```python
end = datetime.now(UTC).date()
start = end - timedelta(days=30)
TimePeriod = {"Start": start.isoformat(), "End": end.isoformat()}
```

**Note**: Cost Explorer has a `$0.01 per API request` charge. This adapter makes ~7 calls per scan. Document this in the class docstring.

#### Pricing Calculation

**Under-utilized commitment**:
```
wasted_commitment = commitment_hourly_spend * (1 - utilization_rate) * 730
monthly_waste = wasted_commitment * ctx.pricing_multiplier
```

**Coverage gap**:
```
uncovered_spend = on_demand_spend * (1 - coverage_rate)
potential_savings = uncovered_spend * avg_sp_discount_rate  # ~30% Compute SP discount
```
`avg_sp_discount_rate` defaults to `0.30` (conservative Compute SP estimate). Use a module-level constant.

**Expiry alert**: No savings calculation — flag as risk. `monthly_savings=0`, severity=HIGH for ≤30 days, MEDIUM for ≤60 days, LOW for ≤90 days.

**Purchase recommendation**: Use `estimatedMonthlySavingsAmount` from the CE API response directly.

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="Commitment Analysis",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "sp_utilization": SourceBlock(
            count=len(under_utilized_sps),
            recommendations=tuple(under_utilized_sps),
            extras={"overall_utilization_rate": float},
        ),
        "sp_coverage_gaps": SourceBlock(
            count=len(coverage_gap_recs),
            recommendations=tuple(coverage_gap_recs),
            extras={"overall_coverage_rate": float},
        ),
        "ri_utilization": SourceBlock(count=..., recommendations=...),
        "ri_coverage_gaps": SourceBlock(count=..., recommendations=...),
        "expiring_commitments": SourceBlock(
            count=len(expiring),
            recommendations=tuple(expiring),
            extras={"expiring_30d": n, "expiring_60d": n, "expiring_90d": n},
        ),
        "purchase_recommendations": SourceBlock(count=..., recommendations=...),
    },
    extras={
        "sp_utilization_rate": float,
        "sp_coverage_rate": float,
        "ri_utilization_rate": float,
        "ri_coverage_rate": float,
    },
)
```

#### HTML Report Integration

**Tab name**: "Commitments"

**Stat cards**:
| Label | Source Path | Formatter |
|---|---|---|
| SP Utilization | `extras.sp_utilization_rate` | percent |
| SP Coverage | `extras.sp_coverage_rate` | percent |
| RI Utilization | `extras.ri_utilization_rate` | percent |
| Monthly Savings | `total_monthly_savings` | currency |

**Grouping**: `GroupingSpec(by="check_category")`.

**Expiring commitments** render with a countdown badge (red for ≤30 days, yellow for ≤60 days, green for ≤90 days).

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "ce:GetSavingsPlansUtilization",
    "ce:GetSavingsPlansUtilizationDetails",
    "ce:GetSavingsPlansCoverage",
    "ce:GetReservationUtilization",
    "ce:GetReservationCoverage",
    "ce:GetSavingsPlansPurchaseRecommendation",
    "ce:GetReservationPurchaseRecommendation",
    "savingsplans:DescribeSavingsPlans"
  ],
  "Resource": "*"
}
```

#### Validation Criteria

- [ ] CE API `$0.01/request` cost documented in class docstring
- [ ] Time period construction is correct (30-day window, UTC, ISO format)
- [ ] Utilization check skipped gracefully if no SPs/RIs exist in account
- [ ] Expiry dates parsed from `savingsplans.describe_savings_plans()` — `TerminationDate` field
- [ ] `ctx.pricing_multiplier` applied only to waste calculations, not to CE API savings values

---

## Phase 2: Medium-Term

---

### 2.1 AI/ML Cost Optimization — Bedrock

**File**: `services/adapters/bedrock.py`
**Priority**: 20/25 | **Estimated effort**: 5–7 days

#### What It Does

Amazon Bedrock costs are driven by: model invocation token counts, Provisioned Throughput (PT) commitments, Knowledge Base OCU hours, and Agent invocations. This adapter surfaces:
- Idle Provisioned Throughput (committed but not invoked)
- Model tier analysis (Haiku vs Sonnet vs Opus usage patterns)
- Prompt caching adoption (where input caching is available but not used)
- Knowledge Base idle OCU
- Cross-region inference cost comparison

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("bedrock", "bedrock-runtime", "cloudwatch")
```

| API Call | Purpose |
|---|---|
| `bedrock.list_provisioned_model_throughputs()` | All PT commitments |
| `bedrock.get_provisioned_model_throughput(provisionedModelId=...)` | PT detail (modelId, status, commitment) |
| `bedrock.list_foundation_models(byOutputModality="TEXT")` | Available models and pricing tiers |
| `bedrock.list_knowledge_bases()` | (via bedrock-agent client) Knowledge bases |
| `bedrock.list_agents()` | (via bedrock-agent client) Agents |
| `cloudwatch.get_metric_data(...)` | `Invocations`, `InputTokenCount`, `OutputTokenCount` per PT model |

**CloudWatch namespace**: `AWS/Bedrock`.

Metric query for idle PT detection:
```python
MetricDataQueries=[{
    "Id": "invocations",
    "MetricStat": {
        "Metric": {
            "Namespace": "AWS/Bedrock",
            "MetricName": "Invocations",
            "Dimensions": [{"Name": "ModelId", "Value": model_id}],
        },
        "Period": 86400,
        "Stat": "Sum",
    },
    "ReturnData": True,
}]
```
If `sum(invocations) == 0` over the last 30 days: idle PT.

#### Pricing Calculation

**Provisioned Throughput idle**:
PT commitment pricing is per model-unit-hour. AWS does not expose this via Pricing API; use module-level constants derived from AWS documentation:

```python
PT_HOURLY_PRICE: dict[str, float] = {
    "anthropic.claude-3-haiku": 0.80,      # $/model-unit/hr
    "anthropic.claude-3-sonnet": 4.00,
    "anthropic.claude-3-5-sonnet": 8.00,
    "anthropic.claude-3-opus": 21.50,
    "amazon.titan-text-lite": 0.30,
    # ... extend as needed
}
monthly_waste = PT_HOURLY_PRICE.get(model_id, 1.0) * model_units * 730 * ctx.pricing_multiplier
```

**On-Demand vs PT crossover**: PT is cost-effective if token volume exceeds breakeven:
```
pt_monthly = hourly_rate * model_units * 730
od_monthly = (input_tokens * input_price + output_tokens * output_price) * 1_000_000
if od_monthly < pt_monthly: recommend switch to on-demand
```

**Knowledge Base idle OCU**: $0.20/OCU/hour × 730 × idle_ocus × `ctx.pricing_multiplier`.

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="Bedrock",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "idle_provisioned_throughput": SourceBlock(count=..., recommendations=...),
        "pt_breakeven_analysis": SourceBlock(count=..., recommendations=...),
        "idle_knowledge_bases": SourceBlock(count=..., recommendations=...),
        "idle_agents": SourceBlock(count=..., recommendations=...),
    },
    extras={
        "pt_count": int,
        "kb_count": int,
        "agent_count": int,
    },
)
```

#### HTML Report Integration

**Tab name**: "Bedrock"

**Stat cards**:
| Label | Source Path | Formatter |
|---|---|---|
| Provisioned Throughputs | `extras.pt_count` | int |
| Idle PTs | `sources.idle_provisioned_throughput.count` | int |
| Monthly Savings | `total_monthly_savings` | currency |

**Grouping**: `GroupingSpec(by="check_category")`.

**Model tier badge**: Display model family (Claude 3, Titan, Llama, etc.) as a colored badge on each PT row.

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock:ListProvisionedModelThroughputs",
    "bedrock:GetProvisionedModelThroughput",
    "bedrock:ListFoundationModels",
    "bedrock-agent:ListKnowledgeBases",
    "bedrock-agent:ListAgents",
    "cloudwatch:GetMetricData"
  ],
  "Resource": "*"
}
```

#### Validation Criteria

- [ ] PT idle detection uses 30-day CloudWatch window, skipped in `fast_mode`
- [ ] PT breakeven analysis only runs when both CloudWatch and PT pricing constants are available
- [ ] Knowledge Base and Agent checks degrade gracefully if `bedrock-agent` client is unavailable
- [ ] `ctx.pricing_multiplier` applied to all savings calculations

---

### 2.2 AI/ML Cost Optimization — SageMaker

**File**: `services/adapters/sagemaker.py`
**Priority**: 20/25 (bundled with 2.1) | **Estimated effort**: 4–5 days

#### What It Does

SageMaker costs accumulate in endpoints, notebook instances, and training jobs. Key optimizations:
- Idle real-time endpoints (no invocations)
- Notebook instances left running outside business hours
- Training jobs using on-demand when Spot is available
- Multi-model endpoint consolidation opportunities

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("sagemaker", "cloudwatch")
```

| API Call | Purpose |
|---|---|
| `sagemaker.list_endpoints(StatusEquals="InService")` | Active endpoints |
| `sagemaker.describe_endpoint(EndpointName=...)` | Endpoint config and instance type |
| `sagemaker.list_notebook_instances(StatusEquals="InService")` | Running notebooks |
| `sagemaker.list_training_jobs(StatusEquals="Completed", SortBy="CreationTime", MaxResults=100)` | Recent training jobs |
| `cloudwatch.get_metric_data(...)` | `Invocations` per endpoint, last 7 days |

**Idle endpoint**: `sum(Invocations) == 0` over last 7 days.

#### Pricing Calculation

**Idle endpoint**:
```python
# Get instance type from endpoint config
instance_type = endpoint_config["InstanceType"]
hourly = ctx.pricing_engine.get_ec2_instance_monthly_price(instance_type) / 730
# SageMaker charges slightly more than EC2 — apply 1.15x multiplier
monthly_cost = hourly * 730 * 1.15 * ctx.pricing_multiplier
```
Fallback constant if `PricingEngine` returns 0: `SAGEMAKER_INSTANCE_MULTIPLIER = 1.15`.

**Spot training savings**: Average 70% savings vs on-demand. Calculate from job duration and instance type.

**Notebook idle**: Same instance pricing model as endpoint, 100% waste if not connected.

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="SageMaker",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "idle_endpoints": SourceBlock(count=..., recommendations=...),
        "idle_notebooks": SourceBlock(count=..., recommendations=...),
        "spot_training": SourceBlock(count=..., recommendations=...),
        "multi_model_consolidation": SourceBlock(count=..., recommendations=...),
    },
    extras={
        "endpoint_count": int,
        "notebook_count": int,
    },
)
```

#### HTML Report Integration

**Tab name**: "SageMaker"

**Stat cards**:
| Label | Source Path | Formatter |
|---|---|---|
| Active Endpoints | `extras.endpoint_count` | int |
| Idle Endpoints | `sources.idle_endpoints.count` | int |
| Running Notebooks | `extras.notebook_count` | int |
| Monthly Savings | `total_monthly_savings` | currency |

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "sagemaker:ListEndpoints",
    "sagemaker:DescribeEndpoint",
    "sagemaker:DescribeEndpointConfig",
    "sagemaker:ListNotebookInstances",
    "sagemaker:DescribeNotebookInstance",
    "sagemaker:ListTrainingJobs",
    "sagemaker:DescribeTrainingJob",
    "cloudwatch:GetMetricData"
  ],
  "Resource": "*"
}
```

---

### 2.3 Network & Data Transfer Cost Analysis

**File**: `services/adapters/network_cost.py`
**Priority**: 16/25 | **Estimated effort**: 4–5 days

#### What It Does

The existing `NetworkModule` (`adapters/network.py`) covers infrastructure waste (idle NAT, unattached EIPs, over-provisioned LBs). This new adapter covers **data transfer costs** surfaced via Cost Explorer — fundamentally different data source, different recommendations:
- Cross-region data transfer (most expensive at $0.02–$0.09/GB)
- Cross-AZ data transfer ($0.01/GB each direction, often overlooked)
- Internet egress patterns (direct vs CloudFront)
- Transit Gateway vs VPC Peering cost comparison

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("ce", "ec2")
```

| API Call | Purpose |
|---|---|
| `ce.get_cost_and_usage(GroupBy=[{"Type":"USAGE_TYPE"}], Filter={"Dimensions":{"Key":"USAGE_TYPE_GROUP","Values":["AWS Data Transfer"]}})` | Data transfer spend breakdown |
| `ec2.describe_vpc_peering_connections(Filters=[{"Name":"status-code","Values":["active"]}])` | Active peering connections |
| `ec2.describe_transit_gateways()` | TGW configurations |
| `ec2.describe_transit_gateway_attachments()` | TGW attachment count and types |

#### Pricing Calculation

Data transfer prices are surfaced directly from Cost Explorer usage costs — no recalculation needed. Optimization savings are estimated:
- Cross-AZ: If same-AZ access patterns are possible, `savings = cross_az_spend * 0.5` (50% of traffic can often be made AZ-local)
- CloudFront vs direct: `savings = egress_spend * 0.40` (CloudFront typically 40–60% cheaper than direct)
- TGW vs Peering: TGW charges $0.05/GB attachment + $0.02/GB processed. Peering is only inter-region data transfer cost. Calculate break-even from attachment count.

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="Network Transfer Costs",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "cross_region_transfer": SourceBlock(count=..., recommendations=...),
        "cross_az_transfer": SourceBlock(count=..., recommendations=...),
        "internet_egress": SourceBlock(count=..., recommendations=...),
        "tgw_vs_peering": SourceBlock(count=..., recommendations=...),
    },
    extras={
        "total_data_transfer_spend_30d": float,
        "cross_region_spend_30d": float,
        "peering_count": int,
        "tgw_count": int,
    },
)
```

#### HTML Report Integration

**Tab name**: "Data Transfer"

**Stat cards**:
| Label | Source Path | Formatter |
|---|---|---|
| Transfer Spend (30d) | `extras.total_data_transfer_spend_30d` | currency |
| Cross-Region Spend | `extras.cross_region_spend_30d` | currency |
| Monthly Savings | `total_monthly_savings` | currency |

**Note**: This tab is complementary to the existing "Network & Infrastructure" tab — link to it in the tab description.

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "ce:GetCostAndUsage",
    "ec2:DescribeVpcPeeringConnections",
    "ec2:DescribeTransitGateways",
    "ec2:DescribeTransitGatewayAttachments"
  ],
  "Resource": "*"
}
```

---

### 2.4 Cost Anomaly Detection Integration

**File**: `services/adapters/cost_anomaly.py`
**Priority**: 15/25 | **Estimated effort**: 3–4 days

#### What It Does

AWS Cost Anomaly Detection runs ML models continuously and flags unexpected spending spikes. This adapter surfaces active anomalies and historical patterns as actionable findings — connecting the anomaly to a potential root cause resource.

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("ce",)
```

| API Call | Purpose |
|---|---|
| `ce.get_anomalies(DateInterval={"StartDate":..., "EndDate":...}, TotalImpact={"NumericOperator":"GREATER_THAN","StartValue":10})` | Anomalies with >$10 impact |
| `ce.get_anomaly_monitors()` | Active monitors (confirms feature is enabled) |
| `ce.get_anomaly_subscriptions()` | Alert subscriptions (identifies gaps) |

**Impact threshold**: `$10` minimum to filter noise. Make this a module-level constant: `MIN_ANOMALY_IMPACT_USD = 10.0`.

#### Pricing Calculation

No savings recalculation — use `totalImpact.totalActualSpend - totalImpact.totalExpectedSpend` from the API response as the anomaly magnitude. This represents identified waste.

```python
anomaly_magnitude = anomaly["totalImpact"]["totalActualSpend"] - anomaly["totalImpact"]["totalExpectedSpend"]
```

Total savings = sum of anomaly magnitudes for recurring patterns (non-one-off anomalies).

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="Cost Anomalies",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "active_anomalies": SourceBlock(
            count=len(active),
            recommendations=tuple(active),
            extras={"total_anomaly_impact": float},
        ),
        "no_monitors_configured": SourceBlock(
            count=0 if monitors_exist else 1,
            recommendations=() if monitors_exist else ({"message": "No anomaly monitors configured"},),
        ),
        "no_subscriptions": SourceBlock(count=..., recommendations=...),
    },
    extras={
        "monitors_configured": bool,
        "subscriptions_configured": bool,
        "anomaly_count_30d": int,
    },
)
```

#### HTML Report Integration

**Tab name**: "Cost Anomalies"

**Special rendering**: Anomalies with `anomalyScore.currentScore > 0.8` render with a red severity badge. Anomalies with monitors not configured render a setup guide callout.

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "ce:GetAnomalies",
    "ce:GetAnomalyMonitors",
    "ce:GetAnomalySubscriptions"
  ],
  "Resource": "*"
}
```

---

### 2.5 Kubernetes/EKS Cost Visibility

**File**: `services/adapters/eks.py`
**Priority**: 15/25 | **Estimated effort**: 7–10 days

#### What It Does

EKS clusters are opaque from a cost perspective — nodes are visible as EC2 instances but pod-level cost allocation requires additional data sources. This adapter:
- Identifies over-provisioned node groups (aggregate pod requests << node capacity)
- Detects idle clusters (no pod workload)
- Surfaces Fargate vs EC2 break-even analysis
- Checks Container Insights enablement (prerequisite for deeper analysis)

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("eks", "ec2", "cloudwatch")
```

| API Call | Purpose |
|---|---|
| `eks.list_clusters()` | All EKS clusters |
| `eks.describe_cluster(name=...)` | Cluster config, version, logging |
| `eks.list_nodegroups(clusterName=...)` | Node groups per cluster |
| `eks.describe_nodegroup(clusterName=..., nodegroupName=...)` | Instance type, desired/min/max size |
| `ec2.describe_instances(Filters=[{"Name":"tag:aws:eks:cluster-name","Values":[cluster_name]}])` | EC2 nodes for a cluster |
| `cloudwatch.get_metric_data(...)` | `node_cpu_utilization`, `node_memory_utilization` from Container Insights namespace |

**CloudWatch namespace for Container Insights**: `ContainerInsights`.

**Container Insights check**: Before querying CW, check if insights are enabled:
```python
# proxy: if any metrics exist in ContainerInsights for this cluster
response = cw.list_metrics(Namespace="ContainerInsights", Dimensions=[{"Name":"ClusterName","Value":cluster}])
insights_enabled = len(response["Metrics"]) > 0
```

#### Pricing Calculation

**Node over-provisioning**:
```python
# For each node group:
node_capacity = sum(cpu_capacity_per_node * node_count)
pod_requests = sum(pod_cpu_requests)  # from Container Insights
utilization = pod_requests / node_capacity
waste_fraction = max(0, 1 - utilization - 0.20)  # keep 20% headroom
monthly_waste = instance_price * node_count * waste_fraction * 730 * ctx.pricing_multiplier
```

**EKS cluster fee**: $0.10/hr per cluster = $73/month. Flag for idle clusters.

Fallback if Container Insights not enabled: report `insights_not_enabled` finding, skip utilization calculations.

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="EKS",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "over_provisioned_nodegroups": SourceBlock(count=..., recommendations=...),
        "idle_clusters": SourceBlock(count=..., recommendations=...),
        "no_container_insights": SourceBlock(count=..., recommendations=...),
        "fargate_analysis": SourceBlock(count=..., recommendations=...),
    },
    extras={
        "cluster_count": int,
        "nodegroup_count": int,
        "container_insights_enabled": bool,
    },
)
```

#### HTML Report Integration

**Tab name**: "EKS"

**Stat cards**:
| Label | Source Path | Formatter |
|---|---|---|
| EKS Clusters | `extras.cluster_count` | int |
| Node Groups | `extras.nodegroup_count` | int |
| Monthly Savings | `total_monthly_savings` | currency |

**Container Insights warning**: If `extras.container_insights_enabled=False`, render a banner: "Enable Container Insights for full EKS cost visibility."

#### IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "eks:ListClusters",
    "eks:DescribeCluster",
    "eks:ListNodegroups",
    "eks:DescribeNodegroup",
    "ec2:DescribeInstances",
    "cloudwatch:GetMetricData",
    "cloudwatch:ListMetrics"
  ],
  "Resource": "*"
}
```

---

## Phase 3: Strategic Capabilities

---

### 3.1 FOCUS 1.2 Export Compliance

**File**: `core/focus_export.py`
**Priority**: 16/25 | **Estimated effort**: 7–10 days

#### What It Does

Generates scanner output as a FOCUS 1.2-compliant CSV or JSON file. FOCUS (FinOps Open Cost and Usage Specification) is the emerging industry standard for cloud cost data, enabling cross-tool comparison.

This is a **core module** (not an adapter) — it transforms existing `ServiceFindings` output into FOCUS format. No new boto3 calls needed.

#### FOCUS 1.2 Column Mapping

| FOCUS Column | Source in Scanner |
|---|---|
| `BillingAccountId` | `ctx.account_id` |
| `BillingPeriodStart` | Scan start timestamp |
| `BillingPeriodEnd` | Scan end timestamp |
| `ChargeType` | `"Usage"` (actual) or `"Adjustment"` (recommendation) |
| `ServiceName` | `findings.service_name` |
| `ServiceCategory` | Mapped from service name (e.g., "Compute", "Storage", "Database") |
| `ResourceId` | `recommendation["resource_id"]` |
| `ResourceName` | `recommendation.get("resource_name", "")` |
| `ListCost` | `recommendation.get("current_monthly_cost", 0.0)` |
| `EffectiveCost` | `ListCost - monthly_savings` |
| `BilledCost` | Same as `EffectiveCost` |
| `ConsumedQuantity` | `recommendation.get("quantity", 0)` |
| `ConsumedUnit` | `recommendation.get("unit", "")` |
| `CommitmentDiscountCategory` | `"Reservation"` or `"SavingsPlan"` if applicable |
| `CommitmentDiscountType` | `"Reserved Instance"` or `"Compute Savings Plan"` etc. |
| `Tags` | `recommendation.get("tags", {})` serialized as JSON string |

#### Implementation

```python
class FocusExporter:
    FOCUS_VERSION = "1.2"
    SERVICE_CATEGORY_MAP: dict[str, str] = {
        "EC2": "Compute",
        "EBS": "Storage",
        "RDS": "Database",
        "Aurora": "Database",
        "S3": "Storage",
        "Lambda": "Compute",
        ...
    }

    def export(
        self,
        findings: dict[str, ServiceFindings],
        ctx: ScanContext,
        output_format: Literal["csv", "json"] = "csv",
    ) -> str:
        """Transform all ServiceFindings into FOCUS 1.2 rows."""
        ...

    def _findings_to_rows(self, service_key: str, findings: ServiceFindings, ctx: ScanContext) -> list[dict]:
        ...
```

#### Integration with `cost_optimizer.py`

Add a `--focus-export` CLI flag. When set, call `FocusExporter.export()` after all scans complete and write to `focus_export_{timestamp}.csv`.

#### Validation Criteria

- [ ] All required FOCUS 1.2 columns present
- [ ] `ServiceCategory` mapping covers all 28 existing adapters
- [ ] Output validates against FOCUS 1.2 JSON schema
- [ ] CSV output is UTF-8 with correct quoting for tag values
- [ ] No PII or sensitive data exposed in exported rows

---

### 3.2 Multi-Account & Organization Support

**Files**: `core/scan_context.py` (extend), `core/org_scanner.py` (new), `cost_optimizer.py` (extend)
**Priority**: 16/25 | **Estimated effort**: 10–14 days

#### What It Does

Extends `ScanContext` to support AWS Organizations cross-account scanning via STS role assumption. Generates per-account, per-OU, and org-wide aggregated reports.

#### Architecture Changes

**New `ScanContext` fields**:
```python
@dataclass
class ScanContext:
    # ... existing fields ...
    organization_id: str | None = None
    assume_role_arn: str | None = None   # e.g. "arn:aws:iam::ACCOUNT_ID:role/CostScannerRole"
    ou_filter: list[str] | None = None   # filter to specific OUs
    parent_account_id: str | None = None # management account
```

**New `OrgScanner` class** (`core/org_scanner.py`):
```python
class OrgScanner:
    def list_accounts(self, ctx: ScanContext) -> list[dict]:
        """List all accounts in the org, filtered by OU if specified."""
        ...

    def assume_role(self, account_id: str, role_name: str, session: AwsSessionFactory) -> ScanContext:
        """Create a child ScanContext with assumed-role credentials for account_id."""
        ...

    def run_org_scan(self, base_ctx: ScanContext, modules: list[ServiceModule]) -> dict[str, dict[str, ServiceFindings]]:
        """Run ScanOrchestrator for each account, return account_id -> findings mapping."""
        ...
```

**Report aggregation**: `ResultBuilder` extended to merge multi-account findings. Total savings = sum across accounts. Per-account breakdown added to `extras`.

#### IAM Requirements

**Management account** (reads org structure):
```json
{"Action": ["organizations:ListAccounts", "organizations:ListAccountsForParent", "organizations:ListRoots", "organizations:ListOrganizationalUnitsForParent"], "Resource": "*"}
```

**Each member account** (assumed role):
```json
{"Action": "sts:AssumeRole", "Resource": "arn:aws:iam::*:role/CostScannerRole"}
```
The assumed `CostScannerRole` in member accounts must have all existing scanner read permissions.

#### CLI Changes

```
cost_optimizer.py --org-scan \
  --assume-role CostScannerRole \
  --ou-filter ou-prod ou-dev \
  --parallel 5
```

---

### 3.3 Infrastructure-as-Code Cost Estimation

**File**: `core/iac_estimator.py`
**Priority**: 15/25 | **Estimated effort**: 10–14 days

#### What It Does

Parses Terraform `.tf` files and CloudFormation templates to estimate monthly cost of declared resources **before deployment**. Integrates with the existing `PricingEngine` for live pricing.

#### Supported Resource Types (MVP)

| Terraform Resource | AWS Service | Pricing Method |
|---|---|---|
| `aws_instance` | EC2 | `PricingEngine.get_ec2_instance_monthly_price(instance_type)` |
| `aws_db_instance` | RDS | `PricingEngine.get_rds_instance_monthly_price(engine, class)` |
| `aws_lambda_function` | Lambda | Invocations × duration estimate |
| `aws_s3_bucket` + `aws_s3_bucket_lifecycle_configuration` | S3 | `PricingEngine.get_s3_monthly_price_per_gb(storage_class)` |
| `aws_elasticache_cluster` | ElastiCache | `PricingEngine.get_elasticache_node_monthly_price(engine, type)` |
| `aws_nat_gateway` | NAT | `PricingEngine.get_nat_hourly() * 730` |

#### Implementation

```python
class IaCEstimator:
    def estimate_terraform(self, tf_dir: str, region: str, pricing_engine: PricingEngine) -> list[CostEstimate]:
        """Parse .tf files and return per-resource cost estimates."""
        ...

    def estimate_cloudformation(self, template_path: str, ...) -> list[CostEstimate]:
        """Parse CF template JSON/YAML and return per-resource cost estimates."""
        ...

    def diff(self, before: list[CostEstimate], after: list[CostEstimate]) -> CostDiff:
        """Compare two estimates and return added/removed/changed resources with delta cost."""
        ...

@dataclass(frozen=True)
class CostEstimate:
    resource_id: str
    resource_type: str
    monthly_cost: float
    pricing_confidence: Literal["high", "medium", "low"]
    pricing_notes: str
```

**Terraform parsing**: Use Python's `hcl2` library (pure Python, no external binary). Parse `resource` blocks, extract `instance_type`, `db_instance_class`, etc. Fall back to regex if `hcl2` not available.

**CloudFormation parsing**: Use Python's `yaml` and `json` stdlib. Handle `!Ref`, `!Sub` intrinsics with best-effort resolution.

#### CLI Integration

```
cost_optimizer.py --iac-estimate ./terraform/
cost_optimizer.py --iac-diff ./terraform/base/ ./terraform/feature-branch/
```

---

### 3.4 Budget & Forecasting Engine

**File**: `core/forecasting.py`
**Priority**: 15/25 | **Estimated effort**: 7–10 days

#### What It Does

Combines AWS Cost Explorer forecasting with scanner optimization findings to project:
- Current month end-of-month spend (from CE)
- 12-month savings trajectory if recommendations are implemented
- Budget vs actual variance per service
- "What-if" savings scenarios

#### AWS APIs

```python
# In cost_optimizer.py, add pre-fetch:
ctx.forecast_data = forecasting.fetch_forecast_data(ctx)
```

| API Call | Purpose |
|---|---|
| `ce.get_cost_forecast(TimePeriod=..., Metric="UNBLENDED_COST", Granularity="MONTHLY")` | Month-end forecast |
| `ce.get_cost_and_usage(TimePeriod=last_12_months, Granularity="MONTHLY", GroupBy=[{"Type":"SERVICE"}])` | Historical spend by service |
| `budgets.describe_budgets(AccountId=ctx.account_id)` | Active budgets |
| `budgets.describe_budget_performance_history(AccountId=..., BudgetName=...)` | Budget vs actual history |

#### Savings Projection Calculation

```python
def project_12_month_savings(findings: dict[str, ServiceFindings]) -> list[MonthlyProjection]:
    """
    Assumption: recommendations are implemented in month 1.
    Month 1: partial savings (50% implementation rate assumption).
    Month 2+: full savings each month.
    Compound: apply 3% YoY growth to base spend.
    """
    monthly_savings = sum(f.total_monthly_savings for f in findings.values())
    return [
        MonthlyProjection(
            month=i,
            baseline_spend=baseline * (1.03 ** (i / 12)),
            optimized_spend=baseline * (1.03 ** (i / 12)) - monthly_savings * (0.5 if i == 0 else 1.0),
            savings=monthly_savings * (0.5 if i == 0 else 1.0),
        )
        for i in range(12)
    ]
```

#### HTML Report Integration

Add a new **"Forecast"** tab with:
- 12-month savings projection chart (SVG line chart, no JS dependencies)
- Budget vs actual variance table by service
- Current month run-rate vs forecast
- "Implement all recommendations" vs "baseline" scenario comparison

---

### 3.5 Tagging Compliance & Governance

**File**: `services/adapters/tag_compliance.py`
**Priority**: 12/25 | **Estimated effort**: 5–7 days

#### What It Does

Audits required tag presence and consistency across all scanned resources. Required tags are configurable.

#### AWS APIs and boto3 Clients

```python
required_clients() -> ("resourcegroupstaggingapi", "ce")
```

| API Call | Purpose |
|---|---|
| `resourcegroupstaggingapi.get_resources(TagFilters=[])` | All taggable resources (paginated) |
| `resourcegroupstaggingapi.get_tag_keys()` | All tag keys in account |
| `ce.list_cost_allocation_tags(Status="Active")` | Which tags are active for cost allocation |

#### Configuration

Required tags are configured at scan time via CLI or config file:
```
cost_optimizer.py --required-tags cost-center environment team application
```

Default: `["cost-center", "environment", "team"]`.

#### `ServiceFindings` Shape

```python
ServiceFindings(
    service_name="Tag Compliance",
    total_recommendations=total,
    total_monthly_savings=0.0,  # tagging is governance, not direct savings
    sources={
        "missing_required_tags": SourceBlock(count=..., recommendations=...),
        "inconsistent_tag_values": SourceBlock(count=..., recommendations=...),
        "inactive_cost_allocation_tags": SourceBlock(count=..., recommendations=...),
    },
    extras={
        "total_resources_audited": int,
        "compliant_resources": int,
        "compliance_rate": float,
        "required_tags": list[str],
    },
)
```

**Note**: `total_monthly_savings=0.0` — tagging does not produce direct savings but enables cost allocation. Document this in the class docstring and HTML tab.

---

### 3.6 Well-Architected Cost Pillar Automated Review

**File**: `core/well_architected_review.py`
**Priority**: 12/25 | **Estimated effort**: 5–7 days

#### What It Does

Maps scanner findings to AWS Well-Architected Framework Cost Pillar questions and generates a scored review. No new boto3 calls — transforms existing `ServiceFindings`.

#### W-A Question to Scanner Coverage Mapping

```python
WA_MAPPINGS: list[WaMapping] = [
    WaMapping(
        question_id="COST01",
        question="How do you implement cloud financial management?",
        scanner_sources=["commitment_analysis", "cost_anomaly"],
        coverage=Coverage.PARTIAL,
    ),
    WaMapping(
        question_id="COST02",
        question="How do you govern usage?",
        scanner_sources=["tag_compliance"],
        coverage=Coverage.PARTIAL,
    ),
    WaMapping(
        question_id="COST03",
        question="How do you monitor usage and cost?",
        scanner_sources=["cost_anomaly", "monitoring"],
        coverage=Coverage.COVERED,
    ),
    WaMapping(
        question_id="COST04",
        question="How do you decommission resources?",
        scanner_sources=["ec2", "ebs", "rds", "elasticache", "ami"],
        coverage=Coverage.COVERED,
    ),
    WaMapping(
        question_id="COST05",
        question="How do you evaluate cost when selecting services?",
        scanner_sources=["compute_optimizer", "aurora"],
        coverage=Coverage.PARTIAL,
    ),
    WaMapping(
        question_id="COST06",
        question="How do you meet cost targets?",
        scanner_sources=["commitment_analysis"],
        coverage=Coverage.PARTIAL,
    ),
    WaMapping(
        question_id="COST07",
        question="How do you optimize your cost drivers?",
        scanner_sources=["ec2", "rds", "lambda", "bedrock", "sagemaker"],
        coverage=Coverage.COVERED,
    ),
    WaMapping(
        question_id="COST08",
        question="How do you manage demand and supply resources?",
        scanner_sources=["network", "eks"],
        coverage=Coverage.PARTIAL,
    ),
    WaMapping(
        question_id="COST09",
        question="How do you optimize data transfer costs?",
        scanner_sources=["network_cost", "cloudfront"],
        coverage=Coverage.COVERED,
    ),
]
```

#### Scoring

```
score = covered_questions / total_questions * 100
grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
```

#### HTML Report Integration

Add a **"W-A Review"** tab with:
- Score card (percentage + letter grade)
- Per-question coverage table (green=covered, yellow=partial, red=gap)
- For gaps: link to the relevant roadmap item or manual review recommendation

---

## Phase 4: Vision Items

These items are scoped at a high level. Full build specifications will be written when Phase 3 reaches completion.

---

### 4.1 Unit Cost Economics Dashboard

**File**: `core/unit_economics.py` (new)

Track infrastructure cost per business KPI (cost-per-user, cost-per-API-call, cost-per-transaction).

**Integration**: Requires customer-defined KPI metrics from CloudWatch Application Insights. Configurable via `--unit-metric namespace/metric-name/unit`.

**Pricing**: No new pricing — divide existing `total_monthly_spend` by KPI metric value.

**HTML tab**: Trend chart (cost/unit over 12 months), service breakdown as cost-per-unit.

---

### 4.2 AI-Powered Cost Explanations

**File**: `core/ai_explanations.py` (new)

Consume AWS Cost Explorer's ML-powered natural language explanations for spend changes.

**API**: `ce.get_cost_and_usage_with_resources()` + `ce.get_anomaly_explanations()` (preview API as of 2025).

**Integration**: Add plain-English summaries to each service tab header.

---

### 4.3 Sustainability / Carbon Footprint

**File**: `services/adapters/sustainability.py` (new)

**API**: AWS Customer Carbon Footprint Tool via `customer-carbon-footprint-tool` API (available via `sustainability` boto3 client).

**Report integration**: Add "Carbon" column alongside "Monthly Savings ($)" showing estimated kg CO₂ reduction per recommendation.

---

### 4.4 Real-Time Cost Anomaly Streaming

**File**: `core/streaming.py` (new)

**Architecture**: EventBridge Scheduled Rule → Lambda (runs the scanner) → SNS/Slack notification on new anomalies. Not a CLI tool — a deployment artifact.

**Deployment**: CloudFormation template packaged with the scanner for customer self-deployment.

---

### 4.5 Terraform Policy-as-Code

**File**: `core/policy_export.py` (new)

Export scanner findings as OPA/Rego policies (`deny` rules for over-provisioned resources). Integrate with CI: if `iac_estimator.py` detects a resource matching a scanner pattern, the OPA policy denies the plan.

---

### 4.6 Marketplace & Third-Party Integrations

**Scope**: Webhook-based notification adapters. No core architecture changes.
- Slack: POST findings summary to webhook on scan complete
- Jira: Create tickets for findings with HIGH severity
- PagerDuty: Alert on anomalies exceeding threshold

---

## Cross-Cutting Requirements

### PricingEngine Extensions Required

New adapters require the following additions to `core/pricing_engine.py`:

| Method | Service Code | Filter |
|---|---|---|
| `get_aurora_acu_hourly()` | `AmazonRDS` | `productFamily=Aurora Serverless v2` |
| `get_bedrock_pt_hourly(model_id)` | Use module constants (Pricing API does not cover Bedrock) |
| `get_sagemaker_instance_monthly(instance_type)` | `AmazonSageMaker` | `instanceType=...` |
| `get_eks_cluster_monthly()` | Module constant: `$0.10/hr * 730 = $73/month` |

### `ScanOrchestrator` Extensions Required

| Change | Reason |
|---|---|
| Extend `_prefetch_advisor_data` cost hub splits to 10 services | Phase 1.2 |
| Add `cost_hub_splits["commitment_analysis"]` pre-fetch | Phase 1.4 |
| Add `org_scanner` branch in `run()` when `ctx.organization_id` is set | Phase 3.2 |
| Add `forecast_data` pre-fetch in `run()` when `--forecast` flag set | Phase 3.4 |

### `services/__init__.py` — New Registrations

Append in order (preserves `ALL_MODULES` append-only invariant):

```python
# Phase 1
from services.adapters.compute_optimizer import ComputeOptimizerModule
from services.adapters.cost_optimization_hub import CostOptimizationHubModule
from services.adapters.aurora import AuroraModule
from services.adapters.commitment_analysis import CommitmentAnalysisModule

# Phase 2
from services.adapters.bedrock import BedrockModule
from services.adapters.sagemaker import SageMakerModule
from services.adapters.network_cost import NetworkCostModule
from services.adapters.cost_anomaly import CostAnomalyModule
from services.adapters.eks import EksModule

# Phase 3
from services.adapters.tag_compliance import TagComplianceModule
```

### Regression Gate

After each phase, run:
```bash
pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v
```

All new adapters must have corresponding golden fixture files under `tests/fixtures/`.

New adapters that call Cost Explorer (`ce`) must be tested with a mocked CE client — never with real AWS credentials in CI.

---

## Implementation Order Summary

| Sprint | Items | Adapters Added | Estimated Weeks |
|--------|-------|----------------|-----------------|
| 1 | Compute Optimizer, Cost Hub | 2 | 1–2 |
| 2 | Savings Plans, Aurora | 2 | 3–4 |
| 3 | Bedrock, SageMaker, Network Cost | 3 | 5–8 |
| 4 | EKS, Cost Anomaly | 2 | 9–12 |
| 5 | FOCUS, Multi-Account, Forecasting, Tagging | 4 core modules + 1 adapter | 13–16 |
| 6+ | W-A Review, Phase 4 vision items | TBD | 17+ |

**Total new adapters through Sprint 4**: 9 (28 → 37)
**Total new core modules through Sprint 5**: 5 (`focus_export`, `org_scanner`, `iac_estimator`, `forecasting`, `well_architected_review`)

---

*This plan is a living document. Each item moves to DONE only after: (1) adapter file exists, (2) registered in ALL_MODULES, (3) regression gate passes, (4) IAM permissions documented in README, (5) golden fixture exists in tests/fixtures/.*
