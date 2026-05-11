# Code Audit Prompt — AWS Cost Optimization Scanner

---

## Role

You are a senior code auditor. Strict, evidence-based, zero-fabrication.

---

## Hard Rules (NEVER violate)

- READ each file in full before making any claim about it.
- NEVER report a finding you have not directly observed in the code.
- NEVER paraphrase code — quote the exact line(s) with `file_path:line_number`.
- NEVER fix, refactor, or modify anything. Read-only audit.
- If unsure whether something is a bug, mark it INFO with the uncertainty stated. Do not omit, do not promote.
- Do NOT flag style, naming, or formatting. Only: logic errors, pricing bugs, contract violations, rendering gaps, silent data loss.
- Do NOT generalize from one adapter to others — every finding is per-file, evidence-based.
- Do NOT comment on what code does correctly unless required to explain why something is NOT a finding.

---

## Codebase

`/Users/amartinawi/Desktop/Cost_OptV1_dev/`

---

## Scope

1. **37 service adapters** in `services/adapters/`:
   `ami`, `api_gateway`, `apprunner`, `athena`, `aurora`, `batch`, `bedrock`,
   `cloudfront`, `commitment_analysis`, `compute_optimizer`, `containers`,
   `cost_anomaly`, `cost_optimization_hub`, `dms`, `dynamodb`, `ebs`, `ec2`,
   `eks`, `elasticache`, `file_systems`, `glue`, `lambda_svc`, `lightsail`,
   `mediastore`, `monitoring`, `msk`, `network`, `network_cost`, `opensearch`,
   `quicksight`, `rds`, `redshift`, `s3`, `sagemaker`, `step_functions`,
   `transfer`, `workspaces`

2. **`html_report_generator.py`** — HTML report renderer

---

## Reading Order (MUST follow)

1. `core/contracts.py` — load `ServiceFindings`, `SourceBlock`, `StatCardSpec`,
   `GroupingSpec` definitions into context before reading any adapter.
2. The 37 adapters — one at a time, alphabetically.
3. `html_report_generator.py` — last, after all adapters (renderer findings depend
   on adapter contracts).

After every 5 adapters, output one checkpoint line:
`✅ Audited [a, b, c, d, e]`

Do NOT emit findings during the audit pass — accumulate all findings and emit
the final report only after reading all files.

---

## Part A — Per-Adapter Checks

### A1. ServiceFindings Contract Compliance

- `scan()` MUST return a `ServiceFindings` instance. Any uncaught exception that
  escapes `scan()` is a bug.
- Each `SourceBlock`: `count` MUST equal `len(recommendations)` exactly.
  A mismatch is a silent discrepancy — the HTML report renders `count` as the
  displayed number but iterates `recommendations` for the table rows.
- `total_recommendations` MUST equal `sum(sb.count for sb in sources.values())`
  unless overridden with a documented reason in the code. Flag independent
  computation that could drift.
- `total_monthly_savings` MUST equal `sum(r["monthly_savings"] for all recs across
  all source blocks)` unless computed independently — flag any independent
  computation that could drift from the per-recommendation values.
- `optimization_descriptions` MUST be present and its keys MUST match `sources`
  keys exactly. Flag if `None` or if any `sources` key has no corresponding entry.
- Every `stat_cards` `source_path` MUST resolve against the actual ServiceFindings
  structure:
  - `"extras.X"` → key `X` exists in the `extras` dict
  - `"sources.X.count"` → source key `X` exists in `sources`
  - `"total_monthly_savings"` → always valid
  Flag any path pointing to a key not guaranteed to be present.
- `GroupingSpec(by="field")` → `"field"` MUST appear as a key in at least one
  recommendation dict returned by this adapter. If the field is absent from all
  recommendation dicts, grouping silently produces one unnamed group.

### A2. Pricing Correctness (Highest Risk)

**Why this matters**: Applying `pricing_multiplier` to API-returned dollar values
inflates every finding by the regional factor (e.g., 1.1x in ap-southeast-1),
producing fabricated savings. Not applying it to formula-based calculations
produces undervalued findings.

**RULE 1 — CE/COH API values are USD actuals. NEVER apply `pricing_multiplier`:**
- These APIs return real account spend at actual billed rates, not normalized
  US list prices. Multiplying would inflate findings by the regional factor.
- `commitment_analysis.py`: CE returns real USD on purchase recommendation savings.
  Multiplier is correct on *calculated waste* (hourly_rate × hours × multiplier)
  but NEVER on `estimatedMonthlySavingsAmount` from the API.
- `cost_optimization_hub.py`: COH `estimatedMonthlySavings` is real billed USD.
  NEVER apply `pricing_multiplier`.
- `network_cost.py`: CE `get_cost_and_usage` returns actual account spend.
  NEVER apply `pricing_multiplier` to those values.

**RULE 2 — Formula-based savings MUST apply `ctx.pricing_multiplier`:**
- These are calculated from list prices (typically us-east-1 base rates) and
  must be adjusted for the scanned region.
- Every formula of the form `rate * hours`, `instance_price * count`, or
  `waste_fraction * monthly_cost` MUST include `* ctx.pricing_multiplier`.
- `compute_optimizer.py`: `estimatedMonthlySavings.value * ctx.pricing_multiplier`
  is correct — Compute Optimizer returns normalized US pricing, multiplier adjusts
  for region.

Per adapter, verify:
- No `pricing_multiplier` applied to raw CE/COH API dollar values.
- `pricing_multiplier` applied to every formula-based savings calculation.
- `sagemaker.py`: `SAGEMAKER_INSTANCE_MULTIPLIER` (1.15) MUST be applied inside
  `_get_instance_monthly()` — verify line by line.
- No hardcoded dollar amount without a named module-level constant.
- Every module-level pricing constant MUST have a comment naming its AWS pricing
  source (e.g., `# us-east-1 on-demand, 2024-Q4, aws.amazon.com/sagemaker/pricing`).

### A3. fast_mode Compliance

`fast_mode = True` means skip ALL CloudWatch metric calls and return config-only
checks. This exists for speed and to avoid CloudWatch API costs.

- Every adapter with `reads_fast_mode = True` MUST check
  `fast_mode = getattr(ctx, "fast_mode", False)` and skip CloudWatch metric calls
  when True.
- Every adapter that calls `ctx.client("cloudwatch")` for metric-based optimization
  MUST declare `reads_fast_mode = True`.
- Adapters using CloudWatch only for billing alarm checks or static config reads
  MAY declare `reads_fast_mode = False` — verify this is intentional and documented
  in the class docstring.
- NEVER call `get_metric_statistics` or `get_metric_data` when `fast_mode` is True.

### A4. Error Isolation

- Every boto3 client call MUST be wrapped in try/except.
- `scan()` itself MUST NOT be wrapped in a blanket try/except — error isolation is
  handled by `ScanOrchestrator.safe_scan()`. Wrapping `scan()` would swallow
  unexpected errors silently.
- No `except Exception: pass` that drops results affecting `total_recommendations`
  or `total_monthly_savings` — silent drops produce undercount in the report.
- When `ctx.client("x")` returns None (client unavailable), the adapter MUST return
  `_empty_findings()` or equivalent — NOT a partially-populated ServiceFindings
  with zeroed fields that could mislead the report renderer.

### A5. Pagination

- Every API call that AWS documents as paginated MUST use either:
  (a) `client.get_paginator(...).paginate()`, OR
  (b) a manual loop on `nextToken` / `NextMarker` / `Marker`.
- High-cardinality resources MUST paginate: EC2 instances, EBS volumes, S3 buckets,
  RDS snapshots, Lambda functions — AWS default page size is typically 100.
- Flag any single `list_*` or `describe_*` call on a paginatable resource that does
  not loop.

### A6. Date Construction

- All time period construction MUST use `date.today()` or
  `datetime.now(timezone.utc)` — NEVER a hardcoded date string.
- CE `TimePeriod` end dates are exclusive. Verify the correct form is used per API:
  some CEs accept `end = today.isoformat()`, others require
  `end = (today + timedelta(days=1)).isoformat()`.
- CloudWatch `StartTime`/`EndTime` MUST use `datetime.now(timezone.utc)` — never
  naive datetimes (no timezone = UTC assumed but fragile in cross-region scans).

### A7. Double-Counting

These resource types are covered by multiple adapters. Verify the exclusions exist:

- `compute_optimizer.py` MUST skip EC2 instances — already covered by `ec2.py`.
- `cost_optimization_hub.py` MUST exclude recommendation types routed to existing
  adapters via `ctx.cost_hub_splits` (`Ec2Instance`, `LambdaFunction`, `EbsVolume`,
  `RdsDbInstance`, `RdsDbCluster`).
- `eks.py` vs `ec2.py`: EKS nodes appear as EC2 instances — verify no savings are
  double-counted between the two adapters.
- `aurora.py` vs `rds.py`: Aurora clusters can appear in both — verify Aurora clusters
  are either filtered out of `rds.py` OR that `aurora.py` covers only checks that
  `rds.py` does not perform.

### A8. required_clients() Accuracy

- Every boto3 client called anywhere in `scan()` MUST be listed in
  `required_clients()`.
- No client listed in `required_clients()` that is never actually called in `scan()`.
- `bedrock` and `bedrock-agent` are distinct boto3 service names — if both are used,
  both MUST be listed separately.

---

## Part B — html_report_generator.py

Read the full file before auditing. Key methods to focus on:
- `_get_service_content()` — per-service rendering entry point
- `_get_service_stats()` — stat card rendering
- `_get_detailed_recommendations()` — grouping and table rendering
- `_get_trends_section()` — Chart.js trends dashboard
- `_get_executive_summary_content()` — summary tab

### B1. stat_cards Rendering

- The `source_path` resolver MUST handle all three path forms correctly:
  - `"total_monthly_savings"` — top-level field
  - `"extras.X"` — nested key in extras dict
  - `"sources.X.count"` — nested count in sources dict
- A `source_path` that resolves to `None` or a missing key MUST display `"0"` or
  `"—"`, NEVER raw `"None"` and NEVER an unhandled exception.
- Formatters MUST behave exactly:
  - `"currency"` → `"$1,234.56"` — NEVER `"$None"`, NEVER `"$0.0"` unrounded
  - `"int"` → `"42"` — NEVER `"42.0"`, NEVER `"None"`
  - `"percent"` → `"73.2%"` — NEVER `"73.2"`, NEVER `"0.73"`
- Stat card rendering MUST be data-driven for all 37 adapters, not a hardcoded
  service list — flag any conditional that skips unknown adapter keys.

### B2. GroupingSpec Rendering

- `GroupingSpec(by="field")` → recommendations MUST be grouped by that field value.
- A recommendation dict missing the `by` field MUST fall into an `"Other"` group —
  NEVER silently dropped.
- Group headers MUST show a human-readable label, not the raw field value.
- Groups with `count = 0` MUST NOT render as empty sections.
- `grouping = None` → flat table MUST render without exception.

### B2a. Finding-Based Grouping (All Renderers)

Resources sharing the same `finding` value MUST render as a SINGLE card listing
all affected resources — NEVER as one card per resource.

- Each Phase B handler and generic renderer MUST group by `finding` (or equivalent
  category field) before emitting HTML. A flat loop over recommendations that emits
  one `<div class="rec-item">` per item is a rendering bug when multiple items share
  the same finding.
- The card header MUST state the finding and resource count:
  e.g., `Finding: OVER_PROVISIONED (5 resources)`.
- Affected resources MUST be listed inside the card as a `<ul>` — not as separate
  top-level cards.
- Verify this holds for every Phase B handler in `PHASE_B_HANDLERS` and for every
  service that falls through to `_render_generic_other_rec`.

### B3. optimization_descriptions Rendering

- When `optimization_descriptions` is present, each source block MUST display its
  title and description.
- When `optimization_descriptions` is `None` (currently: `cost_anomaly.py` and
  `eks.py` both return ServiceFindings without this field), the renderer MUST
  degrade gracefully — no `KeyError`, no `AttributeError`, no blank crash.
- Locate the rendering path near `_get_service_content()` (~line 2003) and verify
  the `None` / missing-key guard is in place.

### B4. None Safety and Edge Cases

- `service_data.get("total_monthly_savings", 0.0)` — NEVER raw dict access without
  a default.
- `service_data.get("extras", {})` — extras can be absent; raw access crashes.
- `sources` dict access MUST handle adapters that return zero source blocks.
- Empty `recommendations` tuple MUST render a "No issues found" message — NEVER an
  empty table with headers and no rows.
- `monthly_savings` of `0.0` or absent on individual recommendations MUST be handled
  by formatters gracefully — `$0.00` is a valid savings value for governance findings.

### B5. Trends Section (Chart.js)

- `_get_trends_section()` MUST handle `trend_analysis = None` in the scan result
  (occurs when Cost Explorer is unavailable) — MUST render a "Trend data
  unavailable" message, not an empty chart or a JavaScript runtime error.
- All user-controlled strings injected into chart datasets (service names, date
  strings) MUST be JSON-encoded — NEVER raw f-string interpolation into JavaScript.
  Raw interpolation is an XSS vector if service names contain quotes or backslashes.
- The CDN script tag MUST use HTTPS: `https://cdn.jsdelivr.net/npm/chart.js`.
- The page MUST load even if the CDN is unreachable — verify the Chart.js script
  tag uses `defer` or `async` and that the rest of the report does not depend on it.

### B6. All 37 Services Render

- Trace the rendering logic for all 37 adapter keys and verify none produces an
  unhandled exception.
- Specifically verify the recently-added adapters render without gaps:
  `aurora`, `commitment_analysis`, `compute_optimizer`, `cost_optimization_hub`,
  `bedrock`, `sagemaker`, `network_cost`, `cost_anomaly`, `eks`.
- The tab navigation MUST include entries for all 37 services.
- No service MUST be silently skipped via a hardcoded allowlist.

### B7. Executive Summary Accuracy

- Total recommendations displayed MUST equal
  `sum(f.total_recommendations for f in all_findings)`.
- Total savings displayed MUST equal
  `sum(f.total_monthly_savings for f in all_findings)`.
- Top savings opportunities list MUST be sorted by `total_monthly_savings`
  descending.
- Services with `total_recommendations = 0` MUST be excluded from the top-issues
  section but MUST be counted in the total services scanned figure.

---

## Output Format (MUST follow exactly)

Two sections: **Part A** and **Part B**. Within each section, group findings by
severity in this order: `### CRITICAL` → `### HIGH` → `### MEDIUM` → `### LOW / INFO`.

Each finding MUST use this exact structure:

```
**[CATEGORY]** `path/to/file.py:LINE`
> exact code quote from that line

1–3 sentences describing the bug and its impact. Not the fix.
```

End each Part with a summary table:

| Adapter / File | Severity | Category | Finding (≤12 words) |
|---|---|---|---|

End the entire report with a **Clean** section listing every adapter with zero findings.

---

## Final Lock

- Any finding without an exact `file:line` + direct code quote MUST be omitted —
  not downgraded, not marked INFO. Omitted.
- If a file cannot be read, state `Could not read [path]` and skip it entirely.
  Do not guess at its contents.
- Do NOT propose fixes.
- Do NOT modify code.
- Do NOT emit partial findings during the audit pass — accumulate, then report.
