# Mock Coverage — AWS Surface for Offline Tests (T-003)

## Strategy

- **Primary**: `moto.mock_aws()` (v5.1.x) intercepts all boto3 calls inside context
- **Fallback**: `botocore.stub.Stubber` for services moto doesn't implement

## Service Coverage Matrix

### moto-covered services (used with data in golden fixture)

| Service | moto Decorator Equivalent | API Calls Used by CostOptimizer | Notes |
|---------|--------------------------|--------------------------------|-------|
| **EC2** | `mock_ec2` | `DescribeInstances`, `DescribeImages`, `DescribeVolumes`, `DescribeSnapshots`, `DescribeAddresses`, `DescribeNatGateways`, `DescribeVpcEndpoints`, `DescribeVpcs`, `DescribeSubnets` | Primary service — 5 recommendations in golden |
| **AMI** | (uses EC2 client) | `DescribeImages` | AMI checks re-use the EC2 client |
| **EBS** | (uses EC2 client) | `DescribeVolumes`, `DescribeSnapshots` | EBS checks re-use the EC2 client — 4 recommendations |
| **S3** | `mock_s3` | `ListAllMyBuckets`, `GetBucketLocation`, `GetBucketLifecycleConfiguration`, `GetBucketVersioning`, `GetBucketReplication`, `GetBucketLogging`, `ListBucket`, `ListBucketVersions`, `ListBucketMultipartUploads`, `GetBucketIntelligentTieringConfiguration` | 3 recommendations in golden |
| **CloudWatch** | `mock_cloudwatch` | `GetMetricStatistics`, `ListMetrics` | Used for CPU/network metrics backing EC2, RDS, Lambda checks |

### moto-covered services (empty in golden fixture — zero resources)

| Service | moto Decorator Equivalent | Notes |
|---------|--------------------------|-------|
| **RDS** | `mock_rds` | `DescribeDBInstances`, `DescribeDBSnapshots` |
| **DynamoDB** | `mock_dynamodb` | `ListTables`, `DescribeTable` |
| **Lambda** | `mock_lambda` | `ListFunctions`, `GetFunctionConfiguration` |
| **EFS** | `mock_efs` | `DescribeFileSystems`, `DescribeLifecycleConfiguration` |
| **FSx** | (partial) | `DescribeFileSystems` |
| **ElastiCache** | `mock_elasticache` | `DescribeCacheClusters`, `DescribeReplicationGroups` |
| **ECS** | `mock_ecs` | `ListClusters`, `DescribeClusters`, `ListServices`, `DescribeServices` |
| **ECR** | `mock_ecr` | `DescribeRepositories`, `ListImages` |
| **ELB/ELBv2** | `mock_elb` / `mock_elbv2` | `DescribeLoadBalancers`, `DescribeTargetGroups`, `DescribeTags` |
| **Auto Scaling** | `mock_autoscaling` | `DescribeAutoScalingGroups` |
| **CloudTrail** | `mock_cloudtrail` | `DescribeTrails`, `GetEventSelectors`, `GetInsightSelectors` |
| **Route53** | `mock_route53` | `ListHostedZones`, `ListResourceRecordSets`, `ListHealthChecks` |
| **CloudFront** | `mock_cloudfront` | `ListDistributions`, `GetDistributionConfig` |
| **API Gateway** | `mock_apigateway` | `GET` |
| **Step Functions** | `mock_stepfunctions` | `ListStateMachines` |
| **STS** | `mock_sts` | `GetCallerIdentity` — used in `__init__` |
| **IAM** | `mock_iam` | Global service |
| **KMS** | `mock_kms` | Key management |
| **SNS** | `mock_sns` | Notification topics |
| **SQS** | `mock_sqs` | Queue analysis |
| **SSM** | `mock_ssm` | Parameter/managed instance checks |
| **Secrets Manager** | `mock_secretsmanager` | Secret scanning |
| **CloudWatch Logs** | `mock_logs` | `DescribeLogGroups` |
| **EventBridge** | `mock_events` | `DescribeEventBuses`, `ListRules` |
| **Backup** | `mock_backup` | `ListBackupPlans`, `GetBackupPlan` |
| **Config** | `mock_config` | Config rule checks |
| **Redshift** | (partial) | `DescribeClusters` |
| **DMS** | (partial) | `DescribeReplicationInstances` |
| **AppRunner** | (partial) | `ListServices` |
| **Transfer** | (partial) | `ListServers` |
| **Kafka/MSK** | (partial) | `ListClusters`, `ListClustersV2` |
| **WorkSpaces** | (partial) | `DescribeWorkspaces` |
| **Batch** | (partial) | `DescribeComputeEnvironments` |
| **Athena** | (partial) | `ListWorkGroups`, `GetWorkGroup` |
| **Glue** | (partial) | `GetJobs`, `GetDevEndpoints`, `GetCrawlers` |

### Stubber fallback services (moto does NOT implement)

| Service | API Call | Reason | Stub Response |
|---------|----------|--------|---------------|
| **Cost Optimization Hub** | `ListRecommendations`, `GetRecommendation` | Not in moto | Empty list |
| **Compute Optimizer** | `GetEC2InstanceRecommendations`, `GetEBSVolumeRecommendations`, `GetRDSDatabaseRecommendations` | Not in moto | Empty recommendations |
| **QuickSight** | `DescribeSpiceCapacity` (possibly) | Partial/no moto support | Empty response |
| **Lightsail** | Some methods | Partial moto support | Minimal stubs |
| **MediaStore** | `ListContainers` | Not in moto | Empty list |
| **Redshift Serverless** | `ListWorkgroups` | Not in moto | Empty list |
| **DMS v2** | `DescribeReplicationConfigs` | Not in moto | Empty list |
| **EKS** | `ListAddons`, `DescribeNodegroup` | Partial moto support | Minimal stubs |

## Error Handling in CostOptimizer

The monolith already wraps all unsupported-service calls in broad `except Exception` handlers:

- `cost_hub`: Guard at line 2260 (`if not self.cost_hub: return []`) + try/except at line 2264
- `compute_optimizer.get_ec2_instance_recommendations()`: try/except at line 2543
- `compute_optimizer.get_ebs_volume_recommendations()`: try/except at line 527
- `compute_optimizer.get_rds_database_recommendations()`: try/except at line 896

This means moto's `NotImplementedError` for unsupported service calls is safely caught.

## Implementation Notes

1. All moto resources are seeded in `tests/fixtures/aws_state/us_east_1.py`
2. The `stubbed_aws` fixture in `conftest.py` enters `mock_aws()` context, seeds state, then yields
3. For truly uncovered services, `tests/aws_stubs/stub_responses.py` provides pre-recorded JSON responses
4. No `@mock_aws` class decorator — fixture uses it as context manager per project rules
