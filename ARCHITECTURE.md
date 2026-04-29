# Technical Architecture

## System Overview

The AWS Cost Optimization Scanner is built as a modular Python application designed for enterprise-scale AWS cost optimization analysis.

```
┌─────────────────────────────────────────────────────────────┐
│                AWS Cost Optimization Scanner v2.5.9         │
├─────────────────────────────────────────────────────────────┤
│  cost_optimizer.py (8,677 lines)                           │
│  ├─ CostOptimizer Class                                     │
│  │  ├─ 54 AWS Service Clients (adaptive retry)             │
│  │  ├─ 220+ Cost Optimization Checks                       │
│  │  ├─ Regional Pricing Engine                             │
│  │  ├─ Service Filtering System (30 categories)            │
│  │  ├─ Cross-Region S3 Support                            │
│  │  ├─ CloudWatch Metrics Integration                      │
│  │  ├─ Intelligent Recommendation Gating                  │
│  │  ├─ Error Tracking System                              │
│  │  └─ Enterprise Error Handling                           │
│  └─ Main Orchestrator (scan_region with filtering)         │
├─────────────────────────────────────────────────────────────┤
│  html_report_generator.py (3,699 lines)                    │
│  ├─ HTMLReportGenerator Class                              │
│  │  ├─ Smart Grouping (11 services)                       │
│  │  ├─ Resource Type Filtering                            │
│  │  ├─ Duplicate Prevention Engine                        │
│  │  ├─ Professional UI Components                         │
│  │  ├─ Interactive Multi-tab Interface                    │
│  │  ├─ Enhanced Error Logging                             │
│  │  └─ Warnings Display                                   │
│  └─ Report Generation Pipeline                             │
└─────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. Cost Optimizer Engine (`cost_optimizer.py`)
- **54 AWS Service Clients**: Comprehensive coverage with adaptive retry
- **Pagination Support**: Handles unlimited resources (tested with 1000+)
- **Regional Pricing**: Accurate cost calculations across AWS regions
- **Error Resilience**: Exponential backoff with specific error handling

### 2. Analysis Engine
- **220+ Optimization Checks**: Comprehensive analysis across all services
- **Real Metrics Integration**: CloudWatch data for accurate analysis where available
- **Storage Class Awareness**: S3 cost estimation across storage classes
- **Intelligent Gating**: Prevents false positives with usage-based recommendations

### 3. Report Generation (`html_report_generator.py`)
- **Professional HTML Reports**: Interactive multi-tab interface with executive summary
- **Executive Dashboard**: Interactive charts showing cost savings distribution
- **Chart.js Integration**: Modern pie and bar charts with click-to-filter functionality
- **Dark Mode Support**: Complete theme system with toggle button and persistence
- **Smart Grouping**: Intelligent organization of 30 service categories
- **Deduplication**: Applied to key report sections
- **Responsive Design**: Works on desktop and mobile devices
- **AWS-Themed Styling**: Professional presentation for executive reporting

## Service Coverage (30 AWS Services)

### Compute & Serverless
| Service | Key Optimizations |
|---------|-------------------|
| **EC2** | Idle/rightsizing, previous gen (t2→t3), burstable credits, Auto Scaling gaps |
| **Lambda** | Low usage, memory sizing, ARM migration (metric‑gated), VPC config review |
| **Auto Scaling** | Static ASGs, non‑prod schedules, oversized instances |
| **Batch** | Spot allocation strategy, Graviton instance consideration |

### Storage & Databases
| Service | Key Optimizations |
|---------|-------------------|
| **EBS** | Unattached volumes, gp2→gp3, old snapshots |
| **S3** | Lifecycle policies, Intelligent‑Tiering, multipart uploads |
| **RDS** | Multi‑AZ in non‑prod, backup retention, gp2→gp3 storage |
| **DynamoDB** | CloudWatch‑backed capacity review, billing mode analysis |
| **ElastiCache** | Graviton migration, reserved nodes, version review |
| **OpenSearch** | Reserved instances, gp2→gp3, utilization review |
| **File Systems** | EFS lifecycle/Archive, One Zone migration, FSx optimizations |
| **Redshift** | Reserved nodes, rightsizing, serverless reservations |

### Networking & Infrastructure
| Service | Key Optimizations |
|---------|-------------------|
| **Network** | EIPs, NAT Gateways, VPC endpoints, Load Balancers |
| **CloudFront** | Price class optimization (traffic‑gated), disabled distributions |
| **API Gateway** | REST→HTTP for simple APIs, caching opportunities |
| **Route53** | Hosted zone and health check review |

### Containers & Orchestration
| Service | Key Optimizations |
|---------|-------------------|
| **Containers** | ECS unused clusters, over‑provisioned services, Container Insights |

### Monitoring & Management
| Service | Key Optimizations |
|---------|-------------------|
| **CloudWatch** | Log retention, excessive storage, custom metrics |
| **Step Functions** | Standard→Express for high‑volume workflows |
| **CloudTrail** | Multi‑region trails, data events, Insights review |
| **AWS Backup** | Retention optimization, schedule review |

### Specialized Services
| Service | Key Optimizations |
|---------|-------------------|
| **Lightsail** | Stopped instances, oversized bundles, unused static IPs |
| **DMS** | Instance rightsizing, serverless configuration review |
| **QuickSight** | SPICE capacity utilization review |
| **App Runner** | Instance configuration review |
| **Transfer Family** | Protocol optimization |
| **MSK** | Cluster rightsizing, serverless configuration review |
| **WorkSpaces** | AlwaysOn → AutoStop recommendation |
| **MediaStore** | Unused containers (metric‑backed) |
| **Glue** | Job DPU sizing, dev endpoints, crawler schedule review |
| **Athena** | Workgroup scan limits, query results lifecycle |

## Key Technical Features

### Enterprise Scalability
- **Pagination Support**: All AWS API calls use pagination to handle unlimited resources
- **Retry Logic**: Exponential backoff with up to 10 attempts for API throttling
- **Error Tracking**: Comprehensive logging of warnings and permission issues
- **Memory Efficiency**: Streaming processing for large datasets

### Intelligent Analysis
- **CloudWatch Integration**: Real metrics for EC2, ECS, EKS, DynamoDB, and Lambda
- **Usage-Based Gating**: Prevents inappropriate recommendations (e.g., ARM migration only for active functions)
- **Traffic-Based Analysis**: CloudFront and API Gateway recommendations based on actual usage
- **Metric-Backed Decisions**: 14-day analysis periods for accurate utilization assessment

### Regional Support
- **Global Coverage**: Supports all AWS regions with region-specific pricing
- **Cross-Region S3**: Proper analysis of buckets across different regions
- **Regional Multipliers**: Accurate cost calculations with region-specific pricing
- **Conservative Fallbacks**: 15% premium for regions without explicit pricing data

### Service Filtering
- **30 Service Categories**: Granular control over which services to analyze
- **Performance Optimization**: 50-80% faster scans when targeting specific services
- **Workflow Support**: Different teams can focus on their areas of responsibility
- **Flexible Combinations**: Support for both inclusion (--scan-only) and exclusion (--skip-service)

## Data Flow

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   AWS APIs      │───▶│  Cost Optimizer  │───▶│  HTML Report    │
│                 │    │                  │    │                 │
│ • 54 Services   │    │ • 220+ Checks    │    │ • Multi-tab UI  │
│ • CloudWatch    │    │ • Filtering      │    │ • Smart Groups  │
│ • Cost Hub      │    │ • Pricing Calc   │    │ • Deduplication │
│ • Compute Opt   │    │ • Error Track    │    │ • Statistics    │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## Performance Characteristics

### Scan Performance
- **Full Scan**: 5-15 minutes for typical enterprise accounts (1000+ resources)
- **Filtered Scan**: 1-5 minutes when targeting specific services
- **Fast Mode**: 50-80% faster for S3-heavy environments
- **Memory Usage**: <500MB for most scans, scales linearly with resource count

### Report Generation
- **HTML Generation**: 10-30 seconds for comprehensive reports
- **File Size**: 1-5MB for typical reports with embedded CSS/JS
- **Browser Compatibility**: Modern browsers with responsive design
- **Interactive Elements**: Client-side filtering and sorting

## Error Handling

### Graceful Degradation
- **Permission Issues**: Continue scan with warnings for inaccessible services
- **API Throttling**: Exponential backoff with intelligent retry logic
- **Service Unavailability**: Skip unavailable services with proper logging
- **Partial Failures**: Generate reports with available data and clear error indicators

### Comprehensive Logging
- **Scan Warnings**: Non-fatal issues that don't prevent analysis
- **Permission Issues**: Detailed IAM permission problems with service context
- **API Errors**: Specific error codes and recommended actions
- **Performance Metrics**: Scan duration and resource counts

## Security Considerations

### Read-Only Access
- **No Write Operations**: Tool only reads AWS resources and configurations
- **Minimal Permissions**: IAM policy includes only necessary read permissions
- **No Data Modification**: Analysis doesn't change any AWS resources
- **Secure Credentials**: Uses standard AWS credential chain

### Data Handling
- **Local Processing**: All analysis performed locally, no external data transmission
- **Temporary Files**: Reports generated locally with user-controlled output paths
- **No Sensitive Data**: Tool doesn't access or store sensitive application data
- **Audit Trail**: Comprehensive logging for security review

## Extensibility

### Adding New Services
1. Add service client initialization in `__init__`
2. Implement service-specific analysis methods
3. Add service to filtering system
4. Update HTML report generator
5. Add IAM permissions to documentation

### Custom Checks
1. Implement check method following naming convention
2. Add to appropriate service analysis method
3. Include cost estimation logic
4. Add to report generation pipeline
5. Update documentation

### Regional Support
1. Add region to `REGIONAL_PRICING` dictionary
2. Update S3 regional multipliers if needed
3. Test with region-specific resources
4. Update documentation

This architecture enables the scanner to handle enterprise-scale AWS environments while providing accurate, actionable cost optimization recommendations across all major AWS services.
