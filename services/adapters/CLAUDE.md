# Service Adapters

34 ServiceModule adapter files in `services/adapters/`. Each implements `scan(ctx) -> ServiceFindings`. Two former adapters were retired from `ALL_MODULES` on 2026-05-14 and their findings now flow per-service: AWS Cost Optimization Hub recommendations are fetched once by `ScanOrchestrator._prefetch_advisor_data` and consumed via `ctx.cost_hub_splits[<service_key>]`; AWS Compute Optimizer recommendations are pulled inline by EC2 / EBS / RDS / Lambda / Containers adapters via the `services.advisor.get_<resource>_compute_optimizer_recommendations` helpers.

## Pricing Models

The 34 adapters fall into six pricing strategies. Counts below sum to 34.
Whatever the strategy, the invariant is the same: a **counted** dollar must be
account-specific and defensible; anything speculative is a `$0` `Counted=False`
advisory (rendered, never summed).

### Live Pricing — `PricingEngine` (13 adapters)
Call `ctx.pricing_engine` methods for AWS Pricing API lookups; fall back to a
region-scaled constant × `ctx.pricing_multiplier` on API failure.

| Adapter | Method | Pricing Source |
|---------|--------|---------------|
| `ec2.py` | OS/license-aware `get_ec2_hourly_price(type, os, license_model)`. Prev-gen / rightsizing / burstable use the **exact current→target price delta** (target = migration map or one-size-down); idle = full cost; cron/batch/instance-store/non-prod-scheduling use `EC2_SAVINGS_FACTORS` **only when corroborated by the CloudWatch idle/low-CPU rightsizing signal** — the adapter builds `corroborated_ids` from the idle/rightsizing/burstable enhanced categories (`_CW_LOW_UTIL_CATEGORIES`) and passes it to `get_advanced_ec2_checks`; an uncorroborated tag lever is a `$0` `Counted=False` advisory that still renders (figure in `AdvisoryEstimate`) but never enters `best_by_instance`/the headline (ec2 H2). Spot = on-demand−Spot via `describe_spot_price_history` (factor-free, never gated). ASG error paths classified via `record_aws_error` (network NET-04). Cross-source + ASG-member dedup in the adapter; every finding records `OS` + `PricingBasis`. | AWS Pricing API + EC2 Spot history |
| `ebs.py` | `get_ebs_monthly_price_per_gb()`; Compute Optimizer via `compute_optimizer_savings()`, each CO rec carrying a per-volume `AuditBasis` | AWS Pricing API + Compute Optimizer |
| `rds.py` | `get_rds_instance_monthly_price(engine, class, multi_az, license_model)` — pins `databaseEdition` (SQL Server/Oracle, from the engine string) + `licenseModel` (from the instance's `LicenseModel`) + Multi-AZ deploymentOption SKU; `get_rds_backup_storage_price_per_gb(engine)` (Aurora backup rate when engine is Aurora — rds L1). Compute Optimizer via `compute_optimizer_savings()`; **consumes Cost Hub** via `ctx.cost_hub_splits["rds"]`. Cross-source dedup + RI/backup demotion in `services/rds_logic.py`; opt-in placeholder→warning mirrors EC2. Multi-AZ-disable & non-prod scheduling are **tag-aware + CloudWatch-gated** (`list_tags_for_resource` Environment/Stage with name-substring fallback; DatabaseConnections; `requires_cloudwatch`/`reads_fast_mode`; Aurora members excluded — rds L2/L3). No gp2→gp3 check (RDS gp2==gp3 base price); backup retention is advisory. **Coverage gaps (intentional):** Aurora Serverless v2 (ACU), Aurora cluster rightsizing, read replicas, stopped instances, Extended Support not priced. | AWS Pricing API + Cost Optimization Hub |
| `dms.py` | `get_dms_instance_monthly_price(instance_class)` for idle/oversized replication instances; CloudWatch-gated. Non-priceable levers → `$0` advisory. | AWS Pricing API + CloudWatch |
| `s3.py` | `get_s3_monthly_price_per_gb()` via `PricingEngine.for_region(bucket_region)` — buckets are global, priced at their home region. Cost summed per storage class at each class's own rate. Savings are **evidence-gated**: only the Standard→Standard-IA delta on bytes proven cold by CloudWatch request metrics (0 GETs/30d); no evidence → $0 advisory. Needs `s3:GetMetricsConfiguration`. See `docs/audits/S3_AUDIT_FINDINGS.md`. | AWS Pricing API |
| `mediastore.py` | `get_s3_monthly_price_per_gb("STANDARD")` for container storage; CloudWatch-gated, advisory when no metric. | AWS Pricing API + CloudWatch |
| `file_systems.py` | `get_efs_monthly_price_per_gb(class)` + `get_efs_ia_access_price_per_gb()` + `get_fsx_storage_price_per_gb(type, storage, deployment)`. Counted = EFS idle-delete, **CloudWatch-gated** EFS IA-lifecycle (cold_gb from `DataReadIOBytes`/`DataWriteIOBytes`, net of IA access charge; advisory if no metrics / fast_mode / net≤0), and **Windows-only** SSD→HDD. FSx file-cache enumeration is paginated (file_systems L3). Lustre/ONTAP/OpenZFS, throughput, ONTAP tiering, backup are advisory. `requires_cloudwatch`/`reads_fast_mode`. No CoH/CO (neither service is covered). | AWS Pricing API + CloudWatch |
| `network.py` | Composite of 5 sub-shims (`elastic_ip`, `nat_gateway`, `vpc_endpoints`, `load_balancer`, ASG via `services/ec2.get_auto_scaling_checks`). Priced via `get_eip_monthly_price()`, `get_nat_gateway_monthly_price()`, `get_vpc_endpoint_monthly_price()`, and `get_alb_monthly_price()` / `get_nlb_monthly_price()` / `get_gwlb_monthly_price()` / `get_clb_monthly_price()` (each ELB type has its own `productFamily`; ALB≠Classic). Each domain has a region-scaled `FALLBACK_*` constant for the `pricing_engine=None` path. Emits **5 per-domain SourceBlocks** — all 5 registered in `PHASE_B_HANDLERS`→`_render_network_enhanced_checks`. ASG block is **advisory**. EIPs on stopped instances are not double-counted against the multiple-EIPs lever (NET-03). NAT same-AZ vs cross-AZ de-duplicated; NAT/LB throughput, missing-endpoint, and Classic-ELB checks are **$0 advisory**. Interface VPC endpoints priced **per-AZ**. Sub-shim failures classified via `record_aws_error`. **Consumes Cost Hub** for NAT Gateways via `ctx.cost_hub_splits["network"]` (`NatGateway`→`network` in the orchestrator `type_map`): AWS-computed per-NAT idle savings render counted, and the local VPC-scoped consolidation/dev-test levers are demoted to advisory in any CoH-covered VPC (CoH > heuristic, dedup by VPC via the NAT→VPC map the shim exposes; partial-VPC coverage under-counts rather than overstates). No CO. | AWS Pricing API + Cost Optimization Hub |
| `elasticache.py` | `get_instance_monthly_price("AmazonElastiCache", node_type, engine=engine)` — engine pins the NodeUsage SKU (Redis/Memcached/Valkey share the instance type — SR-1); engine normalized to match the **case-sensitive** Pricing-API guard (ElastiCache C2). **Consumes Cost Hub** via `ctx.cost_hub_splits["elasticache"]` (authoritative). Underutilized = current→one-size-down node delta × NumNodes (CloudWatch-gated; elasticache H3), Valkey 0.20; **Graviton is the exact `(x86 − Graviton) × NumNodes` delta** (elasticache H2/H4). Single highest-$ lever counted per cluster, rest advisory; Reserved Nodes demoted. The Reserved-Nodes advisory is gated on `NumCacheNodes >= 2` from `describe_cache_clusters`, so it is **effectively Memcached-scoped**: a Redis cluster's replication-group members each report `NumCacheNodes == 1`, so covering Redis would require looping `describe_replication_groups` instead — that lever is $0 advisory, so no counted dollar is affected. **Coverage gaps (intentional):** ElastiCache Serverless (ElastiCacheProcessingUnit-priced) and data-tiering nodes are not priced. | AWS Pricing API + Cost Optimization Hub |
| `opensearch.py` | **Live-priced** via `get_instance_monthly_price("AmazonES", …)` (`.elasticsearch`→`.search` SKU normalization — opensearch L1): idle = full instance×count + gp3 storage; Underutilized = current→one-size-down node delta; Graviton = x86→same-size-Graviton node delta; storage = gp2→gp3 per-GB delta. CoH-authoritative; non-priceable levers → `$0` advisory (opensearch C2/C3, live-audit H4). | AWS Pricing API + Cost Optimization Hub |
| `eks.py` | `get_eks_control_plane_hourly()` (per-cluster $0.10/hr) and `get_eks_extended_support_hourly()` for clusters on an extended-support Kubernetes version; node-group rightsizing uses `get_ec2_hourly_price()` as an **advisory** signal. **Consumes Cost Hub** via `ctx.cost_hub_splits["eks_cost"]`. 0-node groups emit no card; dedup is `check_type`-aware. CloudWatch-gated. | AWS Pricing API + Cost Optimization Hub |
| `sagemaker.py` | `get_sagemaker_instance_monthly(instance_type)` for idle real-time endpoints (CloudWatch `Invocations`-gated) and oversized notebook instances. Idle endpoints are removed from the Active-Endpoints stat count so they are not double-represented (sagemaker L3). Non-priceable / unmeasured levers → `$0` advisory. | AWS Pricing API + CloudWatch |
| `containers.py` | ECS Fargate via `get_fargate_vcpu_hourly()` / `get_fargate_gb_hourly()` / `get_fargate_windows_os_hourly()` and ECR storage via `get_ecr_storage_gb_month()`, each with a `FALLBACK_*` constant on API failure. **Consumes Cost Hub** (`cost_hub_splits["containers"]`) and Compute Optimizer (ECS) inline; authority **CoH > CO > heuristic** with cross-cluster dedup. `describe_repositories` / `list_images` paginated; ECR failures classified via `_ecr_failure`→`record_aws_error` (containers L2/L3). | AWS Pricing API + CoH + Compute Optimizer |

### AWS-supplied dollars — Cost Optimization Hub / Compute Optimizer (4 adapters)
No own rate table — these surface AWS-computed savings (flat float at the CoH/CO
top level) and demote any overlapping local heuristic to advisory.

| Adapter | Method |
|---------|--------|
| `lambda_svc.py` | **Consumes Cost Hub** (`cost_hub_splits["lambda"]`) + Compute Optimizer (memory rightsizing, inline). Dedup by normalized bare function name, authority CoH > CO > enhanced; CO opt-in placeholder → `ctx.warn` + dropped. Excessive-Memory & ARM-migration are **$0 advisory** (metric-gated); **Provisioned Concurrency** priced from a module constant (`_LAMBDA_PC_PRICE_PER_GB_SEC`, arch-aware, region-scaled once) and **CloudWatch-gated** on `ProvisionedConcurrencyUtilization` (saving = `1−max_util`; no metric → $0 advisory). `requires_cloudwatch`/`reads_fast_mode`; each PC finding carries an `AuditBasis`. |
| `aurora.py` | Aurora cluster recommendations sourced from `cost_hub_splits` plus CloudWatch-gated local levers; cluster/ACU rightsizing without a per-resource dollar is a `$0` `Counted=False` advisory. |
| `redshift.py` | **Consumes Cost Hub** (`cost_hub_splits["redshift"]`, authoritative). Pause/resize/RA3-migration heuristics without an account-specific dollar are advisory; CoH-covered clusters suppress the overlapping heuristic. |
| `commitment_analysis.py` | Savings Plans / Reserved-capacity recommendations from Cost Optimization Hub + Cost Explorer; Fargate SP coverage is CloudWatch-gated. The dollar is AWS-supplied; this adapter never fabricates a commitment saving. |

### Module-constant rates, live-validated (8 adapters)
A hardcoded rate (validated against the public price list, region-scaled by
`pricing_multiplier`) — the AWS Pricing API exposes no usable retail SKU for the
billed dimension.

| Adapter | Method |
|---------|--------|
| `dynamodb.py` | Provisioned RCU/WCU hourly rates × 730. **Consumes Cost Hub** (`cost_hub_splits`); ACTIVE-table + non-zero-`ItemCount` gated so transient/empty tables don't produce a counted over-provisioning dollar (dynamodb L3/L4). |
| `glue.py` | Dev endpoints counted at their real DPU footprint × `$0.44/DPU-hour × 730` (`WorkerType`→DPU multiplier, execution-class aware — glue C1/H2). ETL **jobs** are a `$0` `Counted=False` advisory (no per-job usage signal — glue H3). |
| `apprunner.py` | Idle (0-request) service counted at its 24/7 provisioned-**memory** charge: `mem_gb × $0.0070/GB-hr × 730` (apprunner C1). A missing/unparseable `Memory` config → warn + `$0` advisory, never a fabricated 2 GB dollar (apprunner L2). |
| `transfer.py` | `$0.30/protocol/hour × 730` for idle SFTP/FTPS/FTP servers; CloudWatch-gated on `BytesIn`/`BytesOut` (`required_clients()` includes `cloudwatch`). |
| `lightsail.py` | Bundle monthly price from a live-validated module constant table (Lightsail publishes no retail Pricing-API SKU), region-scaled. |
| `workspaces.py` | `WORKSPACE_BUNDLE_MONTHLY` + `WORKSPACE_AUTOSTOP_PRICING` (hardcoded us-east-1 Windows+Included tables, live-validated 2026-06-27), region-scaled by `pricing_multiplier`; the AmazonWorkSpaces Pricing API exposes no usable retail SKU. Bundle-rightsizing and billing-mode recs are gated on the actual `License`/`OperatingSystem` so a BYOL/Linux bundle isn't priced at the Windows+Included rate (workspaces L1). |
| `quicksight.py` | SPICE tier pricing ($0.25–$0.38/GB) from module constants. |
| `bedrock.py` | `PT_HOURLY_PRICE` dict for idle Provisioned Throughputs; an unknown-rate PT → `$0` `Counted=False` advisory. CloudWatch-gated (`requires_cloudwatch`/`reads_fast_mode`); 4 sources (idle PTs, PT break-even, idle knowledge bases, idle agents). No CoH/CO. |

### CloudWatch-metered + constant rate (2 adapters)
Savings derived from a measured CloudWatch volume × a published unit rate.

| Adapter | Method |
|---------|--------|
| `api_gateway.py` | CloudWatch `Count` metric → `(REST $3.50/M − HTTP $1.00/M) × monthly_requests`; a rec with no measured volume (`$0`, fast mode / throttled CW) is `Counted=False` advisory (the prior flat $50 fabrication is gone). REST APIs only. |
| `athena.py` | CloudWatch `ProcessedBytes` → `$5/TB` scanned for workgroups that would benefit from partitioning/compression; advisory when unmeasured. |

### Field-extraction / composite (2 adapters)
Sum the float `EstimatedMonthlySavings` field already attached to each rec (no
rate lookup in the adapter itself).

| Adapter | Method |
|---------|--------|
| `ami.py` | Sums float `EstimatedMonthlySavings` per rec (EBS snapshot GB × the live snapshot rate from `PricingEngine.get_ebs_snapshot_price_per_gb()`; fallback `0.05 × pricing_multiplier`) — does **not** call `parse_dollar_savings()`. Sizes on **actual stored bytes** (`FullSnapshotSizeInBytes`), VolumeSize fallback flagged. **Cross-AMI snapshot dedup**: `_snapshot_storage_gb` takes a `counted_snapshot_ids` set so a snapshot shared by N AMIs is counted **once** (the first AMI); an all-shared AMI becomes a `Counted=False` advisory ("shared — counted under the other AMI"), partial-overlap counts only unique GB (a shared snapshot is billed once and freed only when every referencing AMI is deregistered — counting it per-AMI double-counts). Launch-permission check runs **before** snapshot attribution so a skipped AMI never claims a snapshot id. Unused-AMI age bucketed (30-90 / 90-180 / 180-365 / 1-2y / 2y+). |
| `monitoring.py` | 4-domain composite (CloudWatch custom metrics + CloudTrail + Backup + Route53). Savings use the numeric `EstimatedMonthlySavings` the shim emits (CW custom-metric cost via a 4-tier per-namespace ladder — monitoring L2); best-practice nudges that resolve to `$0` → `Counted=False` advisory. |

### Advisory-only — every rec `$0` `Counted=False` (5 adapters)
No account-specific dollar is defensible without a usage signal the data source
does not expose; the lever is shown and measured spend (where available) is
displayed, but nothing is summed into the headline.

| Adapter | Method |
|---------|--------|
| `batch.py` | Spot/Fargate-Spot/Graviton recs are `$0` advisories (a "60-90% with Spot" figure has no account-specific dollar without per-CE measured spend). Errors classified via `record_aws_error` (batch H2/H3). |
| `cloudfront.py` | Every rec is `$0` + `PricingWarning`; honest data-transfer savings need the CloudWatch `BytesDownloaded` metric + per-distribution `PriceClass`. |
| `msk.py` | `get_msk_broker_hourly_price()` exists, but every provisioned cluster emits a `$0` `Counted=False` advisory (msk C1); MSK Express, MSK Serverless (DCU-priced), and tiered-storage clusters from the listing have no defensible flat saving. No CoH/CO. |
| `network_cost.py` | **Distinct from `network.py`** — Cost Explorer data-transfer spend (cross-region / cross-AZ / internet-egress) + EC2 topology (peering/TGW). CE returns *blended dollars* with no per-flow GB, so no fixed fraction is recoverable; every transfer/TGW rec is a `$0` advisory (the old 0.30/0.50/0.40 factors and circular TGW double-count are removed — network_cost H1/H2). CE/EC2 failures classified via `record_aws_error` (H3). No CoH/CO. |
| `step_functions.py` | CloudWatch `ExecutionsStarted` is read for context, but Standard→Express migration savings are underdetermined (depend on state-transition counts the metric does not expose), so every rec is a `$0` `Counted=False` advisory. |

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