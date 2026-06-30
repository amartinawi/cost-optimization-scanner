# Redshift Adapter Cost-Audit Prompt

A deep, Redshift-specific audit brief in the same structure as the Network /
RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* Redshift code path so the auditor starts from
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
> Service-specific live-audit findings for `redshift`:
> - CoH consumer + largely advisory — verify the bucket is consumed (CoH > heuristic) and that the tab still renders despite `$0` counted; no `Counted=False` rec with a non-zero numeric (advisory-leak).
> - E1 silent failure: the shim swallows all `redshift-serverless` errors with a bare `except Exception: pass` (line 122 of `services/redshift.py`) and routes `describe_clusters` failures to `ctx.warn` instead of `record_aws_error`; classify AccessDenied/throttle as `permission_issue`, else `warn`.

You are auditing the **`redshift`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **RDS** adapter
(`services/adapters/rds.py`, `services/rds_logic.py`) as the canonical model for
Cost-Hub consumption, cross-source dedup, and the **Reserved-Instance→advisory
demotion** rule; treat the **EC2** adapter (`services/adapters/ec2.py`) for the
exact-price-delta (not reduction-factor) rightsizing discipline. The
recently-audited **Lambda** adapter (`services/adapters/lambda_svc.py`) and
`tests/test_lambda_audit_fixes.py` are the worked examples for the
`mark_zero_savings_advisory` pattern and the test style I expect.

### NOTE on structure (Redshift is a single-source, live-priced adapter — with an orphaned Cost-Hub bucket)
- The adapter `services/adapters/redshift.py` → `RedshiftModule.scan` consumes
  **one** source: `services.redshift.get_enhanced_redshift_checks` (source block
  `enhanced_checks`). It emits **no** `cost_optimization_hub` source block.
- **Orphaned Cost-Hub bucket (known issue):** the orchestrator buckets the
  `RedshiftCluster` `currentResourceType` into `ctx.cost_hub_splits["redshift"]`
  (`core/scan_orchestrator.py:type_map`), but `RedshiftModule.scan` **never reads
  it** — those CoH cluster recommendations are silently dropped. (`RedshiftReservedInstances`
  is separately routed to `commitment_analysis`.) Confirm and treat as a primary
  finding. (See `docs/audits/SUMMARY.md` / the orphaned-bucket memo:
  rds/elasticache/opensearch/redshift/s3 are the known populated-but-unconsumed
  buckets — EBS was the only one fixed.)
- **No Compute Optimizer** for Redshift (it does not cover the service), so a
  "missing CO source" finding is not fair game.
- Pricing is **live** via `ctx.pricing_engine.get_instance_monthly_price("AmazonRedshift",
  node_type)` × `NumberOfNodes` × a per-category factor. The method is
  region-correct — the adapter must **not** re-multiply by `ctx.pricing_multiplier`
  (it correctly does not). If `ctx.pricing_engine is None`, **no** savings are
  emitted. `REDSHIFT_NODE_MONTHLY_FALLBACK = 200.0` is defined but **unused** (a
  dead constant — the old undocumented $200 fallback was removed); confirm it is
  truly dead and recommend deleting it.
- Savings factors `REDSHIFT_SAVINGS_FACTORS`: reserved 0.52, pause 1.00,
  default 0.24. `_rs_factor` maps a rec's `CheckCategory` → factor (`reserved`→0.52,
  `pause`/`unused`/`inactive`→1.00, else 0.24).

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` — note Redshift is listed under **Flat-rate
    (1 adapter)? No** — it is *not* in the Live-Pricing table despite using
    `get_instance_monthly_price`. Reconcile the doc against reality: the adapter's
    own docstring says "Flat-rate savings strategy" yet it calls the live Pricing
    API. Flag the drift and the missing CLAUDE.md row.
0b. Confirm module identity in `services/adapters/redshift.py`: `key="redshift"`,
    `cli_aliases=("redshift",)`, `display_name="Redshift"`,
    `required_clients()=("redshift",)`. Note it declares **no**
    `requires_cloudwatch` / `reads_fast_mode` — and indeed reads no CloudWatch, so
    every saving is config-derived with **no usage evidence** (scrutinise in
    Phase 2/4). Note `required_clients` lists only `redshift` even though the shim
    also calls `redshift-serverless`.
0c. Redshift has an (unconsumed) CoH bucket, no CO, and no CloudWatch. Focus on:
    the orphaned CoH bucket, RI-counted-vs-advisory, the reduction-factor vs exact
    delta on rightsizing, the dead hardcoded `$150`/`$100` shim strings vs the
    live-priced counted number (render desync), node-type SKU determinism, and
    RA3 managed-storage / serverless coverage gaps.

### Phase 1 — Understand the code (read before judging)
1. Read the full Redshift path: `services/adapters/redshift.py`
   (`RedshiftModule.scan`, `_rs_factor`, `REDSHIFT_SAVINGS_FACTORS`,
   `REDSHIFT_NODE_MONTHLY_FALLBACK`), `services/redshift.py`
   (`get_enhanced_redshift_checks`, `REDSHIFT_OPTIMIZATION_DESCRIPTIONS`),
   `core/pricing_engine.py` (`get_instance_monthly_price` ~737 →
   `_fetch_generic_instance_price`; trace how `AmazonRedshift` + a node type maps
   to a SKU and whether the filter is deterministic), `core/contracts.py`,
   `core/scan_orchestrator.py` (`type_map`: `RedshiftCluster`→`redshift`,
   `RedshiftReservedInstances`→`commitment_analysis`), `core/result_builder.py`,
   and the reporter. **Render path:** `redshift` is **not** in
   `_PHASE_B_SKIP_PER_REC` and has **no** `PHASE_B_HANDLERS` entry, so
   `enhanced_checks` renders via the **generic per-rec** path
   (`should_fallback_to_per_rec("redshift")` is True; `should_skip_section_header`
   includes `redshift`). Confirm the generic renderer shows each rec's
   `EstimatedSavings` string.
2. List **every** cost check the shim emits, and for each give: trigger
   condition, data source (describe-API only — no CloudWatch), the shim's
   `EstimatedSavings` **string**, the factor the adapter applies, and whether the
   counted number derives from the string or from `get_instance_monthly_price`.
   Confirm the known inventory:
   - **Reserved Instance Optimization** (status `available`, age > 30d, nodes ≥ 2):
     shim string `"${nodes*150:.2f}/month with 1-year RI"`; adapter counts
     `node_price × nodes × 0.52`.
   - **Cluster Rightsizing** (nodes > 3): shim string `"${(nodes-2)*100:.2f}/month
     potential"`; adapter counts `node_price × nodes × 0.24`.
   - **Serverless Optimization** (workgroup status `AVAILABLE`): shim string
     `"$150/month with reservations"`; rec has **no** `NodeType` → adapter's
     `if node_type:` is False → counts **$0** (but the card shows $150).
   - Note `pause_resume_scheduling` and `storage_optimization` buckets are
     declared but **never populated** (dead branches; no pause/resume or RA3
     storage check actually fires despite the `pause` 1.00 factor existing).

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** figure, re-derive it from the live AWS Pricing API:
   - **Node price** `get_instance_monthly_price("AmazonRedshift", node_type)`:
     validate the SKU mapping for RA3 (`ra3.xlplus`/`ra3.4xlarge`/`ra3.16xlarge`),
     DC2 (`dc2.large`/`dc2.8xlarge`), and legacy DS2 node types. Confirm the
     filter is **deterministic** (pinned `instanceType`/`usagetype`, not the first
     of multiple matching SKUs) and that the returned price is the **on-demand
     node-hour × 730**, region-correct. Confirm RA3 vs DC2 are priced as their own
     node types, never cross-priced.
   - **RI factor 0.52**: the shim text claims "24% savings"; the adapter applies
     **0.52** (52%). These disagree — validate the real Redshift RI discount range
     (≈30–75%) and reconcile the headline number with the rendered "24%" text.
   - **Rightsizing factor 0.24 vs exact delta**: "Cluster Rightsizing" counts
     `node_price × nodes × 0.24` while the *recommendation* is to drop to (nodes−2)
     — the exact delta is `node_price × 2` (remove 2 nodes), not 24% of the whole
     cluster. Prefer `current − target` to a flat factor (the universal
     reduction-factor-vs-delta issue).
   - **RA3 managed storage (TODO in code):** the adapter's own TODO notes RA3
     charges managed storage at **$0.024/GB-mo** that is **not** priced — instance
     pricing alone undercounts RA3 cluster cost. Validate the RMS rate and whether
     it belongs in the saving.
   - **Region scaling:** `get_instance_monthly_price` is region-correct; confirm
     no `pricing_multiplier` is applied on top (the adapter correctly omits it) and
     no fallback constant path re-introduces a us-east-1 number.
4. Record a structured **AuditBasis** (rate / region / node-type / formula) on
   each counted finding. A config-only "saving" with no utilization evidence
   (Redshift reads no CloudWatch CPU/query metrics) is not defensible from the
   report alone — flag it.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-cluster stack:** a cluster with `nodes > 3`, `status == available`,
   `age > 30d` fires **both** "Reserved Instance Optimization" (0.52) **and**
   "Cluster Rightsizing" (0.24) — the adapter sums both → 0.76× the cluster cost
   "saved" on one cluster. They are alternative levers (you rightsize first, then
   reserve the smaller cluster). Prove the stack; keep one, or demote RI to
   advisory.
6. **Reserved Instance = commitment lever (the RI rule):** RDS demotes
   `Reserved Instance Opportunities` to `ADVISORY_CATEGORIES` (`Counted=False`)
   and routes RI to the `commitment_analysis` tab; the orchestrator already routes
   `RedshiftReservedInstances` CoH recs to `commitment_analysis`. The Redshift
   adapter currently **counts** its "Reserved Instance Optimization" heuristic —
   demote it to advisory so it never stacks with rightsizing or double-counts the
   commitment_analysis RI view.
7. **Cross-source / orphaned CoH:** because the adapter ignores
   `ctx.cost_hub_splits["redshift"]`, there is no CoH↔heuristic dedup at all. When
   you wire CoH in (the orphaned-bucket fix), add authority CoH > heuristic keyed
   by normalized cluster id, so a cluster surfaced by both CoH and the rightsizing
   heuristic is not double-counted.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. Confirm full pagination of `describe_clusters` and (serverless)
   `list_workgroups`. Re-examine the gates: RI requires `nodes ≥ 2` and `age >
   30d`; rightsizing requires `nodes > 3` (a 3-node over-provisioned cluster is
   skipped). The `ClusterCreateTime` string-fallback hardcodes `age = 30` —
   confirm that path can't mis-gate.
9. **Whole classes skipped:** RA3 vs DC2/DS2 node families (priced but not
   differentiated for storage); Redshift **Serverless** (RPU-based, priced as a
   flat $150 string, contributes $0); **paused** clusters
   (`ClusterStatus == "paused"`) — a paused cluster still bills managed storage but
   is never flagged (the `pause` 1.00 factor and `pause_resume_scheduling` bucket
   exist but nothing populates them); concurrency-scaling and Spectrum spend.
   Confirm each skip is intentional and documented, or a real gap.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except`/`pass`/`return []`. The serverless block in
    `get_enhanced_redshift_checks` is wrapped in a bare `except Exception: pass`
    (no `ctx.warn`) — a `redshift-serverless` permission gap or throttle vanishes
    silently. The outer block does `ctx.warn`. Classify:
    `AccessDenied`/`UnauthorizedOperation` → `ctx.permission_issue`, other →
    `ctx.warn`. Also note `required_clients()` omits `redshift-serverless`, so the
    client may be unavailable and the whole serverless path silently no-ops.
11. Confirm a pricing miss returning `0.0` (the `get_instance_monthly_price`
    fallback, or an unknown `node_type`) **drops** the finding rather than emitting
    a `$0` counted card — the adapter's `if monthly > 0` guard and the
    `if node_type:` guard are the relevant gates; verify nothing slips through with
    a $0 counted saving.
12. Confirm the serverless rec (no `NodeType`, $0 counted) and any unknown-node
    rec are explicitly **advisory** (`Counted=False`), not silently counted as $0
    placeholders inflating `total_recommendations`.

### Phase 6 — Reporting (one tab, counted == rendered)
13. Redshift renders via the **generic per-rec** path (no Phase-B handler). The
    generic renderer surfaces each rec's `EstimatedSavings` **string** —
    `"${nodes*150}/month with 1-year RI"`, `"${(nodes-2)*100}/month potential"`,
    `"$150/month with reservations"` — but the **counted** number is the
    `node_price × nodes × factor` computed in the adapter. These are different
    numbers: the **card shows a hardcoded $150/$100-based string while the tab
    total counts a live-priced figure** → a counted ≠ rendered desync. Decide the
    single source of truth (emit the priced number into `EstimatedSavings`), and
    confirm no source is silently unrendered.
14. **Counted == rendered:** `total_recommendations = len(recs)`,
    `total_monthly_savings` = the priced sum. Verify the per-tab total equals the
    sum of the COUNTED rendered findings (serverless/unknown-node advisory recs
    rendered but $0), and reconcile the executive-summary headline +
    `_calculate_service_savings` against the per-service total. Confirm no finding
    is counted but dropped from the table (or vice-versa).

### Phase 7 — Tooling & evidence
15. Run a real scan scoped to Redshift: `python3 cli.py <region> --scan-only
    redshift`, then `python3 tools/scan_doctor.py <json> --service redshift`.
    Triage every silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and cluster appearing in >1 lever. Reconcile the
    headline against the per-source sum. Caveats: exercise an RA3 multi-node
    cluster (>3 nodes, >30d old — the RI+rightsizing stack), a DC2 cluster, a
    Serverless workgroup (the $0/$150 desync), and a region with CoH Redshift recs
    (to prove the orphaned bucket drops them). Use `.venv/bin/python` (3.14) —
    system `python3` lacks `datetime.UTC`.
16. For any duplication claim, prove it (same cluster id counted by RI and
    rightsizing). For any accuracy claim, show the AWS Pricing API value (RA3/DC2
    node-hour × 730, RMS $0.024/GB-mo) next to the scanner's number, and show the
    dropped CoH recs in `ctx.cost_hub_splits["redshift"]`.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-advisory marked and the
  source block named.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with an ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Add `tests/test_redshift_audit_fixes.py` mirroring
  `tests/test_rds_audit_fixes.py` / `tests/test_lambda_audit_fixes.py`: unit-test
  the pure helpers directly (`_rs_factor` category mapping, the RI+rightsizing
  dedup, the CoH-bucket consumption + cluster-id dedup once wired) and drive
  `RedshiftModule.scan` with a `SimpleNamespace` ctx + monkeypatched
  `get_enhanced_redshift_checks` + a fake `pricing_engine` + fake boto3
  paginators. Cover every fix: orphaned-CoH consumption, RI demotion to advisory,
  rightsizing exact-delta, serverless/unknown-node advisory gating, serverless
  silent-failure classification, render desync, counted == rendered.
- Replace the reduction factors with `current − target` deltas to a concrete
  recommended node count where possible; never fabricate a `$` from a config
  dimension with no usage evidence (Redshift reads no CloudWatch — consider adding
  CPU/query utilization gating before counting rightsizing savings).
- Record a structured **AuditBasis** (rate / region / node-type / formula) on each
  counted finding; delete the dead `REDSHIFT_NODE_MONTHLY_FALLBACK` if confirmed
  unused.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the Redshift golden fixture first; refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) only when a rendering change is intentional, and say so.
- If you find the same orphaned-CoH / RI-counted bug class in
  `elasticache`/`opensearch`, note it as a follow-up (don't fix unprompted). Add
  the `redshift.py` row to `services/adapters/CLAUDE.md`. Stage ONLY the files you
  changed.

### Known issue catalogue to check against (found in prior audits)
- Usage savings from a config dimension alone (capacity/size/RCU-WCU) with NO
  usage metric → fabricated $.
- Wrong engine/edition/OS/license/node-type pricing; reserved priced as on-demand
  or vice-versa.
- Non-deterministic pricing filter (multiple SKUs, MaxResults=1).
- Region: hardcoded constant/fallback not region-scaled via pricing_multiplier,
  OR pricing_multiplier double-applied on an already-region-correct engine path.
- Per-unit RATE string ($/GB, $/hour, $/RCU) counted as a monthly total — must be
  rejected by parse_dollar_savings → $0 advisory.
- Free-tier/free allotment recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + Compute Optimizer + heuristic — authority
  dedup CoH > CO > heuristic by NORMALIZED id.
- Two heuristic checks stacking on one resource, or SUBSET redundancy — fix by
  removal.
- Reduction factor instead of exact price delta (price×factor vs current−target);
  factors off 2-3×.
- $0 "enable X"/opt-in placeholder (CO ResourceId=compute-optimizer-service)
  counted instead of converted to ctx.warn and dropped.
- Metric-gated $0 nudge rendered as COUNTED instead of advisory (Counted=False).
- Cost Hub: (a) currentResourceType with no type_map bucket → dropped (warns only
  on full scan); (b) bucket populated but consumed by NO adapter → dropped
  silently (known orphans: elasticache/opensearch/redshift/s3).
- A source emitted with no PHASE_B handler in a _PHASE_B_SKIP_PER_REC service →
  renders nothing silently.
- Render-time substring/category/Optimized/RI filter desyncing headline from
  cards (filter at SOURCE).
- Coverage gated to a hardcoded family/type/size/state allowlist,
  only-running/only-provisioned, or idle/zero-capacity mishandled.
- CloudWatch/Cost Explorer/CO/CoH permission/throttle failure logged via logger
  only, not ctx.warn/ctx.permission_issue (AccessDenied/Unauthorized/OptInRequired
  → permission_issue).
- CloudWatch reads not gated on ctx.fast_mode (reads_fast_mode not declared).
- Heuristic assuming a usage target with no usage evidence.
- Cross-adapter overlap (same cluster/table/RI in two tabs) — single
  responsibility.
- RI/SP buy recommendation overlapping a rightsizing lever — keep RI/SP advisory,
  rightsize first.
- Each counted finding must carry a structured AuditBasis (rate/region/metric-
  window/formula); counted == rendered.

**Redshift-specific items discovered in the code (verify each):**
- **Orphaned Cost-Hub bucket (CRITICAL):** `ctx.cost_hub_splits["redshift"]` is
  populated from `RedshiftCluster` by the orchestrator but `RedshiftModule.scan`
  never consumes it — real CoH cluster savings are silently dropped. Wire it in
  with CoH > heuristic dedup by normalized cluster id.
- **RI + rightsizing stack on one cluster (HIGH):** a `>3`-node, `>30d`-old,
  `available` cluster fires both "Reserved Instance Optimization" (0.52) and
  "Cluster Rightsizing" (0.24); the adapter sums both → 0.76× cluster cost. Keep
  one lever.
- **RI counted, not advisory (HIGH):** "Reserved Instance Optimization" is
  `Counted=True` here while RDS demotes the equivalent and the orchestrator routes
  `RedshiftReservedInstances` to `commitment_analysis`. Demote to advisory.
- **Reduction factor vs exact delta on rightsizing (HIGH):** "Cluster Rightsizing"
  counts `node_price × nodes × 0.24` for a recommendation to drop to (nodes−2);
  the exact delta is `node_price × 2`. The 0.24 factor over the whole cluster is
  arbitrary.
- **Counted ≠ rendered render desync (HIGH):** the generic renderer shows the
  shim's hardcoded `$nodes*150` / `$(nodes-2)*100` / `$150` strings while the tab
  total counts a live-priced `node_price × nodes × factor` number — and the RI text
  says "24%" while the factor is 52%. Reconcile to one source of truth.
- **Serverless = $0 counted but $150 rendered (MEDIUM):** Serverless recs have no
  `NodeType`, so `if node_type:` is False → $0 counted, yet the card advertises
  "$150/month with reservations". Make advisory and/or price RPUs.
- **Serverless silent failure + missing client (MEDIUM):** the serverless block is
  a bare `except Exception: pass` (no `ctx.warn`), and `required_clients()` omits
  `redshift-serverless` — the whole serverless path can silently no-op.
- **RA3 managed storage unpriced + dead constants (MEDIUM/LOW):** the in-code TODO
  notes RA3 RMS ($0.024/GB-mo) is not priced; `REDSHIFT_NODE_MONTHLY_FALLBACK =
  200.0`, the `pause` 1.00 factor, and the `pause_resume_scheduling`/
  `storage_optimization` buckets are all dead (no check populates them, no paused
  cluster is detected). Delete or implement.

## PROMPT (end)
