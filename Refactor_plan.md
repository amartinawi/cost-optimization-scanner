# AWS_COST_OPTIMIZATION — Refactoring Execution Plan

**Target architecture:** Service Modules
**Source:** Two-file monolith (`cost_optimizer.py` 8,710 LOC + `html_report_generator.py` 4,123 LOC)
**Constraint:** Zero behavioral regression. Output JSON and HTML must be byte-identical (after timestamp normalization AND ±$0.01 numeric-normalization, see G4) before vs. after refactor.
**Mode:** Incremental, test-gated, reversible at every step.
**Plan version:** v1.2 (validated by python-reviewer agent across two passes — see "Validation History" at bottom)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Pre-Flight Checklist (Read Before Starting)](#2-pre-flight-checklist-read-before-starting)
3. [Critical Path](#3-critical-path)
4. [Risk Register](#4-risk-register)
5. [Target Folder Structure](#5-target-folder-structure)
6. [Module Contract](#6-module-contract)
7. [Execution Plan — Group A: Safety Net](#7-execution-plan--group-a-safety-net)
8. [Execution Plan — Group B: Core Infrastructure](#8-execution-plan--group-b-core-infrastructure)
9. [Execution Plan — Group C: Orchestrator](#9-execution-plan--group-c-orchestrator)
10. [Execution Plan — Group D: Service Module Migration](#10-execution-plan--group-d-service-module-migration)
11. [Execution Plan — Group E: Reporter Refactor](#11-execution-plan--group-e-reporter-refactor)
12. [Execution Plan — Group F: Entry Point + Cleanup](#12-execution-plan--group-f-entry-point--cleanup)
13. [Execution Plan — Group G: Test Coverage](#13-execution-plan--group-g-test-coverage)
14. [Execution Plan — Group H: Optional Follow-Ups](#14-execution-plan--group-h-optional-follow-ups)
15. [Validation Gates](#15-validation-gates)
16. [Rollback Strategy](#16-rollback-strategy)
17. [Definition of Done](#17-definition-of-done)

---

## 1. Executive Summary

### What we are doing
Refactor a two-file Python monolith that scans AWS accounts (30 CLI services, 28 internal service keys, ~45 boto3 clients, 220+ checks) and emits a styled HTML cost report — into a **Service Modules architecture** where each AWS service is an isolated, registered module behind a uniform contract.

### Why
- Current `CostOptimizer` is a 92-method God class.
- Reporter hardcodes per-service rendering ladders — every new service requires editing both files.
- Three services (`lightsail`, `dms`, `glue`) already pass diverging `sources` shape — silent rendering bugs.
- No tests. No CI. No way to add a service without touching two 4–9k-line files.

### Scope
- **56 tasks** across 8 groups (A → H).
- **~23 critical-path tasks** gating parity (literal sequential chain; ~37 including upstream parallel dependents).
- **~28 service modules + 2 sub-modules** (`ami`, `snapshots`) split out for reporter tab parity.
- **2 monolith files** to delete (only at the end).

### Non-goals (explicitly out of scope for this refactor)
- No new features.
- No new AWS services.
- No pricing-table refresh.
- No fix of latent bugs unless **explicitly listed** (`self.config` collision, duplicate `__main__`). Other latent bugs are documented for follow-up tasks (T-700+).

---

## 2. Pre-Flight Checklist (Read Before Starting)

Do **not** start Group B until every box below is ticked.

- [ ] **T-000** completed: `git init` run; current monolith committed; tag `v2.5.9-baseline` placed.
- [ ] **T-099** completed: `pyproject.toml` exists; `ruff`, `mypy`, `pytest` configured; `requirements-dev.txt` includes `pytest`, `pytest-mock`, `moto[all]`, `freezegun`; Python pinned `>=3.11` (required for `tuple[str, ...]` and `X | Y` PEP 604 syntax used in §6 contract).
- [ ] Working branch created from baseline (`refactor/service-modules`).
- [ ] AWS test account identified for golden capture (read-only credentials, ideally a non-production account with realistic data).
- [ ] Region(s) chosen for golden capture documented (recommend at least: `us-east-1` + one non-US region to exercise `S3_REGIONAL_MULTIPLIERS`).
- [ ] Decision logged on each item below before T-300 starts:
  - [ ] **Hardcoded fallback savings** ($25/$40/$50/$75/$150/$200/$300): preserve verbatim **(recommended for parity)** or retire.
  - [ ] **`self.config` collision** (line 269 vs 346): preserve bug or fix. **Default: fix in T-103** (config name moves off `self`; collision impossible after refactor).
  - [ ] **`instance_id` NameError** in except blocks (line 422, 463): preserve or fix.
  - [ ] **Duplicate `if __name__ == "__main__"`** in `html_report_generator.py` (line 4059, 4072): remove now (T-407) or defer.
  - [ ] **`S3_REGIONAL_MULTIPLIERS` GLACIER key inconsistency**: preserve verbatim or normalize.
  - [ ] **Registry style**: explicit list (recommended) or decorator-based.
  - [ ] **`print()` vs `logging`**: preserve `print` for byte-equal stdout parity **(recommended)** or migrate to `logging.getLogger(__name__)` post-refactor.
  - [ ] **Mock strategy**: `moto[all]` primary, `botocore.stub.Stubber` fallback for services moto doesn't cover (Cost Optimization Hub, Compute Optimizer). **Decided in T-003.**
  - [ ] **Frozen-time clock**: `freezegun.freeze_time(<T-001 capture timestamp>)` applied in T-002 harness so goldens stay valid as wall-clock advances.
- [ ] Baseline scan run captured: `python3 cost_optimizer.py <region>` → keep raw stdout, JSON, and HTML side-by-side as reference.

---

## 3. Critical Path

These tasks must run sequentially. Anything not on this chain can run in parallel.

```
T-000 (git init + baseline tag)
  → T-099 (tooling baseline — pyproject.toml, ruff/mypy/pytest, moto/freezegun/pytest-mock)
  → T-001 (capture goldens, freeze-time)
  → T-002 (snapshot harness)
  → T-003 (moto primary, Stubber fallback)
  → T-100 (core skeleton, mypy --strict on contracts)
  → T-103 (session/clients/context — full warn/permission schema)
  → T-104 (warning tracking)
  → T-200 (orchestrator stub + safe_scan decorator)
  → T-201 (result builder + total_count + recursive asdict)
  → T-321a (extract get_ami_checks as free function)
  → T-325a (extract k8s_detection helpers to core/)
  → T-315 (cost_hub_categorizer — feeds T-323)
  → T-323 (EC2 — XL)
  → T-322 (EBS — L)
  → T-320 (S3 — L)
  → T-326 (network — XL)
  → T-325 (containers — L)
  → T-324 (file_systems — L)
  → T-405 (reporter grouping — XL)
  → T-404 (reporter stats — L)
  → T-500 (cli.py)
  → T-502 (CI)
```

**Parallelizable streams:**
- **Stream P1:** T-101 (pricing) + T-102 (savings parser) — after T-100.
- **Stream P2:** T-300…T-318 (all low/medium-risk modules) — after T-201.
- **Stream P3:** T-400, T-401, T-402, T-403, T-407 (reporter prep) — after T-100.
- **Stream P4:** T-501 (docs), T-600…T-602 (test coverage) — anytime after their underlying tasks.

---

## 4. Risk Register

| # | Risk | Severity | Mitigation | Owning task |
|---|------|----------|------------|-------------|
| R1 | No tests, no goldens — "no regression" constraint unverifiable | **HIGH** | Group A is non-negotiable Day-1 work. Block all refactor tasks on T-002 green. | T-001, T-002, T-003 |
| R2 | Reporter hard-coupled to scanner dict shape — `volume_counts`, `instance_counts`, `service_counts`, `top_cost_buckets` etc. hardcoded in HTML generator. 3 services already break canonical shape. | **HIGH** | Snapshot-diff the **HTML** on every module migration, not just JSON. T-405/T-406 use HTML diff as gate. | T-405, T-406, every T-3xx |
| R3 | Hardcoded fallback savings drive headline `total_monthly_savings`. ±1 recommendation count = ±$25–$300 swing. | **HIGH** | Preserve fallback formulas verbatim during refactor. Refactor recommendation generation **only after** parity is locked. | All T-3xx (especially T-323, T-322, T-320) |
| R4 | `self.config` collision (line 269 vs 346) — `retry_config` reference lost for any client created after line 346. | MEDIUM | Fix in T-103 by moving retry config onto `ScanContext`. Document that pre-refactor behavior had silent retry-config drop for late-init clients. | T-103 |
| R5 | `S3_REGIONAL_MULTIPLIERS` uses `GLACIER` key in some entries, `GLACIER_FLEXIBLE_RETRIEVAL` / `GLACIER_INSTANT_RETRIEVAL` in others. Lookups silently miss for newer entries. | MEDIUM | Preserve verbatim in T-101. Normalization is T-701 (post-refactor). | T-101, T-701 |
| R6 | `lightsail`, `dms`, `glue` pass raw `enhanced_*_checks.checks` dict instead of `{enhanced_checks:{count, recommendations}}`. | MEDIUM | New module contract normalizes this. Verify HTML output diffs are **expected** (not regressions) for these three. | T-307, T-308, T-309 |
| R7 | ~45 boto3 clients instantiated; some unused. Removal during refactor risks unintended side effects (e.g. permission probe). | MEDIUM | Keep all client instantiations in T-103 (no pruning). Pruning is T-703 (post-refactor). | T-103, T-703 |
| R8 | `total_services_scanned` calculation has quirky logic at lines 3999–4007. | MEDIUM | T-201 must replicate exactly. Add explicit unit test for this counter. | T-201 |
| R9 | **`add_warning` / `add_permission_issue` store structured dicts (`message, service, action, timestamp`), but a naive `ctx.warn(msg: str)` API drops fields and breaks JSON parity.** | **HIGH** | T-103/T-104 contract requires preserving full dict shape. See §6 + T-103 step 4. | T-103, T-104 |
| R10 | **`ServiceFindings` missing `total_count` field.** AMI summary count at `:4007` reads `total_count`, not `total_recommendations`. Without it AMI silently drops out of `total_services_scanned`. | **HIGH** | Add `total_count: int = 0` to `ServiceFindings`. AMI module populates it. | §6 contract, T-201 |
| R11 | Tier-5 ordering breaks dependent extractions: T-321 (AMI) reaches into EC2's still-monolithic `get_ami_checks`; T-325 (containers) and T-326 (network) both call `_is_kubernetes_managed_alb`; T-323 (EC2) consumes Cost Hub categorizer from T-315. | **HIGH** | New sub-tasks T-321a, T-325a inserted; T-315 promoted onto critical path before T-323. | T-315, T-321a, T-325a |
| R12 | Plan assumes git is available, but project is not a git repo today. Per-task / per-group rollback strategy is inert without it. | **HIGH** | Add T-000 — initialize git, baseline commit, tag `v2.5.9-baseline` — as the first task before T-001. | T-000 |
| R13 | `print()` with emojis used throughout source. Plan does not specify whether to keep for parity or migrate to `logging`. Inconsistent decision = drift across modules. | LOW | Pre-flight decision: **preserve `print` for parity**; logging migration deferred to post-refactor cleanup task. | Pre-flight |
| R14 | `concurrent.futures` imported but not visibly used in `scan_region`. If used elsewhere, `ScanContext._warnings` and `_permission_issues` need a `threading.Lock`. | LOW | T-103 to grep for `ThreadPoolExecutor` / `ProcessPoolExecutor`. If found: add lock. If not: remove the unused import in T-702 cleanup. | T-103, T-702 |

---

## 5. Target Folder Structure

```
Cost_OptV1_dev/
├── cli.py                              # entry point (replaces main())
├── core/
│   ├── __init__.py
│   ├── contracts.py                    # ServiceModule Protocol, ServiceFindings, SourceBlock
│   ├── session.py                      # AwsSessionFactory
│   ├── client_registry.py              # lazy boto3 client cache
│   ├── scan_context.py                 # ScanContext: region, account_id, fast_mode, warn(), permission_issue()
│   ├── pricing.py                      # tables + pure cost helpers
│   ├── savings.py                      # parse_savings_dollars, format_estimated_savings
│   ├── filtering.py                    # CLI-key → module-key resolution
│   ├── scan_orchestrator.py            # iterates registry
│   ├── result_builder.py               # composes scan_results dict (preserves JSON shape)
│   └── k8s_detection.py                # _is_kubernetes_managed_alb, _is_eks_nodegroup_asg
├── services/
│   ├── __init__.py                     # ALL_MODULES registry
│   ├── ec2.py
│   ├── ami.py
│   ├── ebs.py
│   ├── snapshots.py                    # split from EBS for tab parity
│   ├── rds.py
│   ├── s3.py
│   ├── lambda_.py
│   ├── dynamodb.py
│   ├── file_systems.py                 # EFS + FSx
│   ├── elasticache.py
│   ├── opensearch.py
│   ├── containers.py                   # ECS + EKS + ECR
│   ├── network.py                      # EIP + NAT + VPC endpoints + LB + Auto Scaling
│   ├── monitoring.py                   # CloudWatch + CloudTrail + Backup + Route53
│   ├── cloudfront.py
│   ├── api_gateway.py
│   ├── step_functions.py
│   ├── lightsail.py
│   ├── redshift.py
│   ├── dms.py
│   ├── quicksight.py
│   ├── apprunner.py
│   ├── transfer.py
│   ├── msk.py
│   ├── workspaces.py
│   ├── mediastore.py
│   ├── glue.py
│   ├── athena.py
│   └── batch.py
├── report/
│   ├── __init__.py
│   ├── builder.py                      # thin HTMLReportGenerator
│   ├── templates/
│   │   ├── base.html.j2
│   │   ├── summary.html.j2
│   │   ├── tabs.html.j2
│   │   ├── service_tab.html.j2         # generic — driven by module metadata
│   │   ├── snapshots.html.j2
│   │   └── amis.html.j2
│   ├── assets/
│   │   ├── report.css                  # extracted from _get_css()
│   │   └── report.js                   # extracted from _get_javascript()
│   └── grouping/
│       ├── __init__.py
│       └── strategies.py               # GroupBy enums, generic groupers
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── golden_scan_results.json
│   │   ├── golden_report.html
│   │   └── recorded_aws_responses/
│   ├── aws_stubs/                      # botocore Stubber recordings
│   ├── per_service/
│   │   └── test_<service>.py × 28
│   ├── test_module_contract.py
│   ├── test_orchestrator.py
│   ├── test_pricing.py
│   ├── test_savings_parser.py
│   ├── test_report_builder.py
│   └── test_regression_snapshot.py     # PRIMARY GATE
├── requirements.txt                    # adds: jinja2
├── docs/                               # update content; structure unchanged
└── (DELETED at end)
    ├── cost_optimizer.py
    └── html_report_generator.py
```

---

## 6. Module Contract

Every service module **MUST** implement this contract:

```python
# core/contracts.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, runtime_checkable
from types import MappingProxyType

CONTRACT_SCHEMA_VERSION = 1

# --- Warning / permission records: schema MUST match current JSON shape -------
@dataclass(frozen=True)
class WarningRecord:
    message: str
    service: str
    timestamp: str                                      # ISO-8601, UTC

@dataclass(frozen=True)
class PermissionIssueRecord:
    message: str
    service: str
    action: str | None
    timestamp: str

# --- Findings -----------------------------------------------------------------
@dataclass(frozen=True)
class SourceBlock:
    count: int
    recommendations: tuple[dict, ...]                   # tuple → enforces immutability

@dataclass(frozen=True)
class StatCardSpec:
    label: str
    source_path: str                                    # dotted path into findings.extras
    formatter: Literal["int", "currency", "percent", "size_gb"] = "int"

@dataclass(frozen=True)
class GroupingSpec:
    by: Literal["check_category", "resource_type", "source"]
    label_path: str | None = None

@dataclass(frozen=True)
class Group:
    label: str
    recommendations: tuple[dict, ...]
    aggregated_savings: float = 0.0

@dataclass(frozen=True)
class ServiceFindings:
    service_name: str
    total_recommendations: int
    total_monthly_savings: float
    sources: dict[str, SourceBlock]                     # NOTE: frozen=True does NOT freeze dict contents — see Trap below
    optimization_descriptions: dict[str, dict] | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    total_count: int = 0                                # AMI parity field (cost_optimizer.py:3893,4007)
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def freeze(self) -> "ServiceFindings":
        """Wrap mutable dict fields in MappingProxyType to enforce read-only at boundaries."""
        return ServiceFindings(
            service_name=self.service_name,
            total_recommendations=self.total_recommendations,
            total_monthly_savings=self.total_monthly_savings,
            sources=MappingProxyType(dict(self.sources)),
            optimization_descriptions=(
                MappingProxyType(dict(self.optimization_descriptions))
                if self.optimization_descriptions is not None else None
            ),
            extras=MappingProxyType(dict(self.extras)),
            total_count=self.total_count,
            schema_version=self.schema_version,
        )

# --- Module Protocol ----------------------------------------------------------
@runtime_checkable
class ServiceModule(Protocol):
    key: str                                            # MUST match scan_results['services'] top-level key
    cli_aliases: tuple[str, ...]
    display_name: str
    stat_cards: tuple[StatCardSpec, ...]
    grouping: GroupingSpec | None
    requires_cloudwatch: bool                           # orchestrator skips when ctx.fast_mode and this is True for low-impact modules
    reads_fast_mode: bool                               # module reads ctx.fast_mode to alter behavior (e.g. S3)

    def required_clients(self) -> tuple[str, ...]: ...
    def scan(self, ctx: "ScanContext") -> ServiceFindings: ...

    # Optional. If absent → orchestrator uses GroupingSpec; else this overrides.
    custom_grouping: Callable[[ServiceFindings], list[Group]] | None
```

**Rules every module MUST follow:**
1. `scan()` MUST NOT raise. The orchestrator wraps it in a `safe_scan(module, ctx)` decorator that catches any exception, logs to `ctx.warn`, and returns an empty `ServiceFindings`. **Authors are not expected to write try/except in `scan()`.** See T-200.
2. `key` MUST match the top-level key under `scan_results['services']` in current JSON output.
3. Recommendation dicts inside `SourceBlock.recommendations` MUST keep today's keys verbatim (`Resource`, `EstimatedSavings`, `Action`, etc.) — the reporter reads them directly.
4. `extras` carries today's per-service counts (e.g. `volume_counts`, `instance_counts`, `efs_counts`, `bucket_counts`, `fast_mode_warning`). `ScanResultBuilder` splats `extras` back to top-level so JSON shape is unchanged.
5. No module imports another module. All shared logic lives in `core/`.
6. **AMI module** MUST populate `total_count` (parity with `cost_optimizer.py:3893`); other modules leave it at default `0`.

### Trap: `frozen=True` does NOT freeze dict contents

A `frozen=True` dataclass blocks reassigning the field (`findings.sources = {}`) but does not block mutation of the dict referenced by the field (`findings.sources["x"] = ...`). For genuine read-only semantics at the boundary between modules and the result builder, call `findings.freeze()` to wrap mutable fields in `MappingProxyType`. The orchestrator does this before passing findings to `ScanResultBuilder`. Module authors do not need to call it.

### Per-service `TypedDict`s for `extras` (recommended, deferred)

`extras: dict[str, Any]` erases types. Recommended enrichment (defer to T-600 batch): each service module defines its own `TypedDict` for its extras (e.g. `EBSExtras { volume_counts: VolumeCountsTD; ... }`) and the module's `scan()` returns a `ServiceFindings` whose `extras` is constructed from that TypedDict. Catch field renames at migration time via `mypy --strict`. Not blocking for parity.

### Recommendation dict shape (de-facto schema)

Today's recommendation dicts have fluctuating key sets across services, but the reporter reads these consistently:
- `Resource` / `ResourceId` / `InstanceId` / `VolumeId` / `BucketName` / `FunctionName` / etc. — resource identity (varies by service).
- `Recommendation` — short action string.
- `EstimatedSavings` — string, free-form (e.g. `"$25/month"`, `"Up to $30/month"`, `"Estimated $X (max estimate)"`). Parsed by `core/savings.py` (T-102).
- `CheckCategory` — used by reporter for grouping (e.g. `"snapshot"`, `"Volume Type Optimization"`).
- Optional: `Note`, `AgeDays`, `SizeGB`, `EstimatedMonthlyCost`.

T-600 must include a "schema linter" test that asserts every emitted recommendation dict carries at least: one resource-identity key, `Recommendation`, `EstimatedSavings`, `CheckCategory`.

---

## 7. Execution Plan — Group A: Safety Net

> **Gate:** No Group B work begins until T-000 → T-003 are all green.

### T-000 — Initialize git baseline
**Risk:** LOW · **Complexity:** S

The current project directory is **not** a git repository. The plan's per-task and per-group rollback strategies (§16) cannot work without one.

**Steps:**
1. ```bash
   cd /Users/amartinawi/Desktop/Cost_OptV1_dev
   git init
   git add -A
   git commit -m "chore: import v2.5.9 monolith baseline"
   git tag v2.5.9-baseline
   git checkout -b refactor/service-modules
   ```
2. Add a minimal `.gitignore` covering: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.venv/`, `*.html` at repo root (so test runs don't dirty the tree), `cost_optimization_scan_*.json`.
3. Verify: `git log --oneline -1` shows the baseline commit; `git tag` shows `v2.5.9-baseline`.

**Acceptance:** Repo is a git repo. Tag `v2.5.9-baseline` resolves. Branch `refactor/service-modules` exists. `.gitignore` in place.

---

### T-099 — Tooling baseline
**Risk:** LOW · **Complexity:** S · **Deps:** T-000

The plan's contract (§6) uses PEP 604 `X | Y` and PEP 585 `tuple[X, ...]` — both require Python ≥ 3.11 (3.10 minimum, 3.11 for full ergonomics in `Literal`/`Protocol`). Without a pinned interpreter and configured static-checker, the contract claims are unverifiable.

**Steps:**
1. Create `pyproject.toml` with:
   ```toml
   [project]
   name = "aws-cost-optimization"
   requires-python = ">=3.11"

   [tool.ruff]
   line-length = 120
   target-version = "py311"

   [tool.mypy]
   python_version = "3.11"
   strict = true
   files = ["core", "services", "report", "cli.py"]

   [tool.pytest.ini_options]
   testpaths = ["tests"]
   addopts = "-q --strict-markers"
   ```
2. Create `requirements-dev.txt`:
   ```
   pytest>=8.0
   pytest-mock>=3.12
   moto[all]>=5.0
   freezegun>=1.4
   ruff>=0.5
   mypy>=1.10
   ```
3. Pin runtime Python: create `.python-version` with `3.11` (or higher available locally).
4. Verify:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt -r requirements-dev.txt
   python -c "import sys; assert sys.version_info >= (3,11)"
   ruff check . && echo "ruff OK"
   mypy --version && pytest --version
   ```

**Acceptance:** `python --version` ≥ 3.11; `ruff`, `mypy`, `pytest`, `moto`, `freezegun` importable; `pyproject.toml` committed.

---

### T-001 — Capture regression goldens
**Risk:** LOW · **Complexity:** S · **Deps:** T-099

**Steps:**
1. Run on a stable test region with realistic data:
   ```bash
   python3 cost_optimizer.py us-east-1 --profile <test-profile> > baseline.log 2>&1
   ```
2. Capture both output files unchanged.
3. Record the wall-clock timestamp at capture (the `scan_time` value inside the JSON). Write it to `tests/fixtures/CAPTURE_TIMESTAMP.txt` — T-002 freezes the harness clock to this exact instant.
4. Move them to `tests/fixtures/`:
   - `cost_optimization_scan_us-east-1_<ts>.json` → `tests/fixtures/golden_scan_results.json`
   - `<profile>_us-east-1.html` → `tests/fixtures/golden_report.html`
5. Record SHA-256 of both files in `tests/fixtures/CHECKSUMS.txt`.
6. Repeat for a non-US region (e.g. `eu-central-1`) to exercise `S3_REGIONAL_MULTIPLIERS`. Each region gets its own captured timestamp.
7. Commit fixtures.

**Acceptance:** Two JSON + two HTML fixtures exist; checksums recorded; capture timestamps recorded; commit tagged `goldens-v1`.

---

### T-002 — Build snapshot regression harness
**Risk:** LOW · **Complexity:** M · **Deps:** T-001

**Steps:**
1. Create `tests/conftest.py` with fixtures for:
   - Loading `golden_scan_results.json`.
   - Loading `golden_report.html`.
   - Reading `CAPTURE_TIMESTAMP.txt` and applying `freezegun.freeze_time(timestamp)` for the duration of every test (session-scoped autouse fixture).
   - **Normalizers** applied before byte comparison:
     - Replace `scan_time` value with `<NORMALIZED>`.
     - Replace `account_id` value with `<NORMALIZED>`.
     - Replace output-filename timestamps with `<NORMALIZED>`.
     - **Numeric tolerance** (gate G4): all `\$[\d,]+\.\d{2}` values are rounded to nearest cent then re-formatted before comparison. ±$0.01 tolerance per cell. Documented as `_normalize_currency()` helper.
2. Create `tests/test_regression_snapshot.py`:
   - Test 1: Run scan against recorded AWS responses (T-003) → byte-equal JSON to golden after normalization.
   - Test 2: Feed golden JSON into report generator → byte-equal HTML to golden after normalization (currency-tolerant).
3. Wire into `pytest`:
   ```bash
   pytest tests/test_regression_snapshot.py -v
   ```

**Acceptance:** `pytest tests/test_regression_snapshot.py` exits 0 against the unmodified monolith. Frozen-clock means re-runs across days produce identical output.

---

### T-003 — Mock the AWS surface
**Risk:** MEDIUM · **Complexity:** L · **Deps:** T-002

**Strategy:** `moto[all]` is the **primary** mock provider. It covers most services here declaratively (state-based, not call-order-bound), avoiding the brittle ordered-replay problem of `Stubber`. Use `botocore.stub.Stubber` only as fallback for services moto cannot cover (Cost Optimization Hub, Compute Optimizer, possibly QuickSight `describe_spice_capacity`).

**Steps:**
1. Identify moto coverage — services in T-001 vs moto's supported list. Write `tests/MOCK_COVERAGE.md` documenting which services use moto vs Stubber.
2. For services moto covers: write `tests/fixtures/aws_state/<region>.py` that seeds moto's mock backend with the resources observed in T-001 (instances, volumes, buckets, etc.).
3. For services moto does NOT cover (Cost Optimization Hub, Compute Optimizer): record raw responses during T-001 run via a `botocore` event hook (`before-send`) and save under `tests/fixtures/recorded_aws_responses/<client>/<api_call>.json`. Build a `Stubber`-based fallback in `tests/aws_stubs/`.
4. Provide `tests/conftest.py` fixture `stubbed_aws` that combines moto's `mock_aws()` decorator with the Stubber fallback for uncovered clients. It returns a `boto3.Session` with all clients pre-mocked.
5. Re-run T-002 against `stubbed_aws` — offline, no AWS credentials needed.

**Acceptance:** `pytest tests/test_regression_snapshot.py` passes with `unset AWS_PROFILE; unset AWS_*` and no network access. Moto vs Stubber split documented in `tests/MOCK_COVERAGE.md`.

---

## 8. Execution Plan — Group B: Core Infrastructure

> **Gate:** T-002 must pass green after every task in this group.

### T-100 — `core/` package skeleton
**Risk:** LOW · **Complexity:** S · **Deps:** T-003

**Steps:**
1. Create `core/__init__.py` (empty).
2. Create `core/contracts.py` with the full Protocol + dataclasses from [§6](#6-module-contract). Including:
   - `WarningRecord`, `PermissionIssueRecord` (preserve full schema — see R9).
   - `SourceBlock`, `StatCardSpec`, `GroupingSpec`, `Group`, `ServiceFindings` (with `total_count` and `schema_version`).
   - `ServiceModule` Protocol with `requires_cloudwatch`, `reads_fast_mode`, typed `custom_grouping`.
   - `CONTRACT_SCHEMA_VERSION = 1` constant.
3. Run `python -c "from core import contracts; print(contracts.CONTRACT_SCHEMA_VERSION)"` to verify import.
4. Run `mypy --strict core/contracts.py` — must exit 0 (no errors, no `Any` leakage).
5. Run `ruff check core/` — must exit 0.
6. Run T-002. Must still pass (no behavior change).

**Acceptance:** Imports clean. `mypy --strict core/contracts.py` exits 0. `ruff check core/` exits 0. T-002 green.

---

### T-101 — Extract pricing tables and pure cost helpers
**Risk:** MEDIUM · **Complexity:** M · **Deps:** T-100

**Steps:**
1. Create `core/pricing.py`. Move verbatim:
   - `S3_STORAGE_COSTS` (line 88)
   - `S3_REGIONAL_MULTIPLIERS` (line 111) — **preserve `GLACIER` vs `GLACIER_FLEXIBLE_RETRIEVAL` inconsistency exactly**
   - `REGIONAL_PRICING` (line 158)
   - `OLD_SNAPSHOT_DAYS`, `OLD_AMI_DAYS`, `LARGE_TABLE_SIZE_GB`, `LOW_CPU_THRESHOLD`, etc. (lines 76–153)
   - `get_regional_pricing_multiplier` (already classmethod — convert to module function)
   - Pure helpers: `_estimate_volume_cost`, `calculate_s3_storage_cost`, `_estimate_s3_bucket_cost`, `_estimate_efs_cost`, `_estimate_fsx_cost`, `_estimate_file_cache_cost`, `get_lightsail_bundle_cost`, `is_static_website_bucket`
2. In `cost_optimizer.py`, replace each removed item with thin re-export delegation:
   ```python
   from core.pricing import REGIONAL_PRICING  # noqa: re-export for legacy callers
   ```
3. Keep class attributes as proxies:
   ```python
   class CostOptimizer:
       S3_STORAGE_COSTS = pricing.S3_STORAGE_COSTS  # for any subclass/instance access
   ```
4. Run T-002 → must pass.

**Acceptance:** T-002 green. `import core.pricing` works. No duplicated constants between `core/pricing.py` and `cost_optimizer.py`.

---

### T-102 — Extract savings parser & formatter
**Risk:** MEDIUM · **Complexity:** M · **Deps:** T-101

**Steps:**
1. Create `core/savings.py` with two pure functions:
   - `parse_savings_dollars(s: str) -> float` — handles all observed formats: `"$25"`, `"Up to $30"`, `"$25/month"`, `"Estimated $X (max estimate)"`, `"$X-$Y"`, etc.
   - `format_estimated_savings(amount: float) -> str` — mirrors `_format_estimated_savings`.
2. Audit `cost_optimizer.py` for every inline savings-parsing pattern. Likely candidates:
   - `re.search(r'\$(\d+\.?\d*)', …)`
   - `.replace('$','').split('/')[0]`
   - `.replace('Up to ', '')`
3. Replace each with `parse_savings_dollars(...)`.
4. Add `tests/test_savings_parser.py` with at least 10 cases covering observed formats.
5. Run T-002.

**Acceptance:** T-002 green. `pytest tests/test_savings_parser.py` green. Zero remaining inline `$` regex parsers in `cost_optimizer.py`.

---

### T-103 — `AwsSessionFactory` + `ClientRegistry` + `ScanContext`
**Risk:** **HIGH** · **Complexity:** L · **Deps:** T-100

**Steps:**
1. Create `core/session.py`:
   ```python
   class AwsSessionFactory:
       def __init__(self, region: str, profile: str | None): ...
       def session(self) -> boto3.Session: ...
       def account_id(self) -> str: ...   # via STS GetCallerIdentity
       def retry_config(self) -> Config: ...
   ```
2. Create `core/client_registry.py`:
   ```python
   class ClientRegistry:
       # Forced us-east-1 list — VERIFIED against cost_optimizer.py:315–345
       _GLOBAL_SERVICES: frozenset[str] = frozenset({
           "route53", "cloudfront", "iam", "support",
           "pricing", "ce", "cur", "budgets",
           "cost-optimization-hub", "organizations",   # organizations was missing in v1.0
       })
       # Aliased clients — same backend, different attribute name
       _ALIASES: dict[str, str] = {"trustedadvisor": "support"}

       def __init__(self, session_factory, retry_config): ...
       def client(self, name: str, region: str | None = None):
           name = self._ALIASES.get(name, name)            # de-dup support/trustedadvisor
           effective_region = "us-east-1" if name in self._GLOBAL_SERVICES else (region or self.default_region)
           return self._cache.setdefault((name, effective_region), self._build(name, effective_region))
   ```
3. Create `core/scan_context.py` — preserve full warning/permission dict schema (R9 fix):
   ```python
   from datetime import datetime, timezone

   @dataclass
   class ScanContext:
       region: str
       account_id: str
       fast_mode: bool
       clients: ClientRegistry
       _warnings: list[WarningRecord] = field(default_factory=list)
       _permission_issues: list[PermissionIssueRecord] = field(default_factory=list)

       def warn(self, message: str, service: str = "") -> None:
           rec = WarningRecord(
               message=message, service=service,
               timestamp=datetime.now(timezone.utc).isoformat(),
           )
           self._warnings.append(rec)
           print(f"⚠️ Warning: {message}")           # preserve stdout parity (cost_optimizer.py:204)

       def permission_issue(self, message: str, service: str, action: str | None = None) -> None:
           rec = PermissionIssueRecord(
               message=message, service=service, action=action,
               timestamp=datetime.now(timezone.utc).isoformat(),
           )
           self._permission_issues.append(rec)
           print(f"🔒 Permission Issue ({service}): {message}")     # preserve stdout (cost_optimizer.py:215)

       def client(self, name: str, region: str | None = None):
           return self.clients.client(name, region)
   ```
4. **Fix `self.config` collision:** `retry_config` lives on `AwsSessionFactory.retry_config()`. The `config` boto3 client is accessed via `ctx.client('config')`. The two never share a name.
5. **Migration shim** so monolith methods keep working through Group D — add a `_ClientShims` mixin on `CostOptimizer`:
   ```python
   class _ClientShims:
       """Temporary: routes self.<aws_client_name> through ctx.client(). Deleted at T-500."""
       def _client(self, name: str): return self._ctx.client(name)

   # In CostOptimizer body:
   ec2 = property(lambda self: self._client("ec2"))
   rds = property(lambda self: self._client("rds"))
   # … one property per name in cost_optimizer.py:286–357 (~45 properties)
   ```
6. Refactor `CostOptimizer.__init__` (lines 217–357) to:
   - Accept (or build internally) a `ScanContext`.
   - Replace 45 `self.<svc> = self.session.client(...)` lines with the property shims above.
   - Replace `self.add_warning` / `self.add_permission_issue` body with calls to `ctx.warn` / `ctx.permission_issue` (preserving full call-site signatures including `service` and `action`).
7. Grep for `concurrent.futures` usage in `cost_optimizer.py`. If used → add `threading.Lock` around `_warnings` and `_permission_issues` mutation. If unused → defer cleanup to T-702.
8. Run T-002.

**Acceptance:** T-002 green. `cost_optimizer.py` has zero direct `boto3.Session()` or `.client()` calls — all routed via `ScanContext`. The `self.config` name collision is gone. JSON arrays preserve full per-record schema: `scan_warnings` records carry three fields (`message`, `service`, `timestamp`); `permission_issues` records carry four fields (`message`, `service`, `action`, `timestamp`).

---

### T-104 — Centralize warning/permission tracking
**Risk:** LOW · **Complexity:** S · **Deps:** T-103

**Steps:**
1. Move `add_warning` / `add_permission_issue` logic to `ScanContext.warn()` / `ScanContext.permission_issue()` — full schema preserved (see T-103 step 3).
2. Replace every `self.add_warning(msg, service)` call site with `ctx.warn(msg, service)`. Preserve the `service` argument at every call (it's positional in some call sites, keyword in others — verify via grep).
3. Replace every `self.add_permission_issue(msg, service, action)` call site with `ctx.permission_issue(msg, service, action)`.
4. Final `scan_results` assembly reads `ctx._warnings` and `ctx._permission_issues` and serializes via `dataclasses.asdict()` so they match today's dict shape (`{message, service, timestamp}` and `{message, service, action, timestamp}`).
5. Run T-002.

**Acceptance:** Final `scan_results['scan_warnings']` and `['permission_issues']` lists byte-equal to golden — including `service`, `action`, and `timestamp` fields.

---

## 9. Execution Plan — Group C: Orchestrator

### T-200 — Introduce `ScanOrchestrator`, `safe_scan`, empty registry
**Risk:** MEDIUM · **Complexity:** L · **Deps:** T-103

**Steps:**
1. Create `core/scan_orchestrator.py`:
   ```python
   def safe_scan(module: ServiceModule, ctx: ScanContext) -> ServiceFindings:
       """Enforce contract rule §6.1 — scan() MUST NOT raise.
       Catches any exception, logs to ctx.warn, returns empty findings."""
       try:
           return module.scan(ctx)
       except Exception as exc:
           ctx.warn(f"[{module.key}] scan failed: {exc}", service=module.key)
           return ServiceFindings(
               service_name=module.display_name,
               total_recommendations=0,
               total_monthly_savings=0.0,
               sources={},
           )

   class ScanOrchestrator:
       def __init__(self, ctx: ScanContext, modules: list[ServiceModule]): ...
       def run(self, scan_only: set[str] | None, skip: set[str] | None) -> dict[str, ServiceFindings]:
           selected = resolve_cli_keys(self.modules, scan_only, skip)
           return {m.key: safe_scan(m, self.ctx) for m in self.modules if m.key in selected}
   ```
2. Create `core/filtering.py` with `resolve_cli_keys(modules, scan_only, skip) -> set[str]`. Replicates today's `service_map` (`cost_optimizer.py:2752–2783`) using each module's `cli_aliases` tuple.
3. Create `services/__init__.py` with `ALL_MODULES: list[ServiceModule] = []` (empty for now).
4. In `cost_optimizer.scan_region()`:
   - For each `if should_scan_service('<key>'):` block, extract its body to a private method `_scan_legacy_<key>(self, ctx) -> ServiceFindings | None`. **Mechanical only** — no logic change.
   - `scan_region` now calls each `_scan_legacy_*` in original order.
5. Run T-002.

**Acceptance:** T-002 green. `scan_region` body is a sequence of `_scan_legacy_*` calls. `safe_scan` covered by a unit test in `tests/test_orchestrator.py` that asserts a deliberately-raising module produces an empty findings + ctx.warn entry.

---

### T-201 — `ScanResultBuilder` (preserves JSON shape)
**Risk:** **HIGH** · **Complexity:** L · **Deps:** T-200

**Steps:**
1. Create `core/result_builder.py`:
   ```python
   from dataclasses import asdict

   class ScanResultBuilder:
       def __init__(self, ctx: ScanContext): ...

       def build(self, findings: dict[str, ServiceFindings]) -> dict:
           return {
               "account_id":        self.ctx.account_id,
               "region":            self.ctx.region,
               "profile":           self.ctx.profile,
               "scan_time":         datetime.now(timezone.utc).isoformat(),
               "scan_warnings":     [asdict(w) for w in self.ctx._warnings],
               "permission_issues": [asdict(p) for p in self.ctx._permission_issues],
               "services":          {k: self._serialize(f) for k, f in findings.items()},
               "summary":           self._summary(findings),
           }

       @staticmethod
       def _serialize(f: ServiceFindings) -> dict:
           # Splat extras to top-level so today's volume_counts / instance_counts /
           # efs_counts / fsx_counts / bucket_counts / table_counts / service_counts /
           # fast_mode_warning keys appear at the same JSON depth as before.
           base = asdict(f)
           extras = base.pop("extras", {}) or {}
           base.pop("schema_version", None)               # internal; not in JSON
           if base.get("total_count", 0) == 0:
               base.pop("total_count", None)              # only AMI emits it (parity with cost_optimizer.py:3893)
           return {**base, **extras}

       @staticmethod
       def _summary(findings: dict[str, ServiceFindings]) -> dict:
           # Replicates cost_optimizer.py:3999–4007 quirky calc:
           # count = number of findings whose total_recommendations > 0 OR total_count > 0
           scanned = sum(
               1 for f in findings.values()
               if f.total_recommendations > 0 or f.total_count > 0
           )
           return {
               "total_services_scanned":  scanned,
               "total_recommendations":   sum(f.total_recommendations for f in findings.values()),
               "total_monthly_savings":   sum(f.total_monthly_savings for f in findings.values()),
           }
   ```
2. `build()` MUST produce the exact JSON shape:
   - Top-level: `account_id`, `region`, `profile`, `scan_time`, `scan_warnings`, `permission_issues`, `services`, `summary`.
   - `services` is a dict; each value is `ServiceFindings` serialized with `extras` splatted so today's `volume_counts`, `instance_counts`, `efs_counts`, `fsx_counts`, `bucket_counts`, `table_counts`, `service_counts`, `fast_mode_warning` keys stay at the same depth.
   - `summary.total_services_scanned` MUST replicate `cost_optimizer.py:3999–4007` quirk (counts services with `total_recommendations > 0` OR `total_count > 0`). Dedicated unit test required.
   - **`total_count` field** appears in serialized JSON **only for AMI** (parity with `cost_optimizer.py:3893`). Other services have it default `0`; serializer drops it when `0`.
3. Replace `scan_region`'s final dict assembly with `ScanResultBuilder.build(...)`.
4. Run T-002.

**Acceptance:** Final JSON byte-equal to golden (after normalization). Dedicated unit test `test_total_services_scanned_quirk` green. AMI's `total_count` field present in serialized JSON; other services do not have a `total_count` key.

---

## 10. Execution Plan — Group D: Service Module Migration

> **Gate:** Run T-002 after **every** module migration. If it fails, **stop**, do not move to next module.

### Migration recipe (apply identically to every module)

For service `X`:

1. **Create `services/X.py`** with class `XModule` implementing the contract:
   ```python
   class XModule:
       key = "x"                                    # canonical key
       cli_aliases = ("x", "...")                   # from service_map
       display_name = "X"
       stat_cards = (...)                           # if X has cards in current report
       grouping = GroupingSpec(...)                 # if X has custom grouping
       def required_clients(self): return ("x", ...)
       def scan(self, ctx): ...                     # returns ServiceFindings
   ```
2. **Move methods:** `get_X_*`, `get_enhanced_X_checks`, `get_X_optimization_descriptions` from `CostOptimizer` to `XModule`.
3. **Translate `self.<client>` → `ctx.client('<name>')`.**
4. **Translate `self.add_warning(...)` → `ctx.warn(...)`.**
5. **Translate hardcoded fallback savings:** preserve verbatim formulas (per pre-flight decision).
6. **Register:** Add `XModule()` to `services/__init__.ALL_MODULES`.
7. **Wire orchestrator:** In `cost_optimizer.scan_region`, replace `_scan_legacy_x()` call with `orchestrator.run_one('x')`.
8. **Delete `_scan_legacy_x` and the original `get_X_*` methods** from `CostOptimizer`.
9. **Run T-002.** Must be green before next task.
10. **Add unit test** `tests/per_service/test_X.py` (deferred to T-600 if blocking).

### Migration order (low → high risk)

#### Tier 1 — Isolated, single-method modules (LOW · S)
| Task | Service | Notes |
|------|---------|-------|
| T-300 | `mediastore` | — |
| T-301 | `transfer` | — |
| T-302 | `apprunner` | — |
| T-303 | `quicksight` | — |
| T-304 | `athena` | — |
| T-305 | `batch` | — |
| T-306 | `workspaces` | — |
| T-310 | `redshift` | — |
| T-311 | `msk` | boto3 client name is `kafka` |
| T-312 | `cloudfront` | force `us-east-1` |
| T-313 | `api_gateway` | uses both `apigateway` + `apigatewayv2` |
| T-314 | `step_functions` | — |

#### Tier 2 — Schema-divergent modules (MEDIUM · M)

> **Each of T-307, T-308, T-309 MUST commit `tests/fixtures/expected_diffs/T-30x.diff`** capturing the exact allowed HTML delta (e.g. previously-empty source loop now renders one source block per group) **before** the task is marked done. The byte-equal gate G3 reads this file and asserts the actual diff matches it exactly.

| Task | Service | Notes |
|------|---------|-------|
| T-307 | `lightsail` | ⚠ Today passes raw `enhanced_lightsail_checks.checks` instead of canonical `{enhanced_checks: {count, recommendations}}`. New contract normalizes — expected HTML diff captured in `tests/fixtures/expected_diffs/T-307.diff`. |
| T-308 | `dms` | Same divergence + CloudWatch CPU. Diff in `T-308.diff`. |
| T-309 | `glue` | Same divergence. Diff in `T-309.diff`. |

#### Tier 3 — Multi-source modules (×4 — T-315b, T-316, T-317, T-318) (MEDIUM · M)
| Task | Service | Notes |
|------|---------|-------|
| T-315b | `lambda` | Lambda module migration. Consumes `core/cost_hub_categorizer.py` (extracted in T-315). Migrates `get_enhanced_lambda_checks` plus the Lambda slice of Cost Optimization Hub categorization. |
| T-316 | `elasticache` | CloudWatch metrics |
| T-317 | `opensearch` | — |
| T-318 | `dynamodb` | CloudWatch + provisioned/on-demand counts |

> **Naming note:** ID `T-315b` (sibling rename, not a sub-task) was introduced in v1.2 after `T-315` was promoted to the critical path for the cost-hub-categorizer extraction. Lambda migration content is unchanged from v1.0/v1.1 except for the new ID. The `a`-suffix convention (T-321a, T-325a) is reserved for pre-extraction sub-tasks; `b` is used here for sibling rename to avoid renumbering T-316–T-318.

#### Tier 3.5 — Pre-extraction sub-tasks (must run BEFORE Tier 5)

These break extraction-order conflicts that would otherwise leave EC2/AMI or containers/network broken simultaneously. **Run in order: T-321a → T-325a → T-315 → Tier 5.**

| Task | Title | Risk · Cx | What it does | Why first |
|------|-------|-----------|--------------|-----------|
| T-321a | Extract `get_ami_checks` to free function | LOW · S | Move `CostOptimizer.get_ami_checks` (called at `cost_optimizer.py:2876`) and helpers to `services/ami.py` as a module-level free function `compute_ami_checks(ctx) -> dict`. Monolith call site becomes `compute_ami_checks(self._ctx)`. No `AmiModule` class yet — that's T-321. | Without this, T-321 (full module) would have to reach into still-monolithic EC2 logic. Free-function step decouples cleanly. |
| T-325a | Extract `_is_kubernetes_managed_alb` and `_is_eks_nodegroup_asg` to `core/k8s_detection.py` | LOW · S | Move `cost_optimizer.py:370` (`_is_kubernetes_managed_alb`) and `:425` (`_is_eks_nodegroup_asg`) verbatim to `core/k8s_detection.py`. Replace original methods with thin delegations. **Preserve latent `instance_id` NameError bugs at lines 422 and 463 verbatim** unless pre-flight decision §2 says otherwise. | Both T-325 (containers) and T-326 (network) call these helpers. If T-325 moved them, T-326 would break; if T-326 moved them, T-325 would break. Pre-extracting to `core/` resolves the dependency. |
| T-315 (promoted) | Extract Cost Optimization Hub categorizer | MEDIUM · M | Move the 21-line `cost_hub_by_service` categorizer at `cost_optimizer.py:2849–2868` to `core/cost_hub_categorizer.py` as `categorize_recommendations(recs: list[dict]) -> dict[str, list[dict]]`. EC2 (T-323) and Lambda (already migrated in Stream P2) both consume this. | EC2 module's `scan()` consumes this. T-315 must precede T-323; promoted from Tier 3 onto critical path. |

#### Tier 4 — Compute Optimizer + multi-source (HIGH)
| Task | Service | Risk · Cx | Notes |
|------|---------|-----------|-------|
| T-319 | `rds` | HIGH · L | Preserve "RDS totals understated" warning at lines 3225–3226 |
| T-321 | `ami` | MEDIUM · M | Wraps T-321a's free function in `AmiModule`. Sets `total_count` on `ServiceFindings` (parity with `cost_optimizer.py:3893`). Register as separate module for reporter tab parity. |

#### Tier 5 — Critical-path heavyweights (HIGH · L/XL)

> **Table row order matches execution order** (and matches the §3 critical path).

| Task | Service | Risk · Cx | Notes |
|------|---------|-----------|-------|
| T-323 | `ec2` | HIGH · XL | Cost Hub + Compute Optimizer + enhanced + advanced; reuses `cost_hub_categorizer` from T-315. **Snapshot data emitted here also feeds `SnapshotsModule` materialized in T-406** — keep recommendation dict shape stable. |
| T-322 | `ebs` | HIGH · L | `gp2` vs other split (3149–3153); `unattached_volumes` vs `enhanced_checks`. **Snapshot data feeds `_extract_snapshots_data` → `SnapshotsModule` materialized in T-406.** |
| T-320 | `s3` | HIGH · L | Cross-region clients per bucket; `top_cost_buckets` / `top_size_buckets` consumed by reporter; `fast_mode_warning` |
| T-326 | `network` | HIGH · XL | EIP + NAT + VPC endpoints + LB + Auto Scaling + advanced EC2; uses `_is_eks_nodegroup_asg` (moved to `core/k8s_detection.py` in T-325a) |
| T-325 | `containers` | HIGH · L | ECS + EKS + ECR; uses `_is_kubernetes_managed_alb` (moved to `core/k8s_detection.py` in T-325a) |
| T-324 | `file_systems` | HIGH · L | EFS + FSx collapsed; preserve EFS-vs-FSx detection in `_get_detailed_recommendations` |
| T-327 | `monitoring` | MEDIUM · L | CloudWatch + CloudTrail + Backup + Route53 |

**Recommended execution order within Tier 5** (aligned with §3 critical path):
T-321 (AMI — Tier 4 warm-up) → T-323 (EC2) → T-322 (EBS) → T-320 (S3) → T-326 (network) → T-325 (containers) → T-324 (file_systems) → T-327 (monitoring).

> **Note on `SnapshotsModule`:** Not a Group D migration target. No current code path produces `'snapshots'` as a top-level service key — it's a reporter-side tab insertion at `html_report_generator.py:991`, materialized by extracting snapshot recommendations emitted by EBS/EC2 enhanced checks. See T-406 for creation.

---

## 11. Execution Plan — Group E: Reporter Refactor

> **Gate:** All T-3xx tasks complete. T-002 green. Modules expose `stat_cards` and `grouping` metadata correctly.

### T-400 — Extract CSS to `report/assets/report.css`
**Risk:** LOW · **Complexity:** S

**Steps:**
1. Copy contents of `_get_css()` (line 130) verbatim to `report/assets/report.css`.
2. Replace `_get_css()` body with file read (cached at module load).
3. Run T-002 (HTML byte-diff).

**Acceptance:** HTML output byte-equal to golden.

---

### T-401 — Extract JS to `report/assets/report.js`
**Risk:** LOW · **Complexity:** S

**Steps:** Identical to T-400 for `_get_javascript()` (line 3786).

---

### T-402 — Introduce Jinja2; convert scaffolding templates
**Risk:** MEDIUM · **Complexity:** M

**Steps:**
1. Add `jinja2>=3.1.0` to `requirements.txt`.
2. Create `report/templates/base.html.j2`, `summary.html.j2`, `tabs.html.j2` from `_build_html`, `_get_summary`, `_get_tabs` f-strings.
3. Replace each method body with `template.render(...)` call.
4. Run T-002.

**Acceptance:** HTML byte-equal after whitespace normalization. Document any expected whitespace differences in `tests/conftest.py` normalizer.

---

### T-403 — Convert executive summary template
**Risk:** MEDIUM · **Complexity:** M

**Steps:** Same pattern as T-402 for `_get_executive_summary_content` (line 1266).

---

### T-404 — Generic `service_tab.html.j2`; drive stats from module metadata
**Risk:** **HIGH** · **Complexity:** L · **Deps:** All T-3xx done

**Steps:**
1. Create `report/templates/service_tab.html.j2` that renders from `module.stat_cards` and `findings.extras`.
2. Each module already declares `stat_cards` (Phase 6 contract). For modules without cards (`lightsail`, `dms`, etc.), `stat_cards = ()` → renders empty.
3. Delete the 7-branch ladder at `_get_service_stats` lines 1728–1786.
4. Run T-002.

**Acceptance:** HTML byte-equal. `_get_service_stats` reduced to one Jinja render call.

---

### T-405 — Drive grouping from module metadata
**Risk:** **HIGH** · **Complexity:** XL · **Deps:** T-404

**Steps:**
1. Create `report/grouping/strategies.py` with generic groupers: `group_by_check_category`, `group_by_resource_type`, `group_by_source`.
2. For each module with custom grouping in current code, choose:
   - Use a stock grouper if pattern fits.
   - Implement `custom_grouping(findings) -> list[Group]` on the module (signature `Callable[[ServiceFindings], list[Group]]` — see §6 contract).
3. Replace `_get_detailed_recommendations` (line 1792) ladder with single template loop driven by `module.grouping` or `module.custom_grouping`.
4. **Snapshot-pin every `EstimatedSavings` string** the modules emit. Add `tests/test_savings_strings.py` that calls each module against canned synthetic resources and asserts the resulting `EstimatedSavings` strings match a frozen snapshot (e.g. via `pytest`'s `pytest-snapshot` or a hand-rolled assertion file). This catches whitespace/casing drift that would silently break the reporter's substring classifiers (`'Up to 90%' in savings_str` etc. — see `cost_optimizer.py:3871`).
5. Run T-002 — **expect this to be the hardest task to land green**.

**Acceptance:** HTML byte-equal **after currency normalization (±$0.01 per cell)** to golden. `_get_detailed_recommendations` reduced to one Jinja render call. `tests/test_savings_strings.py` green.

---

### T-406 — Move `_extract_snapshots_data` / `_extract_amis_data` into modules
**Risk:** MEDIUM · **Complexity:** M

> **`SnapshotsModule` is created in this task.** It is **NOT** a Group D migration target because no current code path produces `'snapshots'` as a top-level service key — it's a reporter-side tab insertion at `html_report_generator.py:991`. The module materializes by reading snapshot-flagged recommendations emitted from EBS (T-322) and EC2 (T-323) enhanced checks via `CheckCategory` field substring match (`'snapshot' in cat.lower()`). The reporter's special-case "insert Snapshots tab right after EBS" logic disappears once `SnapshotsModule` is registered in `ALL_MODULES` immediately after `EbsModule`.

**Steps:**
1. Create `services/snapshots.py` with `SnapshotsModule(ServiceModule)`. Its `scan(ctx)` reads from cached EBS findings (preferred) or re-extracts from EBS's `enhanced_checks.recommendations` filtering by `CheckCategory` containing `'snapshot'` (preserving today's logic at `html_report_generator.py:1059–1066`).
2. Move `_extract_snapshots_data` (`html_report_generator.py:1049`) to `services/snapshots.py`.
3. Move `_extract_amis_data` (`html_report_generator.py:1079`) to `services/ami.py`.
4. Register `SnapshotsModule()` in `ALL_MODULES` immediately after `EbsModule()` so registry order produces the same tab sequence as today.
5. Remove EBS-position special-case at line 991 of HTML generator.
6. Remove standalone-AMI fallback at line 998–1000.
7. Run T-002.

**Acceptance:** HTML tab order byte-equal. `SnapshotsModule` and `AmiModule` both register; `_get_tabs` no longer special-cases EBS or AMI.

---

### T-407 — Remove duplicate `if __name__ == "__main__":` block
**Risk:** LOW · **Complexity:** S

**Steps:**
1. In `html_report_generator.py`, delete the second `if __name__ == "__main__":` block at line 4072 (unreachable / duplicate).
2. Run T-002.

**Acceptance:** Single `__main__` block in file. T-002 green.

---

## 12. Execution Plan — Group F: Entry Point + Cleanup

### T-500 — New `cli.py`
**Risk:** **HIGH** · **Complexity:** M · **Deps:** All T-3xx, all T-4xx, T-201

**Steps:**
1. Create `cli.py`:
   - argparse setup (mirror `cost_optimizer.py:8661`).
   - Build `ScanContext`, `ALL_MODULES`, `ScanOrchestrator`, `ScanResultBuilder`.
   - Run scan, dump JSON, run report.
2. Verify:
   ```bash
   python3 cli.py us-east-1 --scan-only s3 --fast
   ```
3. Run T-002 against `cli.py` instead of `cost_optimizer.py`.
4. **Delete `cost_optimizer.py` and `html_report_generator.py`.**
5. Run T-002 again.

**Acceptance:** `python3 cli.py us-east-1` produces JSON + HTML byte-equal to T-001 goldens. Both monolith files deleted.

---

### T-501 — Update documentation
**Risk:** LOW · **Complexity:** M · **Deps:** T-500

**Steps:**
1. Update `README.md`: new entry command, new structure.
2. Update `ARCHITECTURE.md` and `docs/architecture.md`: Service Modules pattern, registry, contract.
3. Rewrite `docs/tutorials/adding_services.md`:
   - Implement `ServiceModule` contract.
   - Register in `services/__init__.ALL_MODULES`.
   - Add tests in `tests/per_service/test_<name>.py`.
4. Update `CHANGELOG.md`: bump version, document refactor.
5. Update `CLAUDE.md` if AI-agent guidelines reference old file paths.

**Acceptance:** All docs reference real paths in new layout. No mention of deleted files except in CHANGELOG.

---

### T-502 — CI workflow
**Risk:** LOW · **Complexity:** S · **Deps:** T-500

**Steps:**
1. Create `.github/workflows/test.yml` (or equivalent for chosen CI):
   - Install `requirements.txt` + `requirements-dev.txt`.
   - Run `pytest`.
   - Block PR merge on red tests.
2. Verify a sample PR triggers the workflow.

**Acceptance:** CI runs on every PR. PRs fail when `tests/test_regression_snapshot.py` fails.

---

## 13. Execution Plan — Group G: Test Coverage

> Can run in parallel with later Group D/E tasks once underlying modules exist.

### T-600 — Per-service unit tests (×30 — one per `services/*.py` including `ami` and `snapshots`)
**Risk:** LOW · **Complexity:** L (total) · **Deps:** matching T-3xx

**Steps:** For each module, create `tests/per_service/test_<service>.py`:
- Stub the boto3 clients the module declares in `required_clients()`.
- Feed canned responses.
- Assert the returned `ServiceFindings` has expected `total_recommendations`, `total_monthly_savings`, source counts.

**Acceptance:** Each module has at least one happy-path test + one permission-error path test.

---

### T-601 — Pricing/savings/util unit tests + contract conformance
**Risk:** LOW · **Complexity:** M

**Steps:**
1. `tests/test_pricing.py`: regional multiplier lookups for all 33 regions; S3 cost calc; EFS/FSx cost calc.
2. `tests/test_savings_parser.py`: at least 15 input formats (already partially covered in T-102).
3. `tests/test_orchestrator.py`: skip/scan-only logic; module ordering; **`safe_scan` exception handling** (deliberately-raising module → empty findings + ctx.warn entry).
4. `tests/test_module_contract.py`: every module in `ALL_MODULES`:
   - Has all required class attributes (`key`, `cli_aliases`, `display_name`, `stat_cards`, `grouping`, `requires_cloudwatch`, `reads_fast_mode`).
   - `key` matches a top-level key produced by `ScanResultBuilder` for that module.
   - `cli_aliases` is non-empty.
   - `required_clients()` returns a non-empty tuple.
   - **Note:** Do NOT rely on `isinstance(m, ServiceModule)` alone — `@runtime_checkable Protocol` only verifies callable members exist, not class-attribute presence. Use explicit `hasattr` / `getattr` checks.
5. `tests/test_recommendation_schema.py`: schema linter — assert every emitted recommendation dict carries at least one resource-identity key, plus `Recommendation`, `EstimatedSavings`, `CheckCategory` (see §6 "Recommendation dict shape").

### Coverage targets (per tier)

| Tier | Target | Rationale |
|------|--------|-----------|
| `core/` | **95%** | Pure logic, no AWS calls — easy to test, central blast radius |
| `services/` | **80%** | AWS-stubbed; happy path + permission-error path per module |
| `report/` | **70%** | Template rendering harder to cover; rely on T-602 + snapshot tests |

**Acceptance:** All four test files green. Coverage targets met per tier (`pytest --cov=core --cov=services --cov=report --cov-fail-under=...`).

---

### T-602 — Reporter rendering tests
**Risk:** LOW · **Complexity:** M

**Steps:** `tests/test_report_builder.py` — feed synthetic `ServiceFindings` through Jinja templates, assert presence of expected stat-card labels, grouping headings.

**Acceptance:** Reporter tests green.

---

## 14. Execution Plan — Group H: Optional Follow-Ups

> Schedule these **after** parity is locked and the refactor is merged. Each is independent; pick what's worth the effort.

| Task | Description | Risk · Cx |
|------|-------------|-----------|
| T-700 | Decide & document: fix vs preserve `instance_id` NameError at lines 422, 463 | LOW · S |
| T-701 | Reconcile `S3_REGIONAL_MULTIPLIERS` GLACIER key inconsistency | LOW · S |
| T-702 | Drop unused `python-dateutil` from `requirements.txt` | LOW · S |
| T-703 | Audit and prune unused boto3 clients (`organizations`, `support`/`trustedadvisor` duplicate, `pricing`, `ce`, `cur`, `budgets`, `ssm`, `secretsmanager`, `kms`, `sns`, `sqs`, `events`) | MEDIUM · M |

---

## 15. Validation Gates

| Gate | When | Pass criteria | Failure response |
|------|------|---------------|------------------|
| **G0: Tooling baseline ready** | After T-099 | `python --version` ≥ 3.11; `mypy --strict` succeeds on `core/contracts.py`; `ruff` clean; `pytest --co` discovers tests | Fix tooling; do not start T-001. |
| **G1: Snapshot harness ready** | After T-003 | `pytest tests/test_regression_snapshot.py` green offline (no AWS creds, no network); freezegun pinned to capture timestamp | Stop. Refactor cannot proceed without this. |
| **G2: Core extracted, no behavior change** | After T-104 | T-002 still green; `cost_optimizer.py` has no `boto3.Session()` or `.client()` calls; `scan_warnings` and `permission_issues` JSON arrays preserve full schema (`message, service, action, timestamp`) | Revert offending task; re-attempt. |
| **G3: Per-module migration** | After **every** T-3xx | T-002 green; HTML byte-diff matches `tests/fixtures/expected_diffs/T-3xx.diff` exactly (zero unexpected diff for most; recorded diff only for T-307/T-308/T-309) | Revert that single task. Investigate. Do **not** continue to next module. |
| **G4: Reporter parity** | After T-405 | HTML byte-equal to golden after **(a)** whitespace normalization and **(b)** currency normalization (±$0.01 per cell — see T-002 step 1 normalizer); `tests/test_savings_strings.py` green | Identify which template diverges; iterate within T-405. |
| **G5: End-to-end via `cli.py`** | After T-500 | `python3 cli.py <region>` produces JSON byte-equal + HTML byte-equal-with-tolerance to T-001 goldens | Block deletion of monolith files. Diagnose. |
| **G6: CI green on fresh checkout** | After T-502 | Clone repo on clean machine → `pytest` green; `mypy --strict` green; coverage thresholds met (95/80/70 per tier) | Fix CI config; do not merge until green. |

---

## 16. Rollback Strategy

- **Per-task rollback:** Every task is one commit on `refactor/service-modules`. `git revert <sha>` restores prior state. T-002 must pass after revert.
- **Per-group rollback:** Tag each completed group: `refactor/group-A`, `refactor/group-B`, etc. Reset to nearest tag if a group is unsalvageable.
- **Full rollback:** The `v2.5.9-baseline` tag remains untouched. `git checkout v2.5.9-baseline -- .` restores the monolith. Goldens captured in T-001 remain valid forever.
- **Monolith deletion is reversible** until the post-T-500 commit is pushed to `main`. Keep the deletion as a separate commit so it can be reverted independently.

---

## 17. Definition of Done

The refactor is complete when **all** of the following are true:

- [ ] `cost_optimizer.py` and `html_report_generator.py` are deleted from `main`.
- [ ] `python3 cli.py us-east-1` produces JSON byte-equal (after timestamp + account-id normalization) to `tests/fixtures/golden_scan_results.json`.
- [ ] `python3 cli.py us-east-1` produces HTML byte-equal (after timestamp normalization + currency tolerance ±$0.01) to `tests/fixtures/golden_report.html` for every captured region.
- [ ] All 28+ modules in `services/` satisfy the `ServiceModule` contract (verified by `tests/test_module_contract.py` — uses explicit `hasattr` checks, not just `isinstance`).
- [ ] `mypy --strict` exits 0 on `core/`, `services/`, `report/`, `cli.py`.
- [ ] `ruff check .` exits 0.
- [ ] `pytest` exits 0 on a fresh clone with **no AWS credentials and no network**.
- [ ] Coverage thresholds met: `core/` ≥ 95%, `services/` ≥ 80%, `report/` ≥ 70%.
- [ ] CI is green on `main`.
- [ ] `docs/tutorials/adding_services.md` reflects the new module-based extension flow.
- [ ] `CHANGELOG.md` entry merged for the new version.
- [ ] All pre-flight decisions (§2) are documented in `ARCHITECTURE.md`.
- [ ] `tests/MOCK_COVERAGE.md` documents the moto-vs-Stubber split for every AWS service.
- [ ] No `self.config` collision; `cost_optimizer.py:269 vs 346` issue resolved.
- [ ] `scan_warnings` records preserve all three fields (`message`, `service`, `timestamp`); `permission_issues` records preserve all four (`message`, `service`, `action`, `timestamp`).

---

## Appendix A — Task Index (quick reference)

| ID | Title | Risk | Cx | Group |
|----|-------|------|----|----|
| **T-000** | **Initialize git baseline + tag** | **LOW** | **S** | **Pre-A** |
| **T-099** | **Tooling baseline (pyproject.toml, ruff/mypy/pytest, moto/freezegun)** | **LOW** | **S** | **Pre-A** |
| T-001 | Capture regression goldens (frozen-clock) | LOW | S | A |
| T-002 | Snapshot regression harness (freezegun + currency tolerance) | LOW | M | A |
| T-003 | Mock AWS surface (moto primary, Stubber fallback) | MEDIUM | L | A |
| T-100 | `core/` skeleton (mypy --strict gate) | LOW | S | B |
| T-101 | Extract pricing | MEDIUM | M | B |
| T-102 | Extract savings parser | MEDIUM | M | B |
| T-103 | Session/Clients/Context (full warn/permission schema; +`organizations`; `trustedadvisor` dedup; client shims) | HIGH | L | B |
| T-104 | Warning/permission tracking (full dict shape) | LOW | S | B |
| T-200 | Orchestrator stub + `safe_scan` decorator | MEDIUM | L | C |
| T-201 | Result builder (+`total_count`; recursive `asdict`) | HIGH | L | C |
| T-300…T-306 | Tier-1 modules (×7) | LOW | S | D |
| T-307…T-309 | Tier-2 modules (×3) — diff files required | MEDIUM | M | D |
| T-310…T-314 | Tier-1 cont. (×5) | LOW | S | D |
| T-315 | cost_hub_categorizer (promoted onto critical path) | MEDIUM | M | D |
| **T-315b** | **`lambda` module migration (renamed from old T-315 in v1.2)** | **MEDIUM** | **M** | **D** |
| T-316…T-318 | Tier-3 modules (×3) | MEDIUM | M | D |
| T-319 | RDS | HIGH | L | D |
| T-320 | S3 | HIGH | L | D |
| **T-321a** | **Extract `get_ami_checks` to free function** | **LOW** | **S** | **D** |
| T-321 | AMI module (sets `total_count`) | MEDIUM | M | D |
| T-322 | EBS | HIGH | L | D |
| T-323 | EC2 | HIGH | XL | D |
| T-324 | file_systems | HIGH | L | D |
| **T-325a** | **Extract k8s detection helpers to `core/k8s_detection.py`** | **LOW** | **S** | **D** |
| T-325 | containers | HIGH | L | D |
| T-326 | network | HIGH | XL | D |
| T-327 | monitoring | MEDIUM | L | D |
| T-400 | Extract CSS | LOW | S | E |
| T-401 | Extract JS | LOW | S | E |
| T-402 | Jinja scaffolding | MEDIUM | M | E |
| T-403 | Executive summary template | MEDIUM | M | E |
| T-404 | Generic stat cards | HIGH | L | E |
| T-405 | Generic grouping (currency-tolerant gate; savings-string snapshot) | HIGH | XL | E |
| T-406 | Move snapshot/AMI extractors | MEDIUM | M | E |
| T-407 | Remove duplicate `__main__` | LOW | S | E |
| T-500 | `cli.py` + delete monolith | HIGH | M | F |
| T-501 | Update docs | LOW | M | F |
| T-502 | CI | LOW | S | F |
| T-600 | Per-service tests (×30 — 28 services + `ami` + `snapshots` sub-modules) | LOW | L | G |
| T-601 | Pricing/savings/util/contract/schema tests + per-tier coverage | LOW | M | G |
| T-602 | Reporter tests | LOW | M | G |
| T-700…T-703 | Optional cleanups | LOW–MED | S–M | H |

**Total: 61 tasks** (v1.2 added: T-000, T-099, T-321a, T-325a, T-315b; T-315 promoted from Tier 3 to critical path).
**Critical path: 23 sequential nodes** (T-000 → T-099 → T-001 → T-002 → T-003 → T-100 → T-103 → T-104 → T-200 → T-201 → T-321a → T-325a → T-315 → T-323 → T-322 → T-320 → T-326 → T-325 → T-324 → T-405 → T-404 → T-500 → T-502). ~37 if upstream parallel dependents are counted.

---

## Validation History

| Version | Date | Reviewer | Changes |
|---------|------|----------|---------|
| v1.0 | 2026-04-29 | initial draft | 56 tasks · 33 critical-path |
| v1.1 | 2026-04-29 | python-reviewer agent (review of plan against source @ Cost_OptV1_dev) | Added T-000, T-099, T-321a, T-325a; promoted T-315 onto critical path; fixed §6 contract (`Literal` types, `total_count`, `custom_grouping` signature, `WarningRecord` / `PermissionIssueRecord`, `MappingProxyType` trap, `requires_cloudwatch`, `safe_scan`); fixed T-103 (added `organizations`, `trustedadvisor` alias, full warn schema, client shim mixin); fixed T-201 (recursive `asdict`, `total_count` parity); switched T-003 to moto primary; added freezegun in T-002; added per-tier coverage targets in T-601; added gate G0; relaxed gate G4 to ±$0.01 currency tolerance; added Risk Register R9–R14; added `tests/fixtures/expected_diffs/` for T-307/T-308/T-309. **Verdict: APPROVE-WITH-CHANGES → resolved, plan is now executable as-is.** |
| v1.2 | 2026-04-29 | python-reviewer second pass (defect audit of v1.1) | **Defect 1 (blocking):** v1.1 left the lambda module without a migration ID after T-315 was reused for the cost-hub-categorizer extraction. Fixed by introducing `T-315b` for lambda (sibling rename, not pre-extraction sub-task) and explicitly drafting its Notes column. **Defect 2 (blocking):** v1.1 Tier 5 "Recommended execution order" line contradicted the §3 critical path (S3/EBS/EC2 reversed; network/containers reversed). Fixed by aligning Tier 5 sentence AND reordering Tier 5 table rows to match the critical path: T-323 (EC2) → T-322 (EBS) → T-320 (S3) → T-326 (network) → T-325 (containers) → T-324 (file_systems) → T-327 (monitoring); T-321 (AMI from Tier 4) acts as the warm-up before Tier 5. **M1:** "30 service modules" → "~28 modules + 2 sub-modules". **M2:** "four/five fields per record" replaced with explicit "three for warnings / four for permission issues" at both line 636 (T-103 acceptance) AND in §17 Definition of Done. **M3:** T-325a's `instance_id` decision-owner reference fixed from "T-700" to "pre-flight §2". **M4:** `SnapshotsModule` creation point explicitly documented in T-406; cross-references added in T-322 (EBS) and T-323 (EC2). **Sub-fixes from second-pass review:** Tier 5 table row order reordered to match execution sentence; T-600 row count corrected to ×30 (28 services + `ami` + `snapshots`); footer "37 critical path" replaced with "23 sequential nodes (37 incl. upstream dependents)" and full chain enumerated. **Verdict: APPROVE → ready for T-000 kickoff.** |