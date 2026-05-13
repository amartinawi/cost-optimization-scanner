# Architecture

## System Overview

The AWS Cost Optimization Scanner is a modular Python application that performs
260+ cost optimization checks across 36 adapter modules covering 36 AWS service
categories. The system uses a clean layered architecture with typed contracts,
a service registry, and isolated adapter modules.

```
 cli.py                          cost_optimizer.py
 (91 lines, CLI parsing)         (134 lines, thin shell)
        |                                |
        +--------------------------------+
                        |
         AwsSessionFactory  (core/session.py)
         ClientRegistry     (core/client_registry.py)
         ScanContext        (core/scan_context.py)
                        |
              ScanOrchestrator  (core/scan_orchestrator.py)
                        |
         +----------------+----------------+
         |                |                |
   ALL_MODULES[0]   ALL_MODULES[1]  ... ALL_MODULES[35]
   (36 adapters in services/adapters/*.py)
         |                |                |
         +----------------+----------------+
                        |
              ScanResultBuilder  (core/result_builder.py)
                        |
                   JSON result dict
                        |
              HTMLReportGenerator  (2,454 lines)
              + reporter_phase_a.py  (424 lines)
              + reporter_phase_b.py  (1,502 lines)
                        |
                   HTML report
```

All modules communicate through the typed contracts defined in
`core/contracts.py`. No adapter imports another adapter. The orchestrator
depends on the `ServiceModule` Protocol; adapters depend on `ScanContext` and
the shared utilities in `services/`.

---

## Core Layer (`core/`)

| File | Lines | Class / Function | Purpose |
|------|-------|-------------------|---------|
| `contracts.py` | 188 | `ServiceModule`, `ServiceFindings`, `SourceBlock`, `StatCardSpec`, `GroupingSpec`, `Group`, `WarningRecord`, `PermissionIssueRecord` | Typed boundary between all layers |
| `scan_context.py` | 72 | `ScanContext` | Region, account, client registry, pricing engine, warning/permission tracking |
| `pricing_engine.py` | 517 | `PricingEngine`, `PricingCache` | AWS Pricing API client with in-memory cache, 12 public methods for EC2/EBS/RDS/S3/storage/network pricing. PricingCache nested class with TTL eviction |
| `scan_orchestrator.py` | 85 | `ScanOrchestrator`, `safe_scan` | Iterates modules, pre-fetches advisor data, catches per-module failures |
| `result_builder.py` | 70 | `ScanResultBuilder` | Serializes `ServiceFindings` to a JSON-compatible dict matching legacy format |
| `client_registry.py` | 62 | `ClientRegistry` | Caching boto3 client factory with global-service routing |
| `session.py` | 50 | `AwsSessionFactory` | boto3 session with adaptive retry (10 attempts) |
| `filtering.py` | 60 | `resolve_cli_keys` | Resolves `--scan-only` / `--skip-service` CLI tokens to module keys via alias index |

### contracts.py -- The Protocol Boundary

Every adapter implements the `ServiceModule` Protocol (line 97). The Protocol
is `@runtime_checkable` and declares:

```python
class ServiceModule(Protocol):
    key: str                          # "ec2", "s3", "network"
    cli_aliases: tuple[str, ...]      # ("ec2", "instances")
    display_name: str                 # "EC2"
    stat_cards: tuple[StatCardSpec, ...]
    grouping: GroupingSpec | None
    requires_cloudwatch: bool
    reads_fast_mode: bool

    def required_clients(self) -> tuple[str, ...]: ...
    def scan(self, ctx: Any) -> ServiceFindings: ...

    custom_grouping: Callable[[ServiceFindings], list[Group]] | None
```

Adapters return `ServiceFindings` (line 67), a `@dataclass(frozen=True)` with
sources as `dict[str, SourceBlock]`. Each `SourceBlock` (line 40) wraps a
count, a tuple of recommendation dicts, and optional extras spread into the
serialized output.

### scan_context.py -- Execution Context

`ScanContext` holds everything an adapter needs at scan time:

- `region`, `account_id`, `profile`, `fast_mode` -- identity flags
- `clients` -- the `ClientRegistry` for creating boto3 clients
- `pricing_multiplier` -- regional pricing factor from `CostOptimizer.REGIONAL_PRICING`
- `pricing_engine` -- `PricingEngine` instance for live AWS Pricing API lookups with in-memory cache
- `fast_mode` -- skip CloudWatch-heavy scans for faster results
- `old_snapshot_days` -- threshold for old EBS snapshot checks (default 90)
- `cost_hub_splits` -- pre-fetched Cost Optimization Hub recommendations partitioned by service key
- `warn()` / `permission_issue()` -- structured warning collection

Adapters call `ctx.client("ec2")` to get a boto3 client. The ClientRegistry
caches clients and routes global services (CloudFront, Route53, IAM, etc.)
to `us-east-1` automatically.

### scan_orchestrator.py -- Orchestration

`ScanOrchestrator.run()`:

1. Calls `resolve_cli_keys()` to determine which module keys to scan
2. Calls `_prefetch_advisor_data()` -- fetches all Cost Optimization Hub
   recommendations once, splits them into `ctx.cost_hub_splits` by resource
   type. The `type_map` covers `Ec2Instance` -> `"ec2"`, `LambdaFunction` ->
   `"lambda"`, `EbsVolume` -> `"ebs"`, `RdsDbInstance` / `RdsDbCluster` ->
   `"rds"`, `S3Bucket` -> `"s3"`, `ElastiCacheCluster` -> `"elasticache"`,
   `OpenSearchDomain` -> `"opensearch"`, `RedshiftCluster` -> `"redshift"`,
   `EksCluster` -> `"eks"`, `EcsService` / `EcsTask` / `EcsCluster` ->
   `"containers"`, and every reservation / savings-plan type
   (`*ReservedInstances`, `ComputeSavingsPlans`, `EC2InstanceSavingsPlans`,
   `SageMakerSavingsPlans`) -> `"commitment_analysis"`. Anything unrecognised
   is recorded via `ctx.warn` so the map can be extended deliberately rather
   than silently dropped.
3. Iterates `self.modules`, calling `safe_scan(module, ctx)` for each
   selected module
4. `safe_scan()` catches any exception, logs a warning, and returns an
   empty `ServiceFindings` so one broken module never crashes the scan

The dedicated `CostOptimizationHubModule` was retired from `ALL_MODULES` on
2026-05-14. CoH data now flows exclusively through the per-service tabs
populated from `ctx.cost_hub_splits`; the aggregate tab no longer exists.

### client_registry.py -- Client Caching

`ClientRegistry` wraps `AwsSessionFactory` and caches every boto3 client by
`(service_name, region)` tuple. Global services are automatically routed to
`us-east-1` when no explicit region is provided. The `trustedadvisor` service
name is aliased to `support`.

### result_builder.py -- Serialization

`ScanResultBuilder.build()` produces a dict with this structure:

```python
{
    "account_id": "...",
    "region": "...",
    "profile": "...",
    "scan_time": "2026-04-29T...",
    "scan_warnings": [...],
    "permission_issues": [...],
    "services": {
        "ec2": { "service_name": "EC2", "total_recommendations": 12, ... },
        "s3": { ... },
    },
    "summary": {
        "total_services_scanned": 28,
        "total_recommendations": 234,
        "total_monthly_savings": 2550.0,
    },
}
```

`_serialize()` spreads `ServiceFindings.extras` into the output dict, and
`_serialize_source()` does the same for `SourceBlock.extras`. This allows
adapters to inject service-specific keys (e.g. `instance_count` for EC2,
`service_counts` for Containers) without modifying the schema.

### filtering.py -- Service Resolution

`resolve_cli_keys()` builds an alias index from all modules'
`cli_aliases` tuples, then resolves user-provided tokens to canonical module
keys. Supports both inclusion (`--scan-only`) and exclusion (`--skip-service`)
modes. Tokens are matched case-insensitively.

---

## Services Layer (`services/`)

### Module Registry (`services/__init__.py`)

`ALL_MODULES` is an ordered list of 36 instantiated adapter classes. The
runtime tab order in the report is recomputed at render time, sorted by
each service's total monthly savings descending so the architect-skim
lands on the services that hold the most money first. Adding a new adapter
requires only instantiating it and appending to this list.

### Base Class (`services/_base.py`)

`BaseServiceModule` provides default implementations for every Protocol field:

- `key`, `cli_aliases`, `display_name` -- empty strings / tuples
- `stat_cards`, `grouping` -- empty tuple / `None`
- `requires_cloudwatch`, `reads_fast_mode` -- `False`
- `custom_grouping` -- `None`
- `required_clients()` -- returns `()`
- `scan()` -- raises `NotImplementedError`

Adapters subclass this and override only what they need.

### Shared Utilities

| File | Purpose |
|------|---------|
| `_savings.py` (16 lines) | `parse_dollar_savings()` -- regex to extract dollar amounts from strings like `"$12.50/month"` |
| `advisor.py` (153 lines) | `get_detailed_cost_hub_recommendations()`, `get_ec2_compute_optimizer_recommendations()`, `get_ebs_compute_optimizer_recommendations()`, `get_rds_compute_optimizer_recommendations()` |

### Adapter Files (`services/adapters/*.py`)

36 adapter files (excluding `__pycache__`). Each adapter is a thin class that:

1. Declares `key`, `cli_aliases`, `display_name`, `required_clients()`
2. In `scan()`, calls one or more analysis functions from the legacy service
   modules (e.g. `services/ec2.py`, `services/s3.py`)
3. Aggregates recommendations into `ServiceFindings` with named `SourceBlock`
   entries

#### Adapter Categories by Savings Strategy

**Live-pricing adapters (11)** -- Use AWS Pricing API or resource-size-aware calculations:

| Adapter | Key | Pricing Method |
|---------|-----|---------------|
| `workspaces.py` | `workspaces` | `get_instance_monthly_price("AmazonWorkSpaces", bundle)` x 0.40 AutoStop savings |
| `glue.py` | `glue` | `$0.44/DPU/hour` x DPU count x 160 hrs/month x 0.30 rightsizing |
| `lightsail.py` | `lightsail` | `get_instance_monthly_price("AmazonLightsail", bundle)` |
| `apprunner.py` | `apprunner` | `$0.064/vCPU/hour + $0.007/GB/hour` x 730 x 0.30 |
| `transfer.py` | `transfer` | `$0.30/protocol/hour` x protocols x 730 |
| `mediastore.py` | `mediastore` | `get_s3_monthly_price_per_gb("STANDARD")` x storage GB |
| `quicksight.py` | `quicksight` | SPICE tier pricing ($0.25-$0.38/GB) x unused capacity |
| `containers.py` | `containers` | Fargate rates ($0.04048/vCPU + $0.004445/GB/hour) x 730 |
| `dynamodb.py` | `dynamodb` | RCU ($0.00013/hour) + WCU ($0.00065/hour) x 730 x discount |
| `athena.py` | `athena` | CloudWatch ProcessedBytes -> $5/TB x 0.75 scan reduction |
| `step_functions.py` | `step_functions` | CloudWatch ExecutionsStarted -> $0.025/1K transitions x 0.60 |

**Parse-rate adapters (5)** -- Extract dollar amounts from recommendation text
using regex or keyword matching:

| Adapter | Key | Parse Method |
|---------|-----|-------------|
| `cloudfront.py` | `cloudfront` | Fixed $25/rec |
| `api_gateway.py` | `api_gateway` | Keyword-based |
| `elasticache.py` | `elasticache` | Keyword-based |
| `opensearch.py` | `opensearch` | Keyword-based |
| `ami.py` | `ami` | `parse_dollar_savings()` |

**Complex adapters (6)** -- Multiple data sources, CloudWatch metrics,
Compute Optimizer integration:

| Adapter | Key | Sources |
|---------|-----|---------|
| `ec2.py` | `ec2` | Cost Hub, Compute Optimizer, enhanced checks, advanced checks |
| `ebs.py` | `ebs` | Cost Hub, Compute Optimizer, enhanced checks |
| `rds.py` | `rds` | Cost Hub, Compute Optimizer, enhanced checks |
| `s3.py` | `s3` | Custom analysis, lifecycle checks, multipart uploads |
| `lambda_svc.py` | `lambda` | Cost Hub, custom analysis |
| `dynamodb.py` | `dynamodb` | CloudWatch metrics, custom analysis |

**Composite adapters (4)** -- Aggregate multiple sub-services into one module:

| Adapter | Key | Sub-services |
|---------|-----|-------------|
| `file_systems.py` | `file_systems` | EFS (lifecycle, archive) + FSx (optimization) |
| `containers.py` | `containers` | ECS + EKS + ECR |
| `network.py` | `network` | Elastic IP + NAT Gateway + VPC Endpoints + Load Balancer + Auto Scaling (uses live pricing for sub-services) |
| `monitoring.py` | `monitoring` | CloudWatch + CloudTrail + Backup + Route53 |

**Remaining flat-rate adapters (1)** -- Only `batch.py` still uses flat-rate pricing.

| Adapter | Key | Flat Rate |
|---------|-----|-----------|
| `batch.py` | `batch` | varies |

### Legacy Service Modules (`services/*.py`)

The adapter files in `services/adapters/` are thin wrappers. The actual AWS API
calls and analysis logic live in the legacy service modules at `services/`
top level (e.g. `services/ec2.py`, `services/s3.py`, `services/cloudfront.py`).
These modules were preserved during refactoring to minimize risk and maintain
the 220+ optimization checks unchanged.

---

## Data Flow -- End-to-End Request Path

```
1. cli.py main()
   |
   +-- argparse: region, --profile, --fast, --scan-only, --skip-service
   |
   +-- CostOptimizer(region, profile, fast_mode)
       |
       +-- AwsSessionFactory(region, profile)
       |   +-- boto3.Session with adaptive retry (max_attempts=10)
       |   +-- STS get_caller_identity -> account_id
       |
       +-- ClientRegistry(session_factory)
       |   +-- caches boto3 clients by (service, region)
       |   +-- routes global services to us-east-1
       |
       +-- ScanContext(region, account_id, profile, fast_mode, clients, pricing_multiplier)

2. CostOptimizer.scan_region(skip_services, scan_only)
   |
   +-- ScanOrchestrator(ctx, ALL_MODULES)
   |
   +-- orchestrator.run(scan_only, skip)
       |
       +-- resolve_cli_keys(modules, scan_only, skip)
       |   +-- builds alias index from all modules' cli_aliases
       |   +-- resolves user tokens to canonical module keys
       |   +-- returns set of selected keys
       |
       +-- _prefetch_advisor_data(selected_keys)
       |   +-- get_detailed_cost_hub_recommendations(ctx)
       |   +-- splits results into ctx.cost_hub_splits by resource type
       |
       +-- for each module where module.key in selected:
           |
           +-- safe_scan(module, ctx)
               |
               +-- module.scan(ctx) -> ServiceFindings
               |   |
               |   +-- ctx.client("ec2") -> boto3 client (cached)
               |   +-- calls analysis functions from services/*.py
               |   +-- aggregates recommendations into SourceBlock entries
               |   +-- calculates total_monthly_savings
               |   +-- returns frozen ServiceFindings
               |
               +-- on exception: warn + return empty ServiceFindings

3. ScanResultBuilder(ctx).build(findings)
   |
   +-- serializes each ServiceFindings to dict
   +-- spreads extras into output
   +-- produces summary totals

4. HTMLReportGenerator(scan_results)
   |
   +-- Service Descriptor Dict pattern (2,454 lines)
   +-- reporter_phase_a.py: descriptor-driven grouped services (7 branches)
   +-- reporter_phase_b.py: function registry for source handlers
   |
   +-> cost_optimization_report.html
```

---

## Reporter Architecture

### HTMLReportGenerator (`html_report_generator.py`, 2,454 lines)

The reporter uses a **Service Descriptor Dict** pattern. Each AWS service is
described by a descriptor dict that specifies:

- Tab label and display name
- Source keys and how to render them
- Stat cards (total recommendations, savings, resource counts)
- Grouping behavior (by check category, resource type, or custom)
- Phase A / Phase B handler functions

The generator iterates over `scan_results["services"]`, matches each service
key to its descriptor, and renders HTML sections using template methods.

### Phase A (`reporter_phase_a.py`, 424 lines)

Phase A handles **descriptor-driven grouped services** -- services whose
recommendations are rendered through a service descriptor that specifies
grouping and rendering rules. There are 7 distinct rendering branches for
different service categories (file systems, containers, etc.).

Functions like `render_file_systems()` accept a `sources` dict and produce
HTML strings with grouped recommendation lists.

### Phase B (`reporter_phase_b.py`, 1,502 lines)

Phase B provides a **function registry** for source-specific rendering
handlers. Complex services like EC2 have multiple source types (Cost Hub,
Compute Optimizer, enhanced checks, advanced checks) each with its own
rendering function:

- `_render_ec2_enhanced_checks()` -- groups EC2 recs by `CheckCategory`
- `_render_ec2_cost_hub()` -- renders Cost Optimization Hub actions
- Plus handlers for EBS, RDS, S3, Lambda, DynamoDB, and more

Phase B functions receive `(recommendations, source_name, service_data)` and
return HTML strings.

---

## Entry Points

### cli.py (91 lines)

Unified CLI entry point. Parses arguments, constructs `CostOptimizer`, runs the
scan, saves JSON output, and generates the HTML report.

```bash
python3 cli.py us-east-1 --profile production --scan-only s3 --scan-only lambda
```

### cost_optimizer.py (134 lines)

Thin orchestration shell. Responsibilities:

1. `__init__`: Creates `AwsSessionFactory`, resolves account ID, builds
   `ClientRegistry` and `ScanContext`. Contains `REGIONAL_PRICING` dict with
   35 region multipliers.
2. `scan_region()`: Imports `ALL_MODULES`, constructs `ScanOrchestrator`,
   runs it, serializes via `ScanResultBuilder`, returns the JSON dict.

---

## Adding New Services

1. **Create the analysis module** at `services/newservice.py` with functions
   that accept `ScanContext` and return `{"recommendations": [...], ...}`.

2. **Create the adapter** at `services/adapters/newservice.py`:

```python
from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.newservice import get_enhanced_newservice_checks


class NewServiceModule(BaseServiceModule):
    key: str = "newservice"
    cli_aliases: tuple[str, ...] = ("newservice",)
    display_name: str = "NewService"

    def required_clients(self) -> tuple[str, ...]:
        return ("newservice",)

    def scan(self, ctx) -> ServiceFindings:
        result = get_enhanced_newservice_checks(ctx)
        recs = result.get("recommendations", [])
        return ServiceFindings(
            service_name="NewService",
            total_recommendations=len(recs),
            total_monthly_savings=25 * len(recs),
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
        )
```

3. **Register** in `services/__init__.py`:

```python
from services.adapters.newservice import NewServiceModule

ALL_MODULES: list[Any] = [
    # ... existing modules ...
    NewServiceModule(),
]
```

4. **Add reporter descriptors** in `html_report_generator.py` and optionally
   add Phase A/B handlers for custom rendering.

5. **Test** -- add a snapshot test in `tests/test_regression_snapshot.py` using
   the existing mock AWS infrastructure.

No changes to `core/` are required. The adapter is automatically available via
`--scan-only newservice` and included in full scans.

---

## Testing Strategy

### Test Suite (718 lines across 6 files, 153 tests: 152 pass + 1 skip)

| File | Lines | Tests | Purpose |
|------|-------|-------|---------|
| `test_orchestrator.py` | 44 | 1 | ScanOrchestrator end-to-end with stubbed AWS |
| `test_result_builder.py` | 90 | 6 | ScanResultBuilder serialization correctness |
| `test_regression_snapshot.py` | 172 | 17 | Golden JSON snapshot comparison |
| `test_reporter_snapshots.py` | 218 | 4 | Golden HTML snapshot comparison |
| `test_offline_scan.py` | 82 | 1 | Full offline scan with moto mock |
| `test_pricing_engine.py` | 143 | 22 | PricingEngine API lookups, cache TTL, failure handling (21 pass + 1 skip) |
| `conftest.py` | 303 | -- | Fixtures, normalizers, stubbed_aws |

### Snapshot Testing

The test suite uses golden-file snapshot testing:

- `tests/fixtures/golden_scan_results.json` -- expected JSON output
- `tests/fixtures/golden_report.html` -- expected HTML output
- `tests/fixtures/CAPTURE_TIMESTAMP.txt` -- frozen clock reference

`conftest.py` provides normalizers that replace timestamps, account IDs, and
filenames with sentinels before comparison, so snapshots are deterministic.

### AWS Mock Surface

`conftest.py` provides a `stubbed_aws` fixture that combines:

- `moto.mock_aws` for service-level mocking
- `botocore.stub.Stubber` for fine-grained response control
- `freezegun.freeze_time` for deterministic timestamps

The `tests/aws_stubs/` directory contains pre-built stub responses for all 28
service modules.

### Running Tests

```bash
python3 -m pytest tests/ -v
```

Tests are fully offline -- no AWS credentials or network access required.

---

## Key Design Decisions

### Protocol-Based Decoupling

Adapters implement `ServiceModule` Protocol rather than inheriting from a base
class. `@runtime_checkable` allows `isinstance()` checks when needed. The
`BaseServiceModule` class exists for convenience but is not required.

### Frozen Dataclasses

`ServiceFindings`, `SourceBlock`, `WarningRecord`, and `PermissionIssueRecord`
are all `@dataclass(frozen=True)`. This prevents accidental mutation during
scan execution and makes findings safe to log, serialize, or cache.

### safe_scan Error Boundary

Each module is wrapped in `safe_scan()` which catches all exceptions. A failing
module produces a warning and empty findings rather than crashing the entire
scan. This is critical for enterprise environments where IAM permissions vary
per service.

### Pre-Fetched Advisor Data

Cost Optimization Hub recommendations are fetched once in
`_prefetch_advisor_data()` and partitioned into `ctx.cost_hub_splits` by
resource type. Every per-service adapter that has a corresponding bucket
(EC2, EBS, RDS, Lambda, S3, ElastiCache, OpenSearch, Redshift, EKS,
Containers, Commitment Analysis) reads from this cache rather than making
separate API calls, reducing API throttling risk. Since 2026-05-14 the
aggregate `CostOptimizationHubModule` is retired and the per-service
distribution is the only path CoH data takes into the report.

### Extras Spreading

Both `ServiceFindings.extras` and `SourceBlock.extras` are `Mapping[str, Any]`
that get spread into the serialized output dict. This allows adapters to
inject service-specific data (e.g., `instance_count`, `service_counts`,
`bucket_count`) without modifying the core contracts.

### Global Service Routing

`ClientRegistry` automatically routes global services (Route53, CloudFront,
IAM, Cost Optimization Hub, etc.) to `us-east-1`. Adapters don't need to
know which services are global -- they just call `ctx.client("cloudfront")`.

### Live Pricing Engine

`core/pricing_engine.py` provides centralized AWS Pricing API access. All
pricing lookups go through `PricingEngine` which:

- Uses the AWS Price List API (`pricing:GetProducts`) via `us-east-1` endpoint
- Caches results in an in-memory `PricingCache` with configurable TTL (default 6 hours)
- Provides 12 public methods covering EC2 instances, EBS volumes, RDS instances/storage, S3 storage classes, and generic instance/storage lookups
- Returns `0.0` on any API failure, ensuring scans never crash due to pricing unavailability
- Is injected into `ScanContext` at construction time, available to all adapters via `ctx.pricing_engine`

Adapters that use CloudWatch metrics for pricing calculations (athena, step_functions) check `ctx.fast_mode` first and fall back to flat-rate estimates when true.

---

## AWS Well-Architected Framework Alignment

Based on the [AWS Well-Architected Cost Optimization Pillar](https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html), the scanner implements the five key cost optimization practices:

### 1. Cloud Financial Management
- Comprehensive cost analysis across 30 AWS services
- Regional pricing calculations, savings estimates, confidence scoring
- Provides clear understanding of costs and usage patterns

### 2. Expenditure and Usage Awareness
- CloudWatch metrics integration for accurate utilization analysis
- 14-day analysis periods, usage pattern recognition, idle resource detection
- Monitors and analyzes usage patterns to identify cost savings opportunities

### 3. Cost-Effective Resources
- Rightsizing recommendations, Reserved Instance analysis, Savings Plans evaluation
- Instance type optimization, storage class recommendations, Graviton migration
- Uses the right type and size of AWS resources for workloads

### 4. Manage Demand and Supply Resources
- Auto Scaling analysis, capacity optimization, scheduling recommendations
- Static ASG detection, non-production scheduling, burst capacity analysis
- Scales resources based on actual demand patterns

### 5. Optimize Over Time
- Continuous optimization checks, version migration recommendations
- Previous generation detection (t2 to t3), engine version updates, lifecycle policies
- Continuously evaluates and implements cost optimization opportunities

### Regional Pricing Implementation

The scanner uses regional pricing multipliers to adjust savings estimates based on where resources run. `cost_optimizer.py` contains the `REGIONAL_PRICING` dict with multipliers for 35 regions. Regions without explicit pricing data use a conservative 15% premium. All estimates are approximate; use the AWS Pricing Calculator for precise figures.

---

## AWS Pricing Models and Service Optimization Reference

### AWS Pricing Models

Based on [AWS Well-Architected Framework COST07-BP01](https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/cost_pricing_model_analysis.html), the scanner analyzes resources across these pricing models:

**On-Demand**: Pay per hour or second without commitments. Scanner identifies opportunities to migrate to commitment-based pricing.

**Savings Plans**: Up to 72% savings on EC2, Lambda, and Fargate with 1 or 3-year terms. Scanner recommends optimal plans based on usage patterns.

**Reserved Instances**: Up to 75% savings by prepaying for capacity with 1 or 3-year terms. Scanner identifies RI opportunities and optimal coverage.

**Spot Instances**: Up to 90% savings using spare AWS capacity, with 2-minute interruption notice. Scanner evaluates workload suitability.

### Service-Specific Optimization Summary

| Service | Key Optimization Checks |
|---------|------------------------|
| **EC2** | Rightsizing (CPU/memory/network), previous gen migration (t2 to t3, m4 to m5), Graviton recommendations, burstable credit analysis, RI coverage, Spot suitability |
| **Lambda** | Memory right-sizing, ARM/Graviton2 migration (up to 34% better price-performance), provisioned concurrency review, VPC cold start costs |
| **S3** | Storage class optimization (Standard, Standard-IA, One Zone-IA, Glacier variants), Intelligent-Tiering, lifecycle policies, multipart upload cleanup |
| **EBS** | Unattached volume detection, gp2 to gp3 migration, snapshot lifecycle, IOPS utilization |
| **RDS** | Multi-AZ in non-prod, instance rightsizing, gp2 to gp3 storage, backup retention, RI coverage |
| **DynamoDB** | On-Demand vs Provisioned analysis, Auto Scaling review, reserved capacity, GSI optimization |
| **ElastiCache** | Graviton migration, reserved nodes, cluster rightsizing, engine version review |
| **Networking** | NAT Gateway alternatives (VPC Endpoints), unused Elastic IPs, load balancer consolidation, cross-region data transfer |
| **Containers** | ECS Fargate vs EC2 comparison, task right-sizing, ECR lifecycle policies, EKS node optimization |

---

## File Map

```
cost-optimization-scanner/
  cli.py                          (91)   CLI entry point
  cost_optimizer.py               (134)  Thin orchestration shell
  html_report_generator.py        (2454) HTML report generation
  reporter_phase_a.py             (424)  Phase A: grouped services
  reporter_phase_b.py             (1502) Phase B: source handlers
  requirements.txt                       boto3, botocore, python-dateutil
  core/
    __init__.py
    contracts.py                  (188)  Protocol + dataclasses
    scan_context.py               (72)   ScanContext
    pricing_engine.py             (517)  PricingEngine + PricingCache
    scan_orchestrator.py          (85)   ScanOrchestrator + safe_scan
    result_builder.py             (70)   ScanResultBuilder
    client_registry.py            (62)   ClientRegistry
    session.py                    (50)   AwsSessionFactory
    filtering.py                  (60)   resolve_cli_keys
  services/
    __init__.py                   (63)   ALL_MODULES registry (36 adapters)
    _base.py                      (32)   BaseServiceModule
    _savings.py                   (16)   parse_dollar_savings
    advisor.py                    (153)  Cost Hub + Compute Optimizer
    adapters/
      ami.py                      ami.py
      api_gateway.py              api_gateway
      apprunner.py                apprunner
      athena.py                   athena
      batch.py                    batch
      cloudfront.py               cloudfront
      containers.py               containers (ECS+EKS+ECR)
      dms.py                      dms
      dynamodb.py                 dynamodb
      ebs.py                      ebs
      ec2.py                      ec2
      elasticache.py              elasticache
      file_systems.py             file_systems (EFS+FSx)
      glue.py                     glue
      lambda_svc.py               lambda
      lightsail.py                lightsail
      mediastore.py               mediastore
      monitoring.py               monitoring (CW+CT+Backup+R53)
      msk.py                      msk
      network.py                  network (EIP+NAT+VPC+LB+ASG)
      opensearch.py               opensearch
      quicksight.py               quicksight
      rds.py                      rds
      redshift.py                 redshift
      s3.py                       s3
      step_functions.py           step_functions
      transfer.py                 transfer
      workspaces.py               workspaces
    [legacy service modules]      ec2.py, s3.py, cloudfront.py, ...
  tests/
    conftest.py                   (303)  Fixtures, normalizers, stubbed_aws
    test_orchestrator.py          (44)   Orchestrator test
    test_result_builder.py        (90)   Serialization tests
    test_regression_snapshot.py   (172)  Golden JSON snapshots
    test_reporter_snapshots.py    (218)  Golden HTML snapshots
    test_offline_scan.py          (82)   Full offline scan
    test_pricing_engine.py        (143)  PricingEngine tests (22 tests)
    fixtures/                            Golden files
    aws_stubs/                           Pre-built stub responses
```
