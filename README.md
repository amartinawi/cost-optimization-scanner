# AWS Cost Optimization Scanner

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-3.0.0-blue)](https://github.com/amartinawi/cost-optimization-scanner)

A production-ready, read-only AWS cost optimization scanner that analyzes **37 AWS service categories** across **all AWS regions** with **260+ automated checks**. Outputs JSON scan results and interactive HTML reports with per-service tabs, executive summary charts, and dark mode support.

Built for FinOps teams, DevOps engineers, and cloud architects who need actionable cost savings recommendations backed by real CloudWatch metrics — not guesswork.

## Features

- **37 ServiceModule adapters** performing 260+ optimization checks across 37 AWS service categories
- **CloudWatch metric-backed analysis** — 14-day CPU/memory/network utilization prevents false positives
- **Interactive HTML reports** — per-service tabs, executive dashboard with charts, dark mode
- **Service filtering** via `--scan-only` / `--skip-service` with alias resolution (50–80% faster targeted scans)
- **Live AWS Pricing API** — real-time pricing via `core/pricing_engine.py` with in-memory cache, 12 pricing methods across 19 adapters; regional multipliers as fallback for 35 regions
- **Historical spend trend analysis** — Cost Explorer-powered 30/90/180-day trend tracking with anomaly detection and forecasting
- **Enterprise scale** — handles 1000+ resources per service with pagination and throttling retries
- **Zero write operations** — read-only IAM policy, no modifications to your AWS resources

## Architecture Overview

```
cost-optimization-scanner/
  cli.py                          CLI entry point (argparse)
  cost_optimizer.py               Thin orchestration shell (134 lines)
  core/
    contracts.py                  ServiceModule Protocol + dataclasses
    scan_orchestrator.py          Iterates ALL_MODULES via safe_scan
    scan_context.py               Region, clients, pricing, warnings
    result_builder.py             ServiceFindings → JSON dict
    client_registry.py            Caching boto3 client factory
    session.py                    AWS session with adaptive retry
    pricing_engine.py             AWS Pricing API client with in-memory cache (517 lines)
    filtering.py                  --scan-only / --skip-service resolver
    trend_analysis.py             Historical Cost Explorer trend analysis (30/90/180-day)
  services/
    __init__.py                   ALL_MODULES registry (37 adapter instances)
    _base.py                      BaseServiceModule default implementations
    _savings.py                   parse_dollar_savings helper
    advisor.py                    Cost Hub + Compute Optimizer utilities
    adapters/                     37 ServiceModule adapter files (one per service)
  html_report_generator.py        HTML report generation (2,432 lines)
  reporter_phase_a.py             Descriptor-driven grouped service rendering
  reporter_phase_b.py             Function registry for source-specific handlers
  tests/                          pytest suite (5 test files + fixtures + stubs)
```

The `ServiceModule` Protocol in `core/contracts.py` defines the adapter interface. Each of the 37 adapters in `services/adapters/` implements `scan(ctx) → ServiceFindings`. The `ALL_MODULES` list in `services/__init__.py` is the single registry — adding a new adapter means appending one instance. The `core/trend_analysis.py` module enriches scans with historical Cost Explorer trends and anomaly data as a cross-cutting concern.

## Requirements

- Python 3.8+
- **Runtime**: `boto3>=1.34.0`, `botocore>=1.34.0`, `python-dateutil>=2.8.2`
- **Testing** (optional): `pytest`, `moto`, `freezegun`
- AWS credentials with read-only IAM policy (see below)

## Installation

```bash
git clone https://github.com/amartinawi/cost-optimization-scanner.git
cd cost-optimization-scanner
pip install -r requirements.txt
```

## Usage

### Basic Scan

```bash
# Full scan — all 37 services
python3 cli.py us-east-1

# With AWS profile
python3 cli.py us-east-1 --profile production

# Fast mode — skip CloudWatch metrics for faster S3 analysis
python3 cli.py us-east-1 --fast
```

### Options

| Flag | Description |
|------|-------------|
| `region` | AWS region to scan (required positional argument) |
| `--profile` | AWS CLI profile name |
| `--output` | Custom path for HTML report |
| `--fast` | Skip CloudWatch metrics for faster scans |
| `--scan-only SERVICE` | Scan only specified services (repeatable) |
| `--skip-service SERVICE` | Skip specified services (repeatable) |

### Service Filtering

```bash
# Scan only S3 and Lambda
python3 cli.py us-east-1 --scan-only s3 --scan-only lambda

# Skip compute services for storage-focused scan
python3 cli.py us-east-1 --skip-service ec2 --skip-service rds

# Filter by alias (both work)
python3 cli.py us-east-1 --scan-only efs
python3 cli.py us-east-1 --scan-only file_systems
```

### Output

- **JSON**: `cost_optimization_scan_<region>_<timestamp>.json`
- **HTML**: `<account>_<region>.html` (or path from `--output`)

## Supported AWS Services

| Service | CLI Alias | Adapter | Key Optimizations |
|---------|-----------|---------|-------------------|
| EC2 | `ec2`, `instances` | `ec2.py` | Idle instances (CloudWatch), rightsizing, previous-gen (t2→t3), burstable credits, stopped instances |
| EBS | `ebs`, `volumes` | `ebs.py` | Unattached volumes, gp2→gp3, old snapshots, Compute Optimizer |
| RDS | `rds` | `rds.py` | Multi-AZ in non-prod, backup retention, gp2→gp3, Compute Optimizer |
| S3 | `s3` | `s3.py` | Lifecycle policies, Intelligent-Tiering, multipart uploads, empty buckets |
| Lambda | `lambda` | `lambda_svc.py` | Low invocations, memory sizing, ARM migration (metric-gated), provisioned concurrency |
| DynamoDB | `dynamodb` | `dynamodb.py` | RCU/WCU capacity pricing, billing mode review, empty tables |
| Containers | `containers`, `ecs`, `eks` | `containers.py` | Fargate vCPU/memory pricing, ECS unused clusters, EKS node groups, ECR lifecycle policies |
| Network | `network`, `eip`, `nat` | `network.py` | Unused EIPs, NAT optimization, VPC endpoints, load balancer consolidation |
| File Systems | `file_systems`, `efs`, `fsx` | `file_systems.py` | EFS lifecycle/Archive, One Zone migration, FSx optimization |
| ElastiCache | `elasticache` | `elasticache.py` | Graviton migration, reserved nodes, engine version review |
| OpenSearch | `opensearch` | `opensearch.py` | Reserved instances, Graviton migration, gp2→gp3 |
| CloudWatch | `monitoring`, `cloudwatch` | `monitoring.py` | Log retention, stale alarms, CloudTrail data events |
| CloudFront | `cloudfront` | `cloudfront.py` | Price class (traffic-gated), disabled distributions |
| API Gateway | `api_gateway` | `api_gateway.py` | REST→HTTP migration, caching opportunities |
| Step Functions | `step_functions` | `step_functions.py` | CloudWatch execution pricing, Standard→Express migration |
| AMI | `ami`, `amis` | `ami.py` | Unused AMIs, old snapshot cleanup |
| Lightsail | `lightsail` | `lightsail.py` | Live bundle pricing, stopped instances, unused static IPs |
| Redshift | `redshift` | `redshift.py` | Reserved nodes, rightsizing, serverless |
| DMS | `dms` | `dms.py` | Instance rightsizing, serverless review |
| QuickSight | `quicksight` | `quicksight.py` | SPICE capacity pricing (tier-aware) |
| App Runner | `apprunner` | `apprunner.py` | vCPU + memory hourly pricing |
| Transfer Family | `transfer` | `transfer.py` | Per-protocol hourly pricing |
| MSK | `msk`, `kafka` | `msk.py` | Cluster rightsizing, serverless review |
| WorkSpaces | `workspaces` | `workspaces.py` | AlwaysOn→AutoStop with live bundle pricing |
| MediaStore | `mediastore` | `mediastore.py` | S3-equivalent storage pricing |
| Glue | `glue` | `glue.py` | DPU-based pricing ($0.44/DPU/hour), dev endpoints, crawler schedules |
| Athena | `athena` | `athena.py` | CloudWatch scan-volume pricing, workgroup scan limits |
| Batch | `batch` | `batch.py` | Spot allocation strategy, Graviton instances |
| Aurora | `aurora` | `aurora.py` | Cluster rightsizing, Global Clusters, snapshot retention, Graviton migration |
| Commitment Analysis | `commitment_analysis`, `commitments`, `savings_plans`, `ri` | `commitment_analysis.py` | Savings Plans utilization/coverage, RI utilization/coverage, purchase recommendations |
| Compute Optimizer | `compute_optimizer`, `co` | `compute_optimizer.py` | EC2, EBS, and RDS rightsizing recommendations from AWS Compute Optimizer |
| Cost Optimization Hub | `cost_optimization_hub`, `cost_hub`, `hub` | `cost_optimization_hub.py` | Aggregated cost recommendations from AWS Cost Optimization Hub |
| Bedrock | `bedrock` | `bedrock.py` | Provisioned throughput idle analysis, knowledge base optimization, agent utilization |
| SageMaker | `sagemaker` | `sagemaker.py` | Idle endpoints/notebooks, spot training adoption, multi-model consolidation |
| Network Cost | `network_cost` | `network_cost.py` | Cross-region/AZ transfer analysis, internet egress, TGW vs peering |
| Cost Anomaly Detection | `cost_anomaly`, `anomaly` | `cost_anomaly.py` | Anomaly monitors, spend pattern analysis, billing alarm gap detection |
| EKS Cost Visibility | `eks_cost`, `eks_visibility` | `eks_cost.py` | Control plane costs, node group optimization, Fargate vs EC2 comparison, Container Insights coverage |

## Adding a New Service Adapter

1. **Create** `services/adapters/<service>.py` implementing the `ServiceModule` Protocol from `core/contracts.py`:

```python
from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule

class NewServiceModule(BaseServiceModule):
    key: str = "newservice"
    cli_aliases: tuple[str, ...] = ("newservice",)
    display_name: str = "NewService"

    def required_clients(self) -> tuple[str, ...]:
        return ("newservice",)

    def scan(self, ctx) -> ServiceFindings:
        # Call analysis functions, build SourceBlock entries
        ...
```

2. **Extend** `BaseServiceModule` from `services/_base.py` for shared utilities.
3. **Define** `ALIASES` class attribute for `--scan-only` resolution.
4. **Register** the instance in `services/__init__.py` `ALL_MODULES` list.
5. **Test**: `pytest tests/ -v` — verify no regressions.

No changes to `core/` are required. The adapter is automatically available via `--scan-only newservice` and included in full scans.

## Running Tests

```bash
# Full test suite (offline — no AWS credentials needed)
pytest tests/ -v

# Golden-file regression gate
pytest tests/test_regression_snapshot.py -v

# HTML reporter regression gate
pytest tests/test_reporter_snapshots.py -v
```

All tests run offline using `moto` mock AWS and pre-built stub responses in `tests/aws_stubs/`.

## IAM Permissions

The scanner requires read-only access only. See the full IAM policy in the collapsible section below.

<details>
<summary>Complete IAM policy (133 read-only actions)</summary>

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "apigateway:GET",
                "apigatewayv2:GetApis",
                "apprunner:DescribeService",
                "apprunner:ListServices",
                "athena:GetWorkGroup",
                "athena:ListWorkGroups",
                "autoscaling:DescribeAutoScalingGroups",
                "autoscaling:DescribePolicies",
                "backup:GetBackupPlan",
                "backup:GetBackupSelection",
                "backup:ListBackupPlans",
                "backup:ListBackupSelections",
                "batch:DescribeComputeEnvironments",
                "bedrock:GetProvisionedModelThroughput",
                "bedrock:ListProvisionedModelThroughputs",
                "bedrock-agent:ListAgents",
                "bedrock-agent:ListKnowledgeBases",
                "ce:GetAnomalies",
                "ce:GetAnomalyMonitors",
                "ce:GetAnomalySubscriptions",
                "ce:GetCostAndUsage",
                "ce:GetCostForecast",
                "ce:GetReservationPurchaseRecommendation",
                "ce:GetReservationUtilization",
                "ce:GetSavingsPlansCoverage",
                "ce:GetSavingsPlansPurchaseRecommendation",
                "ce:GetSavingsPlansUtilization",
                "ce:GetSavingsPlansUtilizationDetails",
                "cloudfront:GetDistributionConfig",
                "cloudfront:ListDistributions",
                "cloudtrail:DescribeTrails",
                "cloudtrail:GetEventSelectors",
                "cloudtrail:GetInsightSelectors",
                "cloudwatch:DescribeAlarms",
                "cloudwatch:GetMetricData",
                "cloudwatch:GetMetricStatistics",
                "cloudwatch:ListMetrics",
                "compute-optimizer:GetAutoScalingGroupRecommendations",
                "compute-optimizer:GetEBSVolumeRecommendations",
                "compute-optimizer:GetEC2InstanceRecommendations",
                "compute-optimizer:GetECSServiceRecommendations",
                "compute-optimizer:GetLambdaFunctionRecommendations",
                "compute-optimizer:GetRDSDatabaseRecommendations",
                "cost-optimization-hub:GetRecommendation",
                "cost-optimization-hub:ListRecommendations",
                "dms:DescribeReplicationConfigs",
                "dms:DescribeReplicationInstances",
                "dynamodb:DescribeTable",
                "dynamodb:ListTables",
                "ec2:DescribeAddresses",
                "ec2:DescribeImages",
                "ec2:DescribeInstances",
                "ec2:DescribeNatGateways",
                "ec2:DescribeSnapshots",
                "ec2:DescribeSubnets",
                "ec2:DescribeVolumes",
                "ec2:DescribeVpcEndpoints",
                "ec2:DescribeVpcs",
                "ecr:DescribeRepositories",
                "ecr:ListImages",
                "ecs:DescribeClusters",
                "ecs:DescribeServices",
                "ecs:ListClusters",
                "ecs:ListServices",
                "efs:DescribeFileSystems",
                "efs:DescribeLifecycleConfiguration",
                "eks:DescribeAddon",
                "eks:DescribeCluster",
                "eks:DescribeNodegroup",
                "eks:ListAddons",
                "eks:ListClusters",
                "eks:ListFargateProfiles",
                "eks:ListNodegroups",
                "elasticache:DescribeCacheClusters",
                "elasticache:DescribeReplicationGroups",
                "elasticloadbalancing:DescribeLoadBalancers",
                "elasticloadbalancingv2:DescribeListeners",
                "elasticloadbalancingv2:DescribeLoadBalancers",
                "elasticloadbalancingv2:DescribeRules",
                "elasticloadbalancingv2:DescribeTags",
                "elasticloadbalancingv2:DescribeTargetGroups",
                "fsx:DescribeFileCaches",
                "fsx:DescribeFileSystems",
                "glue:GetCrawlers",
                "glue:GetDevEndpoints",
                "glue:GetJobs",
                "kafka:ListClusters",
                "kafka:ListClustersV2",
                "lambda:GetFunctionConfiguration",
                "lambda:ListFunctions",
                "lambda:ListProvisionedConcurrencyConfigs",
                "lightsail:GetInstances",
                "lightsail:GetStaticIps",
                "logs:DescribeLogGroups",
                "mediastore:ListContainers",
                "opensearch:DescribeDomain",
                "opensearch:ListDomainNames",
                "quicksight:DescribeAccountSubscription",
                "quicksight:DescribeSpiceCapacity",
                "quicksight:ListNamespaces",
                "quicksight:ListUsers",
                "rds:DescribeDBClusterSnapshots",
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
                "rds:DescribeDBSnapshots",
                "rds:DescribeGlobalClusters",
                "redshift-serverless:ListWorkgroups",
                "redshift:DescribeClusters",
                "route53:ListHealthChecks",
                "route53:ListHostedZones",
                "route53:ListResourceRecordSets",
                "s3:GetBucketLifecycleConfiguration",
                "s3:GetBucketLocation",
                "s3:GetBucketLogging",
                "s3:GetBucketReplication",
                "s3:GetBucketVersioning",
                "s3:GetBucketWebsite",
                "s3:ListAllMyBuckets",
                "s3:ListBucket",
                "s3:ListBucketIntelligentTieringConfigurations",
                "s3:ListBucketMultipartUploads",
                "s3:ListBucketVersions",
                "s3:ListMultipartUploads",
                "sagemaker:DescribeEndpointConfig",
                "sagemaker:DescribeNotebookInstance",
                "sagemaker:DescribeTrainingJob",
                "sagemaker:ListEndpoints",
                "sagemaker:ListNotebookInstances",
                "sagemaker:ListTrainingJobs",
                "states:ListStateMachines",
                "sts:GetCallerIdentity",
                "transfer:ListServers",
                "workspaces:DescribeWorkspaces"
            ],
            "Resource": "*"
        }
    ]
}
```

</details>

## Cost Estimation Model

Savings estimates use live AWS Pricing API data via `core/pricing_engine.py`, which queries real-time pricing from the AWS Price List Service. The engine caches results with a configurable TTL and provides 12 methods covering EC2, EBS, RDS, S3, and generic instance/storage lookups. Regional pricing multipliers (`pricing_multiplier` in `ScanContext`) adjust fallback estimates for regions with sparse pricing data. For precise projections beyond the scanner's scope, use [AWS Pricing Calculator](https://calculator.aws/) or [AWS Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/).

## Known Limitations

- Cost Optimization Hub checks require Business/Enterprise AWS Support plan
- CloudWatch metrics may be sparse for newly created resources (14-day analysis window)
- Legacy shim files in `services/*.py` contain analysis logic — active adapters live in `services/adapters/` only
- `--fast` mode skips CloudWatch queries and live pricing API calls, falling back to flat-rate estimates — produces less accurate recommendations

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — Technical architecture, data flow, and design decisions
- [CHANGELOG.md](CHANGELOG.md) — Version history
- [CONTRIBUTING.md](CONTRIBUTING.md) — Contributor guidelines

## License

MIT License — see [LICENSE](LICENSE) for details.
