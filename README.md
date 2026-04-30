# AWS Cost Optimization Scanner 💰

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-3.0.0-blue)](https://github.com/aws-cost-optimizer/aws-cost-optimizer)
[![AWS Services](https://img.shields.io/badge/AWS%20Services-30-orange)](https://aws.amazon.com/)
[![Cost Checks](https://img.shields.io/badge/Cost%20Checks-220+-red)](https://github.com/aws-cost-optimizer/aws-cost-optimizer)
[![Global Regions](https://img.shields.io/badge/AWS%20Regions-Global-green)](https://aws.amazon.com/about-aws/global-infrastructure/)
[![Service Filtering](https://img.shields.io/badge/Service%20Filtering-30%20Services-purple)](https://github.com/aws-cost-optimizer/aws-cost-optimizer)

A comprehensive, production-ready AWS cost optimization tool that analyzes **30 AWS services** with **220+ cost optimization checks** across all AWS regions. Features intelligent service filtering, CloudWatch-backed recommendations, and professional HTML reports.

## 🎯 Key Benefits

- 💡 **Identify potential savings** across 30 AWS services with 220+ automated checks
- 🔍 **CloudWatch-backed analysis** with real metrics to prevent false positives
- 📊 **Professional HTML reports** with interactive multi-tab interface, executive summary, and dark mode
- 📈 **Executive dashboard** with interactive charts for cost savings visualization
- 🌙 **Dark mode support** with toggle button and theme persistence
- 🏢 **Enterprise-scale support** for accounts with 1000+ resources per service
- 🌍 **Global region support** with regional pricing multipliers
- ⚡ **Smart retry logic** handles API throttling gracefully with exponential backoff
- 🎯 **Service filtering** for faster, targeted scans (30 service categories)
- ⚡ **Fast mode** for large S3 environments (100+ buckets)
- 📊 **Comprehensive error tracking** with warnings and permission issues

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- AWS CLI configured with appropriate permissions
- boto3 library

### Installation

```bash
# Clone the repository
git clone https://github.com/aws-cost-optimizer/aws-cost-optimizer.git
cd aws-cost-optimizer

# Install dependencies
pip install -r requirements.txt
```

### Basic Usage

```bash
# Run comprehensive scan (all 30 services)
python3 cost_optimizer.py us-east-1

# With specific AWS profile
python3 cost_optimizer.py us-east-1 --profile production

# Fast mode for large S3 environments (100+ buckets)
python3 cost_optimizer.py us-east-1 --fast
```

### 🎯 Service Filtering

Target specific services for faster, focused scans:

```bash
# Scan only S3 and Lambda for serverless optimization
python3 cost_optimizer.py us-east-1 --scan-only s3 --scan-only lambda

# Skip compute services for storage-focused scan
python3 cost_optimizer.py us-east-1 --skip-service ec2 --skip-service rds

# Fast S3-only storage optimization scan
python3 cost_optimizer.py us-east-1 --fast --scan-only s3
```

## 📊 Services Analyzed (30 Services)

| Service | Key Optimizations |
|---------|-------------------|
| **EC2** | Idle instances (CloudWatch), rightsizing, previous gen (t2→t3), burstable credits, Auto Scaling gaps, stopped instances |
| **EBS** | Unattached volumes, gp2→gp3, old snapshots, underutilized volume settings |
| **RDS** | Multi‑AZ in non‑prod, backup retention, gp2→gp3 storage, non‑prod scheduling, stopped DBs |
| **S3** | Lifecycle policies, Intelligent‑Tiering, multipart uploads, versioning cleanup, replication review, empty buckets |
| **Lambda** | Low invocations, memory sizing, provisioned concurrency review, VPC configuration costs, ARM migration (metric‑gated) |
| **DynamoDB** | Capacity rightsizing with CloudWatch, billing mode review, reserved capacity suggestions, empty tables |
| **Containers** | ECS unused clusters, over‑provisioned services, Container Insights enablement, ECR lifecycle policies |
| **Network** | Unused EIPs, NAT optimization, missing VPC endpoints, load balancer consolidation |
| **File Systems** | EFS lifecycle/Archive (regional), One Zone migration, idle file systems, FSx optimizations |
| **ElastiCache** | Graviton migration, reserved nodes opportunity, underutilized clusters, engine version review, Valkey evaluation |
| **OpenSearch** | Reserved instances (multi‑node), Graviton migration, gp2→gp3 storage, idle/underutilized domains |
| **CloudWatch** | Log retention, excessive log storage, custom metric volume, stale alarms |
| **CloudFront** | Price class optimization (traffic‑gated), disabled distributions, origin shield review |
| **API Gateway** | REST→HTTP for simple APIs, caching opportunities |
| **Step Functions** | Standard→Express for high‑volume workflows, non‑prod scheduling |
| **Auto Scaling** | Static ASGs, non‑prod 24/7, oversized instances |
| **CloudTrail** | Multi‑region trails, data events (all buckets/functions), Insights review |
| **AWS Backup** | Retention optimization, cross‑region copy review, backup schedule frequency |
| **Route53** | Hosted zones and health check review |
| **Lightsail** | Stopped instances, oversized bundles, unused static IPs |
| **Redshift** | Reserved nodes (age‑gated), rightsizing by node count, serverless reservations |
| **DMS** | Instance rightsizing (CPU), serverless configuration review |
| **QuickSight** | SPICE capacity utilization review |
| **App Runner** | Instance configuration review (heuristic) |
| **Transfer Family** | Protocol optimization |
| **MSK** | Cluster rightsizing (heuristic), serverless configuration review |
| **WorkSpaces** | AlwaysOn → AutoStop recommendation (usage review required) |
| **MediaStore** | Unused containers (metric‑backed) |
| **Glue** | Job DPU sizing, dev endpoints, crawler schedule review |
| **Athena** | Workgroup scan limits, query results lifecycle |
| **Batch** | Spot allocation strategy, Graviton instance consideration |

## 🎯 Available Services for Filtering (30 Total)

| Service | Description | Key Optimizations |
|---------|-------------|-------------------|
| `ec2` | EC2 instances, AMIs, compute optimization | Idle/rightsizing, previous gen (t2→t3), burstable credits |
| `ebs` | EBS volumes, snapshots, storage optimization | Unattached volumes, gp2→gp3, old snapshots |
| `rds` | RDS databases and managed database optimization | Multi‑AZ in non‑prod, backup retention, gp2→gp3 |
| `s3` | S3 buckets, lifecycle policies, storage classes | Lifecycle, Intelligent‑Tiering, multipart uploads |
| `lambda` | Lambda functions, memory, runtime optimization | Low usage, memory sizing, ARM migration (metric‑gated) |
| `dynamodb` | DynamoDB tables and capacity optimization | CloudWatch‑backed capacity review |
| `efs` | EFS file systems and storage class optimization | Lifecycle/Archive (regional), One Zone migration |
| `elasticache` | ElastiCache clusters and engine optimization | Graviton migration, reserved nodes, version review |
| `opensearch` | OpenSearch domains and instance optimization | Reserved instances, gp2→gp3, utilization review |
| `containers` | ECS/EKS clusters and ECR repositories | ECS usage checks, Container Insights, ECR lifecycle |
| `network` | EIPs, NAT Gateways, Load Balancers | Unused resources, NAT/VPC endpoint review |
| `monitoring` | CloudWatch logs, metrics, and CloudTrail | Log retention, custom metrics, data events |
| `backup` | AWS Backup jobs and retention policies | Retention optimization, schedule review |
| `cloudfront` | CloudFront distributions and caching | Price class (traffic‑gated), disabled dists |
| `api_gateway` | API Gateway REST/HTTP APIs | REST→HTTP for simple APIs, caching |
| `step_functions` | Step Functions workflows | Standard→Express for high‑volume workflows |
| `auto_scaling` | Auto Scaling groups | Static ASGs, non‑prod schedules |
| `route53` | Route 53 DNS and health checks | Hosted zone/health check review |
| `lightsail` | Lightsail instances and resources | Stopped instances, unused static IPs |
| `redshift` | Redshift clusters and serverless | Reserved nodes, rightsizing, serverless reservations |
| `dms` | Database Migration Service | Instance rightsizing, serverless review |
| `quicksight` | QuickSight BI service | SPICE capacity utilization |
| `apprunner` | App Runner container service | Instance configuration review (heuristic) |
| `transfer` | Transfer Family (SFTP/FTPS) | Protocol optimization |
| `msk` | Managed Streaming for Apache Kafka | Cluster rightsizing (heuristic), serverless review |
| `workspaces` | WorkSpaces virtual desktops | AlwaysOn → AutoStop recommendation |
| `mediastore` | Elemental MediaStore | Unused containers (metric‑backed) |
| `glue` | AWS Glue ETL jobs | Job DPU sizing, dev endpoints, crawler review |
| `athena` | Athena query service | Workgroup scan limits, results lifecycle |
| `batch` | AWS Batch compute | Spot allocation strategy, Graviton consideration |

### Service Filtering Benefits
- ⚡ **Faster scans** when targeting specific services (50-80% time reduction)
- 🎯 **Focused analysis** reduces noise and improves actionability
- 📊 **Cleaner reports** with only relevant recommendations
- 🔧 **Workflow optimization** for different team responsibilities

## 📈 Sample Output

```
🚀 AWS Cost Optimization Scanner v3.0.0 - Modular Architecture
📍 Scanning region: us-east-1
👤 AWS profile: production
🎯 Scanning only: s3, lambda

📊 TARGETED SCAN RESULTS
┌─────────────────────┬─────────────┬──────────────────┬─────────────────────┐
│ Service             │ Resources   │ Recommendations  │ Est. Monthly Savings│
├─────────────────────┼─────────────┼──────────────────┼─────────────────────┤
│ S3                  │ 89          │ 234              │ $2,100.00          │
│ Lambda              │ 67          │ 89               │ $450.00            │
└─────────────────────┴─────────────┴──────────────────┴─────────────────────┘

💰 Total Potential Monthly Savings: $2,550.00
⚡ Scan completed with service filtering
📄 Detailed HTML report: cost_optimization_report.html
```

## 🌍 Global Region Support

Regional pricing is applied with per‑region multipliers where defined. Regions without explicit multipliers use a conservative default. For precise pricing, use AWS Pricing Calculator or Cost Explorer.

**Cost Estimation Model:**
- Uses region-specific pricing multipliers and heuristic calculations
- Savings estimates based on AWS published pricing and industry benchmarks
- **Note**: Estimates are approximate; actual savings may vary based on usage patterns, Reserved Instance coverage, and Savings Plans
- For precise cost projections, use AWS Pricing Calculator or AWS Cost Explorer

## 🔐 IAM Permissions

<details>
<summary>Click to expand complete IAM policy for 30 services + optimization services + STS (103 permissions)</summary>

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "cost-optimization-hub:ListRecommendations",
                "cost-optimization-hub:GetRecommendation",
                "compute-optimizer:GetEC2InstanceRecommendations",
                "compute-optimizer:GetEBSVolumeRecommendations",
                "compute-optimizer:GetRDSDatabaseRecommendations",
                "ec2:DescribeInstances",
                "ec2:DescribeVolumes",
                "ec2:DescribeSnapshots",
                "ec2:DescribeImages",
                "ec2:DescribeAddresses",
                "ec2:DescribeNatGateways",
                "ec2:DescribeVpcEndpoints",
                "ec2:DescribeVpcs",
                "ec2:DescribeSubnets",
                "rds:DescribeDBInstances",
                "rds:DescribeDBSnapshots",
                "s3:ListAllMyBuckets",
                "s3:GetBucketLocation",
                "s3:GetBucketLifecycleConfiguration",
                "s3:GetBucketWebsite",
                "s3:GetBucketVersioning",
                "s3:GetBucketReplication",
                "s3:GetBucketLogging",
                "s3:ListBucket",
                "s3:ListBucketVersions",
                "s3:ListBucketMultipartUploads",
                "s3:ListMultipartUploads",
                "s3:ListBucketIntelligentTieringConfigurations",
                "dynamodb:ListTables",
                "dynamodb:DescribeTable",
                "lambda:ListFunctions",
                "lambda:GetFunctionConfiguration",
                "lambda:ListProvisionedConcurrencyConfigs",
                "efs:DescribeFileSystems",
                "efs:DescribeLifecycleConfiguration",
                "fsx:DescribeFileSystems",
                "fsx:DescribeFileCaches",
                "elasticache:DescribeCacheClusters",
                "elasticache:DescribeReplicationGroups",
                "opensearch:ListDomainNames",
                "opensearch:DescribeDomain",
                "ecs:ListClusters",
                "ecs:DescribeClusters",
                "ecs:ListServices",
                "ecs:DescribeServices",
                "eks:ListClusters",
                "eks:DescribeCluster",
                "eks:ListNodegroups",
                "eks:DescribeNodegroup",
                "eks:ListAddons",
                "ecr:DescribeRepositories",
                "ecr:ListImages",
                "elasticloadbalancing:DescribeLoadBalancers",
                "elasticloadbalancingv2:DescribeLoadBalancers",
                "elasticloadbalancingv2:DescribeListeners",
                "elasticloadbalancingv2:DescribeRules",
                "elasticloadbalancingv2:DescribeTags",
                "elasticloadbalancingv2:DescribeTargetGroups",
                "autoscaling:DescribeAutoScalingGroups",
                "autoscaling:DescribePolicies",
                "cloudwatch:GetMetricStatistics",
                "cloudwatch:ListMetrics",
                "cloudwatch:DescribeAlarms",
                "logs:DescribeLogGroups",
                "cloudtrail:DescribeTrails",
                "cloudtrail:GetEventSelectors",
                "cloudtrail:GetInsightSelectors",
                "backup:ListBackupPlans",
                "backup:GetBackupPlan",
                "backup:ListBackupSelections",
                "backup:GetBackupSelection",
                "route53:ListHostedZones",
                "route53:ListResourceRecordSets",
                "route53:ListHealthChecks",
                "cloudfront:ListDistributions",
                "cloudfront:GetDistributionConfig",
                "apigateway:GET",
                "apigatewayv2:GetApis",
                "states:ListStateMachines",
                "lightsail:GetInstances",
                "lightsail:GetStaticIps",
                "redshift:DescribeClusters",
                "redshift-serverless:ListWorkgroups",
                "dms:DescribeReplicationInstances",
                "dms:DescribeReplicationConfigs",
                "quicksight:DescribeAccountSubscription",
                "quicksight:ListNamespaces",
                "quicksight:ListUsers",
                "quicksight:DescribeSpiceCapacity",
                "apprunner:ListServices",
                "apprunner:DescribeService",
                "transfer:ListServers",
                "kafka:ListClusters",
                "kafka:ListClustersV2",
                "workspaces:DescribeWorkspaces",
                "mediastore:ListContainers",
                "glue:GetJobs",
                "glue:GetDevEndpoints",
                "glue:GetCrawlers",
                "athena:ListWorkGroups",
                "athena:GetWorkGroup",
                "batch:DescribeComputeEnvironments",
                "sts:GetCallerIdentity"
            ],
            "Resource": "*"
        }
    ]
}
```

**Note**: This policy provides read-only access for cost analysis. No write permissions are required or granted.

</details>

## 🏗️ Architecture

The scanner uses a modular architecture with a thin orchestrator shell and 28 independent ServiceModule adapters:

```
CLI (cli.py)
  └─ CostOptimizer (cost_optimizer.py) — thin shell
       ├─ AwsSessionFactory (core/session.py)
       ├─ ClientRegistry (core/client_registry.py)
       └─ ScanOrchestrator (core/scan_orchestrator.py)
            └─ 28 ServiceModule adapters (services/adapters/*.py)
                 └─ ScanResultBuilder (core/result_builder.py) → JSON/HTML
```

### Key Design Principles
- **ServiceModule Protocol**: Each service implements a standard interface (`scan(context) → ServiceFindings`)
- **Thin shell**: `cost_optimizer.py` is ~130 lines — AWS session init + delegates to orchestrator
- **Adapter pattern**: 28 self-contained adapters, one file per service
- **Caching client registry**: boto3 clients are shared and reused across adapters
- **Safe scan wrapper**: Each adapter runs in error-isolated context

### Project Structure

| File | Lines | Purpose |
|------|-------|---------|
| `cost_optimizer.py` | 130 | Thin CostOptimizer shell |
| `cli.py` | 91 | Unified CLI entry point |
| `core/contracts.py` | ~100 | ServiceModule Protocol, ServiceFindings, SourceBlock |
| `core/scan_orchestrator.py` | 67 | ScanOrchestrator + safe_scan |
| `core/scan_context.py` | 50 | ScanContext (region, clients, warnings) |
| `core/result_builder.py` | 59 | ScanResultBuilder (JSON serialization) |
| `core/client_registry.py` | 54 | Caching boto3 client registry |
| `core/session.py` | 42 | AWS session factory |
| `core/filtering.py` | 58 | CLI service-key resolver |
| `services/__init__.py` | ~60 | ALL_MODULES registry (28 adapters) |
| `services/_base.py` | ~80 | BaseServiceModule base class |
| `services/_savings.py` | ~40 | parse_dollar_savings helper |
| `services/advisor.py` | ~200 | Cost Hub + Compute Optimizer utilities |
| `services/adapters/*.py` | 28 files | ServiceModule adapters |
| `html_report_generator.py` | 2,432 | HTML report generation |
| `reporter_phase_a.py` | 424 | Phase A descriptor-driven grouped services |
| `reporter_phase_b.py` | 1,502 | Phase B function registry for source handlers |

### Adapter Categories (28 adapters)

**Flat-rate** (12): lightsail, redshift, dms, quicksight, apprunner, transfer, msk, workspaces, mediastore, glue, athena, batch

**Parse-rate** (6): cloudfront, api_gateway, step_functions, elasticache, opensearch, ami

**Complex** (6): ec2, ebs, rds, s3, lambda, dynamodb

**Composite** (4): file_systems, containers, network, monitoring

## 📚 Documentation

- **[Architecture](ARCHITECTURE.md)** - Technical architecture and implementation details
- **[Technical Documentation](TECHNICAL_DOCUMENTATION.md)** - AWS Well-Architected aligned technical details
- **[AWS Services Reference](AWS_SERVICES_REFERENCE.md)** - Comprehensive AWS services cost optimization guide
- **[Contributing](CONTRIBUTING.md)** - Guidelines for contributors
- **[Changelog](CHANGELOG.md)** - Version history and updates
- **[Claude Skills](CLAUDE_SKILLS.md)** - AI agent skills for project enhancement

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details on:

- Adding new service support
- Implementing additional checks
- Improving documentation
- Reporting issues and bugs

### Development Setup
```bash
# Clone and setup
git clone https://github.com/aws-cost-optimizer/aws-cost-optimizer.git
cd aws-cost-optimizer

# Install dependencies
pip install -r requirements.txt

# Run with your AWS credentials
python3 cost_optimizer.py us-east-1 --profile your-profile
```

### Adding a New Service Adapter

1. Create `services/adapters/your_service.py` implementing the `ServiceModule` protocol
2. Register it in `services/__init__.py` via `ALL_MODULES`
3. The adapter receives a `ScanContext` and returns `ServiceFindings`
4. See `services/_base.py` for the base class and existing adapters for patterns

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

- 📖 **Documentation**: Check our comprehensive guides
- 🐛 **Issues**: Report bugs via [GitHub Issues](https://github.com/aws-cost-optimizer/aws-cost-optimizer/issues)
- 💬 **Discussions**: Join our [GitHub Discussions](https://github.com/aws-cost-optimizer/aws-cost-optimizer/discussions)

## ⭐ Star History

If you find this tool helpful, please consider giving it a star! ⭐

---

**Made with ❤️ for the AWS community**
