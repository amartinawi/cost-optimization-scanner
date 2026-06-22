# RDS Adapter — Cost-Accuracy Remediation Plan

**Date**: 2026-06-22
**Scope**: `rds` adapter only (`services/adapters/rds.py`, `services/rds.py`,
`services/advisor.py` RDS paths, `core/pricing_engine.py` RDS methods,
`reporter_phase_b.py` RDS handlers).
**Audit source**: RDS adapter audit (this session). Prices validated live via the
AWS Pricing API (us-east-1, publicationDate 2026-06-19).
**Reference implementations**: `EC2Module` (cross-source dedup, placeholder→warning,
CoH consumption) and EBS (`services/advisor.get_ebs_compute_optimizer_recommendations`
permission classification + at-source `Optimized` filtering).

> Status: **PLAN ONLY — no code changed.** Implementation is gated on explicit
> approval of a fix subset. IDs (C1, H1…) match the audit deliverable.

---

## 0. Guiding principles

1. **Every emitted recommendation must carry a concrete, account-specific $ saving.**
   A $0 / blank-savings finding is a bug: drop it or convert it to a warning.
2. **Filter at the source, not at render.** The adapter is the single place that
   decides what counts; the reporter only formats. This keeps
   *counted == rendered == per-tab total*.
3. **De-duplicate by normalized resource id** with authority order
   **CoH > Compute Optimizer > heuristics** — never sum the same DB twice.
4. **Never trust a hardcoded/flat rate where AWS publishes a real one.** Validate
   each sub-component (instance vs storage vs backup) independently.
5. **No silent failures.** Permission/opt-in/throttle paths go to
   `ctx.permission_issue` / `ctx.warn`, never `logger`-only.
6. **Document as you go.** Each fix updates code docstrings, the adapter guide,
   and the changelog in the same change (see §4 Documentation Workstream).

---

## 1. Pre-flight (do once, before any fix)

| Step | Action | Why |
|------|--------|-----|
| P-1 | Confirm in-flight tree state. `core/pricing_engine.py`, `services/ec2.py`, `tests/test_ec2_audit_fixes.py` are already modified by other work — **do not touch unrelated hunks**; stage only RDS-relevant lines. | Avoid clobbering parallel work. |
| P-2 | Capture baseline: `pytest tests/test_rds_adapter.py tests/test_regression_snapshot.py tests/test_reporter_snapshots.py` (record green). | Regression anchor. |
| P-3 | Re-read `services/adapters/CLAUDE.md` pricing table + `docs/audits/SUMMARY.md` RDS row (currently "PASS — reference implementation"; this plan downgrades that claim). | Docs must end consistent with code. |
| P-4 | Note the regression gate renders **static golden fixtures** — source/adapter edits do NOT move snapshots; only reporter-rendering edits do. Refresh with `SNAPSHOT_UPDATE=1` only for H1's new renderer, and say so. | Avoid chasing phantom diffs. |

---

## 2. Fix-by-fix remediation (sequenced)

Fixes are ordered so that shared touch-points (pricing engine, advisor) are
edited once, and so each step leaves the suite green. Recommended landing order:
**H4 → H5 → H2 → M3 → M4 → M2 → C1 → H3 → M1 → H1 → L-series.**

Rationale for order: advisor/pricing-engine plumbing first (low blast radius),
then the adapter-shaping fixes (C1/H3/M1) that depend on correct rates, then H1
(largest — new source + renderer + snapshot refresh), then cosmetics/docs.

---

### H4 — Classify RDS Compute Optimizer permission/opt-in failures on `ctx`

**Problem.** `services/advisor.py:163-180` catches `except Exception` and only
`logger.warning`s; `AccessDenied`/`Unauthorized` never reach
`ctx.permission_issue`. Because the advisor swallows everything, the adapter's
own `except ClientError` (`services/adapters/rds.py:93-102`) is unreachable dead
code. Confirmed live: `OptInRequiredException` produced `permission_issues: []`.

**Change.** Mirror `get_ebs_compute_optimizer_recommendations` (advisor.py:147-159):
```text
except Exception as e:
    msg = str(e)
    if "OptInRequiredException" in msg or "not registered" in msg:
        return [_compute_optimizer_opt_in_rec("RDS", "rightsizing")]
    if "AccessDenied" in msg or "UnauthorizedOperation" in msg:
        ctx.permission_issue(..., service="rds",
                             action="compute-optimizer:GetRDSDatabaseRecommendations")
        return []
    ctx.warn(f"Compute Optimizer RDS recommendations unavailable: {msg}", service="rds")
    return []
```
**Adapter cleanup.** Since the advisor no longer raises, simplify
`rds.py:91-104` to a single `try/except Exception → ctx.warn` (or remove the now
redundant `ClientError` branch) and add a comment that classification happens in
the advisor.

**Test.** `test_rds_co_accessdenied_records_permission_issue`,
`test_rds_co_optin_returns_placeholder`, `test_rds_co_other_error_warns` —
monkeypatch a fake `compute-optimizer` client that raises each error class;
assert `ctx._permission_issues` / `ctx._warnings`.

**Docs.** Docstring on `get_rds_compute_optimizer_recommendations` documenting the
three-way classification; note in adapter guide that RDS now matches EBS/EC2.

**Acceptance.** Live scan on the unenrolled account shows the opt-in surfaced as a
warning (not silent); an AccessDenied path records a `permission_issue`.

---

### H5 — Filter non-actionable CO findings at the source

**Problem.** `advisor.py:169-175` appends **all** `rdsDBRecommendations`, including
`Optimized` / no-savings / `UnderProvisioned` (a cost *increase*). EC2/EBS filter
at source; RDS pushes it to the renderer (reporter_phase_b.py:449-461) →
`total_recommendations` inflated, cards render nothing → counted > rendered.

**Change.** Add an `_actionable()` predicate in
`get_rds_compute_optimizer_recommendations` that keeps a rec only when
`finding != "Optimized"` AND it has a rank-1 option with
`estimatedMonthlySavings.value > 0`. Apply on both the first page and the
`nextToken` loop. (Reuse `services/ebs_logic.is_actionable_co_finding` if its
semantics fit; otherwise add an RDS-local predicate and unit-test it directly.)

**Test.** `test_rds_co_drops_optimized`, `test_rds_co_drops_zero_savings`,
`test_rds_co_keeps_overprovisioned_with_savings`.

**Docs.** Docstring mirroring the EC2 wording ("filtering at the source keeps the
counted total and the rendered table in agreement").

**Acceptance.** A CO payload with N recs of which K are actionable yields exactly
K in `co_recs`.

---

### H2 — Convert the CO opt-in placeholder to a warning (drop from counts)

**Problem.** The synthetic `ResourceId="compute-optimizer-service"`,
`estimatedMonthlySavings=0.0` placeholder is counted as a recommendation.
Confirmed live: 0 RDS instances → `total_recommendations=1, savings=0.0`; the
renderer skips it → counted=1, rendered=0.

**Change.** In `services/adapters/rds.py:scan()`, mirror EC2 (ec2.py:105-115):
```text
co_raw = get_rds_compute_optimizer_recommendations(ctx)
if any(r.get("ResourceId") == "compute-optimizer-service" for r in co_raw):
    ctx.warn("AWS Compute Optimizer is not enabled — RDS rightsizing "
             "recommendations from Compute Optimizer are unavailable "
             "(enable it for additional savings detection).", service="rds")
co_recs = [r for r in co_raw if r.get("ResourceId") != "compute-optimizer-service"]
```

**Test.** `test_rds_placeholder_becomes_warning_not_rec` — feed the placeholder;
assert `total_recommendations == 0`, `compute_optimizer` source count 0, one warning.

**Docs.** Comment block matching EC2's rationale; adapter-guide note.

**Acceptance.** Re-run the live unenrolled scan → RDS reports **0** recommendations
and one warning (today it reports 1 phantom rec).

---

### M3 — Fix the RDS storage `volumeType` filter (live path is dead)

**Problem.** `core/pricing_engine.py:641` filters
`volumeType=storage_type.upper()` → `"GP2"/"GP3"/"IO1"`. Validated valid values
are `"General Purpose"`, `"General Purpose-GP3"`, `"Provisioned IOPS"`,
`"Provisioned IOPS-IO2"`. The live path never matches → always falls back.

**Change.** Add an explicit map in `_fetch_rds_storage_price`:
```text
_RDS_STORAGE_VOLUME_TYPES = {
    "gp2": "General Purpose",
    "gp3": "General Purpose-GP3",
    "io1": "Provisioned IOPS",
    "io2": "Provisioned IOPS-IO2",
}
```
Filter on the mapped value; keep `deploymentOption` (and consider adding
`databaseEngine="Any"` to pin the generic SKU deterministically — there are
per-engine duplicate rows at the same price).

**Test.** `test_fetch_rds_storage_price_maps_volume_type` with a fake pricing
client asserting the emitted filter value is `"General Purpose-GP3"` for gp3.

**Docs.** Method docstring lists the mapping; remove the misleading "gp2, gp3,
io1" wording that implied those are the wire values.

**Acceptance.** Live lookup returns $0.115 for gp2/gp3 from the API path (not the
fallback counter).

---

### M4 — Per-engine `deploymentOption` for Multi-AZ pricing

**Problem.** `_fetch_rds_instance_price` (pricing_engine.py:628) always filters
`deploymentOption="Multi-AZ"`. Validated values include
`"Multi-AZ (SQL Server Mirror)"` and `"Multi-AZ (readable standbys)"`. For SQL
Server, the Multi-AZ leg misses → falls back to the MySQL constant, so a SQL
Server Multi-AZ-disable saving is a generic MySQL number.

**Change.** Resolve the deployment label from the engine:
```text
def _rds_deployment_option(engine, multi_az):
    if not multi_az: return "Single-AZ"
    if engine in {"sqlserver-ee","sqlserver-se","sqlserver-ex","sqlserver-web"}:
        return "Multi-AZ (SQL Server Mirror)"
    return "Multi-AZ"
```
(Plain `"Multi-AZ"` remains correct for MySQL/Postgres/MariaDB/Oracle; the
readable-standbys cluster variant is out of scope unless we detect it.)

**Test.** `test_fetch_rds_instance_price_sqlserver_multiaz_label`.

**Docs.** Note the SQL Server mirror deployment in the method docstring.

**Acceptance.** SQL Server Multi-AZ price comes from the API, not the MySQL fallback.

---

### M2 — Pin the RDS backup-storage pricing filter

**Problem.** `_fetch_rds_backup_price` (pricing_engine.py:646-651) filters only
`location` + `productFamily="Storage Snapshot"` with `MaxResults=1` — the
"multiple SKUs, MaxResults=1" anti-pattern.

**Change.** Add a deterministic discriminator (`usagetype` ending in
`RDS:ChargedBackupUsage`, or the appropriate `group`) and validate the value via
`get_pricing_attribute_values`. Keep the $0.095 fallback.

**Test.** `test_fetch_rds_backup_price_filter_is_pinned` asserting the filter set.

**Docs.** Method docstring states the pinned usagetype.

**Acceptance.** Lookup returns the documented backup rate deterministically.

---

### C1 — Remove the phantom gp2→gp3 "20% storage savings"

**Problem (validated).** RDS gp2 and gp3 **base storage cost the same**:
`General Purpose` = **$0.115/GB-Mo**, `General Purpose-GP3` = **$0.115/GB-Mo**
(every engine, us-east-1). The check `savings = allocated × gp2_price × 0.20`
(`services/rds.py:345-366`) fabricates a saving that does not exist. (Contrast
EBS, where gp3 $0.08 < gp2 $0.10.) RDS gp3's benefit is the included 3000 IOPS /
125 MBps baseline vs gp2's 3 IOPS/GB — savings only exist for volumes paying for
provisioned IOPS above that baseline.

**Decision required (pick one):**
- **C1-a (recommended): remove the check entirely.** It cannot produce a
  defensible per-GB $ delta and no provisioned-IOPS signal is available without a
  describe/CloudWatch read. Removing it is consistent with the io1/io2 "review
  IOPS" check already deleted (rds.py:367-368).
- **C1-b: replace with an IOPS-delta check.** Only for gp2 volumes whose
  *provisioned/observed* IOPS demand ≤ the gp3-included 3000 baseline AND whose
  size implies gp2 over-allocates IOPS; price the real gp3 IOPS/throughput add-on
  delta. Requires reading provisioned IOPS (describe) and likely CloudWatch
  `ReadIOPS/WriteIOPS` (respect `ctx.fast_mode`). Higher effort; emit nothing when
  evidence is absent.

**Change (C1-a).** Delete the `storage_type == "gp2"` block and its
`storage_optimization` appends; drop `get_rds_monthly_storage_price_per_gb` usage
here (M3 still fixes the engine method for correctness/other callers). Update
`RDS_OPTIMIZATION_DESCRIPTIONS["storage_optimization"]` or remove if now unused.

**Test.** `test_rds_no_phantom_gp2_gp3_savings` — a gp2 instance yields no
storage_optimization rec.

**Docs.** CHANGELOG entry explaining *why* (gp2==gp3 base price in RDS); a one-line
caveat in the adapter guide so the check is not re-added by reflex.

**Acceptance.** No storage-migration $ emitted for gp2 RDS instances.

---

### H3 — De-duplicate recommendations at source (counted == rendered)

**Problem.** `_aggregate_rds_savings` dedups *savings* (max per ARN) but
`total_recs` counts every rec and **all** recs render. One non-prod Multi-AZ MySQL
DB fires Multi-AZ + Schedule + RI (+Backup), each a separate card with its own
$/month, while the tab total shows only the max → visible cards exceed the tab
total.

**Change.** Replace "dedup only the total" with EC2-style **source-level dedup**
(ec2.py:142-176):
1. Build a per-ARN authority resolver: for each DB-ARN keep the single
   highest-savings finding across {CO, enhanced checks}, authority
   **CoH(after H1) > CO > heuristics** as tie-break, max-$ within a tier.
2. Snapshot recs (different ARN namespace) and RI "up to" recs need explicit
   handling — decide whether RI remains a distinct, clearly-labelled
   *non-stacking* card or is folded into the per-DB max (see M1).
3. Emit only the surviving recs into the sources; `total_recommendations`,
   `total_monthly_savings`, and the rendered cards then all agree.

Extract the resolver as a pure function (e.g. `services/rds_logic.py`,
mirroring `services/ebs_logic.py`) with `normalize_rds_arn` +
`dedupe_by_authority` so it is unit-testable in isolation.

**Test.** Port `tests/test_rds_adapter.py` style: same DB-ARN across CO + 3
enhanced checks → exactly one emitted finding == max; separate DBs independent;
snapshot ARNs independent; assert `total_recommendations == len(emitted)`.

**Docs.** New `services/rds_logic.py` module docstring; adapter-guide note on
authority order; reference from `services/adapters/CLAUDE.md`.

**Acceptance.** For any service JSON, `sum(rendered card $) == per-tab total ==
findings.total_monthly_savings`, and `total_recommendations == count of rendered
cards`.

---

### M1 — Gate Reserved Instances on usage evidence (or fold into H3)

**Problem.** `reserved_instances` fires on **every** `available` DB (incl.
dev/test), savings = `monthly × matrix%`, no steadiness evidence — the main driver
of H3 card inflation and conceptual overlap with the `commitment_analysis` tab
(CoH `RdsReservedInstances`).

**Change (choose):**
- **M1-a:** keep RI but only for instances with a steadiness signal (CloudWatch
  uptime / not flagged as a scheduling candidate); respect `ctx.fast_mode`
  (skip + one warning). Set `reads_fast_mode=True`, `requires_cloudwatch=True` on
  the module.
- **M1-b:** demote RI to a single, clearly *non-stacking* "up to $X" advisory card
  per DB that H3 excludes from the counted total (since CoH/commitment_analysis is
  the authoritative RI source).

**Test.** `test_rds_ri_skipped_in_fast_mode`,
`test_rds_ri_requires_steady_signal` (M1-a) or
`test_rds_ri_not_counted_in_total` (M1-b).

**Docs.** Document the chosen RI policy and its relationship to
`commitment_analysis` in the adapter guide.

**Acceptance.** RI no longer stacks into the headline; behavior matches the chosen
policy.

---

### H1 — Consume the Cost Optimization Hub `rds` bucket (the silent orphan)

**Problem.** `core/scan_orchestrator.py:86-101` routes `RdsDbInstance` /
`RdsDbCluster` into `ctx.cost_hub_splits["rds"]`, but no adapter reads it and no
warning fires → real CoH RDS savings silently dropped. There is also **no**
`("rds","cost_optimization_hub")` reporter handler.

**Change.**
1. **Adapter:** read `coh_recs = [r for r in ctx.cost_hub_splits.get("rds", [])
   if _coh_is_renderable(r)]` (port the EC2 renderable predicate, adjusted for RDS
   — drop RI-purchase recs that belong to `commitment_analysis`, and N/A-resource
   recs). Add a `cost_optimization_hub` `SourceBlock`. Sum its savings via the
   top-level `estimatedMonthlySavings` float.
2. **Dedup:** feed CoH into the H3 resolver as the **top authority** by normalized
   DB id, so a DB surfaced by CoH suppresses the CO and heuristic findings.
3. **Reporter:** register `("rds","cost_optimization_hub"): _render_cost_hub_source`
   in `PHASE_B_HANDLERS` (reporter_phase_b.py:2246+). RDS is already in
   `_PHASE_B_SKIP_PER_REC`, so without a handler the source renders nothing.

**Test.** `test_rds_consumes_cost_hub_split`,
`test_rds_coh_suppresses_co_and_heuristic_for_same_db` (dedup authority),
`test_rds_coh_ri_purchase_excluded` (stays in commitment_analysis domain).

**Snapshot.** This is the **only** fix that changes rendering. After verifying the
new card renders correctly, refresh reporter goldens with `SNAPSHOT_UPDATE=1` and
state explicitly in the commit that the snapshot move is intentional (new RDS CoH
renderer). Inspect the RDS golden fixture first (it may be sparse).

**Docs.** Update `services/adapters/CLAUDE.md` (RDS now consumes CoH like EC2);
update the orphaned-bucket memory note; CHANGELOG.

**Acceptance.** With CoH enrolled (or a mocked split), CoH RDS recs render in the
RDS tab, deduped against CO/heuristics, and counted once.

---

### L-series — Hygiene, evidence, and documentation

| ID | Change | File |
|----|--------|------|
| L1 | Stop defaulting unknown engines to MySQL silently — either map all RDS engines or `ctx.warn` + skip pricing when unmapped. | `core/pricing_engine.py:623` |
| L2 | Add a structured `AuditBasis` to each emitted finding (rate, region, engine, metric/window if any, formula string), so the number is reconstructable from the report alone. Define the shape once (small dataclass/dict) and attach in `services/rds.py`. | `services/rds.py`, maybe `core/contracts.py` |
| L3 | Keep the backup-retention and old-snapshot estimates but make their disclosed-limitation labels first-class in the `AuditBasis` (not just prose), so the report shows the assumption. | `services/rds.py` |
| L4 | Remove dead constants (`RDS_MULTI_AZ_REDUCTION`, `RDS_RESERVED_INSTANCE_REDUCTION`) and never-populated `checks` keys; or wire them if intended. | `services/rds.py` |
| L5 | Confirm `reads_fast_mode` / `requires_cloudwatch` flags match reality after M1/C1. | `services/adapters/rds.py` |

---

## 3. Test strategy

- **Unit (pure functions):** `_aggregate_rds_savings` (existing) extended; new
  `services/rds_logic.py` resolver (`normalize_rds_arn`, `dedupe_by_authority`);
  pricing-engine filter assertions (M2/M3/M4) with a fake pricing client.
- **Adapter-level:** drive `RdsModule.scan` with a `SimpleNamespace` ctx +
  monkeypatched `get_rds_compute_optimizer_recommendations`,
  `get_enhanced_rds_checks`, `get_rds_instance_count`, and a fake
  `cost_hub_splits`. Cover: placeholder→warning (H2), CO filtering (H5), permission
  classification (H4), CoH consumption + dedup authority (H1/H3), counted==rendered
  invariant, fast-mode skip (M1), no phantom gp2→gp3 (C1).
- **Invariant test (new, high value):**
  `total_recommendations == total rendered recs` and
  `total_monthly_savings == sum of per-finding savings after dedup` — assert
  directly on a built `ServiceFindings`.
- **Boto fakes:** fake paginators for `describe_db_instances`,
  `describe_db_snapshots`, `describe_db_clusters`, `describe_db_cluster_snapshots`,
  and (if M1-a) CloudWatch `get_metric_statistics`.
- **Coverage target:** ≥80% on `services/rds.py`, `services/adapters/rds.py`,
  `services/rds_logic.py` per repo testing rules.

**Regression gate:** `pytest tests/test_regression_snapshot.py
tests/test_reporter_snapshots.py` must stay green. Only H1 legitimately moves a
snapshot (new renderer) — refresh with `SNAPSHOT_UPDATE=1` and call it out.

---

## 4. Documentation workstream (done alongside code, not after)

| Doc | Update |
|-----|--------|
| Code docstrings | Every changed function (advisor classification, pricing filters, dedup resolver, adapter scan) gets Google-style docstrings explaining the *why*, mirroring EC2/EBS wording. |
| `services/adapters/CLAUDE.md` | RDS row: note CoH consumption (H1), at-source CO filtering (H5), placeholder→warning (H2), and the removed gp2→gp3 check (C1). Add RDS to the "Consuming Compute Optimizer / CoH" prose. |
| `docs/audits/SUMMARY.md` | Downgrade the stale "RDS — PASS, reference implementation" row; reference this plan and the resolved finding IDs. |
| `CHANGELOG.md` | One entry per landed fix group, conventional-commit style (`fix(rds): …`), each stating the user-visible accuracy impact (esp. C1's phantom-savings removal and H1's recovered CoH savings). |
| `core/CLAUDE.md` | If `core/contracts.py` gains an `AuditBasis` (L2), document it in the contracts row. |
| Memory note | Update `orphaned-cost-hub-buckets.md` to mark `rds` resolved (mirroring the EBS 2026-06-22 entry); leave elasticache/opensearch/redshift/s3 flagged. |
| New: `services/rds_logic.py` | Module docstring describing authority order + normalization, cross-linked from the adapter. |

---

## 5. Out-of-scope follow-ups (note only, do not fix unprompted)

- Same orphaned-CoH-bucket pattern (H1 class) almost certainly affects
  `elasticache`, `opensearch`, `redshift`, `s3` (per memory note). File as a
  separate audit.
- Whether the `commitment_analysis` tab should be the *sole* RI authority
  (removing RDS-side RI entirely) is a product decision beyond this adapter.

---

## 6. Suggested commit slicing

1. `fix(rds): classify Compute Optimizer permission/opt-in on ctx` (H4, H5, H2)
2. `fix(rds): correct RDS pricing filters (storage volumeType, MAZ deployment, backup)` (M3, M4, M2)
3. `fix(rds): remove phantom gp2→gp3 storage savings (gp2==gp3 base price)` (C1)
4. `fix(rds): source-level dedup so counted == rendered` (H3, M1, new rds_logic.py)
5. `feat(rds): consume Cost Optimization Hub bucket + renderer` (H1, snapshot refresh)
6. `chore(rds): AuditBasis, hygiene, docs` (L1–L5 + doc workstream)

Each commit: green suite, RDS-only staged files, docstrings + CHANGELOG in the
same commit.
