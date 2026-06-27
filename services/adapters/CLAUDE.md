# Service Adapters

34 ServiceModule adapter files in `services/adapters/`. Each implements `scan(ctx) -> ServiceFindings`. Two former adapters were retired from `ALL_MODULES` on 2026-05-14 and their findings now flow per-service: AWS Cost Optimization Hub recommendations are fetched once by `ScanOrchestrator._prefetch_advisor_data` and consumed via `ctx.cost_hub_splits[<service_key>]`; AWS Compute Optimizer recommendations are pulled inline by EC2 / EBS / RDS / Lambda / Containers adapters via the `services.advisor.get_<resource>_compute_optimizer_recommendations` helpers.

## Pricing Models

Adapters use one of three pricing strategies:

### Live Pricing (19 adapters)
Use `ctx.pricing_engine` (PricingEngine) for AWS Pricing API lookups. Fall back to `ctx.pricing_multiplier` on API failure.

| Adapter | Method | Pricing Source |
|---------|--------|---------------|
| `ec2.py` | OS/license-aware `get_ec2_hourly_price(type, os, license_model)`. Prev-gen / rightsizing / burstable use the **exact current→target price delta** (target = migration map or one-size-down); idle = full cost; cron/batch/instance-store/non-prod-scheduling use `EC2_SAVINGS_FACTORS` **only when corroborated by the CloudWatch idle/low-CPU rightsizing signal** — the adapter builds `corroborated_ids` from the idle/rightsizing/burstable enhanced categories (`_CW_LOW_UTIL_CATEGORIES`) and passes it to `get_advanced_ec2_checks`; an uncorroborated tag lever is a `$0` `Counted=False` advisory that still renders (figure in `AdvisoryEstimate`) but never enters `best_by_instance`/the headline (ec2 H2). Spot = on-demand−Spot via `describe_spot_price_history` (factor-free, never gated). Cross-source + ASG-member dedup in the adapter; every finding records `OS` + `PricingBasis`. | AWS Pricing API + EC2 Spot history |
| `ebs.py` | `get_ebs_monthly_price_per_gb()`; Compute Optimizer via `compute_optimizer_savings()` | AWS Pricing API |
| `rds.py` | `get_rds_instance_monthly_price(engine, class, multi_az, license_model)` — pins `databaseEdition` (SQL Server/Oracle, from the engine string) + `licenseModel` (from the instance's `LicenseModel`) + Multi-AZ deploymentOption SKU; `get_rds_backup_storage_price_per_gb()`. Compute Optimizer via `compute_optimizer_savings()`; **consumes Cost Hub** via `ctx.cost_hub_splits["rds"]`. Cross-source dedup + RI/backup demotion in `services/rds_logic.py`; opt-in placeholder→warning mirrors EC2. Multi-AZ-disable & non-prod scheduling are **CloudWatch-gated** (DatabaseConnections; `requires_cloudwatch`/`reads_fast_mode`). No gp2→gp3 check (RDS gp2==gp3 base price); backup retention is advisory (free allotment = 100% of provisioned). Aurora-aware: snapshots priced at the Aurora backup rate ($0.021 vs standard $0.095/GB-mo), instance pricing pins Aurora storage mode (Standard vs I/O-Optimized). **Coverage gaps (intentional):** Aurora Serverless v2 (ACU), Aurora cluster rightsizing, read replicas, stopped instances, and Extended Support are not priced. | AWS Pricing API + Cost Optimization Hub |
| `s3.py` | `get_s3_monthly_price_per_gb()` via `PricingEngine.for_region(bucket_region)` — buckets are global, priced at their home region. Cost summed per storage class at each class's own rate. Savings are **evidence-gated**: only the Standard→Standard-IA delta on bytes proven cold by CloudWatch request metrics (0 GETs/30d); no evidence → $0 advisory. Needs `s3:GetMetricsConfiguration`. See `docs/audits/S3_AUDIT_FINDINGS.md`. | AWS Pricing API |
| `lambda_svc.py` | **Consumes Cost Hub** (`cost_hub_splits["lambda"]`) + Compute Optimizer (memory rightsizing, inline). Cross-source dedup by normalized bare function name (handles qualified ARNs), authority CoH > CO > enhanced; CO opt-in placeholder → `ctx.warn` + dropped (mirrors EC2/RDS). Enhanced checks: Excessive-Memory & ARM-migration are **$0 advisory** (metric-gated, `Counted=False`); **Provisioned Concurrency** priced from a module constant (`_LAMBDA_PC_PRICE_PER_GB_SEC`, arch-aware x86/arm64, region-scaled once) and **CloudWatch-gated** on `ProvisionedConcurrencyUtilization` (saving = unused fraction `1−max_util`; no metric → $0 advisory). `requires_cloudwatch`/`reads_fast_mode`; each PC finding carries a structured `AuditBasis`. | AWS APIs + module constant + CloudWatch |
| `dynamodb.py` | RCU/WCU hourly rates × 730 | Module constants |
| `containers.py` | Fargate vCPU/mem hourly rates × 730 | Module constants |
| `network.py` | Composite of 5 sub-shims (`elastic_ip`, `nat_gateway`, `vpc_endpoints`, `load_balancer`, ASG via `services/ec2.get_auto_scaling_checks`). Priced via `get_eip_monthly_price()`, `get_nat_gateway_monthly_price()`, `get_vpc_endpoint_monthly_price()`, and `get_alb_monthly_price()` / `get_nlb_monthly_price()` / `get_gwlb_monthly_price()` / `get_clb_monthly_price()` (each ELB type has its own `productFamily`; ALB≠Classic). Each domain has a region-scaled `FALLBACK_*` constant for the `pricing_engine=None` path. Emits **5 per-domain SourceBlocks** (`elastic_ips`/`nat_gateways`/`vpc_endpoints`/`load_balancers`/`auto_scaling_groups`) — all 5 registered in `PHASE_B_HANDLERS`→`_render_network_enhanced_checks`. ASG block is **advisory** (rightsizing owned by the EC2 tab). NAT same-AZ vs cross-AZ savings are de-duplicated; NAT/LB throughput, missing-endpoint, and Classic-ELB checks are **$0 advisory** (metric/rate-gated). Interface VPC endpoints priced **per-AZ**. Sub-shim failures classified via `services/_aws_errors.record_aws_error` (AccessDenied→permission_issue, else warn). No CoH/CO. | AWS Pricing API |
| `network_cost.py` | **Distinct from `network.py`** — analyzes Cost Explorer data-transfer spend (cross-region / cross-AZ / internet-egress) + EC2 topology (peering/TGW). **Advisory-only:** CE returns *blended dollars* with no per-flow GB, co-location, or topology signal, so no fixed fraction is defensibly recoverable. Every transfer/TGW rec is a `$0` `Counted=False` advisory (measured spend + lever shown, never summed) — the old 0.30/0.50/0.40 reduction factors and the circular TGW branch (re-derived GB from dollars already scored → ~20% double-count) are removed (network_cost H1/H2). A counted dollar would require a per-flow GB signal the CE query does not expose. CE/EC2 failures classified via `record_aws_error` (H3). No CoH/CO. | Cost Explorer (advisory) |
| `file_systems.py` | `get_efs_monthly_price_per_gb(class)` + `get_efs_ia_access_price_per_gb()` + `get_fsx_storage_price_per_gb(type, storage, deployment)`. Counted = EFS idle-delete, **CloudWatch-gated** EFS IA-lifecycle (cold_gb from `DataReadIOBytes`/`DataWriteIOBytes`, net of IA access charge; advisory if no metrics / fast_mode / net≤0), and **Windows-only** SSD→HDD. Lustre/ONTAP/OpenZFS, throughput, ONTAP tiering, backup are advisory. `requires_cloudwatch`/`reads_fast_mode`. No CoH/CO (neither service is covered). | AWS Pricing API + CloudWatch |
| `workspaces.py` | `get_instance_monthly_price("AmazonWorkSpaces", ...)` | AWS Pricing API |
| `glue.py` | Dev endpoints counted at their real DPU footprint × `$0.44/DPU-hour × 730` (`WorkerType`→DPU multiplier, execution-class aware — glue C1/H2; the old flat 160-hr/5-DPU assumption is removed). ETL **jobs** are a `$0` `Counted=False` advisory (no per-job usage signal — glue H3). | Module constant (live-validated `USE1-DEVED-DPU-Hour`) |
| `lightsail.py` | `get_instance_monthly_price("AmazonLightsail", ...)` | AWS Pricing API |
| `apprunner.py` | Idle (0-request) service counted at its 24/7 provisioned-**memory** charge only: `mem_gb × $0.007/GB-hr × 730` (apprunner C1; the active vCPU charge is not quantified here — an idle service's recoverable saving is the standing memory cost). | Module constant (live-validated `$0.0070/GB-hr`) |
| `transfer.py` | `$0.30/protocol/hour × 730` | Module constant |
| `mediastore.py` | `get_s3_monthly_price_per_gb("STANDARD")` | AWS Pricing API |
| `quicksight.py` | SPICE tier pricing ($0.25–$0.38/GB) | Module constants |
| `athena.py` | CloudWatch ProcessedBytes → $5/TB | CloudWatch + constant |
| `step_functions.py` | CloudWatch ExecutionsStarted → $0.025/1K | CloudWatch + constant |
| `elasticache.py` | `get_instance_monthly_price("AmazonElastiCache", node_type, engine=engine)` — engine pins the NodeUsage SKU (Redis/Memcached/Valkey share the instance type — SR-1); the engine string is normalized via `.capitalize()` to match the **case-sensitive** Pricing-API guard, else a lowercase `"redis"` silently priced $0 (**ElastiCache C2**). **Consumes Cost Hub** via `ctx.cost_hub_splits["elasticache"]` (authoritative: a CoH-covered cluster suppresses its heuristic levers). Per-category rates: Underutilized 0.30 (CloudWatch-gated), Valkey 0.20; **Graviton is NOT a flat rate** — counted as the exact `(x86 node price − Graviton node price) × NumNodes` delta (elasticache H2; the old flat 0.20 overcounted ~4.3×). `NumNodes` threaded onto every lever so multi-node clusters price like-for-like (elasticache H1). Single highest-$ lever counted per cluster, rest advisory; Reserved Nodes demoted (commitment lever). | AWS Pricing API + Cost Optimization Hub |

### Parse-rate (5 adapters)
Extract dollar amounts from recommendation text or use keyword-based estimates:

| Adapter | Method |
|---------|--------|
| `cloudfront.py` | Fixed $25/rec |
| `api_gateway.py` | Keyword-based |
| `opensearch.py` | Keyword-based |
| `ami.py` | `parse_dollar_savings()` |
| `monitoring.py` | Fixed per-rec estimates |

### Advisory-only (1 adapter)
| Adapter | Method |
|---------|--------|
| `batch.py` | Spot/Fargate-Spot/Graviton recs are emitted as `$0` `Counted=False` advisories (the prior flat per-rec estimate is removed — a "60-90% with Spot" figure has no account-specific dollar without per-CE measured spend). Errors classified via `record_aws_error` (batch H2/H3). |

## Consuming AWS Cost Optimization Hub Buckets

`services/_coh_dedup.py` holds the shared helpers for adapters that render a
`ctx.cost_hub_splits[<svc>]` bucket inline (elasticache, redshift, opensearch,
…): `normalize_resource_id` (canonical de-dup key — strips an ARN to its final
segment so a CoH `resourceArn` and a heuristic `ClusterId`/`DomainName`
converge), `is_renderable_coh_rec` (drops RI/SP purchase recs that belong in
`commitment_analysis` and `N/A`-resource recs), and `coh_savings`. CoH is the
**authority**: when it covers a resource, the adapter demotes that resource's
heuristic levers to `Counted=False` so the same saving is never double-counted
(CoH > CO > heuristic). Mirrors the inline `_coh_is_renderable` in the RDS
adapter (SR-3).

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