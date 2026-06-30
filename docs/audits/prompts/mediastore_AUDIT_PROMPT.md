# MediaStore Adapter Cost-Audit Prompt

A deep, MediaStore-specific audit brief in the same structure as the Network /
Lambda / RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* MediaStore code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.
MediaStore is the canonical **borrowed-rate** adapter — it prices container
storage at the **S3 Standard $/GB** rate because AWS Elemental MediaStore is a
retired media-object store with no dedicated PricingEngine method.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `mediastore`:
> - This service emits `$0` advisory recs ALONGSIDE counted ones (it is a counted/advisory split, not advisory-only) — verify the tab still renders even when ALL recs happen to be advisory (D2; the tab gate keys off RENDERED cards, counted + advisory, not the counted-only headline count), and confirm no `Counted=False` rec carries a non-zero numeric (advisory-leak, B1).
> - (B2) On the counted path the adapter overwrites `EstimatedMonthlySavings` with the PricingEngine-derived rate (no multiplier) but does NOT update `rec["EstimatedSavings"]`, leaving the shim's string (computed at `0.023 × ctx.pricing_multiplier`) stale — verify string and numeric agree to the cent in every branch.

You are auditing the **`mediastore`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **S3** adapter
(`services/adapters/s3.py`, `docs/audits/S3_AUDIT_FINDINGS.md`) as the canonical
storage-pricing model whose `get_s3_monthly_price_per_gb` rate MediaStore
borrows, and the recently-audited **Lambda** adapter
(`services/adapters/lambda_svc.py`) as the worked example for the
`mark_zero_savings_advisory` pattern, rate-string rejection, AuditBasis, and the
test style I expect.

### NOTE on structure (mediastore is NOT shaped like a CoH or PricingEngine-native adapter)
- The adapter `services/adapters/mediastore.py` (`MediastoreModule.scan`)
  consumes ONE helper, `get_enhanced_mediastore_checks` in
  `services/mediastore.py`, and emits a **single `enhanced_checks` SourceBlock**.
- MediaStore is covered by **neither Cost Optimization Hub nor Compute
  Optimizer** — it is not in `_HUB_SERVICES` / `type_map`, and pulls no CO
  helper. A "missing CoH/CO source" finding is NOT fair game here.
- **Borrowed rate (the central pricing fact):** the adapter prices storage at the
  **S3 Standard** rate. When `ctx.pricing_engine` is present it calls
  `ctx.pricing_engine.get_s3_monthly_price_per_gb("STANDARD")` (region-correct,
  no multiplier); otherwise it falls back to `0.023 × ctx.pricing_multiplier`
  (module constant × multiplier — L2.3.2). The savings = `EstimatedStorageGB ×
  price_per_gb`. NOTE: the helper ALSO computes a parallel `savings_str` using
  its OWN inline `0.023 × ctx.pricing_multiplier` storage rate PLUS a `0.02 $/GB`
  ingest cost — that string is then OVERWRITTEN by the adapter's `EstimatedMonthlySavings`.
  Trace which number actually counts.
- There is **NO advisory split** and **no `Counted` flag**: `total_recommendations
  = len(recs)` counts EVERY emitted rec regardless of whether it carries a dollar.
  Confirm whether a $0 rec (no `EstimatedStorageGB`) inflates the count — it is
  emitted with `EstimatedMonthlySavings = 0.0` + a `PricingWarning` but is STILL
  in `recs` and thus counted by `len(recs)`.
- Rendering is **generic Phase-B per-rec**: mediastore is NOT in `_PHASE_A_SERVICES`,
  NOT in `_PHASE_B_SKIP_PER_REC`, and has NO entry in `PHASE_B_HANDLERS`, so
  `should_fallback_to_per_rec` is True → `render_generic_per_rec("mediastore",
  recs, "enhanced_checks")` renders it.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and find the `mediastore.py` row
    (Live Pricing, `get_s3_monthly_price_per_gb("STANDARD")`). **Reconcile the
    doc against reality:** confirm `core/pricing_engine.py` exposes
    `get_s3_monthly_price_per_gb` (~line 665) and that the borrowed S3 rate is
    the intended model. There is NO `get_mediastore_*` PricingEngine method —
    confirm and note the borrow.
0b. Confirm module identity in `services/adapters/mediastore.py`:
    `key="mediastore"`, `cli_aliases=("mediastore",)`, `display_name="MediaStore"`,
    `reads_fast_mode=True`, `required_clients()` → `("mediastore","cloudwatch")`.
    Note it does NOT declare `requires_cloudwatch` even though
    `get_enhanced_mediastore_checks` reads CloudWatch (`RequestCount`,
    `BytesDownloaded`, `BytesUploaded`, `BucketSizeBytes`) — flag the mismatch.
0c. Note that `reads_fast_mode=True` is declared but the helper
    `get_enhanced_mediastore_checks(ctx)` takes only `ctx` and **never checks
    `ctx.fast_mode`** — every container does the full 14-day CloudWatch sweep
    even in fast mode. Flag this as a fast-mode gating gap (mirror the Lambda fix).

### Phase 1 — Understand the code (read before judging)
1. Read every file in the path: `services/adapters/mediastore.py`,
   `services/mediastore.py` (`get_enhanced_mediastore_checks`,
   `MEDIASTORE_OPTIMIZATION_DESCRIPTIONS`), `services/_savings.py`
   (`parse_dollar_savings`), `core/contracts.py`, `core/pricing_engine.py`
   (`get_s3_monthly_price_per_gb`), `core/scan_orchestrator.py`,
   `core/result_builder.py`, and the reporter
   (`reporter_phase_b.py:render_generic_per_rec`, `should_fallback_to_per_rec`,
   `_GENERIC_SOURCE_TYPES["enhanced_checks"] = "Metric Backed"`;
   `html_report_generator.py` dispatch ~line 3315 onward).
2. List **every** cost check, and for each give: trigger condition, the data
   source (MediaStore `list_containers`, CloudWatch metrics), the savings
   formula, and the constant/rate it embeds. The known (and ONLY) emitted check:
   - **Unused Resource Cleanup:** an `ACTIVE` container with `total_activity == 0`
     over 14 days (`RequestCount + BytesDownloaded + BytesUploaded` summed). It
     captures `EstimatedStorageGB` (from `BucketSizeBytes` avg) and `IngestCost`
     (`monthly_ingest_gb × 0.02`). The adapter then recomputes
     `EstimatedMonthlySavings = EstimatedStorageGB × S3-Standard-rate` (the
     ingest cost is NOT re-added in the adapter path — confirm).
   Note the helper builds two other empty buckets (`access_optimization`,
   `cors_policies`) that emit nothing.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each savings figure, re-derive it from the live AWS Pricing API and
   confirm it matches:
   - **Borrowed S3 rate justification:** validate that AWS Elemental MediaStore
     storage actually bills near the S3 Standard rate ($0.023/GB-mo us-east-1).
     MediaStore had its OWN pricing (storage + requests + data-transfer) before
     retirement — confirm whether S3 Standard is a defensible proxy or an
     understatement/overstatement, and whether the saving should be the
     container's *actual* MediaStore storage rate. If the borrowed rate is not
     defensible, the counted number is wrong.
   - **Region correctness:** confirm the engine path
     (`get_s3_monthly_price_per_gb("STANDARD")`) is region-correct and the
     `0.023 × ctx.pricing_multiplier` fallback applies the multiplier ONCE (the
     `if ctx.pricing_engine: … else: … × multiplier` split is correct only if the
     engine path is NOT also multiplied — confirm no double-apply).
   - **Ingest cost `0.02 $/GB`:** the helper's `ingest_cost = monthly_ingest_gb ×
     0.02` is a hardcoded constant inside the `savings_str` (which is then
     overwritten by the adapter). Validate the $0.02/GB ingest rate against the
     API and determine whether ingest cost SHOULD be part of the counted saving
     (it is currently dropped by the adapter). A saving that omits a real,
     measured ingest cost understates; a saving that adds a per-GB ingest RATE as
     a monthly total overstates.
   - **`EstimatedStorageGB` basis:** it is the LAST `BucketSizeBytes` average
     datapoint over 14 days. Confirm that single-point average is a fair monthly
     storage figure (not a rate, not a spike).
4. Confirm the savings basis is defensible from the report alone: the rec must
   make the rate / region / metric-window / formula legible. There is currently
   NO structured `AuditBasis` on the MediaStore rec — adding one (rate / region /
   `BucketSizeBytes` window / formula) is a likely finding. A deletion saving
   should equal 100% of the measured storage cost (the container is unused), so
   confirm the formula is `storage_gb × rate` for a delete recommendation.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** only one check fires per container; confirm a container
   cannot appear twice (the three `checks` buckets but only `unused_containers`
   populates). No stacking risk, but verify.
6. **Cross-source:** N/A — no CoH/CO. Confirm nothing re-surfaces the container.
7. **Cross-adapter:** MediaStore borrows the S3 rate but operates on MediaStore
   containers (distinct from S3 buckets) — confirm a MediaStore container is NOT
   also counted by the S3 adapter (different service, different API), and that no
   `_extract_*` synthetic tab pulls it in.

### Phase 4 — Coverage (works for ALL containers, not a subset)
8. `list_containers` is called ONCE with no pagination — confirm MediaStore
   `list_containers` returns a `NextToken` for large accounts and whether the
   single call misses containers beyond the first page (a coverage gap).
9. Are whole classes skipped? The check fires ONLY for `status == "ACTIVE"`
   containers with `total_activity == 0`. Confirm: (a) a low-but-nonzero-activity
   container with large idle storage is intentionally excluded (no
   under-utilization check exists — a coverage gap worth noting); (b) non-ACTIVE
   (CREATING/DELETING) containers are correctly skipped. Note MediaStore is a
   retired service (new containers cannot be created) — most accounts will be
   empty; document the sparse-data caveat.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except: pass`, bare `except`, `logger`-only, and `continue`
    fallback. Specifically:
    - The per-metric loop uses `except Exception: continue` (line ~57) and the
      size/ingest reads use bare `except Exception:` → `storage_gb = 0` /
      `ingest_cost = 0`. A throttled or DENIED CloudWatch read silently becomes
      "zero activity / zero storage", which both (a) can FALSELY flag an active
      container as unused (zero activity from a failed read) and (b) zeroes the
      saving. This is a CRITICAL silent-failure class — classify
      `AccessDenied`/`Unauthorized` → `ctx.permission_issue`, throttle → `ctx.warn`
      (mirror the `_report_aws_error` pattern in `services/efs_fsx.py`).
    - The outer `except Exception as e: ctx.warn(...)` covers `list_containers`
      and per-container metric blocks — confirm an AccessDenied on
      `mediastore:ListContainers` routes to `ctx.permission_issue`, not a generic
      `ctx.warn`.
11. Does a pricing miss fall back to `0.0`/blank and still emit a counted
    finding? A container with no `EstimatedStorageGB` is emitted with
    `EstimatedMonthlySavings = 0.0` + `PricingWarning` but is STILL counted by
    `total_recommendations = len(recs)`. This is a count-hygiene bug: a $0 rec
    inflates the headline count. It should be advisory (`Counted=False`) and
    excluded from the count (mirror the S3 `total_recs` hygiene + Lambda
    `mark_zero_savings_advisory`).
12. **CloudWatch gating / fast-mode:** the helper never checks `ctx.fast_mode`
    despite `reads_fast_mode=True` on the adapter — confirm and flag. The adapter
    also omits `requires_cloudwatch` while reading four CloudWatch metrics.
13. Are opt-in / "enable X" nudges emitted as $0 records that inflate the count?
    The empty `access_optimization` / `cors_policies` buckets emit nothing today,
    but confirm no future $0 placeholder leaks into `recs`.

### Phase 6 — Reporting (one tab, counted == rendered)
14. **Handler wiring:** mediastore has NO `PHASE_B_HANDLERS` entry and is NOT in
    `_PHASE_B_SKIP_PER_REC`, so it renders via `render_generic_per_rec`. Confirm
    the `enhanced_checks` source actually reaches the generic per-rec renderer
    (a source with no handler in a skip-per-rec service would render nothing —
    mediastore is NOT skip-per-rec, so the per-rec fallback fires). Confirm the
    source-type label resolves (`_GENERIC_SOURCE_TYPES["enhanced_checks"] =
    "Metric Backed"`).
15. **Counted == rendered:** `total_recommendations = len(recs)` and
    `total_monthly_savings = sum(rec_savings)`. Confirm the rendered per-rec cards
    equal the counted set, and that a $0 rec (PricingWarning) is either excluded
    from BOTH the count and the cards, or shown as advisory and excluded from the
    count — but never counted-without-dollars. Reconcile the executive-summary
    headline against the per-service total.
16. Confirm no finding is counted in `total_recommendations` /
    `total_monthly_savings` but dropped from the table (or vice-versa).

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to mediastore:
    `python3 cli.py <region> --scan-only mediastore`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service mediastore`.
    Triage every: silent failure, `$0`/missing-savings finding, and container
    appearing in >1 source/tab. Reconcile the headline against the per-source
    sum. Caveats: MediaStore is RETIRED — most/all regions will have zero
    containers; you may not be able to exercise the live path. If so, drive the
    pure pricing/count logic via unit tests instead and document the empty-account
    reality. Use `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`.
18. For any accuracy claim, show the AWS Pricing API value (S3 Standard per-GB at
    the region, and any surviving MediaStore storage/ingest rate) next to the
    scanner's borrowed `0.023` constant and `0.02` ingest constant.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** (silent CloudWatch failure
  → false-unused, $0 rec counted, no fast-mode gating, no `requires_cloudwatch`)
  from **known limitations** (retired service, borrowed S3 rate, no pagination,
  no under-utilization check). End with a short, ID'd fix plan (C1/H1/M1…).

### Implementation (only after I approve)
- Add a `tests/test_mediastore_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py`: drive `MediastoreModule.scan` with a
  `SimpleNamespace` ctx (pricing_engine present and absent), monkeypatched
  `get_enhanced_mediastore_checks`, and fake boto3 `mediastore`/`cloudwatch`
  clients. Cover every fix: borrowed-rate correctness (engine vs fallback ×
  multiplier, no double-apply), $0-rec count hygiene (advisory, not counted),
  CloudWatch failure classification (permission vs warn), fast-mode gating,
  `requires_cloudwatch` declaration, AuditBasis presence, pagination.
- Convert the $0 (no-storage) rec to advisory (`Counted=False`) and exclude from
  `total_recommendations` (mirror S3 count hygiene); decide whether ingest cost
  belongs in the counted saving.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for mediastore first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same silent-CloudWatch / count-hygiene bug in a sibling adapter
  out of scope, note it as a follow-up (don't fix unprompted).
- Update the `mediastore.py` row in `services/adapters/CLAUDE.md` if it changes.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)
- Usage savings from a config dimension alone (size) with NO usage metric →
  fabricated $. (mediastore uses measured `BucketSizeBytes` — confirm.)
- Wrong storage-class/region pricing; container priced at scan region not its region.
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`).
- Region: hardcoded constant/fallback not region-scaled via `pricing_multiplier`,
  OR double-applied on an already-region-correct engine path. (mediastore borrows
  the S3 rate — verify the `0.023 × multiplier` fallback is single-applied.)
- Per-unit RATE string (`$/GB`, `$/request`) counted as a monthly total —
  rejected by `parse_dollar_savings` → $0 advisory. (The `0.02 $/GB` ingest is a
  RATE — confirm it is not counted as a total.)
- Free-tier/free allotment recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic. (N/A: no CoH/CO.)
- Two heuristic checks stacking on one resource, or SUBSET redundancy.
- Reduction factor instead of exact price delta (price×factor vs current−target).
- $0 "enable X"/opt-in placeholder counted instead of converted to `ctx.warn`/advisory.
  (**Live:** the no-storage rec is counted with $0 — fix to advisory.)
- Metric-gated $0 nudge rendered as COUNTED instead of advisory (`Counted=False`).
- Cost Hub orphan / dropped-type. (N/A: mediastore not CoH/CO covered — do NOT
  flag a "missing CoH source".)
- A source emitted with no `PHASE_B` handler in a skip-per-rec service → renders
  nothing silently. (mediastore is NOT skip-per-rec → generic per-rec fallback fires.)
- Render-time substring/category filter desyncing headline from cards.
- Coverage gated to a hardcoded class/type/state allowlist.
  (**Live:** check fires only for `ACTIVE` + `total_activity == 0`.)
- CloudWatch permission/throttle failure logged via logger only, not
  `ctx.warn`/`ctx.permission_issue` (AccessDenied/Unauthorized → permission_issue).
  (**Live:** bare `except` → `storage_gb=0`/`total_activity=0`, can FALSELY flag unused.)
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` declared but
  helper never checks it — **live gap**).
- Heuristic assuming a usage target with no usage evidence.
- Cross-adapter overlap (same container/bucket in two tabs).
- Fixed per-rec estimate treated as a realized saving rather than advisory.
- Each counted finding must carry a structured AuditBasis; counted == rendered.

#### mediastore-specific items
- **Retired service / sparse data:** MediaStore no longer accepts new containers;
  expect empty accounts. Validate via unit tests, not necessarily a live scan.
- **Borrowed S3 rate defensibility:** confirm S3 Standard is a fair proxy for
  MediaStore storage, or replace with the actual MediaStore storage rate.
- **Dropped ingest cost:** the helper computes `ingest_cost` but the adapter's
  `EstimatedMonthlySavings = EstimatedStorageGB × rate` drops it. Decide whether a
  delete saving should include avoided ingest, and never count a `$/GB` ingest
  RATE as a monthly total.
- **`requires_cloudwatch` missing:** the adapter reads four CloudWatch metrics but
  does not declare `requires_cloudwatch`, so `--no-cloudwatch` cannot opt out and
  the orchestrator cannot pre-fetch — declare it (mirror S3).
- **False-unused from failed reads:** a CloudWatch AccessDenied/throttle silently
  yields `total_activity = 0`, flagging an active container as unused — the single
  most dangerous bug here. A failed activity read must abstain (warn + skip), not
  assert "unused".

## PROMPT (end)
</content>
