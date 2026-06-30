# DMS Adapter Cost-Audit Prompt

A deep, DMS-specific audit brief in the same structure as the Lambda /
RDS / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* DMS code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `dms`:
> - None beyond the cross-cutting lessons — run the invariant sweeps in `_LIVE_AUDIT_LESSONS.md` and the known-issue catalogue below (advisory-leak, string↔numeric agreement, flat-global rate scaling, dedup granularity, silent-failure classification).

You are auditing the **`dms`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the recently-audited
**Lambda** adapter (`services/adapters/lambda_svc.py`) as the worked example for
the `mark_zero_savings_advisory` / metric-gated `$0`-advisory pattern, the
arch/constant-aware pricing discipline, the structured `AuditBasis`, and the
test style I expect; treat **RDS** (`services/adapters/rds.py`,
`services/rds_logic.py`) as the model for CloudWatch-gated non-prod findings and
cross-source dedup; and **EC2** (`services/adapters/ec2.py`) as the model for the
exact current→target price-delta and the `$0`-placeholder→warning pattern.

### NOTE on structure (DMS is a small flat-factor adapter with a CPU gate)
- The adapter is `services/adapters/dms.py` → `DmsModule.scan`. It delegates
  discovery to **`services/dms.py` → `get_enhanced_dms_checks(ctx)`** (a free
  function, NOT a legacy `ServiceModule`), then re-prices everything itself.
- `get_enhanced_dms_checks` returns `{"recommendations": [...], "checks": {...}}`
  where `checks` has THREE buckets — `serverless_migration`,
  `instance_rightsizing`, `unused_instances`. **`serverless_migration` is
  intentionally always empty** (the "Variable based on usage" DMS Serverless
  monitor finding was removed as a non-quantified nudge; the
  `describe_replication_configs` paginator runs but emits nothing).
- The adapter emits SourceBlocks named after those buckets PLUS a fourth,
  `multi_az_review`, that it constructs itself in `scan()` (non-prod Multi-AZ →
  Single-AZ). So the emitted sources are `serverless_migration` (count 0),
  `instance_rightsizing`, `unused_instances`, `multi_az_review`.
- **Pricing strategy:** live only. `ctx.pricing_engine.get_instance_monthly_price(
  "AWSDatabaseMigrationSvc", instance_class.replace("dms.", ""))`. There is **no
  fallback constant** — a pricing miss (`monthly == 0`) is explicitly skipped
  ("skip rather than fabricate $50 fallback"), and an unknown `InstanceClass` is
  skipped too. `savings += monthly * 0.35` for the rightsizing factor;
  `savings += monthly * 0.5` for the Multi-AZ→Single-AZ factor.
- **The `EstimatedSavings` strings are display-only.** Unlike the network /
  ami adapters, DMS does NOT route through `parse_dollar_savings`. The counted
  number is `ServiceFindings.total_monthly_savings`, computed independently in
  `scan()` from the live price × factor. So the human string
  ("Rightsize for ~35% savings…", "Full instance cost if terminated") can
  **desync** from the counted dollars — check this explicitly.
- DMS consumes **neither Cost Optimization Hub nor Compute Optimizer**. It is not
  in `core/scan_orchestrator.py` `_HUB_SERVICES` / `type_map`, and pulls no CO
  helper. A "missing CoH/CO source" finding is NOT fair game here — drop that
  axis. (DMS has no `currentResourceType` bucket in `type_map` at all.)
- **Rendering is Phase A.** `dms` is in `reporter_phase_a.py` `_PHASE_A_SERVICES`
  and `PHASE_A_DESCRIPTORS["dms"]` (`extract_detail=_extract_dms_details`,
  `savings_mode="conditional"`, `close_div_location="inner"`,
  `PHASE_A_INNER_CLOSE_GROUP`). `should_skip_section_header("dms")` is True.
  There is NO `("dms", …)` entry in `PHASE_B_HANDLERS`, and `dms` is NOT in
  `_PHASE_B_SKIP_PER_REC` — the Phase A source-loop renders it grouped by
  `CheckCategory`. `html_report_generator._get_affected_resources_list` lists
  `dms`.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`. DMS is NOT in the Live-Pricing table row
    list explicitly — confirm its actual strategy from the code: live
    `get_instance_monthly_price("AWSDatabaseMigrationSvc", …)` with **no
    fallback constant**, flat 0.35 / 0.50 factors. Note any doc drift.
0b. Confirm module identity in `services/adapters/dms.py`: `key="dms"`,
    `cli_aliases=("dms",)`, `display_name="DMS"`, `reads_fast_mode=True`,
    `required_clients()` → `("dms", "cloudwatch")`. **Flag the contradiction:**
    `reads_fast_mode=True` is declared, but `get_enhanced_dms_checks` reads
    CloudWatch (`get_metric_statistics` CPUUtilization) **unconditionally** —
    there is no `ctx.fast_mode` check anywhere in `services/dms.py`. Decide
    whether fast-mode is actually honored.
0c. DMS has **no AWS advisory source** (no CoH / CO) and a single CloudWatch
    metric (CPUUtilization). Focus on: pricing accuracy (the Single-AZ vs
    Multi-AZ SKU ambiguity), the intra-adapter double-count across the three
    CPU buckets, the Multi-AZ factor stacking, the swallowed CloudWatch failure,
    and the display-string-vs-counted-number desync.

### Phase 1 — Understand the code (read before judging)
1. Read the full path: `services/adapters/dms.py` (`DmsModule.scan`),
   `services/dms.py` (`get_enhanced_dms_checks`, `DMS_OPTIMIZATION_DESCRIPTIONS`),
   `core/contracts.py` (`ServiceFindings`, `SourceBlock`), `services/_base.py`
   (`BaseServiceModule`), `core/pricing_engine.py`
   (`get_instance_monthly_price` ~line 737 and `_fetch_generic_instance_price`
   ~line 1241 → `_call_pricing_api`), `core/scan_orchestrator.py`,
   `core/result_builder.py`, and the reporter
   (`reporter_phase_a.py` `_extract_dms_details` ~line 185,
   `PHASE_A_DESCRIPTORS["dms"]` ~line 292, `_render*`/`render_grouped_by_category`;
   `html_report_generator.py` `_get_affected_resources_list`).
2. List **every** cost check and for each give: trigger condition, data source
   (DMS describe-API vs CloudWatch metric vs adapter-level config heuristic),
   the exact `EstimatedSavings` string template, the factor it applies in
   `scan()`, and whether the counted dollar comes from `scan()`'s pricing math
   or the display string. Map each to its emitting SourceBlock. The known
   inventory to confirm:
   - **`instance_rightsizing`** — `status=="available"` AND `avg_cpu < 30`
     (7-day AWS/DMS CPUUtilization, `Period=3600`, `Average`). String:
     `"Rightsize for ~35% savings on instance cost"`. Counted: `monthly * 0.35`.
   - **`unused_instances`** — same instance, additionally `avg_cpu < 5`. String:
     `"Full instance cost if terminated"`. Counted: ALSO `monthly * 0.35` (the
     `scan()` loop applies 0.35 to **every** rec, including unused).
   - **`instance_rightsizing` (except fallback)** — if the CloudWatch call raises,
     the inner `except Exception:` at `services/dms.py:100` appends a rightsizing
     rec anyway (`"Review replication instance utilization…"`, `Note:` "Verify
     actual CPU…"). Counted: `monthly * 0.35`, with NO metric behind it.
   - **`multi_az_review`** — adapter-level: `rec.get("MultiAZ")` true AND the
     identifier/tags contain `dev|test|staging|sandbox|nonprod`. Counted:
     `monthly * 0.5` (or `"~50% of instance cost"` advisory string + `$0` when the
     price is unavailable, via `EstimatedMonthlySavings`).
   - **`serverless_migration`** — always empty (count 0). Confirm it emits nothing.

### Phase 2 — Accuracy of every number (validate with MCP)
3. Validate the DMS replication-instance rate against the live Pricing API
   (`AWSDatabaseMigrationSvc`, attribute `instanceType`, region scaling via the
   engine). Confirmed facts to re-verify and build on:
   - The Pricing API `instanceType` is **un-prefixed** (`t3.medium`, `c5.large`),
     so `instance_class.replace("dms.", "")` is correct.
   - **There are TWO SKUs per instance type** — Single-AZ
     (`usagetype InstanceUsg:dms.<type>`) and Multi-AZ
     (`usagetype Multi-AZUsg:dms.<type>`), and **Multi-AZ is exactly 2× Single-AZ**:
     `dms.t3.medium` = `$0.0745/hr` (Single, ≈ `$54.39/mo`) vs `$0.149/hr`
     (Multi, ≈ `$108.77/mo`); `dms.c5.large` = `$0.119/hr` (Single) vs `$0.238/hr`
     (Multi). **`_fetch_generic_instance_price` filters on `instanceType` +
     `location` only and `_call_pricing_api` returns the FIRST `PriceList` row** —
     so the returned price is **non-deterministic** between Single-AZ and Multi-AZ.
     This is the universal "non-deterministic pricing filter (multiple SKUs)"
     issue, made concrete. Quantify the blast radius: a Single-AZ instance priced
     off the Multi-AZ SKU is counted at 2× its real rate.
   - **0.35 is a reduction factor, not a price delta.** No target instance class
     is chosen; the saving is `current_price × 0.35`, not `current − target`.
     Compare against the EC2 model (exact current→target delta) and judge whether
     35% is defensible for a "rightsize or move to Serverless" lever with no
     target sizing.
   - **Multi-AZ 0.5 factor** is only correct if the live price returned is the
     **Multi-AZ** price (since Multi-AZ = 2× Single, the saving of dropping to
     Single = the Single-AZ price = 50% of the Multi-AZ bill). If the
     non-deterministic lookup returns the **Single-AZ** SKU, `monthly * 0.5`
     under-states the real saving by half. Tie this directly to the SKU ambiguity
     above and recommend pinning `usagetype` to `Multi-AZUsg`/`InstanceUsg`
     explicitly.
   - Validate DMS Serverless (DCU) is genuinely NOT priced (the serverless bucket
     is empty) — confirm no fabricated Serverless dollars leak anywhere.
4. Confirm each counted finding is defensible from the report alone. A 35%
   rightsizing number on a 0-datapoint instance, or a Multi-AZ number whose SKU
   side is unknown, is a finding. Record a structured **AuditBasis** (rate /
   region / metric-window / formula / Single-vs-Multi-AZ SKU) on each counted
   finding, as the Lambda/RDS audits did.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter (the big one):** an instance with `avg_cpu < 5` produces a rec
   in BOTH `instance_rightsizing` (`< 30`) AND `unused_instances` (`< 5`). The
   `scan()` loop iterates **all** `recs` and applies `monthly * 0.35` to each, so
   the same instance is counted **twice** (≈ 0.70× monthly) — and the
   `unused_instances` rec's own string claims "Full instance cost", a third,
   inconsistent basis. Prove this with a real or synthetic low-CPU instance and
   decide the correct single owner (an unused instance should be counted once,
   at full terminate value or as rightsizing — not both).
6. **Multi-AZ stacking:** the same low-CPU non-prod Multi-AZ instance is counted
   at `0.35` (rightsizing) AND `0.5` (Multi-AZ→Single-AZ) — two heuristic levers
   discounting the same bill (potentially `0.35 + 0.35 + 0.5 = 1.2×` the real
   monthly cost). Decide whether Multi-AZ removal and rightsizing can legitimately
   stack, and if not, fix by removal/ordering, not double-count.
7. **Cross-adapter:** DMS replication instances are not surfaced by any other
   adapter (EC2/RDS do not enumerate `describe_replication_instances`) and no
   `_extract_*` helper in `html_report_generator.py` pulls DMS into a synthetic
   tab — confirm both, then drop this axis.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Confirm full pagination of `describe_replication_instances` and
   `describe_replication_configs`. Confirm the gate `status == "available"` does
   not silently exclude billable-but-not-"available" instances (e.g.
   `modifying`, `storage-full`) that still incur cost.
9. Are whole classes skipped? DMS Serverless (DCU-based) is entirely unpriced
   (intentional — document it). Multi-AZ detection relies on tag/identifier
   keywords for "non-prod" — a non-prod instance without those keywords is
   silently skipped for the Multi-AZ lever. The `avg_cpu` fallback
   `sum(...) / max(len(datapoints), 1)` makes a **zero-datapoint** instance
   compute `avg_cpu = 0.0`, which passes BOTH `< 30` and `< 5` — a brand-new or
   metric-less instance is flagged as unused AND rightsizable. Confirm this is
   the actual behavior and treat it as a coverage/false-positive bug.

### Phase 5 — Silent failures (nothing fails quietly)
10. Enumerate every swallow:
    - `services/dms.py:100` `except Exception:` around the CloudWatch
      `get_metric_statistics` call — it swallows ANY failure (incl.
      `AccessDenied`, `ThrottlingException`, `OptInRequired`) and **still appends
      a counted 35% rightsizing rec** with no `ctx.warn` / `ctx.permission_issue`.
      This both hides a permission/throttle problem AND fabricates a saving on an
      instance with no usable metric. Classify:
      `AccessDenied`/`UnauthorizedOperation`/`OptInRequired` → `ctx.permission_issue`,
      other → `ctx.warn`; and gate the fabricated rec.
    - `services/dms.py:123` `except Exception: pass` around the serverless
      paginator — a `describe_replication_configs` permission gap vanishes
      silently. Record it.
    - `services/dms.py:126` outer `except Exception as e: ctx.warn(...)` — confirm
      it records but does not classify permission errors as `permission_issue`.
11. A pricing miss (`monthly == 0`) is skipped for the dollar tally but the rec
    is still emitted by the shim — confirm such a rec renders with `$0`/no
    savings and is NOT counted toward `total_monthly_savings`, and decide whether
    a `$0` rightsizing rec should be advisory or dropped (mirror the Lambda
    `Counted=False` advisory pattern).
12. **CloudWatch gating / fast-mode:** `reads_fast_mode=True` is declared but the
    CPUUtilization read in `services/dms.py` is NOT gated on `ctx.fast_mode`.
    Confirm and decide: either honor fast-mode (skip the CW read, emit advisory)
    or drop the `reads_fast_mode` claim. Mirror the Lambda fast-mode fix.

### Phase 6 — Reporting (one tab, counted == rendered)
13. **Phase A render wiring:** trace `_extract_dms_details` (`reporter_phase_a.py`
    ~185). It reads `rec.get("InstanceId")`. **The `multi_az_review` recs carry
    `"Resource"`, NOT `"InstanceId"`** (and no `AvgCPU`), so they render with
    resource id `"Unknown"` in the DMS tab. Confirm this desync and fix the key
    (or the extractor). Verify all four sources
    (`serverless_migration`/`instance_rightsizing`/`unused_instances`/`multi_az_review`)
    flow through the Phase A `CheckCategory` grouping and that the empty
    `serverless_migration` (count 0) renders nothing rather than an empty group.
14. **Counted == rendered:** confirm `total_recommendations =
    len(recs) + len(multi_az_recs)` and that `total_monthly_savings` equals the
    sum of the per-rec counted dollars. Critically, the **display
    `EstimatedSavings` string can differ from the counted number** (unused says
    "Full instance cost" but counts 35%; rightsizing says "~35%" as prose). Check
    the rendered card shows a number consistent with what is summed into the
    headline, and reconcile the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings`) against the
    per-service DMS total.
15. Confirm no finding is counted but dropped from the table (or vice-versa)
    across all four sources and all `CheckCategory` groups.

### Phase 7 — Tooling & evidence
16. Run a real scan scoped to DMS:
    `python3 cli.py <region> --scan-only dms`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service dms`.
    Triage every: silent failure, `$0`/missing-savings finding, and instance
    appearing in more than one source (the canonical example: a `< 5%` CPU
    instance in BOTH `instance_rightsizing` and `unused_instances`). Reconcile the
    headline against the per-source sum. Caveats: DMS is often absent — try a
    region that actually has replication instances; CloudWatch metrics may be
    empty (exercise the `avg_cpu = 0.0` fallback and the `except` path). Use
    `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`
    (`services/dms.py` imports `from datetime import UTC`).
17. For any duplication claim, prove it: show the same `InstanceId` in two
    sources with two counted contributions. For any accuracy claim, show the AWS
    Pricing API value next to the scanner's number — e.g. the Single-AZ
    (`InstanceUsg:dms.t3.medium` $0.0745/hr) vs Multi-AZ
    (`Multi-AZUsg:dms.t3.medium` $0.149/hr) SKUs and which one
    `get_instance_monthly_price` actually returned.

### Deliverable
- The complete check list (Phase 1.2), per source, with the counted basis and
  the display string marked where they diverge.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add a `tests/test_dms_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive
  `DmsModule.scan` with a `SimpleNamespace` ctx + a fake `dms` client/paginator
  and a fake `cloudwatch` client returning controlled `Datapoints`; assert the
  double-count fix (a `< 5%` instance counted once), the Single-AZ/Multi-AZ SKU
  pinning, the Multi-AZ-vs-rightsizing stacking rule, the swallowed-CloudWatch
  classification (`permission_issue` vs `warn` and no fabricated 35% rec),
  fast-mode skip, the `multi_az_review` `Resource`/`InstanceId` render key, and
  counted == rendered.
- For any heuristic that assumes a usage target (35% with no target class; a rec
  on a 0-datapoint instance) replace it with a defensible delta or make it a `$0`
  advisory (`Counted=False`) — respect `ctx.fast_mode` and never fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula /
  Single-vs-Multi-AZ SKU) on each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for DMS first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same SKU-ambiguity / swallowed-CloudWatch / display-desync bug
  in a sibling adapter out of scope, note it as a follow-up (don't fix unprompted).
- Update the `dms` row/notes in `services/adapters/CLAUDE.md` to match reality.
- Stage ONLY the files you changed when committing.

### Known-issue catalogue to check against

**Universal (from prior audits — verify each is absent or fix it):**
- Usage savings computed from a config dimension alone (memory/size/capacity/RCU-WCU/DPU)
  with NO usage metric → fabricated $.
- Wrong architecture/edition/OS/license/node-type pricing (arm64 as x86; BYOL as
  license-included; Windows as Linux; SQL/Oracle edition default; reserved as on-demand).
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`) instead of pinned filters.
- Region: hardcoded constant/fallback not region-scaled via `pricing_multiplier`, OR
  `pricing_multiplier` double-applied on an already-region-correct engine/CO path.
- Per-unit RATE string ($/GB, $/hour, $/request, $/1K) counted as a monthly total —
  must be rejected by `parse_dollar_savings` → $0 advisory.
- Free-tier / free resource (Lambda free tier; Gateway VPC endpoints; free per-ENI IP;
  free backup allotment) recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority dedup
  CoH > CO > heuristic, by NORMALIZED resource id (strip ARN; mind version/alias/cluster-vs-instance).
- Two heuristic checks stacking on the same resource (rightsize + migrate discount the
  same bill), or SUBSET redundancy (one population ⊆ another) — fix by removal not dedup.
- Reduction factor instead of exact price delta (`price × factor` vs `current − target`);
  validated factors off 2-3×.
- $0 "enable X"/opt-in placeholder (CO `ResourceId=compute-optimizer-service`) counted
  as a recommendation instead of converted to `ctx.warn` and dropped.
- Metric-gated $0 nudge rendered as a COUNTED opportunity instead of advisory (`Counted=False`).
- Cost Hub: (a) a `currentResourceType` with no `type_map` bucket → dropped (warns only
  on a full scan); (b) a bucket populated but consumed by NO adapter → dropped with NO
  warning (dead-renderer tell; known orphans: elasticache / opensearch / redshift / s3).
- A source the adapter emits with no `PHASE_B_HANDLERS` entry in a
  `_PHASE_B_SKIP_PER_REC` service → renders nothing, silently.
- Render-time substring/category/Optimized/RI filter desyncing the headline from the
  visible cards (filter at the SOURCE, not at render).
- Coverage gated to a hardcoded family/type/size/state allowlist, only-running/
  only-provisioned, or a scaled-to-zero/idle resource flagged for savings.
- CloudWatch / Cost Explorer / CO / CoH permission or throttling failure logged via
  `logger` only, not recorded via `ctx.warn` / `ctx.permission_issue`
  (AccessDenied/Unauthorized/OptInRequired → permission_issue).
- CloudWatch reads not gated on `ctx.fast_mode` (and `reads_fast_mode` not declared);
  agent-metric dimension mismatch (CWAgent mem/disk under more dimensions than InstanceId
  → `get_metric_statistics` by InstanceId alone silently returns nothing).
- Heuristic that assumes a usage target ("shrink to 20GB") with no usage evidence.
- Cross-adapter overlap (same volume/IP/snapshot/ASG/instance/cluster in two tabs) —
  single responsibility; add to the dedup `covered` set.
- Spot/discounted resources priced at on-demand; Spot recommended without an explicit
  interruptible-workload signal.
- RI / SP buy recommendation overlapping a rightsizing lever — keep RI/SP advisory,
  rightsize first.
- Each counted finding must carry a structured AuditBasis (rate/region/metric-window/
  formula) so the number is defensible from the report alone; counted == rendered.

**DMS-specific (found while grounding this prompt — confirm each):**
- **Single-AZ vs Multi-AZ SKU ambiguity.** `get_instance_monthly_price(
  "AWSDatabaseMigrationSvc", <type>)` filters `instanceType` only, but each type
  has TWO SKUs (`InstanceUsg:dms.<type>` Single-AZ and `Multi-AZUsg:dms.<type>`
  Multi-AZ = 2×). `_call_pricing_api` returns the first row → the priced rate (and
  therefore the 35% and 50% savings) is non-deterministic and can be 2× off. Pin
  `usagetype`.
- **Triple-counting a `< 5%` CPU instance.** It lands in both
  `instance_rightsizing` and `unused_instances`; the `scan()` loop applies
  `monthly * 0.35` to each (≈ 0.70× the bill), and the unused rec's string claims
  "Full instance cost" — three inconsistent bases on one instance.
- **Multi-AZ factor stacked on rightsizing.** A non-prod Multi-AZ low-CPU instance
  is counted at `0.35` (rightsizing) AND `0.5` (Multi-AZ→Single-AZ) on the same
  bill — decide if these levers may legitimately stack.
- **Swallowed CloudWatch failure still emits a counted rec.** `services/dms.py:100`
  `except Exception:` hides `AccessDenied`/throttling AND appends a 35% rightsizing
  rec with no metric — neither `ctx.warn`/`ctx.permission_issue` nor a `$0`
  advisory.
- **Zero-datapoint → `avg_cpu = 0.0`.** `sum(...) / max(len, 1)` makes a
  metric-less instance pass both `< 30` and `< 5`, flagging it unused AND
  rightsizable with no evidence.
- **`reads_fast_mode=True` not honored.** The CPUUtilization read in
  `services/dms.py` is never gated on `ctx.fast_mode`.
- **Phase A render desync.** `_extract_dms_details` reads `InstanceId`, but
  `multi_az_review` recs use `Resource` → rendered as `"Unknown"`.
- **0.35 is a flat reduction factor, not a current→target delta** — no target
  instance class is selected (contrast EC2's exact delta).

## PROMPT (end)
