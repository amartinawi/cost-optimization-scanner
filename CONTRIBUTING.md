# Contributing to AWS Cost Optimization Scanner

We welcome contributions. This guide covers the v3.x modular architecture with 36 ServiceModule adapters (AWS Cost Optimization Hub findings are routed per-service via `ctx.cost_hub_splits` rather than living in a dedicated adapter).

## Getting Started

### Prerequisites

- Python 3.8+
- boto3
- pytest, moto, freezegun (for testing)

### Development Setup

```bash
git clone https://github.com/amartinawi/cost-optimization-scanner.git
cd cost-optimization-scanner
pip install -r requirements.txt

# Run tests (offline — no AWS credentials needed)
pytest tests/ -v
```

## How to Contribute

### Reporting Issues

- Use GitHub Issues
- Include AWS region, Python version, and error output
- Check existing issues first

### Code Contributions

- Fork the repository
- Create a feature branch (`git checkout -b feature/new-service`)
- Follow coding standards below
- Run the regression gate before pushing
- Submit a pull request

## Project Structure

```
cli.py                          CLI entry point (argparse)
cost_optimizer.py               Thin orchestration shell (134 lines)
core/
  contracts.py                  ServiceModule Protocol + dataclasses
  scan_orchestrator.py          Iterates ALL_MODULES via safe_scan
  scan_context.py               Region, clients, pricing, warnings
  result_builder.py             ServiceFindings -> JSON dict
  client_registry.py            Caching boto3 client factory
  session.py                    AWS session with adaptive retry
  pricing_engine.py             AWS Pricing API with in-memory cache
  filtering.py                  --scan-only / --skip-service resolver
services/
  __init__.py                   ALL_MODULES registry (36 instances)
  _base.py                      BaseServiceModule default implementations
  _savings.py                   parse_dollar_savings helper
  advisor.py                    Cost Hub + Compute Optimizer utilities
  adapters/                     36 ServiceModule adapter files
html_report_generator.py        HTML report generation
reporter_phase_a.py             Descriptor-driven grouped rendering
reporter_phase_b.py             Function registry for source handlers
tests/                          pytest suite (moto + stubs)
```

### Key Files

| File | Purpose |
|------|---------|
| `core/contracts.py` | `ServiceModule` Protocol, `ServiceFindings`, `SourceBlock` dataclasses |
| `services/_base.py` | `BaseServiceModule` — default Protocol implementation |
| `services/__init__.py` | `ALL_MODULES` — append-only registry of adapter instances |
| `services/_savings.py` | `parse_dollar_savings()` — shared utility for dollar parsing |
| `core/pricing_engine.py` | `PricingEngine` — live AWS Pricing API with 12 methods |
| `core/scan_orchestrator.py` | `ScanOrchestrator` — iterates modules, calls `safe_scan` |

## Adding a New Service Adapter

No changes to `core/` are required. The adapter is automatically available via `--scan-only` and included in full scans.

### Step 1: Create the adapter

Create `services/adapters/<service>.py`:

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
        client = ctx.clients["newservice"]
        recommendations = []

        try:
            paginator = client.get_paginator("list_resources")
            for page in paginator.paginate():
                for resource in page.get("Resources", []):
                    # Analyze resource, build recommendation dicts
                    pass
        except Exception as exc:
            ctx.warnings.append(str(exc))

        return ServiceFindings(
            service_name=self.display_name,
            total_recommendations=len(recommendations),
            total_monthly_savings=0.0,
            sources={
                "resource_checks": SourceBlock(
                    count=len(recommendations),
                    recommendations=tuple(recommendations),
                ),
            },
        )
```

### Step 2: Register in ALL_MODULES

Edit `services/__init__.py` — add the import and append the instance:

```python
from services.adapters.newservice import NewServiceModule

ALL_MODULES: list[Any] = [
    # ... existing modules ...
    NewServiceModule(),  # append at end
]
```

### Step 3: Test

```bash
pytest tests/ -v
pytest tests/test_regression_snapshot.py -v
pytest tests/test_reporter_snapshots.py -v
```

## Coding Standards

### Style

- PEP 8 with type annotations on all function signatures
- Google-style docstrings
- ATX headers (`#`) in Markdown — no emoji in headers or code
- `black` for formatting, `isort` for imports, `ruff` for linting

### AWS Integration

- Always use pagination for list operations
- Handle `ClientError` gracefully — use `ctx.warnings.append()` or `ctx.permission_issues.append()`
- Use `ctx.clients[<name>]` from `ClientRegistry` (never create boto3 clients directly)
- Access pricing via `ctx.pricing_engine` — never hardcode rates

### Cost Calculations

- Use `ctx.pricing_engine` methods for live pricing
- Apply `ctx.pricing_multiplier` for regional adjustments
- Fall back to `0.0` on pricing API failure — scans never crash due to pricing

### Error Handling

```python
from botocore.exceptions import ClientError

try:
    response = client.describe_resources()
except ClientError as exc:
    error_code = exc.response["Error"]["Code"]
    if error_code in ("UnauthorizedOperation", "AccessDenied"):
        ctx.permission_issues.append(f"Missing permission: {error_code}")
    else:
        ctx.warnings.append(f"Could not analyze service: {exc}")
except Exception as exc:
    ctx.warnings.append(f"Unexpected error: {exc}")
```

## Testing

All tests run offline using `moto` mock AWS and pre-built stub responses in `tests/aws_stubs/`. No AWS credentials are needed.

### Running Tests

```bash
# Full test suite
pytest tests/ -v

# Golden-file regression gate (required before merge)
pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v
```

### Test Files

| File | Purpose |
|------|---------|
| `tests/test_offline_scan.py` | End-to-end scan with moto mocks |
| `tests/test_orchestrator.py` | ScanOrchestrator unit tests |
| `tests/test_pricing_engine.py` | PricingEngine unit tests |
| `tests/test_result_builder.py` | ResultBuilder serialization tests |
| `tests/test_regression_snapshot.py` | Golden-file regression gate |
| `tests/test_reporter_snapshots.py` | HTML reporter regression gate |
| `tests/conftest.py` | Shared fixtures |
| `tests/aws_stubs/` | Pre-built AWS API responses |

### Writing Tests

- Use `moto` mocks for AWS API calls
- Use stubs from `tests/aws_stubs/` for consistent responses
- Follow existing test patterns in the suite

## Pull Request Checklist

Before submitting:

- [ ] Code follows PEP 8 with type annotations
- [ ] Google-style docstrings on all public methods
- [ ] Error handling uses `ctx.warnings` / `ctx.permission_issues`
- [ ] No hardcoded pricing rates — uses `ctx.pricing_engine`
- [ ] New adapters registered in `services/__init__.py` `ALL_MODULES`
- [ ] `pytest tests/ -v` passes
- [ ] `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v` passes
- [ ] No emoji in code or Markdown headers

### PR Description Template

```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New service adapter
- [ ] New optimization check
- [ ] Documentation update
- [ ] Performance improvement

## Testing
- [ ] pytest tests/ -v passes
- [ ] Regression gate passes
- [ ] New tests added (if applicable)
```

## Resources

- [ARCHITECTURE.md](ARCHITECTURE.md) — Technical architecture and design decisions
- [CHANGELOG.md](CHANGELOG.md) — Version history
- [AWS Pricing](https://aws.amazon.com/pricing/)
- [boto3 Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)
