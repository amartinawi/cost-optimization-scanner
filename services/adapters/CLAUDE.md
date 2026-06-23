# Service Adapters

34 ServiceModule adapter files in `services/adapters/`. Each implements `scan(ctx) -> ServiceFindings`. Two former adapters were retired from `ALL_MODULES` on 2026-05-14 and their findings now flow per-service: AWS Cost Optimization Hub recommendations are fetched once by `ScanOrchestrator._prefetch_advisor_data` and consumed via `ctx.cost_hub_splits[<service_key>]`; AWS Compute Optimizer recommendations are pulled inline by EC2 / EBS / RDS / Lambda / Containers adapters via the `services.advisor.get_<resource>_compute_optimizer_recommendations` helpers.

## Pricing Models

Adapters use one of three pricing strategies:

### Live Pricing (19 adapters)
Use `ctx.pricing_engine` (PricingEngine) for AWS Pricing API lookups. Fall back to `ctx.pricing_multiplier` on API failure.

| Adapter | Method | Pricing Source |
|---------|--------|---------------|
| `ec2.py` | OS/license-aware `get_ec2_hourly_price(type, os, license_model)`. Prev-gen / rightsizing / burstable use the **exact current→target price delta** (target = migration map or one-size-down); idle = full cost; cron/batch/dedicated/instance-store/non-prod-scheduling use `EC2_SAVINGS_FACTORS`; spot = on-demand−Spot via `describe_spot_price_history`. Cross-source + ASG-member dedup in the adapter; every finding records `OS` + `PricingBasis`. | AWS Pricing API + EC2 Spot history |
| `ebs.py` | `get_ebs_monthly_price_per_gb()`; Compute Optimizer via `compute_optimizer_savings()` | AWS Pricing API |
| `rds.py` | `get_rds_instance_monthly_price(engine, class, multi_az, license_model)` — pins `databaseEdition` (SQL Server/Oracle, from the engine string) + `licenseModel` (from the instance's `LicenseModel`) + Multi-AZ deploymentOption SKU; `get_rds_backup_storage_price_per_gb()`. Compute Optimizer via `compute_optimizer_savings()`; **consumes Cost Hub** via `ctx.cost_hub_splits["rds"]`. Cross-source dedup + RI/backup demotion in `services/rds_logic.py`; opt-in placeholder→warning mirrors EC2. Multi-AZ-disable & non-prod scheduling are **CloudWatch-gated** (DatabaseConnections; `requires_cloudwatch`/`reads_fast_mode`). No gp2→gp3 check (RDS gp2==gp3 base price); backup retention is advisory (free allotment = 100% of provisioned). Aurora-aware: snapshots priced at the Aurora backup rate ($0.021 vs standard $0.095/GB-mo), instance pricing pins Aurora storage mode (Standard vs I/O-Optimized). **Coverage gaps (intentional):** Aurora Serverless v2 (ACU), Aurora cluster rightsizing, read replicas, stopped instances, and Extended Support are not priced. | AWS Pricing API + Cost Optimization Hub |
| `s3.py` | `get_s3_monthly_price_per_gb()` via `PricingEngine.for_region(bucket_region)` — buckets are global, priced at their home region. Cost summed per storage class at each class's own rate. Savings are **evidence-gated**: only the Standard→Standard-IA delta on bytes proven cold by CloudWatch request metrics (0 GETs/30d); no evidence → $0 advisory. Needs `s3:GetMetricsConfiguration`. See `docs/audits/S3_AUDIT_FINDINGS.md`. | AWS Pricing API |
| `lambda_svc.py` | Cost Hub + Compute Optimizer | AWS APIs |
| `dynamodb.py` | RCU/WCU hourly rates × 730 | Module constants |
| `containers.py` | Fargate vCPU/mem hourly rates × 730 | Module constants |
| `network.py` | `get_eip_monthly()`, `get_nat_hourly()`, etc. (5 methods) | AWS Pricing API |
| `file_systems.py` | `get_s3_monthly_price_per_gb("STANDARD")` | AWS Pricing API |
| `workspaces.py` | `get_instance_monthly_price("AmazonWorkSpaces", ...)` | AWS Pricing API |
| `glue.py` | `$0.44/DPU/hour × 160 hrs` | Module constant |
| `lightsail.py` | `get_instance_monthly_price("AmazonLightsail", ...)` | AWS Pricing API |
| `apprunner.py` | `$0.064/vCPU/hr + $0.007/GB/hr × 730` | Module constants |
| `transfer.py` | `$0.30/protocol/hour × 730` | Module constant |
| `mediastore.py` | `get_s3_monthly_price_per_gb("STANDARD")` | AWS Pricing API |
| `quicksight.py` | SPICE tier pricing ($0.25–$0.38/GB) | Module constants |
| `athena.py` | CloudWatch ProcessedBytes → $5/TB | CloudWatch + constant |
| `step_functions.py` | CloudWatch ExecutionsStarted → $0.025/1K | CloudWatch + constant |
| `elasticache.py` | `get_elasticache_node_monthly_price()` | AWS Pricing API |

### Parse-rate (5 adapters)
Extract dollar amounts from recommendation text or use keyword-based estimates:

| Adapter | Method |
|---------|--------|
| `cloudfront.py` | Fixed $25/rec |
| `api_gateway.py` | Keyword-based |
| `opensearch.py` | Keyword-based |
| `ami.py` | `parse_dollar_savings()` |
| `monitoring.py` | Fixed per-rec estimates |

### Flat-rate (1 adapter)
| Adapter | Method |
|---------|--------|
| `batch.py` | Fixed per-rec estimate |

## Consuming AWS Compute Optimizer Recommendations

`compute_optimizer_savings(rec)` in `services/_savings.py` extracts savings
from the nested `*RecommendationOptions[N].savingsOpportunity.estimatedMonthlySavings`
path used by Compute Optimizer (EC2, EBS, and RDS — both
`instanceRecommendationOptions` and `storageRecommendationOptions`). **Always** use
this helper rather than `rec.get("estimatedMonthlySavings", 0)`, which only
works for Cost Optimization Hub (flat float at top level) and silently returns
0 for Compute Optimizer.

## Adding Live Pricing to an Adapter

1. Check if `PricingEngine` has a suitable method (see `core/pricing_engine.py`)
2. In `scan()`, use `ctx.pricing_engine.method_name(...)` for the lookup
3. Multiply by `ctx.pricing_multiplier` **only** for module-constant or fallback paths.
   `PricingEngine` methods already return region-correct prices — do NOT multiply twice.
4. Wrap in try/except — fall back to a constant × `ctx.pricing_multiplier` on failure.
5. If using CloudWatch metrics, check `ctx.fast_mode` first and bail out cheaply when set.


<claude-mem-context>
# Recent Activity

<!-- This section is auto-generated by claude-mem. Edit content outside the tags. -->

### May 12, 2026

| ID | Time | T | Title | Read |
|----|------|---|-------|------|
| #5212 | 1:53 AM | ✅ | Updated CLAUDE.md to document RDS Compute Optimizer savings integration | ~350 |
</claude-mem-context>