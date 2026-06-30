# Transfer Family Adapter Cost-Audit Prompt

A deep, Transfer-Family-specific audit brief in the same structure as the
Network / Lambda / RDS / EC2 audits. Paste the **PROMPT** section into a fresh
session.

It is pre-grounded in the *actual* `transfer` code path so the auditor starts
from facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

> **⚠ Latest live-audit findings (2026-06-30) — read these FIRST, then this prompt.**
> Before auditing, also read and paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
> — the recurring cost-fidelity bug *classes* confirmed in live deep audits (with
> real examples, ready-to-run JSON invariant sweeps, and the audit-method traps that
> cause FALSE findings). Run those sweeps before manual tracing.
>
> Service-specific live-audit findings for `transfer`:
> - None beyond the cross-cutting lessons — run the invariant sweeps in `_LIVE_AUDIT_LESSONS.md` and the known-issue catalogue below (advisory-leak, string↔numeric agreement, flat-global rate scaling, dedup granularity, silent-failure classification).
> - E1 — `transfer_svc.py` line 124 catches all `list_servers` failures with `ctx.warn()` not `record_aws_error`, so `AccessDenied` silently zeroes all transfer findings without a `permission_issue` entry; line 98 swallows `get_metric_statistics` errors with no classification either.

You are auditing the **`transfer`** adapter of this AWS cost-optimization scanner
(AWS Transfer Family — SFTP / FTPS / FTP / AS2 file-transfer servers). Scope is
strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the recently-audited
**Lambda** adapter (`services/adapters/lambda_svc.py`) as the worked example for
the `mark_zero_savings_advisory` / `Counted=False` pattern, the module-constant
arch-aware rate priced and region-scaled exactly once, the structured
`AuditBasis`, and the test style I expect. Treat the **network** adapter
(`services/adapters/network.py` + sub-shims) as the model for a savings string
that desyncs from the counted number and for fallback-path region scaling.
Treat **S3 / file_systems** as the model for CloudWatch evidence-gating a
usage-dependent saving instead of fabricating one.

### NOTE on structure (transfer is a thin adapter over one shim)

- The adapter is `services/adapters/transfer.py` → `TransferModule.scan`. It is a
  **single-source** adapter: it calls `get_enhanced_transfer_checks(ctx)` from
  the shim `services/transfer_svc.py`, then **re-prices** each returned rec and
  emits ONE `enhanced_checks` SourceBlock. There is no `services/transfer.py`
  legacy shim — `services/transfer_svc.py` IS the helper.
- **Two writers of the savings number, and they disagree.** The shim writes a
  human **string** `EstimatedSavings` (e.g. `"$219.00/mo from removing 1
  protocol(s)"`) using a **hardcoded `0.30`** and **no** region scaling. The
  adapter then writes a **numeric** `EstimatedMonthlySavings` from its own
  module constant `TRANSFER_PER_PROTOCOL_HOUR = 0.30` × 730 ×
  `ctx.pricing_multiplier` (region-scaled). `total_monthly_savings` sums the
  **numeric**; the renderer prints the **string**. Hold this distinction through
  Phases 2 and 6 — it is the central desync.
- Pricing is a **module constant**, not a `PricingEngine` method. There is no
  `get_transfer_*` method in `core/pricing_engine.py`; confirm that and confirm
  the constant is region-scaled via `ctx.pricing_multiplier` on the numeric path
  only (the shim string is **not** scaled).
- Transfer consumes **neither Cost Optimization Hub nor Compute Optimizer**. It
  is **not** in `core/scan_orchestrator.py` `_prefetch_advisor_data`'s
  `_HUB_SERVICES`, has no `type_map` entry, and pulls no
  `services.advisor.get_*_compute_optimizer_recommendations` helper. So a
  "missing CoH/CO source" finding is **NOT** fair game here — savings are
  expected to be locally derived. Drop those axes.
- The shim reads **CloudWatch** (`BytesUploaded` / `BytesDownloaded`, 14-day Sum)
  but only to attach an informational data-transfer **note** — it never produces
  a counted dollar. The module declares **neither** `requires_cloudwatch` **nor**
  `reads_fast_mode`, even though the shim guards the CW read with
  `if not ctx.fast_mode`. Flag the missing declarations.

### Phase 0 — Orient (5-minute map before judging)

0a. Open `services/adapters/CLAUDE.md`, find the `transfer.py` row (listed under
    Live Pricing as `$0.30/protocol/hour × 730`, "Module constant").
    **Reconcile the doc against reality:** confirm there is no PricingEngine
    method, that `0.30` lives in two places (the shim line 54 string literal and
    the adapter's `TRANSFER_PER_PROTOCOL_HOUR`), and that the doc's "Module
    constant" label is correct. Note that the table classifies transfer under
    "Live Pricing" even though it uses no live API — flag the mislabel if you
    agree.
0b. Confirm module identity in `services/adapters/transfer.py`: `key="transfer"`,
    `cli_aliases=("transfer",)`, `display_name="Transfer Family"`,
    `required_clients()` returns `("transfer",)`. Note that the shim *also* uses
    a `cloudwatch` client (`ctx.client("cloudwatch")`) that is **not** declared
    in `required_clients()` — confirm whether the registry lazily provides it and
    whether that is intentional.
0c. Transfer is one of the few adapters with **no AWS advisory source**. Focus on
    pricing accuracy, the string-vs-numeric desync, the stopped-server
    miscount, the "remove all-but-one protocol" assumption, and render wiring.

### Phase 1 — Understand the code (read before judging)

1. Read the whole path: `services/adapters/transfer.py` (the re-pricing loop,
   lines ~43–63), `services/transfer_svc.py` (`get_enhanced_transfer_checks`,
   `TRANSFER_OPTIMIZATION_DESCRIPTIONS`), `core/contracts.py`
   (`ServiceFindings`, `SourceBlock`), `core/scan_context.py` (`pricing_multiplier`,
   `fast_mode`, `warn`, `permission_issue`), `core/scan_orchestrator.py` (confirm
   transfer is absent from `_HUB_SERVICES`), `core/result_builder.py`, and the
   reporter path (`reporter_phase_b.py` — there is **no** `("transfer", …)` entry
   in `PHASE_B_HANDLERS`; `render_generic_per_rec` →
   `_render_generic_other_rec`; `should_fallback_to_per_rec("transfer")` is
   `True`; transfer is in neither `_PHASE_A_SERVICES` nor
   `_PHASE_B_SKIP_PER_REC`), and `html_report_generator.py`
   `_get_detailed_recommendations`.
2. List **every** cost check the shim produces, and for each give: trigger,
   data source (Transfer/`list_servers` describe vs CloudWatch vs pure config),
   the exact `EstimatedSavings` string template, the constant it embeds, the
   adapter's re-priced `EstimatedMonthlySavings` formula, and whether it is
   **counted** or should be **$0 advisory**. The known check inventory to confirm:
   - **`protocol_optimization`** (shim ~49–59): trigger `state == "ONLINE" and
     len(protocols) > 1`. String `f"${removable * 0.30 * 730:.2f}/mo from
     removing {removable} protocol(s)"` with `removable = len(protocols) - 1`.
     Adapter re-prices: `RemovableProtocols` if present else `len(protocols)-1`,
     × `0.30 × 730 × pricing_multiplier`. **Counted** (summed). This is the only
     genuinely-counted check.
   - **`unused_servers`** (shim ~93–103): trigger `state in ["STOPPED",
     "OFFLINE"]`. String `EstimatedSavings = "Full server hourly costs"` (a
     non-dollar phrase). **But** the adapter's re-pricing loop still runs on this
     rec: `RemovableProtocols` is absent, `Protocols` is a list, so it computes
     `removable = max(0, len(protocols)-1)` and writes a positive
     `EstimatedMonthlySavings` that is **added to `savings`**. Trace this
     carefully — a STOPPED/OFFLINE server bills nothing, so any counted saving
     here is fabricated (Phase 3/5).
   - **`endpoint_optimization`**: declared in the shim's `checks` dict but
     **never populated** — confirm it always emits zero recs (dead branch).
   - **data-transfer note** (shim ~61–91): CloudWatch `BytesUploaded` +
     `BytesDownloaded` over 14 days → `DataTransferCostGB` +
     `DataTransferCostNote` (`~${total_gb * 0.09:.2f}/mo S3 data transfer`).
     Informational only, never counted. Note the **`$0.09/GB`** rate.
3. Confirm the single emitted SourceBlock is `enhanced_checks` and that
   `total_recommendations == len(recs)` (all categories flattened by the shim's
   `all_recommendations`).

### Phase 2 — Accuracy of every number (validate with MCP)

4. Validate the **$0.30/protocol-hour** constant against the live AWS Pricing API
   (`service_code="AWSTransfer"`, `usagetype` `*-ProtocolHours`, unit `Hourly`).
   Confirm SFTP, FTPS, FTP, **and AS2** all price at **$0.30/hr** ProtocolHours in
   us-east-1, and that `× 730 = $219.00/protocol/month`. Confirm the per-protocol
   model is correct: AWS bills **per enabled protocol per hour the endpoint is
   provisioned**, so removing one protocol from an ONLINE multi-protocol server
   saves exactly `$0.30/hr × 730`. Record a structured **AuditBasis**
   (rate / region / formula) — there is none today.
5. **Data-transfer note rate is wrong.** The shim's `~$0.09/GB` is generic S3
   internet egress, not Transfer Family's own data fee. The Pricing API shows the
   Transfer Family data fee is **$0.04/GB uploaded + $0.04/GB downloaded**
   (`USE1-UploadBytes` / `USE1-DownloadBytes`, both `$0.04/GB`, for S3 and EFS
   backends; PGP-decrypt is a separate `$0.10/GB`). Confirm and flag the `$0.09`.
   This is a NOTE (never counted), so it is a LOW accuracy finding — but it
   misinforms the operator.
6. **Region scaling — split path.** The adapter's numeric `EstimatedMonthlySavings`
   multiplies by `ctx.pricing_multiplier` (region-scaled, correct). The shim's
   `EstimatedSavings` **string** hardcodes `0.30 × 730` with **no** multiplier —
   so in a non-us-east-1 region the rendered string shows us-east-1 dollars while
   the counted number is region-correct. Confirm Transfer Family ProtocolHours
   actually varies by region (query a second region, e.g. `ap-southeast-2`) so the
   desync is material, and confirm whether `pricing_multiplier` is even the right
   scalar for a Transfer-specific rate (it is a coarse EC2-derived regional index,
   not a Transfer-specific multiplier).
7. **Connectors and web apps are different SKUs the adapter does not model.**
   SFTP **connectors** bill `$0.001/connector-call + $0.40/GB` retrieved/sent (not
   $0.30/protocol-hr); **HTTPS web-app** units bill `$0.50/hr/unit`. The adapter's
   re-pricing loop guards against non-endpoint shapes (`if not isinstance(protocols,
   list): … continue`), so confirm a connector returned by `list_servers` (it is
   not — connectors are `list_connectors`) cannot slip through and be priced at
   the protocol rate. Document these as out-of-scope SKUs, not bugs.

### Phase 3 — Duplication (no dollar counted twice)

8. **Intra-adapter:** can one server match both `protocol_optimization` and
   `unused_servers`? An ONLINE server with >1 protocol hits the first; a
   STOPPED/OFFLINE server hits the second. They are mutually exclusive on `state`,
   so confirm no single server is in both lists — but DO confirm the
   `unused_servers` rec is not *also* getting a protocol-removal number layered on
   top of its "Full server hourly costs" string (Phase 5).
9. **Cross-adapter:** Transfer servers front **S3 / EFS** buckets/filesystems. The
   data-transfer note references S3 transfer cost — confirm the same GB is not
   also counted by the `s3` or `file_systems` adapters (it is not, because the
   note is never counted; verify). Confirm no `_extract_*` helper in
   `html_report_generator.py` pulls Transfer resources into a synthetic tab.

### Phase 4 — Coverage (works for ALL resources, not a subset)

10. Confirm full pagination of `list_servers` (the shim uses
    `get_paginator("list_servers").paginate()` — verify it iterates all pages).
11. Are whole classes skipped? The protocol check only fires for `state ==
    "ONLINE"` — a server in `STARTING`/`STOPPING` state is skipped (acceptable).
    `unused_servers` only fires for `STOPPED`/`OFFLINE`. Confirm `endpoint_type`
    (PUBLIC vs VPC vs VPC_ENDPOINT) does not change the per-protocol rate
    (it does not) and that the adapter does not need it. Note that **AS2** servers
    and **multi-protocol** servers are handled by the generic `len(protocols)`
    logic. Confirm **web-app** resources (`list_web_apps`) and **connectors**
    (`list_connectors`) are out of scope by design (different APIs), and decide
    whether that is a documented coverage gap.

### Phase 5 — Silent failures (nothing fails quietly)

12. Find every `except`, `logger`-only, and `return`-fallback path:
    - The shim's **outer** `try/except Exception as e:` (~105–106) routes to
      `ctx.warn(... , "transfer")` — confirm it classifies
      `AccessDenied`/`UnauthorizedOperation` to `ctx.permission_issue` (it does
      **not** today — it always `warn`s, hiding an IAM gap on `transfer:ListServers`).
    - The shim's **inner** CloudWatch `try/except Exception:` (~86–91) **swallows**
      any CW failure and only sets a `DataTransferCostNote` — no `ctx.warn` /
      `ctx.permission_issue`. A throttled or `AccessDenied` `cloudwatch:GetMetricStatistics`
      vanishes. Classify it.
    - The adapter (`transfer.py`) wraps the shim in **no** try/except — confirm a
      shim crash propagates to `safe_scan` (acceptable) and is recorded.
13. Does a re-pricing miss still emit a finding? Trace the `else` branch where
    `Protocols` is not a list: the rec gets `EstimatedMonthlySavings = 0.0` via
    `setdefault` and `continue` — confirm it is then a $0 finding that still
    renders. A $0 counted finding must be advisory or skipped.
14. **fast_mode / CloudWatch gating:** the shim guards the CW read with `if not
    ctx.fast_mode`, but the module declares neither `requires_cloudwatch` nor
    `reads_fast_mode`. Confirm the orchestrator still provides a CW client under
    fast mode and mirror the Lambda fast-mode declaration fix.

### Phase 6 — Reporting (counted == rendered)

15. **Rendered string vs counted number (the central desync).** `transfer` has no
    `PHASE_B_HANDLERS` entry, so each rec renders via `render_generic_per_rec` →
    `_render_generic_other_rec`, which prints the raw **`EstimatedSavings`**
    string (`"$219.00/mo …"` for protocol, **`"Full server hourly costs"`** for
    unused) — NOT the adapter's region-scaled `EstimatedMonthlySavings`. So the
    visible dollar and the counted dollar can differ (region scaling) and the
    unused-server card shows a non-dollar phrase while contributing a (fabricated)
    counted number. Trace `_render_generic_other_rec` (it also dumps every scalar
    rec field, so `EstimatedMonthlySavings` is printed too, side-by-side with the
    string — confirm both appear and reconcile). This is a CRITICAL render-desync
    candidate; document exactly what the operator sees.
16. **Counted == rendered:** confirm `total_recommendations == len(recs)` and
    `total_monthly_savings` equals the sum of the COUNTED `EstimatedMonthlySavings`
    values. Reconcile the per-tab headline and the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings`) against the
    per-service total. Note the executive summary reads `EstimatedSavings`
    (string) in some paths (`html_report_generator.py` ~318) — confirm which
    number flows where for transfer.
17. Confirm no finding is counted but dropped from the table (or vice-versa).

### Phase 7 — Tooling & evidence

18. Run a real scan scoped to transfer:
    `python3 cli.py <region> --scan-only transfer`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service transfer`.
    Triage every silent failure, every `$0`/non-dollar-string finding (separate
    genuine advisory from leakage), and the string-vs-numeric reconciliation. Use
    `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`. Caveats:
    Transfer Family servers are rare; if the account has none, drive the shim with
    a fake paginator. Exercise a STOPPED/OFFLINE multi-protocol server to prove
    the fabricated counted saving, and a non-us-east-1 region to prove the string
    desync.
19. For the accuracy claim, show the AWS Pricing API value ($0.30/protocol-hr;
    $0.04/GB up + $0.04/GB down) next to the scanner's constants (`0.30`, `0.09`).
    For the desync claim, show one rec's `EstimatedSavings` string next to its
    `EstimatedMonthlySavings` numeric in a non-us-east-1 scan.

### Deliverable

- The complete check list (Phase 1.2), with counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)

- Add `tests/test_transfer_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive
  `TransferModule.scan` with a `SimpleNamespace` ctx (with `pricing_multiplier`,
  `fast_mode`, `warn`, `permission_issue`) + a fake `transfer` paginator + a fake
  `cloudwatch` client, asserting: ONLINE multi-protocol → counted region-scaled
  saving with a single source of truth (string == numeric); STOPPED/OFFLINE →
  **no** fabricated protocol-removal saving (advisory or full-server only);
  non-endpoint shape → $0 advisory not counted; CW failure →
  `ctx.warn`/`ctx.permission_issue`; fast_mode → CW skipped.
- Make the savings number **single-sourced**: the shim should carry the explicit
  `RemovableProtocols` count and let the adapter own the dollar string + number
  (region-scaled), so the rendered string never diverges from the counted total.
- Record a structured **AuditBasis** (rate / region / formula) on each counted
  finding. Fix the `$0.09/GB` note to the validated `$0.04/GB up + $0.04/GB down`.
- Declare `requires_cloudwatch` / `reads_fast_mode` if the CW note is kept.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for transfer first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same string-vs-numeric or silent-failure bug in a sibling
  module-constant adapter (apprunner / glue / dynamodb / containers) out of scope,
  note it as a follow-up (don't fix unprompted).
- Update the `transfer.py` row in `services/adapters/CLAUDE.md` to match reality.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (universal — found in prior audits)

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

### Service-specific issues to check against (found grounding this adapter)

- **String-vs-numeric desync (CRITICAL candidate):** the shim writes the displayed
  `EstimatedSavings` string with a hardcoded `0.30 × 730` and **no** region
  scaling, while the adapter writes the counted `EstimatedMonthlySavings` with
  `× ctx.pricing_multiplier`. Rendered dollars ≠ counted dollars off us-east-1.
- **Stopped-server fabricated saving (HIGH):** `unused_servers`
  (STOPPED/OFFLINE) carries the string `"Full server hourly costs"`, yet the
  adapter's re-pricing loop computes a positive protocol-removal
  `EstimatedMonthlySavings` and **adds it to the counted total** — a stopped
  server bills nothing, so the number is fabricated and the string/number
  disagree.
- **"Remove all-but-one protocol" with no usage evidence (HIGH):** `removable =
  len(protocols) - 1` assumes every protocol beyond the first is unused — there is
  no per-protocol CloudWatch/usage signal proving it. Should be a $0 advisory
  (or evidence-gated) per the universal "usage target with no evidence" rule.
- **Data-transfer note rate wrong (LOW):** `~$0.09/GB` is generic S3 egress; the
  Transfer Family data fee is `$0.04/GB up + $0.04/GB down` (validated). The note
  is never counted, but it misinforms.
- **CloudWatch silent-swallow + missing declarations (MEDIUM):** the inner CW
  `except Exception:` only sets a note (no `ctx.warn`/`ctx.permission_issue`); the
  outer shim `except` always `warn`s (never `permission_issue` on `AccessDenied`);
  the module declares neither `requires_cloudwatch` nor `reads_fast_mode` despite
  reading CW.
- **No structured AuditBasis on the one counted check (MEDIUM):** the protocol
  saving carries no rate/region/formula basis, so the number is not defensible
  from the report alone.
- **Dead `endpoint_optimization` branch + `$0` non-endpoint fallback (LOW):**
  `endpoint_optimization` is declared but never populated; the `Protocols`-not-a-list
  path emits a `$0.00` rec that still renders.
- **Doc mislabel (LOW):** `services/adapters/CLAUDE.md` lists transfer under
  "Live Pricing" though it uses a module constant and no AWS Pricing API call.

## PROMPT (end)
