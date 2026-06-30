# Audit-Prompt Generation Spec (internal — for prompt-authoring agents)

You are generating reusable, **code-grounded cost-audit PROMPT** documents for the
AWS Cost Optimization Scanner. These are PROMPTS a future auditor pastes into a
fresh session to audit ONE adapter — they are NOT audits, and generating them must
NOT modify any source `.py`. The ONLY files you create are the prompt `.md` files,
one per assigned service, at `docs/audits/prompts/<service_key>_AUDIT_PROMPT.md`.

## Grounding (do this BEFORE writing each prompt)
1. Read `docs/NETWORK_AUDIT_PROMPT.md` (gold-standard exemplar — match its structure
   and depth EXACTLY), `docs/SERVICE_AUDIT_PROMPT.md` (generic template), and
   `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md` (recurring bug classes CONFIRMED in
   live audits, with real examples, detection sweeps, and the audit-method traps
   that cause false findings — every generated prompt must tell the auditor to
   paste it alongside).
2. Read the adapter `services/adapters/<file>.py` in full.
3. Read its helpers: `services/<key>.py` and `services/<key>_logic.py` if they exist;
   any sub-shims it imports; the `PricingEngine` methods it calls in
   `core/pricing_engine.py`; how it consumes Cost Hub
   (`ctx.cost_hub_splits[...]`) and/or Compute Optimizer
   (`services/advisor.py` `get_<x>_compute_optimizer_recommendations`); its reporter
   handler in `reporter_phase_b.py` / `reporter_phase_a.py` (find the
   `(service_key, source_name)` entries in `PHASE_B_HANDLERS`; whether the key is in
   `_PHASE_A_SERVICES` / `PHASE_A_DESCRIPTORS` / `_PHASE_B_SKIP_PER_REC`); and how
   `html_report_generator.py` dispatches it. For CoH check
   `core/scan_orchestrator.py` `type_map` / `_HUB_SERVICES`.
4. Note exactly: module key, cli_aliases, display_name, required_clients,
   requires_cloudwatch / reads_fast_mode, emitted source-block names, pricing
   strategy (live method names + any hardcoded module constants/fallbacks), EVERY
   cost check + its savings formula + constant + counted-vs-advisory status, dedup
   logic and authority order, fast_mode handling, and every silent-failure path
   (`except: pass`, bare `except`, `logger`-only, `return []` / `return 0.0`).

## Structure (mirror docs/NETWORK_AUDIT_PROMPT.md EXACTLY)
- Title + 2-line intro + `## PROMPT (copy from here)`.
- Opening paragraph: "You are auditing the `<key>` adapter…" — scope is strictly
  cost, work read-only first, validate via AWS Pricing MCP + AWS Knowledge MCP,
  trace real code, and name the canonical sibling reference adapters for THIS
  service's patterns (EC2 for dedup/$0-placeholder; RDS for cross-source dedup /
  RI demotion / CoH consumption; Lambda for metric-gated $0 advisory + arch-aware
  constant; S3/file_systems for CloudWatch evidence-gating; network for
  parse-rate/keyword).
- A "NOTE on structure" block calling out anything non-obvious about THIS adapter
  (composite vs single; naming quirks; whether it consumes CoH/CO; module
  constants; Phase A vs Phase B rendering; synthetic tab).
- Phase 0 Orient (reconcile the `services/adapters/CLAUDE.md` row against reality;
  module identity; whether CoH/CO is even fair game for this service).
- Phase 1 Understand (files to read + enumerate every check: trigger, source,
  formula, constant, counted-vs-advisory, emitting SourceBlock).
- Phase 2 Accuracy (validate EACH rate against the live AWS Pricing API with the
  correct service code + SKU/usagetype; region scaling; engine-vs-fallback;
  double-multiply; arch/edition/license/OS correctness where relevant; require a
  structured AuditBasis).
- Phase 3 Duplication (intra-adapter stacking/subset; cross-source CoH>CO>heuristic
  by normalized id; cross-adapter overlap — name THIS service's specific risks).
- Phase 4 Coverage (pagination; hardcoded type/family/size/state allowlists;
  whole-class skips; zero-capacity/idle resources).
- Phase 5 Silent failures (every except/pass/logger-only/return-fallback; classify
  AccessDenied/Unauthorized/OptInRequired → permission_issue; pricing miss → $0;
  CoH dropped-type modes; opt-in placeholder → warn; fast_mode gating of CW reads).
- Phase 6 Reporting (single tab; every emitted source has a registered handler or a
  deliberate per-rec fallback; counted == rendered; advisory $0 rendered-not-counted;
  reconcile the executive-summary headline).
- Phase 7 Tooling (`python3 cli.py <region> --scan-only <key>`;
  `tools/scan_doctor.py <json> --service <key>`; use `.venv/bin/python` (3.14);
  prove duplication + accuracy claims with real ids / Pricing API values).
- Deliverable (check list + severity-grouped findings with file+line+evidence+impact;
  confirmed bugs vs limitations; ID'd fix plan C1/H1/M1…).
- Implementation (tests mirroring `tests/test_lambda_audit_fixes.py` &
  `tests/test_rds_audit_fixes.py`; regression gate
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`;
  AuditBasis; update the `services/adapters/CLAUDE.md` row; stage only changed files).
- Known-issue catalogue: the UNIVERSAL list below verbatim (including the live-audit
  additions), a one-line instruction to ALSO paste `docs/audits/prompts/_LIVE_AUDIT_LESSONS.md`
  (recurring bug classes + ready-to-run invariant sweeps + audit-method traps), THEN 4-8
  service-specific items you discovered from the code.

## UNIVERSAL known-issue catalogue (embed verbatim in every prompt)
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

### Live-audit additions (2026-06-30 — see `_LIVE_AUDIT_LESSONS.md` for examples + sweeps)
- Advisory-leak: a `Counted=False` rec carrying a non-zero `EstimatedMonthlySavings`
  numeric (must be `0.0`; recoverable figure goes in `PotentialMonthlySavings`).
- String ↔ numeric divergence: `EstimatedSavings` string and `EstimatedMonthlySavings`
  numeric (and AuditBasis) must agree to the cent in EVERY branch — capping/reconciling
  the string but not the numeric, or vice-versa, is a silent field overstatement.
- A globally-FLAT rate (public IPv4 / EIP $3.65; Route53 $0.50/zone) region-scaled by
  `pricing_multiplier` in the fallback path (flat charges must NOT be scaled — the mirror
  of the region item above).
- Dedup at too-coarse a scope (demoting every lever in a VPC/cluster because CoH covered
  ONE resource) — suppresses independent resources; dedup at the resource-id granularity
  and prefer EXCLUSION (drop the covered resource from the heuristic's input) over blanket
  demotion. A dedup fix's failure mode is UNDER-count + advisory-leak + claim-order.
- Dedup claim-order: claim a resource into the "seen" set only AFTER all skip checks AND
  only once it actually contributes a counted dollar; distinguish "all-shared" (→ $0
  advisory) from "unsizable/no-data" (→ skip) when both yield `incremental == 0`.
- Same physical snapshot counted under >1 AMI (shared backing snapshots) — a snapshot is
  billed once and freed only when every referencing AMI is deregistered; count it once,
  emit the co-dependent AMI as a `Counted=False` advisory.
- Advisory-only service rendered with NO tab — the tab gate must key off RENDERED cards
  (counted + advisory), while the headline COUNT keys off counted-only; `total_services_scanned`
  must match rendered service tabs (synthetic Snapshots/AMIs tabs are intentionally extra).
- A de-minimis saving that rounds to `$0.00` still emitting a card — gate on the rounded
  potential, not just raw size.
- A flat-%-of-spend estimate with no per-resource signal (e.g. an "Unknown"-service SP
  coverage gap = the whole account's on-demand spend × a flat discount) — non-actionable,
  suppress or $0-advisory; never let it near the headline.
- Silent-failure fix completeness: thread `ctx` into EVERY call site of a helper (the s3
  `GetBucketWebsite` second call site was missed first pass), and record only genuine
  enumeration/metric failures — keep the normal fallback paths (paginator-unavailable,
  `NoSuchWebsiteConfiguration`, empty-datapoints) silent.
- Audit-method traps (avoid FALSE findings): CoH recs carry savings in camelCase
  `estimatedMonthlySavings` (empty PascalCase string); unattached EBS in
  `EstimatedMonthlyCost`; EC2/EBS CoH render as a few action-GROUPED cards (not one per
  rec); a CoH `estimatedSavingsPercentage` ~1pp off is AWS rounding, not our bug.

## Rules
- Read-only on source; the ONLY file you create per service is its prompt `.md`.
- Cite REAL file paths, function names, constant values, source-block names, and line
  regions you actually saw — no unfilled `<SERVICE>` placeholders.
- If a service genuinely has no CoH/CO/CloudWatch/shim, say so explicitly and DROP those
  axes (don't pad).
- Keep each prompt self-contained (a future session pastes ONE file).
