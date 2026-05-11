# AWS Cost Optimization Scanner — Future Roadmap

> **Status**: Research-backed roadmap based on AWS documentation, re:Invent 2024/2025 launches, FinOps Foundation standards, and competitive tool analysis.
> **Last Updated**: May 2026
> **Current Coverage**: 28 adapters (EC2, AMI, EBS, RDS, FileSystems/EFS+FSx, S3, DynamoDB, Lambda, Containers/ECS+Fargate, Network/EIP+NAT+VPC+ELB+ASG, Monitoring/CloudWatch, ElastiCache, OpenSearch, CloudFront, API Gateway, StepFunctions, Lightsail, Redshift, DMS, QuickSight, AppRunner, Transfer, MSK, Workspaces, MediaStore, Glue, Athena, Batch)

---

## Executive Summary

The scanner currently covers 28 adapters across 30 AWS service categories with rightsizing, idle resource detection, and reservation analysis. This roadmap identifies **28 new capabilities** across 4 phases, targeting:

- **87% of AWS billable services** (up from ~60% today)
- **Cost Optimization Hub parity** (18 recommendation types vs our current ~12)
- **FOCUS 1.2 compliance** for FinOps maturity
- **AI/ML cost visibility** (Bedrock, SageMaker — fastest-growing AWS spend category)

---

## Priority Framework

Each item is scored on a **1-5 scale** for Impact (savings potential) and Feasibility (implementation ease).

| Score | Impact Meaning | Feasibility Meaning |
|-------|---------------|-------------------|
| 5 | Top-3 AWS cost driver | Uses existing SDK patterns, <1 week |
| 4 | Significant cost area | New SDK calls, 1-2 weeks |
| 3 | Moderate savings | New API integration, 2-3 weeks |
| 2 | Niche / edge case | Complex logic, 3-4 weeks |
| 1 | Minimal savings | Requires architecture changes |

**Priority = Impact × Feasibility** (max 25)

---

## Phase 1: Quick Wins (1–2 weeks each)

High-impact items that extend existing patterns with minimal effort.

### 1.1 Compute Optimizer Integration ⭐ Priority: 25/25

**Impact**: 5 | **Feasibility**: 5

AWS Compute Optimizer generates rightsizing recommendations for EC2, EBS, Lambda, ECS, RDS, Aurora, and NAT Gateway. Instead of calculating rightsizing ourselves, consume Compute Optimizer's ML-powered recommendations.

**What to build**:
- New adapter: `services/adapters/compute_optimizer.py`
- Call `GetRecommendations` and `GetEC2RecommendationProjectedMetrics` APIs
- Map recommendation types to our `ServiceFindings` structure
- Support: EC2 (instance type), EBS (volume type/size), Lambda (memory/provisioned concurrency), ECS (task size), Auto Scaling Group (instance type)

**Why now**: Compute Optimizer is free for basic recommendations (enhanced requires Opt-In). Covers 7 resource types in one adapter. Our current EC2/EBS/Lambda rightsizing logic can be supplemented or replaced.

**API calls needed**:
```python
compute_optimizer.get_recommendation_summaries()
compute_optimizer.get_ec2_instance_recommendations()
compute_optimizer.get_ebs_volume_recommendations()
compute_optimizer.get_lambda_function_recommendations()
compute_optimizer.get_ecs_service_recommendations()
compute_optimizer.get_auto_scaling_group_recommendations()
```

---

### 1.2 Cost Optimization Hub Aggregation ⭐ Priority: 24/25

**Impact**: 5 | **Feasibility**: ~5

Cost Optimization Hub provides a unified view of optimization recommendations across 18+ types. Use it as an additional data source and cross-validation layer.

**What to build**:
- New adapter: `services/adapters/cost_optimization_hub.py`
- Call `ListRecommendations` with account/region filters
- Map `RecommendationType` enum to our finding categories
- De-duplicate against our own adapter findings

**Supported recommendation types** (from AWS docs):
- `Ec2InstanceRightsizing`, `Ec2InstanceSavingsPlans`, `ComputeSavingsPlans`
- `EbsVolumeRightsizing`, `EbsSnapshotDelete`
- `LambdaMemoryOptimization`, `LambdaEventBridgeIdleRule`
- `RdsInstanceRightsizing`, `RdsInstanceSavingsPlans`, `RdsComputeSavingsPlans`
- `S3StorageOptimization`, `S3GlacierInstantAccess`
- `EcsServiceRightsizing`, `EksClusterRightsizing`
- `ElastiCacheReservedInstances`, `OpenSearchReservedInstances`
- `RedshiftReservedInstances`

**Why now**: Single API gives us 18 recommendation types we can cross-reference. Zero compute on our side.

---

### 1.3 Aurora-Specific Checks ⭐ Priority: 20/25

**Impact**: 5 | **Feasibility**: 4

Aurora accounts for 30%+ of database spend in most enterprises. It has unique optimization opportunities that generic RDS checks miss.

**What to build**:
- New adapter: `services/adapters/aurora.py`
- Checks:
  - Aurora Serverless v2 scaling misconfiguration (min/max capacity too wide)
  - Aurora Global Database replica lag and cross-region cost
  - Aurora backtrack storage growth
  - Aurora clone sprawl (forgotten clones)
  - Aurora I/O-Optimized vs Standard storage tier analysis
  - Aurora read replica utilization vs writer instance sizing

**API calls needed**:
```python
rds.describe_db_clusters()
rds.describe_db_cluster_snapshots()
rds.describe_global_clusters()
cloudwatch.get_metric_data()  # for ServerlessV2Capacity, Lag metrics
```

**Why now**: Aurora is the fastest-growing database engine. Serverless v2 adoption is accelerating. I/O-Optimized storage is a major cost lever.

---

### 1.4 Savings Plans & RI Coverage Analysis ⭐ Priority: 20/25

**Impact**: 5 | **Feasibility**: 4

Track commitment coverage across the account and identify under-utilized commitments.

**What to build**:
- New adapter: `services/adapters/commitment_analysis.py`
- Checks:
  - Savings Plans utilization rate (target: >95%)
  - Reserved Instance utilization and coverage
  - Upfront vs No-Upfront SP optimization
  - SP expiration alerts (30/60/90 days)
  - RI/SP coverage gaps by service, instance family, region
  - Recommend optimal SP portfolio based on usage patterns

**API calls needed**:
```python
costexplorer.get_savings_plans_utilization()
costexplorer.get_savings_plans_utilization_details()
costexplorer.get_savings_plans_coverage()
costexplorer.get_reservation_utilization()
costexplorer.get_reservation_coverage()
costexplorer.get_savings_plans_purchase_recommendation()
costexplorer.get_reservation_purchase_recommendation()
```

**Why now**: Savings Plans are the #1 cost lever for most AWS accounts. Without coverage analysis, organizations leave 20-40% savings on the table.

---

## Phase 2: Medium-Term (2–4 weeks each)

Moderate complexity items that expand into new AWS service categories.

### 2.1 AI/ML Cost Optimization (Bedrock + SageMaker) ⭐ Priority: 20/25

**Impact**: 5 | **Feasibility**: 4

Generative AI is the fastest-growing AWS spend category. Bedrock alone has multiple cost optimization levers.

**What to build**:
- New adapter: `services/adapters/bedrock.py`
- Checks:
  - On-Demand vs Provisioned Throughput analysis
  - Model usage by tier (Claude Haiku vs Sonnet vs Opus — right-sizing model selection)
  - Prompt caching utilization
  - Cross-region inference cost comparison
  - Bedrock Agents idle detection
  - Knowledge Base OCU right-sizing
  - Batch inference job scheduling opportunities

**New adapter**: `services/adapters/sagemaker.py`
- Checks:
  - SageMaker endpoint idle detection
  - Notebook instance usage patterns (left running overnight?)
  - Training job instance type optimization
  - Multi-model endpoint consolidation
  - Spot training adoption rate

**API calls needed**:
```python
bedrock.list_foundation_models()
bedrock.get_model_invocation_logging_configuration()
cloudwatch.get_metric_data()  # Invocations, TokenCount, Latency
sagemaker.list_endpoints()
sagemaker.list_notebook_instances()
sagemaker.list_training_jobs()
```

**Why now**: AI/ML spend grew 6x in 2024-2025. Most organizations have zero visibility into per-model, per-use-case costs.

---

### 2.2 Network & Data Transfer Cost Analysis ⭐ Priority: 16/25

**Impact**: 4 | **Feasibility**: 4

Data transfer is often the #3 cost after compute and storage, but rarely optimized.

**What to build**:
- New adapter: `services/adapters/network_cost.py`
- Checks:
  - Cross-region data transfer costs
  - Cross-AZ data transfer patterns
  - Internet egress optimization (CloudFront vs direct)
  - VPC Peering vs Transit Gateway cost comparison
  - PrivateLink cost efficiency
  - Direct Connect utilization

**API calls needed**:
```python
costexplorer.get_cost_and_usage()  # with filter for data transfer
cloudwatch.get_metric_data()  # NetworkIn/Out across resources
ec2.describe_vpc_peering_connections()
ec2.describe_transit_gateways()
```

---

### 2.3 Cost Anomaly Detection Integration ⭐ Priority: 15/25

**Impact**: 4 | **Feasibility**: ~4

AWS Cost Anomaly Detection uses ML to identify spending anomalies. Consume and enhance these alerts.

**What to build**:
- New adapter: `services/adapters/cost_anomaly.py`
- Checks:
  - Active anomaly alerts
  - Historical anomaly patterns (seasonal waste)
  - Anomaly root cause analysis (link to specific resource changes)
  - Anomaly-to-recommendation correlation

**API calls needed**:
```python
ce.get_anomalies()
ce.get_anomaly_subscriptions()
ce.get_anomaly_monitors()
```

---

### 2.4 Kubernetes/EKS Cost Visibility ⭐ Priority: 15/25

**Impact**: 4 | **Feasibility**: ~4

EKS is the standard for Kubernetes on AWS. Cost allocation within clusters is a major FinOps gap.

**What to build**:
- New adapter: `services/adapters/eks.py`
- Checks:
  - EKS cluster node utilization (CPU/memory)
  - Fargate vs EC2 backtesting
  - Karpenter node right-sizing recommendations
  - Pod resource request vs actual usage (waste from over-provisioning)
  - Namespace/label-based cost allocation
  - Unused or under-utilized LoadBalancers from K8s Services

**API calls needed**:
```python
eks.list_clusters()
eks.describe_cluster()
ec2.describe_instances()  # filter by eks:cluster-name tag
cloudwatch.get_metric_data()  # Container Insights metrics
```

---

## Phase 3: Strategic Capabilities (4–8 weeks each)

Major feature expansions requiring architectural consideration.

### 3.1 FOCUS 1.2 Export Compliance ⭐ Priority: 16/25

**Impact**: 4 | **Feasibility**: 4

The FinOps Open Cost and Usage Specification (FOCUS) is becoming the industry standard for cloud cost data. AWS added FOCUS 1.2 support in Cost Explorer.

**What to build**:
- New module: `core/focus_export.py`
- Generate cost reports in FOCUS 1.2 schema
- Map scanner findings to FOCUS `CommitmentDiscountCategory`, `CommitmentDiscountType`
- Enable cross-tool comparison (our findings vs CloudHealth vs Kubecost, all in FOCUS format)
- Support FOCUS column set: `BillingAccountId`, `BillingPeriodStart`, `ChargeType`, `ConsumedQuantity`, `EffectiveCost`, `ListCost`, `ResourceCategory`, `ServiceCategory`, etc.

**Why this matters**: FinOps Foundation is pushing FOCUS as the standard. Organizations with mature FinOps practices will expect FOCUS-compliant output.

---

### 3.2 Multi-Account & Organization Support ⭐ Priority: 16/25

**Impact**: 4 | **Feasibility**: 4

Enterprise AWS deployments use Organizations with 10-1000+ accounts. The scanner currently operates on a single account.

**What to build**:
- Extend `ScanContext` to accept Organization access
- Use `organizations.ListAccounts()` to enumerate accounts
- Assume roles across accounts via STS
- Aggregate findings across organizational units (OUs)
- Generate per-account, per-OU, and org-wide reports
- Tag-based cost allocation mapping

**Architecture changes**:
```python
# Current
ScanContext(account_id="123456", region="us-east-1", ...)

# Target
ScanContext(
    account_id="123456",
    region="us-east-1",
    organization_id="o-abc123",
    ou_filter=["ou-production", "ou-development"],
    assume_role="CostScannerRole",
    ...
)
```

---

### 3.3 Infrastructure-as-Code Cost Estimation (Infracost Pattern) ⭐ Priority: 15/25

**Impact**: 4 | **Feasibility**: ~4

Shift-left cost optimization — catch expensive infrastructure before it's deployed.

**What to build**:
- New module: `core/iac_estimator.py`
- Parse Terraform `.tf` files → extract resource declarations → estimate monthly cost
- Parse CloudFormation templates → same
- Compare current state vs proposed state (diff costing)
- Generate cost impact reports for pull requests
- Integration with CI/CD (GitHub Actions, GitLab CI)

---

### 3.4 Budget & Forecasting Engine ⭐ Priority: 15/25

**Impact**: 4 | **Feasibility**: ~4

Use AWS Cost Explorer forecasting + our optimization findings to project savings.

**What to build**:
- New module: `core/forecasting.py`
- Current month forecast (AWS provides this via CE)
- 12-month savings projection from optimization recommendations
- Budget vs actual tracking per service/account/tag
- "What-if" scenarios (e.g., "What if we migrated to Graviton?")
- Savings realization tracking (did implemented recommendations actually save money?)

**API calls needed**:
```python
costexplorer.get_cost_forecast()
costexplorer.get_usage_forecast()
budgets.describe_budgets()
budgets.describe_budget_performance_history()
```

---

### 3.5 Tagging Compliance & Governance ⭐ Priority: 12/25

**Impact**: 3 | **Feasibility**: 4

Tag governance is a prerequisite for effective cost allocation.

**What to build**:
- New adapter: `services/adapters/tag_compliance.py`
- Checks:
  - Required tag presence audit (cost-center, environment, team, application)
  - Tag value consistency (e.g., "prod" vs "production" vs "Prod")
  - Untagged resources by service and cost
  - Tag policy enforcement recommendations
  - Cost allocation tag activation status

---

### 3.6 Well-Architected Cost Pillar Automated Review ⭐ Priority: 12/25

**Impact**: 3 | **Feasibility**: 4

Generate automated Well-Architected Cost Pillar reviews by mapping our checks to W-A questions.

**What to build**:
- New module: `core/well_architected_review.py`
- Map scanner findings to W-A Cost Pillar questions
- Generate a W-A-aligned report with scores
- Identify gaps where we can't answer a W-A question (recommend manual review)

**W-A Cost Pillar mapping** (from research):
| W-A Question | Scanner Coverage |
|---|---|
| COST01: Cost-effective resources | ✅ Compute Optimizer + our rightsizing |
| COST02: Match supply and demand | ✅ Auto Scaling checks |
| COST03: Cost optimization when demand changes | ✅ Reserved/SP analysis |
| COST04: Software licensing costs | ❌ NEW: License Manager integration |
| COST05: Data transfer costs | ❌ NEW: Network cost adapter |
| COST06: Use managed services | ✅ Service-specific checks |
| COST07: Bring workloads closer to users | ✅ CloudFront price class + distribution checks |
| COST08: Implement governance | ❌ NEW: Tag compliance, budgets |
| COST09: Identify and manage spend anomalies | ❌ NEW: Cost anomaly integration |

---

## Phase 4: Vision & Long-Term Bets

Exploratory capabilities that position the scanner as a next-generation FinOps platform.

### 4.1 Unit Cost Economics Dashboard ⭐ Priority: 12/25

Track cost per business metric: cost-per-user, cost-per-transaction, cost-per-api-call.

- Integrate with CloudWatch Application Insights
- Map infrastructure costs to business KPIs
- Trend unit costs over time (efficiency or deterioration?)

### 4.2 AI-Powered Cost Explanations (re:Invent 2025) ⭐ Priority: 10/25

AWS launched AI-powered cost explanations in Cost Explorer. Integrate natural language cost analysis.

- Consume AWS's AI explanations via API
- Generate plain-English optimization narratives
- "Your EC2 costs increased 23% this month because..."

### 4.3 Sustainability / Carbon Footprint Integration ⭐ Priority: 10/25

Map cost optimization to carbon reduction.

- Use AWS Customer Carbon Footprint Tool data
- Show carbon savings alongside cost savings
- Graviton migration = cost savings + carbon reduction
- Right-sizing = fewer resources = lower PUE impact

### 4.4 Real-Time Cost Anomaly Streaming ⭐ Priority: 8/25

Move from batch scanning to real-time cost monitoring.

- EventBridge → Lambda → cost anomaly detection pipeline
- Sub-minute cost anomaly alerts
- Integration with Slack/PagerDuty for cost incidents

### 4.5 Terraform/OpenTofu Policy-as-Code (Sentinel/OPA) ⭐ Priority: 8/25

Generate cost governance policies from scanner findings.

- Export optimization rules as OPA/Rego policies
- Export as Terraform Sentinel policies
- Enforce cost constraints in CI/CD before deployment

### 4.6 Marketplace & Third-Party Integrations ⭐ Priority: 6/25

- Slack bot for cost alerts and optimization digest
- Jira integration for cost optimization tickets
- Datadog/Grafana dashboards for cost metrics
- PagerDuty integration for cost anomalies

---

## Architecture Recommendations

### Current Architecture Gaps (from codebase analysis)

| Gap | Current State | Recommended |
|-----|--------------|-------------|
| No commitment analysis | Only on-demand rightsizing | Add SP/RI adapter |
| No cost forecasting | Point-in-time scan only | Add forecasting module |
| Single-account only | `ScanContext` takes one account | Add org/multi-account |
| No external data sources | All self-computed | Add Compute Optimizer + Cost Hub integration |
| No historical tracking | Each scan is independent | Add scan history + delta tracking |
| No alerting | Report-only | Add threshold-based alerts |

### Recommended New Core Modules

```
core/
├── contracts.py          # (existing) ServiceModule protocol
├── scan_orchestrator.py  # (existing) Scan execution
├── result_builder.py     # (existing) Output formatting
├── forecast_engine.py    # NEW: Cost forecasting & projection
├── focus_export.py       # NEW: FOCUS 1.2 compliant output
├── wa_review.py          # NEW: Well-Architected mapping
├── iac_estimator.py      # NEW: IaC cost estimation
├── scan_history.py       # NEW: Historical scan tracking
└── alerting.py           # NEW: Threshold-based alerts

services/adapters/
├── ec2.py                  # (existing) EC2 rightsizing, idle, burstable
├── ami.py                  # (existing) Unused AMIs, snapshot cleanup
├── ebs.py                  # (existing) Unattached, gp2→gp3, snapshots
├── rds.py                  # (existing) Multi-AZ, backup, Compute Optimizer
├── file_systems.py         # (existing) EFS lifecycle/IA, FSx optimization
├── s3.py                   # (existing) Lifecycle, Intelligent-Tiering
├── dynamodb.py             # (existing) RCU/WCU capacity, billing mode
├── lambda_svc.py           # (existing) Memory sizing, ARM, provisioned concurrency
├── containers.py           # (existing) Fargate pricing, ECS/EKS/ECR
├── network.py              # (existing) EIP, NAT, VPC endpoints, LB, ASG
├── monitoring.py           # (existing) Log retention, stale alarms, CloudTrail
├── elasticache.py          # (existing) Graviton, reserved nodes
├── opensearch.py           # (existing) Reserved instances, Graviton
├── cloudfront.py           # (existing) Price class, disabled distributions
├── api_gateway.py          # (existing) REST→HTTP, caching
├── step_functions.py       # (existing) Standard→Express migration
├── lightsail.py            # (existing) Bundle pricing, stopped instances
├── redshift.py             # (existing) Reserved nodes, serverless
├── dms.py                  # (existing) Instance rightsizing, serverless
├── quicksight.py           # (existing) SPICE capacity pricing
├── apprunner.py            # (existing) vCPU + memory pricing
├── transfer.py             # (existing) Per-protocol pricing
├── msk.py                  # (existing) Cluster rightsizing, serverless
├── workspaces.py           # (existing) AlwaysOn→AutoStop, bundle pricing
├── mediastore.py           # (existing) S3-equivalent pricing
├── glue.py                 # (existing) DPU pricing, dev endpoints
├── athena.py               # (existing) Scan-volume pricing, workgroups
├── batch.py                # (existing) Spot strategy, Graviton
├── compute_optimizer.py    # P1: AWS Compute Optimizer integration
├── cost_optimization_hub.py # P1: Cost Optimization Hub aggregation
├── aurora.py               # P1: Aurora-specific checks
├── commitment_analysis.py  # P1: SP/RI coverage analysis
├── bedrock.py              # P2: AI/ML (Bedrock) optimization
├── sagemaker.py            # P2: SageMaker optimization
├── network_cost.py         # P2: Network & data transfer
├── cost_anomaly.py         # P2: Cost anomaly integration
├── eks.py                  # P2: Kubernetes/EKS visibility
└── tag_compliance.py       # P3: Tag governance
```

---

## Competitive Benchmarking

| Capability | Our Scanner | Kubecost | CloudHealth | Spot.io | Infracost |
|---|---|---|---|---|---|
| EC2 rightsizing | ✅ | ❌ | ✅ | ✅ | ❌ |
| EBS optimization | ✅ | ❌ | ✅ | ❌ | ❌ |
| RDS/Aurora | ✅/❌ | ❌ | ✅ | ❌ | ❌ |
| Lambda memory | ✅ | ❌ | ❌ | ❌ | ❌ |
| S3 lifecycle | ✅ | ❌ | ✅ | ❌ | ❌ |
| Savings Plans | ❌ | ❌ | ✅ | ✅ | ❌ |
| K8s cost | ❌ | ✅ | ❌ | ❌ | ❌ |
| IaC estimation | ❌ | ❌ | ❌ | ❌ | ✅ |
| AI/ML costs | ❌ | ❌ | ❌ | ❌ | ❌ |
| FOCUS export | ❌ | ❌ | ❌ | ❌ | ❌ |
| Network costs | ✅ | ❌ | ✅ | ❌ | ❌ |
| Forecasting | ❌ | ❌ | ✅ | ❌ | ✅ |

**Unique differentiators if roadmap is completed**:
1. **Only tool** with AI/ML cost optimization (Bedrock + SageMaker)
2. **Only tool** with FOCUS 1.2 export
3. **Only tool** with IaC cost estimation + runtime scanning combined
4. **Only tool** with Well-Architected Cost Pillar automated mapping

---

## Implementation Priority Matrix

```
                     HIGH IMPACT
                         │
    P1: Commitment      │    P1: Compute Optimizer
    Analysis (20)       │    Integration (25) ★
                         │    P1: Cost Optimization
    P1: Aurora (20)     │    Hub (24) ★
                         │
    P2: AI/ML (20)      │    P1: Savings Plans
                         │
    P3: Multi-Account   │    P2: Network (16)
    (16)                │
    P3: FOCUS (16)      │    P2: Anomaly (15)
    P3: Forecasting (15)│    P2: EKS (15)
                         │
    P4: Unit Cost (12)  │    P3: Tagging (12)
    P4: W-A Review (12) │    P3: IaC Estimation (15)
                         │    P4: Sustainability (10)
    LOW FEASIBILITY     │    P4: AI Explanations (10)
         ───────────────┼────────────── HIGH FEASIBILITY
                         │
    P4: Policy-as-Code  │
    (8)                 │
    P4: Streaming (8)   │
    P4: Marketplace (6) │
                         │
                     LOW IMPACT
```

---

## Recommended Implementation Order

### Sprint 1 (Weeks 1-2): Foundation Layer
1. Compute Optimizer Integration (P=25)
2. Cost Optimization Hub Aggregation (P=24)

### Sprint 2 (Weeks 3-4): High-Value Services
3. Savings Plans & RI Coverage (P=20)
4. Aurora-Specific Checks (P=20)

### Sprint 3 (Weeks 5-8): Service Expansion
5. AI/ML Optimization - Bedrock + SageMaker (P=20)
6. Network & Data Transfer (P=16)

### Sprint 4 (Weeks 9-12): Platform Maturity
7. EKS / Kubernetes Visibility (P=15)
8. Cost Anomaly Detection (P=15)

### Sprint 5 (Weeks 13-16): Strategic Features
9. FOCUS 1.2 Export (P=16)
10. Multi-Account Support (P=16)
11. Budget & Forecasting Engine (P=15)
12. Tagging Compliance (P=12)

### Sprint 6+ (Weeks 17+): Vision Items
13. IaC Cost Estimation (P=15)
14. Well-Architected Automated Review (P=12)
15. Unit Cost Economics (P=12)
16. AI-Powered Explanations (P=10)
17. Sustainability / Carbon (P=10)
18. Real-Time Streaming (P=8)
19. Policy-as-Code (P=8)
20. Marketplace Integrations (P=6)

---

## Success Metrics

| Metric | Current | Sprint 3 Target | Sprint 5 Target |
|--------|---------|-----------------|-----------------|
| AWS adapters | 28 | 30 | 34+ |
| Recommendation types | ~12 | 25+ | 35+ |
| % of typical AWS bill addressed | ~60% | ~80% | ~90% |
| FinOps FOCUS compliance | None | — | FOCUS 1.2 |
| Multi-account support | No | No | Yes |
| Forecasting capability | No | No | 12-month |
| AI/ML cost visibility | None | Full | Full |

---

*This roadmap is a living document. Re-evaluate priorities quarterly based on AWS service updates, customer feedback, and FinOps maturity.*
