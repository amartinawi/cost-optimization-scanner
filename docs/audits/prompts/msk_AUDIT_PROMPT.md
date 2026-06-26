# MSK Adapter Cost-Audit Prompt

A deep, MSK-specific audit brief in the same structure as the Lambda / RDS /
EC2 / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* MSK code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`msk`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **Lambda** adapter
(`services/adapters/lambda_svc.py`) as the canonical model for the
metric-gated `$0` advisory pattern (`mark_zero_savings_advisory`,
`Counted=False`), the arch-aware module constant priced region-scaled once, and
the rate-string-rejection test style I expect; treat **EC2**
(`services/adapters/ec2.py`) as the model for the **exact current→target price
delta** (vs a blanket reduction factor) and the `$0`-placeholder→`ctx.warn`
pattern; and treat **RDS** (`services/adapters/rds.py`, `services/rds_logic.py`)
as the model for SKU-pinned, edition/license-correct pricing filters.

### NOTE on structure (MSK is a thin FLAT-RATE adapter with a decoupled total)
- `services/adapters/msk.py` → `MskModule` is a `BaseServiceModule`. Its
  `scan()` calls one helper, `get_enhanced_msk_checks(ctx)` in
  `services/msk.py`, then computes the **entire tab dollar total in the adapter
  body** — NOT per recommendation.
- The helper emits recommendation dicts whose `EstimatedSavings` is a **human
  string** (`"$200/month potential"`, `"20% with gp3 migration + retention
  optimization"`). **These strings are never parsed.** `parse_dollar_savings`
  and `mark_zero_savings_advisory` are **not** imported or used here. Instead the
  adapter loops the recs and accumulates
  `savings += (broker_monthly + storage_monthly) * 0.30`. So the **counted
  number and the rendered string are computed by two unrelated code paths** —
  this is the central thing to verify (counted == rendered is NOT guaranteed).
- The adapter emits a **single SourceBlock named `enhanced_checks`**
  (`count=len(recs)`). Remember this for Phase 6.
- MSK consumes **neither Cost Optimization Hub nor Compute Optimizer.** It is not
  in `core/scan_orchestrator.py` `_HUB_SERVICES`, there is no `MskCluster`
  entry in the orchestrator `type_map`, and the adapter pulls no
  `services.advisor.get_*_compute_optimizer_recommendations` helper. AWS CoH/CO
  do not cover MSK — a "missing CoH/CO source" finding is **NOT fair game**;
  savings are expected to be locally derived.
- The adapter declares neither `requires_cloudwatch` nor `reads_fast_mode`.
  This is **correct** — the helper reads no CloudWatch (it only calls
  `kafka:list_clusters` / `list_clusters_v2`). But note the consequence: the
  rightsizing heuristic fires with **no utilization metric at all** (its own
  `Note` admits "Verify actual throughput and utilization before downsizing").

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`. MSK is **not** in the Live-Pricing table
    body but uses a live method (`get_msk_broker_hourly_price`) plus a
    module-constant storage rate — reconcile the doc against reality and add/fix
    the row if the audit lands fixes. Confirm module identity in
    `services/adapters/msk.py`: `key="msk"`, `cli_aliases=("msk",)`,
    `display_name="MSK"`, `required_clients()` → `("kafka",)`.
0b. Map the pricing surface in `core/pricing_engine.py`:
    `get_msk_broker_hourly_price(instance_type)` (~line 713) →
    `_fetch_msk_broker_price(instance_type)` (~line 1226). Note the broker
    price is region-correct from the engine; the **storage rate `0.10` is an
    inline module literal** in `msk.py` and is multiplied by
    `ctx.pricing_multiplier` independently. Confirm the broker price is NOT
    multiplied again (it must not be — see the adapter comment ~lines 45–47).
0c. MSK has **no AWS advisory source** (no CoH / CO / CloudWatch). Drop those
    axes. Focus on: (1) whether the live broker-price filter actually matches
    the AWS Pricing API, (2) the 0.30 reduction factor and the decoupled
    counted-vs-rendered total, (3) the substring trigger / coverage, and
    (4) render wiring of the single `enhanced_checks` source.

### Phase 1 — Understand the code (read before judging)
1. Read in full: `services/adapters/msk.py`; `services/msk.py`
   (`get_enhanced_msk_checks`, `MSK_OPTIMIZATION_DESCRIPTIONS`);
   `core/pricing_engine.py` `get_msk_broker_hourly_price` +
   `_fetch_msk_broker_price`; `services/_savings.py` (to confirm
   `parse_dollar_savings` / `mark_zero_savings_advisory` are NOT used and
   whether they *should* be); `core/contracts.py` (`ServiceFindings`,
   `SourceBlock`); `core/result_builder.py`; and the reporter path —
   `reporter_phase_b.py` (`PHASE_B_HANDLERS`, `_GENERIC_SOURCE_TYPES`,
   `SOURCE_TYPE_MAP`, `_PHASE_B_SKIP_PER_REC`, `_PHASE_A_SERVICES`,
   `render_generic_per_rec` / `_render_generic_other_rec`) and
   `html_report_generator.py`.
2. Enumerate **every** check the helper emits, with: trigger condition, data
   source (pure config heuristic vs metric), the exact `EstimatedSavings`
   string, and whether that string is **counted or ignored** by the adapter's
   savings loop. The known inventory to confirm:
   - **Cluster rightsizing** — trigger: `state == "ACTIVE" and instance_type and
     "large" in instance_type`. Emits `EstimatedSavings="$200/month potential"`,
     `CheckCategory="Cluster Rightsizing"`, carries `InstanceType` +
     `NumberOfBrokerNodes`. This is the **only** rec the adapter's savings loop
     scores (it gates on `rec.get("InstanceType")`).
   - **Storage optimization** — trigger: `volume_size > 1000`. Emits
     `EstimatedSavings="20% with gp3 migration + retention optimization"`,
     `CheckCategory="MSK Storage Optimization"`, carries `VolumeSize` but **no
     `InstanceType`** → contributes **$0** to the counted total yet **is still
     counted in `total_recommendations`** and its 20% string is never quantified.
   - **Serverless migration** — intentionally removed in the helper
     (`list_clusters_v2` loop discards results); confirm nothing re-introduces an
     unquantified "Variable based on usage" rec. Note the `# TODO` in the adapter
     about DCU-hour pricing ($0.06/DCU-hour) — serverless comparison is
     directional only.
3. Trace the adapter savings formula precisely:
   `hourly = get_msk_broker_hourly_price(InstanceType)`;
   `broker_monthly = hourly * 730 * num_brokers`;
   `volume_size = rec.get("BrokerStorageGB", 100)`;
   `storage_monthly = volume_size * 0.10 * num_brokers * pricing_multiplier`;
   `savings += (broker_monthly + storage_monthly) * 0.30`. Note: cluster
   recommendations **never set `BrokerStorageGB`** (the helper only sets
   `VolumeSize` on storage recs, which are skipped here) → `volume_size`
   **always defaults to 100 GB**, and `num_brokers` defaults to 3.

### Phase 2 — Accuracy of every number (validate with MCP)
4. **Broker price filter is the headline accuracy risk — validate first.**
   `_fetch_msk_broker_price` builds
   `filters=[{"Field":"instanceType",...},{"Field":"location",...}]` against
   service code `AmazonMSK`. Using the Pricing MCP, run
   `get_pricing_service_attributes("AmazonMSK")` — confirm whether
   `instanceType` is even a valid attribute. (As of this writing the real
   filterable attributes are `computeFamily`, `usagetype`, `storageFamily`,
   `vcpu`, `memoryGib`, `productFamily`, `location` — there is **no
   `instanceType`**.) If `instanceType` does not exist, the
   `TERM_MATCH`/`EQUALS` filter never matches → the API returns no rows →
   `_fetch_msk_broker_price` returns `None` → `get_msk_broker_hourly_price`
   **always falls back** to `get_ec2_hourly_price(clean_type) * 1.4` (or `0.15`).
   Prove it: query the real broker rate
   (`computeFamily="m5.large"`, `productFamily="Managed Streaming for Apache
   Kafka (MSK)"`) — the on-demand `RunBroker` "Broker-hours" SKU is **$0.21/hr**
   in us-east-1 — and compare to the EC2-derived fallback
   (`m5.large` ≈ $0.096 × 1.4 ≈ $0.134, ~36% low). Classify the dead live path
   and the under-priced fallback markup factor (1.4) as findings.
5. **Storage rate `0.10`.** Confirm against
   `AmazonMSK` `usagetype="USE1-Kafka.Storage.GP2"` — it is **$0.10/GB-Mo** in
   us-east-1 (validated). The literal is us-east-1-correct and **is**
   region-scaled via `pricing_multiplier` — confirm that's right and that the
   broker leg is NOT double-multiplied. Note: provisioned MSK storage is GP2
   (`Kafka.Storage.GP2`); Tiered storage and Express brokers
   (`express.m7g.*`, `t3.small`) are priced differently — confirm coverage.
6. **The 0.30 reduction factor.** `savings = (broker+storage) * 0.30` is a
   blanket "downsize ~30%" with **no usage/utilization evidence** and **no
   target instance**. This is the universal "reduction factor instead of exact
   price delta" + "usage savings from a config dimension alone" anti-pattern.
   Decide the defensible fix: either compute an exact current→target broker
   delta (one size down, mirror EC2's price-delta approach) or demote to a `$0`
   advisory (mirror Lambda's metric-gated `Counted=False`). Record a structured
   **AuditBasis** (rate / region / formula / the absence of a metric) on any
   finding that survives.
7. **Counted vs rendered.** Confirm the rendered card shows the helper's
   `EstimatedSavings` string ("$200/month potential") while the tab total is the
   30%-factor number — i.e. the visible per-rec dollar and the counted total are
   **different numbers from different code paths**. This is a reporting-integrity
   defect; the auditor must reconcile them.

### Phase 3 — Duplication (no dollar counted twice)
8. **Intra-adapter:** can one cluster match both checks (rightsizing AND
   storage>1000GB) and have its bill discounted twice? Today the storage rec
   contributes $0 (no `InstanceType`), so there is no *dollar* double-count — but
   if a fix starts quantifying the storage rec, a large cluster would be counted
   in both. Pre-empt: define the single owner and the dedup set.
9. **Cross-adapter:** MSK brokers are EBS-backed and Kafka traffic is cross-AZ.
   Confirm MSK broker volumes are **not** also surfaced by the **EBS** adapter
   (MSK-managed EBS is not user-visible, but verify `describe_volumes` does not
   pick them up), and that MSK inter-broker cross-AZ traffic is not also counted
   by the **network** or **network_cost** adapters.
10. **Synthetic tabs:** confirm no `_extract_*` helper in
    `html_report_generator.py` pulls MSK resources into a synthetic
    (Snapshots/AMIs-style) tab.

### Phase 4 — Coverage (works for ALL clusters, not a subset)
11. The rightsizing trigger is `"large" in instance_type` — a **substring**
    match. This (a) matches every `*.large`, `*.xlarge`, `*.2xlarge` … (the
    substring "large" is in "xlarge"), and (b) **excludes** `t3.small` and any
    future small graviton type that could still be over-provisioned. Decide
    whether the gate should be utilization-based, not name-based.
12. The trigger is gated to `state == "ACTIVE"`. Confirm clusters in other
    states (e.g. `UPDATING`, `MAINTENANCE`) are intentionally skipped and won't
    silently lose a real saving.
13. Confirm `list_clusters` pagination (it uses a paginator — good) and that
    provisioned-vs-serverless (`list_clusters_v2`) coverage is intentional.
    Express brokers and serverless (DCU) clusters are currently un-priced —
    document the gap, don't fabricate.

### Phase 5 — Silent failures (nothing fails quietly)
14. `get_enhanced_msk_checks` wraps the whole scan in `except Exception as e:
    ctx.warn(...)`. Classify: an `AccessDenied` / `UnauthorizedOperation` /
    `OptInRequired` on `kafka:ListClusters` should be `ctx.permission_issue`,
    not a generic `ctx.warn`. The inner `list_clusters_v2` block swallows with a
    bare `except Exception: pass` — a v2 permission/throttle failure vanishes
    entirely; confirm whether that matters now that the serverless finding is
    removed.
15. **Pricing-miss → $0 path.** When `pricing_engine is None`, `savings` stays
    `0.0` but the recs are **still emitted and counted in
    `total_recommendations`** with their human strings — a tab that shows
    recommendations but $0 counted savings. When `get_msk_broker_hourly_price`
    falls back to `0.15` (EC2 lookup also failed), the number is fabricated.
    Decide which of these should become `$0` advisory (`Counted=False`) vs a
    recorded warning.
16. There is no CloudWatch and no `fast_mode` path — confirm that's acceptable
    and that the rightsizing heuristic's lack of a utilization metric is the
    real defect (a usage-target assumption with no usage evidence).

### Phase 6 — Reporting (one tab, counted == rendered)
17. **Source-name vs handler.** The adapter emits `("msk","enhanced_checks")`.
    `PHASE_B_HANDLERS` has no `("msk", …)` entry, and `msk` is **not** in
    `_PHASE_B_SKIP_PER_REC` nor `_PHASE_A_SERVICES`. Trace exactly how the
    source reaches the renderer: confirm it falls through to
    `render_generic_per_rec` → `_render_generic_other_rec` (the generic
    "other" card). Confirm the resource id resolves (the generic renderer reads
    `ClusterName` in its or-chain) and that the card renders.
18. **Source-confidence badge.** `_GENERIC_SOURCE_TYPES["enhanced_checks"] =
    "Metric Backed"`, and there is **no** `SOURCE_TYPE_MAP[("msk",
    "enhanced_checks")]` override. But MSK's checks are **pure config heuristics**
    (instance-name substring, volume>1000) with **no metric** — labelling them
    "Metric Backed" misrepresents confidence. This is exactly the precedent the
    S3 audit fixed with `("s3","enhanced_checks") → "Audit Based"`. Propose the
    analogous override.
19. **Counted == rendered.** Reconcile: `total_recommendations = len(recs)`
    (includes the $0 storage rec) vs `total_monthly_savings` (the 30%-factor sum
    over only `InstanceType`-bearing recs). Confirm the per-tab headline, the
    executive-summary contribution (`_get_executive_summary_content` /
    `_calculate_service_savings`), and the visible card dollar string all
    reconcile — they currently can't, because the card shows "$200/month
    potential" while the total is a different computed number.

### Phase 7 — Tooling & evidence
20. Run a real scan scoped to MSK:
    `.venv/bin/python cli.py <region> --scan-only msk`
    then pass the JSON through
    `.venv/bin/python tools/scan_doctor.py <json> --service msk`.
    Use `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`.
    Triage every: silent failure, `$0`/fabricated-savings finding, and
    counted-vs-rendered mismatch. MSK clusters are expensive to stand up; if the
    account has none, drive `MskModule.scan` and `get_enhanced_msk_checks` with a
    `SimpleNamespace` ctx + a fake `kafka` paginator returning crafted
    `ClusterInfoList` entries (ACTIVE m5.large with 3 brokers; a >1000GB volume).
21. Prove the accuracy claims: show the AWS Pricing API value
    (`AmazonMSK` `computeFamily="m5.large"` → $0.21/hr broker; `USE1-Kafka.
    Storage.GP2` → $0.10/GB-Mo) next to the scanner's fallback-derived number,
    and demonstrate the `instanceType` filter returning zero rows.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-ignored marked per rec.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file
  + line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add `tests/test_msk_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive
  `MskModule.scan` with a `SimpleNamespace` ctx + monkeypatched
  `get_enhanced_msk_checks` and a fake `kafka` client/paginator; assert the
  broker price actually comes from a matching Pricing filter (not the fallback),
  that the counted total equals the sum of the rendered counted findings, that
  the storage/percentage rec is `$0` advisory (`Counted=False`), and that an
  `AccessDenied` becomes `ctx.permission_issue`.
- Fix the `_fetch_msk_broker_price` filter to a real attribute
  (`computeFamily` and/or `usagetype`), pinned so it does not become a
  non-deterministic multi-SKU `MaxResults=1` lottery; validate against the
  Pricing API.
- Replace the 0.30 blanket factor with an exact current→target delta or a `$0`
  advisory; record a structured **AuditBasis** on every counted finding so the
  number is defensible from the report alone.
- Add a `SOURCE_TYPE_MAP[("msk","enhanced_checks")]` confidence override if the
  checks remain config-heuristic.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for `msk` first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same dead-filter / reduction-factor / counted-vs-rendered bug
  in a sibling adapter out of scope, note it as a follow-up (don't fix
  unprompted).
- Update the `msk` row in `services/adapters/CLAUDE.md` to match reality.
- Stage ONLY the files you changed when committing.

### Known issue catalogue to check against

**Universal (verbatim — every adapter):**
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

**MSK-specific (found in this code):**
- **Dead live-price filter:** `_fetch_msk_broker_price` filters on
  `Field="instanceType"`, which is **not a valid `AmazonMSK` attribute** (real
  dims: `computeFamily` / `usagetype` / `storageFamily` / `vcpu` / `memoryGib`).
  The filter never matches → every broker price silently uses the
  `get_ec2_hourly_price × 1.4` (or `0.15`) fallback. Validated real m5.large
  broker = $0.21/hr vs ~$0.134 fallback (~36% low).
- **Counted ≠ rendered:** the tab total is `(broker+storage) × 0.30` computed in
  the adapter, fully decoupled from the per-rec `EstimatedSavings` string
  (`"$200/month potential"`, `"20% with gp3…"`). The strings are never parsed;
  `parse_dollar_savings` / `mark_zero_savings_advisory` are unused.
- **Blanket 0.30 reduction factor** with no utilization metric and no target
  instance — the rec's own `Note` admits throughput is unverified. Fabricated $
  from a config dimension alone.
- **Phantom storage default:** cluster-rightsizing recs never set
  `BrokerStorageGB`, so `storage_monthly` always assumes **100 GB** (and
  `num_brokers` defaults to 3) regardless of the real cluster.
- **Storage rec is counted-but-unpriced:** the `volume_size > 1000`
  recommendation has no `InstanceType`, contributes **$0** to savings, yet is
  counted in `total_recommendations` and its "20%" string is never quantified —
  should be a `$0` advisory.
- **Substring trigger `"large" in instance_type`** over-matches every
  `*.large/*.xlarge/*.2xlarge` and excludes `t3.small` / small graviton — a
  name-based gate where a utilization gate belongs; also gated to `state ==
  "ACTIVE"` only.
- **Confidence mislabel:** the single `enhanced_checks` source inherits the
  generic `"Metric Backed"` badge, but MSK checks are config heuristics with no
  metric — needs an `("msk","enhanced_checks")` override (mirror the S3 fix).
- **Generic exception → `ctx.warn`:** `kafka:ListClusters` `AccessDenied` is
  reported as a generic warn, not `ctx.permission_issue`; the inner
  `list_clusters_v2` `except: pass` swallows v2 failures silently.

## PROMPT (end)
