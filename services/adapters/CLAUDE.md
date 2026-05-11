# Service Adapters

37 ServiceModule adapter files in `services/adapters/`. Each implements `scan(ctx) -> ServiceFindings`.

## Pricing Models

Adapters use one of three pricing strategies:

### Live Pricing (19 adapters)
Use `ctx.pricing_engine` (PricingEngine) for AWS Pricing API lookups. Fall back to `ctx.pricing_multiplier` on API failure.

| Adapter | Method | Pricing Source |
|---------|--------|---------------|
| `ec2.py` | `get_ec2_instance_monthly_price()` | AWS Pricing API |
| `ebs.py` | `get_ebs_monthly_price_per_gb()` | AWS Pricing API |
| `rds.py` | `get_rds_instance_monthly_price()`, `get_rds_monthly_storage_price_per_gb(multi_az=)` | AWS Pricing API |
| `s3.py` | `get_s3_monthly_price_per_gb()` | AWS Pricing API |
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

## Adding Live Pricing to an Adapter

1. Check if `PricingEngine` has a suitable method (see `core/pricing_engine.py`)
2. In `scan()`, use `ctx.pricing_engine.method_name(...)` for the lookup
3. Multiply by `ctx.pricing_multiplier` for regional adjustment
4. Wrap in try/except — fall back to `flat_rate × ctx.pricing_multiplier` on failure
5. If using CloudWatch metrics, check `ctx.fast_mode` first
