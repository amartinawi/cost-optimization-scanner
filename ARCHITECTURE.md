# Architecture

## System Overview

The AWS Cost Optimization Scanner is a modular Python application that performs
220+ cost optimization checks across 28 adapter modules covering 30 AWS service
categories. The system was refactored from a single 8,500-line monolith into a
clean layered architecture with typed contracts, a service registry, and
isolated adapter modules.

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
   ALL_MODULES[0]   ALL_MODULES[1]  ... ALL_MODULES[27]
   (28 adapters in services/adapters/*.py)
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
| `contracts.py` | 109 | `ServiceModule`, `ServiceFindings`, `SourceBlock`, `StatCardSpec`, `GroupingSpec`, `Group`, `WarningRecord`, `PermissionIssueRecord` | Typed boundary between all layers |
| `scan_context.py` | 50 | `ScanContext` | Region, account, client registry, warning/permission tracking |
| `scan_orchestrator.py` | 67 | `ScanOrchestrator`, `safe_scan` | Iterates modules, pre-fetches advisor data, catches per-module failures |
| `result_builder.py` | 59 | `ScanResultBuilder` | Serializes `ServiceFindings` to a JSON-compatible dict matching legacy format |
| `client_registry.py` | 54 | `ClientRegistry` | Caching boto3 client factory with global-service routing |
| `session.py` | 42 | `AwsSessionFactory` | boto3 session with adaptive retry (10 attempts) |
| `filtering.py` | 58 | `resolve_cli_keys` | Resolves `--scan-only` / `--skip-service` CLI tokens to module keys via alias index |

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
   type (`Ec2Instance` -> `"ec2"`, `LambdaFunction` -> `"lambda"`, etc.)
3. Iterates `self.modules`, calling `safe_scan(module, ctx)` for each
   selected module
4. `safe_scan()` catches any exception, logs a warning, and returns an
   empty `ServiceFindings` so one broken module never crashes the scan

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

`ALL_MODULES` is an ordered list of 28 instantiated adapter classes. The order
determines scan and report output order. Adding a new adapter requires only
instantiating it and appending to this list.

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

29 files (28 adapters + `__pycache__`). Each adapter is a thin class that:

1. Declares `key`, `cli_aliases`, `display_name`, `required_clients()`
2. In `scan()`, calls one or more analysis functions from the legacy service
   modules (e.g. `services/ec2.py`, `services/s3.py`)
3. Aggregates recommendations into `ServiceFindings` with named `SourceBlock`
   entries

#### Adapter Categories by Savings Strategy

**Flat-rate adapters (12)** -- Assign a fixed dollar savings per recommendation:

| Adapter | Key | Flat Rate |
|---------|-----|-----------|
| `lightsail.py` | `lightsail` | $12/rec |
| `redshift.py` | `redshift` | varies |
| `dms.py` | `dms` | varies |
| `quicksight.py` | `quicksight` | varies |
| `apprunner.py` | `apprunner` | varies |
| `transfer.py` | `transfer` | varies |
| `msk.py` | `msk` | varies |
| `workspaces.py` | `workspaces` | varies |
| `mediastore.py` | `mediastore` | varies |
| `glue.py` | `glue` | varies |
| `athena.py` | `athena` | varies |
| `batch.py` | `batch` | varies |

**Parse-rate adapters (6)** -- Extract dollar amounts from recommendation text
using regex or keyword matching:

| Adapter | Key | Parse Method |
|---------|-----|-------------|
| `cloudfront.py` | `cloudfront` | Fixed $25/rec |
| `api_gateway.py` | `api_gateway` | Keyword-based |
| `step_functions.py` | `step_functions` | Keyword-based |
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
| `network.py` | `network` | Elastic IP + NAT Gateway + VPC Endpoints + Load Balancer + Auto Scaling |
| `monitoring.py` | `monitoring` | CloudWatch + CloudTrail + Backup + Route53 |

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

### Test Suite (575 lines across 5 files)

| File | Lines | Tests | Purpose |
|------|-------|-------|---------|
| `test_orchestrator.py` | 39 | 1 | ScanOrchestrator end-to-end with stubbed AWS |
| `test_result_builder.py` | 80 | 6 | ScanResultBuilder serialization correctness |
| `test_regression_snapshot.py` | 167 | 17 | Golden JSON snapshot comparison |
| `test_reporter_snapshots.py` | 207 | 4 | Golden HTML snapshot comparison |
| `test_offline_scan.py` | 82 | 1 | Full offline scan with moto mock |
| `conftest.py` | 283 | -- | Fixtures, normalizers, stubbed_aws |

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
resource type. Complex adapters (EC2, EBS, RDS, Lambda) read from this cache
rather than making separate API calls, reducing API throttling risk.

### Extras Spreading

Both `ServiceFindings.extras` and `SourceBlock.extras` are `Mapping[str, Any]`
that get spread into the serialized output dict. This allows adapters to
inject service-specific data (e.g., `instance_count`, `service_counts`,
`bucket_count`) without modifying the core contracts.

### Global Service Routing

`ClientRegistry` automatically routes global services (Route53, CloudFront,
IAM, Cost Optimization Hub, etc.) to `us-east-1`. Adapters don't need to
know which services are global -- they just call `ctx.client("cloudfront")`.

---

## File Map

```
Cost_OptV1_dev/
  cli.py                          (91)   CLI entry point
  cost_optimizer.py               (134)  Thin orchestration shell
  html_report_generator.py        (2454) HTML report generation
  reporter_phase_a.py             (424)  Phase A: grouped services
  reporter_phase_b.py             (1502) Phase B: source handlers
  requirements.txt                       boto3, botocore, python-dateutil
  core/
    __init__.py
    contracts.py                  (109)  Protocol + dataclasses
    scan_context.py               (50)   ScanContext
    scan_orchestrator.py          (67)   ScanOrchestrator + safe_scan
    result_builder.py             (59)   ScanResultBuilder
    client_registry.py            (54)   ClientRegistry
    session.py                    (42)   AwsSessionFactory
    filtering.py                  (58)   resolve_cli_keys
  services/
    __init__.py                   (63)   ALL_MODULES registry (28 adapters)
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
    conftest.py                   (283)  Fixtures, normalizers, stubbed_aws
    test_orchestrator.py          (39)   Orchestrator test
    test_result_builder.py        (80)   Serialization tests
    test_regression_snapshot.py   (167)  Golden JSON snapshots
    test_reporter_snapshots.py    (207)  Golden HTML snapshots
    test_offline_scan.py          (82)   Full offline scan
    fixtures/                            Golden files
    aws_stubs/                           Pre-built stub responses
```
