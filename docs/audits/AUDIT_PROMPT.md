# Multi-Layer Per-Service Audit Prompt

> A reusable, codebase-grounded audit prompt for any single AWS service adapter
> in this repository. Three independent layers — **Technical**, **Calculation**,
> **Reporting** — each with explicit pass/fail criteria, evidence requirements,
> and a fixed output schema. Run all three layers per service.

## Scope Rule: Cost Only (added 2026-05-14)

This scanner is strictly cost-optimization. **Every check must produce a concrete account-specific $ saving** via live `PricingEngine` lookups, per-resource math, or `parse_dollar_savings`. During audit, classify each rec generator as **KEEP** (real $) or **REMOVE** (one of: health/state monitoring, version-upgrade nudge, security/compliance, resilience/DR, best-practice without quantified savings, `$0/month — quantify after X` placeholder, percentage-range estimate without per-account baseline). Add a new L1 finding for every REMOVE candidate. See `CHANGELOG.md` [3.4.0] for the precedent purge.

---

## How to use

1. Pick **one** service from `services/__init__.py` `ALL_MODULES` (e.g. `ec2`, `s3`, `lambda`, `dynamodb`).
2. Replace every `<SERVICE>` token below with that service key (lowercase, matches `module.key`).
3. Run each layer (L1, L2, L3) **in order** — L2 depends on L1 facts; L3 depends on L1+L2.
4. Produce the output report using the schema in **Section 6**.
5. Do **not** invent file paths, line numbers, fields, or AWS APIs. If a fact is not in the codebase, write `UNVERIFIED`.

**Evidence rule**: every finding must cite `file:line` (e.g. `services/adapters/ec2.py:55`) — no exceptions.
**Severity rule**: use exactly one of `CRITICAL | HIGH | MEDIUM | LOW | INFO`.
**Verdict rule**: per layer, one of `PASS | CONDITIONAL-PASS | WARN | FAIL`. Overall = worst of three.
**External-validation rule**: every CRITICAL or HIGH finding in L2 (Calculation) MUST be corroborated by at least one external source (AWS Pricing MCP, AWS docs via WebFetch, or boto3 reference via Context7). Cite the URL/MCP tool call so the finding is reproducible. See Section 0.1.

---

## 0. Pre-flight: Anchor the audit (always run first)

### 0.1 — External validation toolchain

Before writing any finding, the auditor MUST be ready to invoke these external sources. They turn "the code says X" into "the code says X **and** AWS/boto3 says Y, so the gap is real":

| Tool | When to use | Example call |
|------|------------|--------------|
| `mcp__aws-pricing-mcp-server__get_pricing_service_codes` | Discover the canonical AWS service code for a service (verify adapter uses the right one) | one-shot at audit start |
| `mcp__aws-pricing-mcp-server__get_pricing_service_attributes` | List filterable attributes for a service to verify adapter filter logic | `get_pricing_service_attributes('AmazonEC2')` |
| `mcp__aws-pricing-mcp-server__get_pricing_attribute_values` | Confirm an attribute value used in the adapter is legal | `get_pricing_attribute_values('AmazonEC2', 'instanceType')` |
| `mcp__aws-pricing-mcp-server__get_pricing` | Fetch a live price to compare against the adapter's hardcoded / parsed value | `get_pricing('AmazonEC2', 'us-east-1', [...])` |
| `mcp__aws-pricing-mcp-server__get_price_list_urls` | Bulk pricing snapshots when L2-005-style hardcoded constants need point-in-time validation | rare |
| `mcp__aws-pricing-mcp-server__get_bedrock_patterns` | Bedrock service only — required by the MCP server's own instructions | bedrock audit only |
| `mcp__aws-cloudwatch-mcp-server__get_metric_metadata` | Verify a CloudWatch namespace + metric name the adapter queries actually exists | EC2 audit: `AWS/EC2 / CPUUtilization` |
| `mcp__aws-cloudwatch-mcp-server__get_metric_data` | Sanity-check a metric query pattern (e.g. periodicity, statistic) | sparingly |
| `WebFetch` | Fetch a specific AWS docs page (boto3 reference, pricing page, API schema) — prefer over WebSearch when you already know the URL | `WebFetch("https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/compute-optimizer/client/get_ec2_instance_recommendations.html")` |
| `WebSearch` | Find a doc page when you don't know the URL, or sanity-check that a behavior is documented | `"AWS Compute Optimizer estimatedMonthlySavings response schema"` |
| `mcp__Context7__resolve-library-id` + `query-docs` | boto3 / botocore API references for fields, defaults, pagination | `resolve-library-id('boto3')` → `query-docs(<id>, 'EC2 describe_instances pagination')` |
| `mcp__github__search_code` | Search GitHub for canonical reference implementations or AWS samples to compare against | optional |

**Disconnected MCPs to avoid** (do not call, they will fail):
- `mcp__aws-knowledge-mcp-server__*` — disconnected in this environment; use `WebFetch` on `docs.aws.amazon.com` URLs instead.

**Validation cadence**:
- **L1 Technical**: no external validation required — pure code inspection. May use `Context7` for boto3 method signatures.
- **L2 Calculation**: external validation **mandatory** for every CRITICAL/HIGH finding (pricing constants, schema fields, units, regional behavior).
- **L3 Reporting**: external validation **optional** — focus on internal field-name parity with renderers.

**Recording external evidence**: each cited external source must appear in the report as:
```
External: <tool name> → <URL or key argument> → <one-sentence summary of what it confirmed>
```

### 0.2 — Codebase anchor files

Read these to ground the audit:

| File | Purpose |
|------|---------|
| `core/contracts.py` | `ServiceModule` Protocol, `ServiceFindings`, `SourceBlock` schema |
| `core/scan_context.py` | `ScanContext` fields available to adapters |
| `core/pricing_engine.py` | Public pricing methods + fallback constants |
| `core/scan_orchestrator.py` | `safe_scan` error-isolation behavior |
| `core/result_builder.py` | How `ServiceFindings` → JSON |
| `services/_base.py` | `BaseServiceModule` defaults |
| `services/_savings.py` | `parse_dollar_savings()` semantics |
| `services/__init__.py` | `ALL_MODULES` registration order |
| `services/adapters/<SERVICE>.py` | The adapter under test |
| `services/<SERVICE>.py` (if exists) | Legacy shim with analysis logic |
| `reporter_phase_a.py` and `reporter_phase_b.py` | Which renderer the service uses |
| `tests/fixtures/recorded_aws_responses/<SERVICE>*` | Recorded AWS responses if any |
| `tests/fixtures/reporter_snapshots/savings/<SERVICE>.txt` | Golden savings snapshot |

Record these facts before starting any layer:

- Adapter class name and file path (with line number of class definition)
- `ALL_MODULES` registration order index (0-based position in `services/__init__.py`)
- Renderer path (Phase A list, Phase B handler dict, or per-record fallback)
- Whether a legacy shim exists in `services/<SERVICE>.py` and what functions it exports
- Test files that exercise this adapter (grep `tests/` for the service key)

---

## 1. LAYER 1 — TECHNICAL AUDIT

**Goal**: verify the adapter is a correct, defensive, contract-conformant `ServiceModule`. No math, no rendering — just structure, AWS calls, error handling.

### L1.1 — ServiceModule Protocol conformance

| # | Check | Pass criterion |
|---|-------|---------------|
| L1.1.1 | Class declares `key: str` matching the dict key in `ALL_MODULES` | string literal, lowercase, no spaces |
| L1.1.2 | `cli_aliases` is a non-empty `tuple[str, ...]` and includes `key` | tuple, contains `key` value |
| L1.1.3 | `display_name` is set and stable | non-empty string |
| L1.1.4 | `required_clients()` returns the **exact** boto3 service names used inside `scan()` | every `ctx.client(X)`/`ctx.clients[X]` is in the tuple |
| L1.1.5 | If the adapter calls `cloudwatch.get_metric_statistics`/`get_metric_data`, `requires_cloudwatch = True` | flag matches code |
| L1.1.6 | If the adapter behavior changes on `ctx.fast_mode`, `reads_fast_mode = True` | flag matches code |
| L1.1.7 | `scan(ctx)` returns a `ServiceFindings` (not a dict) | annotated return type + actual constructor call |
| L1.1.8 | All `sources` values are `SourceBlock` instances with `recommendations` as a **tuple** | construct uses `tuple(...)` not list |

### L1.2 — AWS API hygiene

| # | Check | Pass criterion |
|---|-------|---------------|
| L1.2.1 | List operations use paginators (`client.get_paginator(...)`) | paginator used for any list/describe-many call |
| L1.2.2 | No silent `except: pass` blocks swallowing all errors | every `except` either records to `ctx.warn` / `ctx.permission_issue` or re-raises |
| L1.2.3 | `ClientError` access-denied / unauthorized paths route to `ctx.permission_issue(...)` | distinct branch for `AccessDenied`/`UnauthorizedOperation` |
| L1.2.4 | All other exceptions route to `ctx.warn(...)` with the service key | non-permission errors logged |
| L1.2.5 | Region-specific clients use `ctx.client(name, region=...)` when the AWS service is non-regional or pricing-only | matches client's actual regional model |
| L1.2.6 | No direct `boto3.client(...)` calls (must go through `ClientRegistry`) | only `ctx.client(...)` / `ctx.clients[...]` |
| L1.2.7 | No hardcoded `region_name`/`account_id` literals | values come from `ctx.region`/`ctx.account_id` |

### L1.3 — State and purity

| # | Check | Pass criterion |
|---|-------|---------------|
| L1.3.1 | `scan()` is pure w.r.t. the `ctx` object — no mutation of `ctx` fields other than `_warnings`/`_permission_issues` via the documented helpers | no direct writes to `ctx.region`, `ctx.account_id`, `ctx.cost_hub_splits`, etc. |
| L1.3.2 | No module-level mutable state shared across scans | no module-level `dict`/`list` that scan() appends to |
| L1.3.3 | No filesystem or network writes | no `open(..., "w")`, no `requests.post`, no `boto3` non-read APIs |

### L1.4 — `--scan-only` / `--skip-service` resolution

| # | Check | Pass criterion |
|---|-------|---------------|
| L1.4.1 | Every alias in `cli_aliases` resolves via `core.filtering.resolve_cli_keys` | manually run `resolve_cli_keys` with each alias; expect `{key}` |
| L1.4.2 | No alias conflicts with another adapter's `key` or `cli_aliases` | grep across `services/__init__.py` |

### L1.5 — Error isolation

| # | Check | Pass criterion |
|---|-------|---------------|
| L1.5.1 | Raising inside `scan()` produces empty findings via `safe_scan` (not a crash) | unit/integration test demonstrates this OR explicit comment confirming reliance on `safe_scan` |
| L1.5.2 | Adapter never raises bare `Exception` to abort the entire scanner run | grep for unhandled `raise` |

**L1 verdict**: `PASS` only if every L1.* checks pass. Single `CRITICAL` (e.g. L1.1.4 lying about clients, L1.2.6 bypass of ClientRegistry) → `FAIL`. Any L1.2.2 silent-swallow → at most `WARN`.

---

## 2. LAYER 2 — CALCULATION AUDIT

**Goal**: verify every dollar that ends up in `total_monthly_savings` is **traceable, reproducible, and unit-correct**. No false confidence from hardcoded constants pretending to be live pricing.

### L2.1 — Pricing source attribution

For **every** dollar value that flows into the recommendation's savings or into `total_monthly_savings`, classify the source as exactly one of:

| Source | Definition | Acceptable? |
|--------|------------|-------------|
| `live`         | Computed via `ctx.pricing_engine.<method>(...)` | YES — preferred |
| `aws-api`      | Comes from a live AWS API field (`estimatedMonthlySavings` from Cost Hub / Compute Optimizer) | YES |
| `parsed`       | Extracted from a string via `parse_dollar_savings(...)` | CONDITIONAL — only if string itself came from `live` or `aws-api` upstream |
| `module-const` | Hardcoded constant in the adapter or legacy shim | WARN — must justify why no live method exists |
| `fallback`     | Constant from `core/pricing_engine.py` `FALLBACK_*` | OK only when wrapped in try/except and `_use_fallback` logs it |
| `derived`      | Computed from another rec's value (percentage, multiplier) | WARN unless ratio is publicly documented (e.g. Multi-AZ = 2× Single-AZ) |
| `arbitrary`    | Number with no documented basis | **FAIL** |

Produce a table for the audited adapter:

```
Recommendation type → Source classification → Evidence (file:line)
```

### L2.2 — Unit correctness

| # | Check | Pass criterion |
|---|-------|---------------|
| L2.2.1 | All entries in `total_monthly_savings` are **USD/month** | not `$/hour`, not `$/year`, not `$/GB`, not `$/request` |
| L2.2.2 | Hourly→monthly conversions use **730 hours/month** (AWS standard) | not 720, not 744, not 8760/12 |
| L2.2.3 | Storage rates are **`$/GB-month`** when multiplied by GB; **never** mixed with TB or GiB silently | unit comment or variable name confirms |
| L2.2.4 | IOPS pricing uses `$/IOPS-month` not `$/IOPS-hour` | matches `PricingEngine.get_ebs_iops_monthly_price` semantics |
| L2.2.5 | Free-tier amounts (e.g. first 50 GB EBS snapshots, first 1 GB Lambda transfer) are subtracted before multiplying by rate | if the service has a free tier, the deduction is present or explicitly out-of-scope |

### L2.3 — Regional pricing

| # | Check | Pass criterion |
|---|-------|---------------|
| L2.3.1 | Live `PricingEngine` calls **do not** double-apply `ctx.pricing_multiplier` (PricingEngine already returns region-correct prices) | grep: no `ctx.pricing_engine.X() * ctx.pricing_multiplier` for live calls |
| L2.3.2 | Module-constant or `parse_dollar_savings` paths **do** apply `ctx.pricing_multiplier` | constant × multiplier confirmed |
| L2.3.3 | `parse_dollar_savings` fallback `50.0` for percentage-only strings is multiplied by `ctx.pricing_multiplier` if used | check `services/_savings.py:20` and adapter usage |
| L2.3.4 | Multi-AZ resources pass `multi_az=True` to `get_rds_monthly_storage_price_per_gb` | adapter respects RDS `MultiAZ` flag |

### L2.4 — Fallback transparency

| # | Check | Pass criterion |
|---|-------|---------------|
| L2.4.1 | When `PricingEngine` returns `0.0` (API miss with no fallback constant), the recommendation either omits dollar savings or attaches a `pricing_warning` field | no silent zero in `EstimatedSavings` masquerading as "free" |
| L2.4.2 | When fallback is used, `ctx.pricing_engine.warnings` is non-empty after the scan | inspect `engine.warnings` in a unit test or live run |
| L2.4.3 | `ctx.pricing_engine.stats["fallbacks"]` counter increments on fallback path | confirmed via `log_summary()` or stats inspection |

### L2.5 — Aggregation correctness

| # | Check | Pass criterion |
|---|-------|---------------|
| L2.5.1 | `total_monthly_savings` is the **sum of all per-recommendation savings across all sources** | hand-sum sample, compare to returned value |
| L2.5.2 | No double-counting when the same resource appears in Cost Optimization Hub **and** Compute Optimizer **and** enhanced checks | dedupe key (Resource ARN/ID) checked, or only the highest-savings source is counted, or split is documented |
| L2.5.3 | `total_recommendations` equals `sum(SourceBlock.count for SourceBlock in sources.values())` | unit/contract test enforces this invariant |
| L2.5.4 | When `total_recommendations == 0`, `total_monthly_savings == 0.0` exactly | no orphan dollar amount |
| L2.5.5 | `extras` numeric counters (resource counts, instance counts) are consistent with `total_count` and source counts | cross-check |

### L2.6 — Reproducibility

| # | Check | Pass criterion |
|---|-------|---------------|
| L2.6.1 | Two consecutive scans of the same account/region produce the same `total_monthly_savings` (within ±$0.01 from API jitter) | run twice with same fixtures, diff |
| L2.6.2 | Golden snapshot `tests/fixtures/reporter_snapshots/savings/<SERVICE>.txt` matches a fresh offline scan | `pytest tests/test_reporter_snapshots.py -k <SERVICE>` passes |

### L2.7 — External validation (MANDATORY for CRITICAL/HIGH)

For every CRITICAL or HIGH finding raised in L2.1–L2.6, run **at least one** of these and record the result:

| Finding type | Required validation | Tool |
|---|---|---|
| Hardcoded price constant differs from AWS | Fetch live price for the SKU + region the adapter targets, compare | `mcp__aws-pricing-mcp-server__get_pricing` |
| Wrong AWS Pricing service code (e.g. `AmazonDMS` vs `AWSDatabaseMigrationSvc`) | Confirm canonical code | `mcp__aws-pricing-mcp-server__get_pricing_service_codes` |
| Wrong filter attribute or value | Confirm attribute exists & value is legal | `get_pricing_service_attributes` + `get_pricing_attribute_values` |
| Wrong field name on AWS API response (e.g. top-level vs nested) | Confirm canonical schema | `WebFetch` boto3 ref page OR `Context7` query |
| CloudWatch metric name / namespace wrong | Confirm namespace + metric | `mcp__aws-cloudwatch-mcp-server__get_metric_metadata` |
| Unit error (hourly vs monthly, $/GB vs $/TB) | Confirm AWS docs definition | `WebFetch` AWS docs URL |
| 730-hours-per-month assumption | AWS publishes hours/month assumption in EC2 on-demand pricing docs | `WebFetch` pricing page |
| Free-tier deduction missing/wrong | Confirm free-tier rules | `WebFetch` service free-tier page |

When a validation **disproves** a draft finding, demote or remove the finding. Do not keep findings that the external source contradicts.

**L2 verdict**: `PASS` only if every L2.* checks pass. Any `arbitrary`-classified value → `FAIL`. Any L2.2 unit error → `FAIL`. Any L2.3.1 double-multiplier → `HIGH` and `WARN` minimum. Any L2.5.2 double-count → `HIGH` and `WARN` minimum. **Any CRITICAL/HIGH L2 finding without an external-validation citation → demote to MEDIUM (or remove) until validation is performed.**

---

## 3. LAYER 3 — REPORTING AUDIT

**Goal**: verify the data the adapter produces survives the path from `ServiceFindings` through `ScanResultBuilder` JSON into the HTML report **without loss, mislabeling, or silent drop**.

### L3.1 — JSON contract fidelity

| # | Check | Pass criterion |
|---|-------|---------------|
| L3.1.1 | `ScanResultBuilder._serialize` produces a dict with keys: `service_name`, `total_recommendations`, `total_monthly_savings`, `sources`, `total_count` (when non-zero), `optimization_descriptions` (when set), plus any `extras` flattened in at the top level | run resulter on a sample finding |
| L3.1.2 | Every `SourceBlock` serializes to `{"count": int, "recommendations": list[dict]}` plus any extras | verified |
| L3.1.3 | Recommendation dicts contain field names that match the HTML renderer's expected keys (see L3.2) | per-source field-list audit |
| L3.1.4 | No `Decimal`, `datetime`, `bytes`, or non-JSON-native types in recommendation values | `json.dumps(result)` succeeds without `default=` |
| L3.1.5 | No `None` masquerading as a savings number; `None` is acceptable only for optional metadata fields | grep |

### L3.2 — Renderer compatibility

Identify the renderer path:

- **Phase A** (`reporter_phase_a.py`): service key is in `_PHASE_A_SERVICES`; recommendations are grouped by `CheckCategory` or per-FS heuristic.
- **Phase B** (`reporter_phase_b.py`): a `(service_key, source_name)` tuple appears in `PHASE_B_HANDLERS` → a custom handler renders that source.
- **Generic per-record fallback**: service not in `_PHASE_B_SKIP_PER_REC` and no handler → generic renderer in `html_report_generator.py`.

Verify:

| # | Check | Pass criterion |
|---|-------|---------------|
| L3.2.1 | Every `source_name` returned by the adapter is either rendered by Phase A, has a Phase B handler, or is handled by the generic fallback | no orphan source name |
| L3.2.2 | Field names the renderer reads (e.g. `Resource`, `Recommendation`, `EstimatedSavings`, `CheckCategory`, `priority`, `Reason`, `InstanceType`, etc.) are **present** in every recommendation the adapter emits | grep handler body for `rec.get(...)` and confirm adapter supplies those keys |
| L3.2.3 | Priority/severity field uses one of the values `_priority_class` in `reporter_phase_a.py:14` recognises (`high|critical|medium|warning|low|info|informational`) | otherwise priority class silently drops to "" |
| L3.2.4 | `optimization_descriptions` (if set) covers every distinct `CheckCategory` the adapter emits | unmapped categories render with no description |
| L3.2.5 | `extras` keys the HTML report expects (e.g. `instance_count`, `volume_counts`, `bucket_count`) are present and match their expected types | renderer-side `result.get(...)` cross-checked |

### L3.3 — Numeric formatting and consistency

| # | Check | Pass criterion |
|---|-------|---------------|
| L3.3.1 | `EstimatedSavings` strings that the renderer displays follow the `$N.NN/month` (or `Up to $N.NN/month`) pattern that `parse_dollar_savings` understands | sample 3 recs, regex-match |
| L3.3.2 | When the adapter emits a numeric `EstimatedMonthlyCost` (e.g. EBS unattached volumes), the renderer column header reads "cost" not "savings" | label matches semantic |
| L3.3.3 | `total_monthly_savings` displayed at the service tab top matches `sum(per-rec savings)` shown in the table to within rounding | hand-verify or use snapshot test |
| L3.3.4 | Dark-mode and light-mode HTML both render every recommendation (no CSS class hides recs in one theme) | inspect rendered HTML for both themes |

### L3.4 — Warning and permission propagation

| # | Check | Pass criterion |
|---|-------|---------------|
| L3.4.1 | Permission issues raised by this adapter appear in the JSON `permission_issues[]` array | `ctx._permission_issues` carries the record |
| L3.4.2 | Warnings raised by this adapter appear in the JSON `scan_warnings[]` array | same |
| L3.4.3 | Both arrays are surfaced in the HTML report's "Permission Issues" / "Scan Warnings" sections (not silently dropped) | scan a permission-denied fixture and inspect HTML |

### L3.5 — Empty-state behavior

| # | Check | Pass criterion |
|---|-------|---------------|
| L3.5.1 | Adapter on an account with **zero** resources of this type returns `ServiceFindings(total_recommendations=0, total_monthly_savings=0.0, sources={...empty SourceBlocks...})` | unit test with moto/stub |
| L3.5.2 | HTML report still includes the service tab (or hides it consistently with sibling services) when totals are zero | snapshot inspection |
| L3.5.3 | No `KeyError`, `IndexError`, or `TypeError` raised on the zero-resource path | error-isolated by `safe_scan`, but also clean by construction |

**L3 verdict**: `PASS` only if every L3.* check passes. Any L3.2.2 missing-field that produces blank cells in the HTML → `HIGH` and `WARN`. Any L3.1.4 JSON-incompatible type → `FAIL`. Any L3.4 swallowed warning → `HIGH`.

---

## 4. Cross-layer red flags (auto-FAIL if found)

If **any** of these conditions hold, the overall verdict for the service is `FAIL` regardless of layer scores:

1. The adapter writes to AWS (any non-read API call) — violates the read-only contract.
2. `total_monthly_savings` includes a dollar amount the adapter cannot trace to a source (per L2.1).
3. The adapter bypasses `ClientRegistry` (direct `boto3.client(...)`).
4. A pricing constant exists in the adapter that contradicts `core/pricing_engine.py` `FALLBACK_*` (two different "fallback" prices for the same SKU).
5. Recommendations contain raw AWS account-IDs or ARNs that include a **different** account than `ctx.account_id`.
6. The adapter mutates `ALL_MODULES` or any other module-level shared state.
7. Service key collisions with another adapter's `key` or `cli_aliases`.

---

## 5. Verification commands

Before declaring `PASS`, run **all** of these for the audited service:

```bash
# 1. Static checks
ruff check services/adapters/<SERVICE>.py services/<SERVICE>.py
mypy services/adapters/<SERVICE>.py

# 2. Registration check
python3 -c "from services import ALL_MODULES; \
m = next(x for x in ALL_MODULES if x.key == '<SERVICE>'); \
print('key:', m.key); print('aliases:', m.cli_aliases); \
print('clients:', m.required_clients()); \
print('cw:', getattr(m, 'requires_cloudwatch', False))"

# 3. CLI resolution
python3 -c "from core.filtering import resolve_cli_keys; from services import ALL_MODULES; \
print(resolve_cli_keys(ALL_MODULES, {'<SERVICE>'}, None))"

# 4. Offline scan (uses moto / recorded fixtures)
pytest tests/test_offline_scan.py -k <SERVICE> -v

# 5. Regression gate
pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -k <SERVICE> -v

# 6. PricingEngine stats after a real scan (if --live possible)
#    Inspect engine.stats and engine.warnings — fallbacks should be intentional, not silent.
```

---

## 6. Output schema

Produce a single Markdown file named `docs/audits/<SERVICE>.audit.md` with this exact structure:

````markdown
# Audit: <SERVICE> Adapter

**Adapter**: `services/adapters/<SERVICE>.py`
**Legacy shim**: `services/<SERVICE>.py` (if exists, else "none")
**ALL_MODULES index**: <N>
**Renderer path**: <Phase A | Phase B | Generic per-record>
**Date**: <YYYY-MM-DD>
**Auditor**: <name or "automated">

## Verdict
| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | <PASS/CONDITIONAL-PASS/WARN/FAIL> | <count> |
| L2 Calculation | <…> | <count> |
| L3 Reporting   | <…> | <count> |
| **Overall**    | **<worst of three>** | |

## Pre-flight facts
- Adapter class: `<ClassName>` at `services/adapters/<SERVICE>.py:<line>`
- Required boto3 clients: `<tuple>`
- Sources emitted: `<list of source names>`
- Pricing methods consumed (from PricingEngine): `<list>` or "none"
- Test files: `<list>`

## L1 — Technical findings
For each finding:
### L1-<NNN> <short title>  [SEVERITY]
- **Check**: <which L1.x.x check failed>
- **Evidence**: `file:line` — <quoted snippet>
- **Why it matters**: <one sentence>
- **Recommended fix**: <one sentence, concrete>

## L2 — Calculation findings
Same structure. Plus the source-classification table:

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| <rec type> | live/aws-api/parsed/… | `file:line` | YES/CONDITIONAL/WARN/NO | `<tool → URL/args → result>` or `n/a` |

### External validation log

For every CRITICAL/HIGH L2 finding, append a row:

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| L2-NNN | <tool name> | <key args or URL> | <one-line result> | confirms / refutes |

## L3 — Reporting findings
Same structure. Plus the field-presence table:

| Source | Renderer | Required fields | Missing fields |
|---|---|---|---|
| <source name> | <handler or generic> | <list> | <list or "none"> |

## Cross-layer red flags
- <auto-FAIL item> — Evidence: `file:line`
(or: "None.")

## Verification log
```
<paste output of the 6 commands in Section 5>
```

## Recommended next steps (prioritized)
1. [CRITICAL] …
2. [HIGH] …
3. [MEDIUM] …
4. [LOW] …
````

---

## 7. What this audit explicitly does **not** cover

State these up front so the audit's scope is honest:

- IAM policy completeness (covered by separate IAM audit; see README.md "IAM Permissions").
- Long-running performance / pagination throttling under > 10,000 resources (covered by load tests, not this audit).
- Cross-service correctness (e.g. EBS vs. EC2 deduplication is partially covered by L2.5.2 but full cross-service audit is separate).
- Multi-account / Organizations behavior (single-account scope assumed).
- AWS Pricing API SLA / regional availability gaps (audit only verifies the adapter handles failures, not that AWS itself returns sane prices).

If any of these matter for a given service, open a follow-up audit with the explicit out-of-scope item lifted.
