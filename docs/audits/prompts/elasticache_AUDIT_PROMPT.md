# ElastiCache Adapter Cost-Audit Prompt

A deep, ElastiCache-specific audit brief in the same structure as the Network /
RDS / Lambda audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* ElastiCache code path so the auditor starts
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
> Service-specific live-audit findings for `elasticache`:
> - CoH consumer — verify the bucket is actually consumed (orphan-bucket check) and dedup is CoH > heuristic by NORMALIZED id (strip ARN).

You are auditing the **`elasticache`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the recently-audited
**RDS** adapter (`services/adapters/rds.py`, `services/rds_logic.py`,
`tests/test_rds_audit_fixes.py`) as the canonical sibling for **Cost
Optimization Hub consumption**, cross-source dedup, RI/commitment demotion, and
the `AuditBasis` / advisory-`$0` patterns. Treat the **network** adapter
(`docs/NETWORK_AUDIT_PROMPT.md`) as the reference for the rate-string / keyword
boundary and `parse_dollar_savings` semantics.

### NOTE on structure (elasticache is a shim + keyword-rate adapter)
- The adapter `services/adapters/elasticache.py` → `ElasticacheModule.scan` is
  thin. The real check logic lives in the legacy shim
  **`services/elasticache.py`** → `get_enhanced_elasticache_checks(ctx)`, which
  describes clusters and emits raw `EstimatedSavings` **strings** (mostly
  percentages like `"30-50%"`, `"20-40%"`). The adapter then **re-prices** each
  rec into a real `EstimatedMonthlySavings` dollar figure via a
  per-`CheckCategory` discount-rate table (`rate_by_category`), NOT by parsing
  the string.
- Pricing strategy: per-node monthly on-demand price via
  `ctx.pricing_engine.get_instance_monthly_price("AmazonElastiCache", node_type)`
  × `NumNodes` × category rate. PricingEngine methods are region-correct — the
  adapter does **not** re-multiply by `ctx.pricing_multiplier` (correct; do not
  add it). There is **no** `_logic.py` helper file.
- The adapter emits a **single** `enhanced_checks` SourceBlock (all five
  category lists flattened into one rec list). Reporter handler is
  `("elasticache", "enhanced_checks") → _render_elasticache_enhanced_checks`
  (reporter_phase_b.py ~line 1052); elasticache is a **Phase B** service and is
  **NOT** in `_PHASE_B_SKIP_PER_REC`, so a per-rec fallback renderer also exists.
- **`requires_cloudwatch = True`** is declared (the shim reads
  `AWS/ElastiCache` `CPUUtilization`). Note the adapter does **NOT** declare
  `reads_fast_mode` — confirm whether the CloudWatch reads are skipped under
  `ctx.fast_mode` (they are not gated in the shim; see Phase 5).

### NOTE on the Cost Optimization Hub orphan-bucket risk (READ FIRST)
This is the single highest-value axis for this adapter. The orchestrator
(`core/scan_orchestrator.py:_prefetch_advisor_data`) maps CoH
`currentResourceType == "ElastiCacheCluster" → "elasticache"` (type_map ~line
92) and `"elasticache"` is in `_HUB_SERVICES` (~line 57), so the bucket
`ctx.cost_hub_splits["elasticache"]` **is populated** on a scan that selects
elasticache. **But `ElasticacheModule.scan` never reads
`ctx.cost_hub_splits["elasticache"]`** — grep confirms only ec2/ebs/rds/lambda/
dynamodb/containers/eks/commitment_analysis consume their buckets. So every
ElastiCache CoH recommendation AWS returns is bucketed and then **silently
dropped with no warning** (the "no service bucket" warning fires only for
*unbucketed* types on a full scan, never for a populated-but-unconsumed bucket).
This is a known prior-audit orphan (see `MEMORY.md` "Orphaned Cost Hub
buckets"). Confirm it still holds and decide the fix: either consume the bucket
(mirroring RDS) with CoH > heuristic dedup by normalized cluster id, or remove
`elasticache` from `_HUB_SERVICES`/type_map so the drop is explicit.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md` and `core/CLAUDE.md`. Both claim
    elasticache prices via **`get_elasticache_node_monthly_price()`**. **That
    method does not exist** in `core/pricing_engine.py` — the adapter actually
    calls the generic `get_instance_monthly_price("AmazonElastiCache",
    node_type)` (pricing_engine.py ~line 737). Record this doc-vs-reality drift
    and plan to correct the CLAUDE.md rows.
0b. Confirm module identity in `services/adapters/elasticache.py`:
    `key="elasticache"`, `cli_aliases=("elasticache",)`,
    `display_name="ElastiCache"`, `required_clients()` → `("elasticache",
    "cloudwatch")`, `requires_cloudwatch=True`, no `reads_fast_mode`.
0c. **Do the orphaned-CoH-bucket check from the NOTE above first.** Grep
    `ctx.cost_hub_splits` across `services/adapters/` and confirm elasticache is
    absent. This is the key axis — anchor the whole audit on it.

### Phase 1 — Understand the code (read before judging)
1. Read end-to-end: `services/adapters/elasticache.py`,
   `services/elasticache.py` (the shim), `services/_savings.py`
   (`parse_dollar_savings`, `mark_zero_savings_advisory` — note the adapter does
   NOT currently use either; it computes dollars from the rate table),
   `core/contracts.py` (`ServiceFindings`, `SourceBlock`, `Counted`),
   `core/pricing_engine.py` (`get_instance_monthly_price`,
   `_fetch_generic_instance_price`, fallback-to-`0.0` path),
   `core/scan_orchestrator.py:_prefetch_advisor_data`, and the reporter
   (`reporter_phase_b.py:_render_elasticache_enhanced_checks` ~1052,
   `html_report_generator.py:_calculate_service_savings` ~3076 and
   `_get_service_content`).
2. List **every** check the shim emits, with: trigger, data source, the raw
   `EstimatedSavings` string, the adapter's `CheckCategory`→rate mapping, and
   whether it ends **counted** or **advisory**. The known inventory:
   - **Valkey Migration** (`valkey_migration`) — every cluster with
     `engine.lower()=="redis"`; string "~20% cheaper"; adapter rate **0.20**.
   - **Graviton Migration** (`graviton_migration`) — node_type not in
     `GRAVITON_FAMILIES` (`m7g/r7g/m6g/r6g/c7g/c6g/t4g`); string "20-40%";
     adapter rate **0.20**.
   - **Reserved Nodes Opportunity** (`reserved_nodes`) — `NumCacheNodes >= 2`;
     string "30-60%"; adapter forces `Counted=False` (commitment lever, overlaps
     the commitment tab).
   - **Underutilized Cluster** (`underutilized_clusters`) — CloudWatch
     `CPUUtilization` avg over 14d `< LOW_CPU_THRESHOLD (20)`, excluding
     already-smallest sizes per `SMALLEST_SIZES`; string "30-50%"; adapter rate
     **0.30**.
   - **Old engine versions** (`old_engine_versions`) — **dead**: the list key
     exists but nothing is appended (comment notes the version-freshness nudge
     was removed). Confirm it stays empty.
   For each note the dedup behavior (Phase 3) and the `CheckCategory` string
   match — the rate table keys MUST equal the exact category strings the shim
   writes (`"Valkey Migration"`, `"Graviton Migration"`, `"Underutilized
   Cluster"`); a typo silently yields rate 0.0 → $0 → advisory.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** figure, re-derive from the live AWS Pricing API:
   - **Node price** — `get_pricing("AmazonElastiCache", region, …)`. Confirm the
     `instanceType` attribute value matches what the adapter passes: the shim
     emits `node_type` like `cache.r6g.large` **with the `cache.` prefix**. Check
     whether `_fetch_generic_instance_price("AmazonElastiCache",
     "cache.r6g.large")` returns a price or falls to `0.0` (DMS strips its
     `dms.` prefix; elasticache does **not** strip `cache.` — verify which form
     the ElastiCache Pricing API `instanceType` attribute actually uses, e.g.
     `cache.r6g.large`). A wrong/non-matching value → `0.0` → the rec silently
     drops to advisory. Validate Redis vs Memcached and node-family rates.
   - **Valkey 0.20** — validate that Valkey is priced ~20% under Redis for the
     same node type (ElastiCache for Valkey pricing). Confirm the discount is a
     real per-hour node-rate delta, not a guess.
   - **Graviton 0.20** — the string says "20-40%" but the adapter counts a flat
     **0.20**. Validate the x86→Graviton node-rate delta for the actual family
     and label it; flag the string/rate mismatch (rendered "20-40%" vs counted
     20%).
   - **Underutilized 0.30** — a downsize saving should be the **exact
     current→one-smaller node price delta**, not `price × 0.30`. Validate the
     0.30 factor against a real one-size-down delta (the EC2 audit found
     reduction factors off 2–3×). `SMALLEST_SIZES` correctly excludes
     already-minimal nodes — confirm coverage of all families present.
   - **Region scaling** — `get_instance_monthly_price` is region-correct and is
     NOT re-multiplied; confirm no double-apply and that `pricing_engine is
     None` (fallback) yields `monthly=0.0` → $0 → advisory (no us-east-1
     leakage).
4. Record a structured **AuditBasis** (node rate / region / CPU metric-window /
   rate-or-delta formula) on each counted finding so the number is defensible
   from the report alone.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-cluster lever dedup (already partly handled):** the adapter keeps the
   single highest-$ counted lever per `ClusterId` (`best_per_cluster`) and marks
   the rest `Counted=False`. Confirm Valkey vs Graviton vs Underutilized are
   genuinely **alternative** remediations on the same nodes (they are) and that
   the dedup key (`ClusterId`) is populated on every rec — note the
   `graviton_migration` rec carries `ClusterId` but **no `NumNodes`/`NodeType`
   defaulting**? (it has `NodeType`, not `NumNodes` → `num_nodes` defaults to 1,
   so Graviton is priced for 1 node while Valkey/Underutilized use real
   `NumNodes`). That asymmetry can flip which lever "wins" — flag it.
6. **Cross-source (the CoH orphan):** if you wire CoH consumption, dedup by
   normalized cluster id with authority **CoH > heuristic**; a CoH
   rightsize/Graviton rec and the heuristic Graviton/Underutilized rec are the
   same dollar. Until wired, document that no double-count exists *because* CoH
   is dropped — but that is the orphan bug, not a clean state.
7. **Cross-adapter:** Reserved Nodes here is forced advisory and the real RI buy
   recommendation lives in `commitment_analysis` (CoH
   `ElastiCacheReservedInstances → commitment_analysis`). Confirm no counted
   ElastiCache RI dollar exists in two tabs.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. The shim iterates `describe_cache_clusters(ShowCacheNodeInfo=True)` and skips
   `status != "available"`. Confirm: (a) **replication groups / cluster-mode
   (sharded) clusters** are covered — `describe_cache_clusters` returns
   per-node-group members; does the per-cluster pricing double-count shards or
   miss them? Cross-check `describe_replication_groups`. (b) **ElastiCache
   Serverless** (caches billed by ElastiCacheProcessingUnits/GB) is **not** a
   `CacheCluster` — confirm it is intentionally out of scope and not silently
   missed. (c) Pagination is via `get_paginator` (good).
9. Are whole classes skipped? Memcached vs Redis vs Valkey engines; Global
   Datastore secondary clusters; data-tiering (r6gd) nodes. Confirm each skip is
   intentional and documented.

### Phase 5 — Silent failures (nothing fails quietly)
10. Failure paths:
    - Shim outer `except Exception as e: ctx.warn(...)` (good) — but classify:
      `AccessDenied`/`UnauthorizedOperation` should be
      `ctx.permission_issue`, not a generic `ctx.warn`.
    - Inner CloudWatch `except Exception … logger.warning(...) ; continue`
      (~line 159) — a CloudWatch permission/throttle failure drops the
      underutilized signal **silently** (logger only, not `ctx`). Mirror the
      Lambda fast-mode/permission fix.
11. Pricing miss → `get_instance_monthly_price` returns `0.0` →
    `EstimatedMonthlySavings = 0` → the dedup loop marks it `Counted=False`.
    Confirm a $0 rec is never counted, and that the `prefix-not-stripped` risk
    from Phase 2 is not silently zeroing **every** counted figure.
12. **fast_mode:** the shim reads CloudWatch unconditionally; the adapter does
    not declare `reads_fast_mode`. Confirm whether a `--fast`/`ctx.fast_mode`
    scan still issues `get_metric_statistics` per cluster (it does) — that is a
    fast-mode violation; gate it and declare `reads_fast_mode`.
13. Percentage-only nudges (Reserved "30-60%", and any rate that maps to 0.0)
    must land advisory (`Counted=False`), rendered but excluded from the count.

### Phase 6 — Reporting (one tab, counted == rendered)
14. Single source `enhanced_checks` → handler
    `_render_elasticache_enhanced_checks` exists; confirm every rec reaches it
    and that advisory recs (Reserved, superseded levers, $0) render but do not
    inflate the count. Note the renderer groups by `CheckCategory` and may show
    `resources[0]["EstimatedSavings"]` (the raw "20-40%"/"30-50%" string) — check
    it does **not** desync from the counted `EstimatedMonthlySavings` dollars
    (rendered "20-40%" next to a counted 20% figure is a legibility bug).
15. **Counted == rendered:** `total_monthly_savings` sums only the winning
    counted lever per cluster; `total_recommendations = len(recs)` includes
    advisory. Confirm `html_report_generator._calculate_service_savings`
    short-circuits on the adapter's `total_monthly_savings` (it does when > 0)
    and the per-tab "(N counted, M advisory)" split (`_counted_advisory_counts`)
    matches the `Counted` flags the adapter set.
16. Confirm no rec is counted in the total but missing from the table, or vice
    versa, across all categories.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to elasticache:
    `python3 cli.py <region> --scan-only elasticache`
    then `python3 tools/scan_doctor.py <json> --service elasticache`.
    Triage every: silent failure, `$0`/advisory finding (separate genuine
    advisory from a prefix-zeroed leak), and resource in >1 lever. Reconcile the
    headline against the per-source sum. Use `.venv/bin/python` (3.14) — system
    `python3` lacks `datetime.UTC`. Caveats: try a region with Redis **and**
    Memcached; exercise a cluster-mode (sharded) replication group; a sparse
    account may have no underutilized clusters (exercise the $0-advisory path).
18. Prove the CoH orphan: run a full scan in an account with ElastiCache CoH
    recommendations and show `ctx.cost_hub_splits["elasticache"]` populated while
    the rendered elasticache tab contains none of them. For any accuracy claim,
    show the AWS Pricing API node rate next to the scanner's number.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-advisory and the
  category→rate mapping marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file
  + line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with an ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Add `tests/test_elasticache_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive
  `ElasticacheModule.scan` with a `SimpleNamespace` ctx + a fake elasticache
  paginator + fake cloudwatch + a stub `pricing_engine`; assert the
  category→rate math, per-cluster lever dedup, Reserved→advisory, prefix
  handling, fast-mode skip, CoH consumption/dedup (if wired), and
  counted==rendered.
- If you wire CoH: consume `ctx.cost_hub_splits["elasticache"]`, dedup by
  normalized cluster id (CoH > heuristic), and demote duplicated heuristics to
  advisory — exactly like RDS.
- Record a structured **AuditBasis** (node rate / region / CPU window /
  formula) on each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the elasticache golden fixture first; refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY for an intentional rendering change, and say so.
- Update the `elasticache.py` row in `services/adapters/CLAUDE.md` and the
  `get_elasticache_node_monthly_price` row in `core/CLAUDE.md` to match reality.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against (found in prior audits)
- Usage savings from a config dimension alone (node/instance/bundle) with **no
  usage metric** → fabricated $.
- Wrong engine/node-type/edition/running-mode pricing; reserved priced as
  on-demand.
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`).
- Region: hardcoded constant/fallback not region-scaled via
  `pricing_multiplier`, OR double-applied on an already-region-correct engine
  path.
- Per-unit **rate string** (`$/GB`, `$/hour`) counted as a monthly total —
  rejected by `parse_dollar_savings` → $0 advisory.
- Free-tier recommended for a saving it cannot realize.
- Same resource counted by Cost Hub + heuristic — authority dedup CoH >
  heuristic by **normalized id**.
- Two heuristic checks stacking, or SUBSET redundancy — fix by removal.
- **Reduction factor instead of exact price delta** (validated factors can be
  off 2–3×).
- `$0` "enable X"/opt-in placeholder counted instead of converted to `ctx.warn`
  and dropped.
- Metric-gated `$0` nudge rendered as COUNTED instead of advisory
  (`Counted=False`).
- Cost Hub: (a) `currentResourceType` with no `type_map` bucket → dropped (warns
  only on full scan); (b) bucket populated but consumed by **NO adapter** →
  dropped with **NO warning** (KNOWN ORPHANS: elasticache/opensearch/redshift/s3
  — **elasticache is one of them; verify and fix**).
- A source emitted with no `PHASE_B` handler in a `_PHASE_B_SKIP_PER_REC`
  service → renders nothing silently.
- Render-time category filter desyncing headline from cards (filter at SOURCE).
- Coverage gated to a hardcoded type/state/running-mode allowlist.
- CloudWatch/Cost Explorer/CoH permission/throttle failure logged via logger
  only, not `ctx.warn`/`ctx.permission_issue` (AccessDenied/Unauthorized →
  `permission_issue`).
- CloudWatch reads not gated on `ctx.fast_mode` (`reads_fast_mode` not declared).
- Heuristic assuming usage with no evidence.
- Cross-adapter overlap.
- Each counted finding must carry a structured **AuditBasis** (rate/region/
  engine/metric-window/formula); counted == rendered.

#### ElastiCache-specific additions
- **CoH orphan (CRITICAL candidate):** `ctx.cost_hub_splits["elasticache"]` is
  populated by the orchestrator but consumed by no adapter — every ElastiCache
  CoH recommendation is silently dropped. Fix by consuming + deduping, or remove
  from `_HUB_SERVICES`/type_map so the drop is explicit.
- **`cache.` prefix not stripped:** the node_type passed to
  `get_instance_monthly_price("AmazonElastiCache", "cache.r6g.large")` may not
  match the Pricing API `instanceType` attribute → `0.0` → every counted figure
  silently zeroes. Verify the exact attribute value form.
- **Graviton priced for 1 node:** the `graviton_migration` rec omits `NumNodes`,
  so it defaults to 1 while Valkey/Underutilized use real `NumNodes` — distorts
  the per-cluster "best lever" dedup. Carry `NumNodes` on every rec.
- **Rendered % vs counted rate desync:** the shim emits "20-40%"/"30-50%"
  strings; the adapter counts flat 0.20/0.30. The renderer may show the string
  next to the counted dollar — reconcile or relabel.
- **Reduction-factor levers:** Valkey 0.20 / Graviton 0.20 / Underutilized 0.30
  are flat factors, not exact node-price deltas. Underutilized especially should
  be the current→one-smaller node delta.
- **Doc drift:** `get_elasticache_node_monthly_price()` is documented but does
  not exist; the adapter uses `get_instance_monthly_price`. Correct both
  CLAUDE.md files.
- **fast_mode not declared:** CloudWatch CPU reads run even under `ctx.fast_mode`.
- **Serverless / cluster-mode coverage:** ElastiCache Serverless and sharded
  replication groups may be miscounted or missed by the per-`CacheCluster` loop.

## PROMPT (end)
