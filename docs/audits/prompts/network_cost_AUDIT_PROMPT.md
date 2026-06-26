# Network-Cost (Data Transfer) Adapter Cost-Audit Prompt

A deep, data-transfer-specific audit brief in the same structure as the Lambda
/ RDS / EC2 / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* `network_cost` code path so the auditor
starts from facts, not a blind find-replace. Scope is **strictly cost**: every
emitted recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`network_cost`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **Network** adapter
(`services/adapters/network.py`, and its audit brief
`docs/NETWORK_AUDIT_PROMPT.md`) as the canonical *sibling* for parse-rate /
keyword classification and — critically — for the **cross-adapter overlap**
this adapter shares with it (NAT data-processing and transfer; see Phase 3);
treat **Lambda** (`services/adapters/lambda_svc.py`) as the model for the
metric-gated `$0` advisory (`mark_zero_savings_advisory`, `Counted=False`) and
the test style I expect; treat **EC2** for the exact-delta-not-reduction-factor
discipline.

### NOTE on structure (network_cost is DISTINCT from the `network` adapter)
- **This is NOT the `network` adapter.** `network` (`NetworkModule`) prices
  EIP / NAT / VPC-endpoint / LB / ASG resources. `network_cost`
  (`NetworkCostModule`) prices **data-transfer SPEND** read from **Cost
  Explorer**, plus VPC-peering / Transit-Gateway topology from EC2. They are
  separate tabs that can double-count the same NAT/cross-AZ/egress dollars — the
  overlap is the single biggest thing to verify here (Phase 3).
- Module identity (`services/adapters/network_cost.py`): `key="network_cost"`,
  `cli_aliases=("network_cost", "data_transfer")`,
  `display_name="Data Transfer"`, `required_clients()` → `("ce", "ec2")`,
  `requires_cloudwatch=False`, `reads_fast_mode=False`. It declares
  `stat_cards` (3) and `grouping = GroupingSpec(by="check_category")`.
- **Data source:** one Cost Explorer `get_cost_and_usage` call
  (`USAGE_TYPE_GROUP = ["AWS Data Transfer"]`, grouped by `USAGE_TYPE`, metrics
  `UnblendedCost` + `UsageQuantity`, `MONTHLY`, last 30 days), paginated via
  `NextPageToken`. Plus EC2 `describe_vpc_peering_connections` (active only) and
  `describe_transit_gateways`. **CE is billed $0.01 per request** — a silent CE
  failure wastes the call and emits nothing.
- The adapter emits **four SourceBlocks**: `cross_region_transfer`,
  `cross_az_transfer`, `internet_egress`, `tgw_vs_peering`. Remember this for
  Phase 6. Savings are carried as a numeric `monthly_savings` field per rec
  (snake_case), and the adapter sets `total_monthly_savings` =
  `sum(monthly_savings for Counted)` after calling `mark_zero_savings_advisory`.
- `network_cost` consumes **neither Cost Optimization Hub nor Compute
  Optimizer** (not in `scan_orchestrator._HUB_SERVICES` / `type_map`, no CO
  helper). A "missing CoH/CO source" finding is **NOT fair game** — savings are
  expected to be locally derived from CE.
- **Field-naming quirk:** recs use snake_case keys (`resource_id`,
  `check_category`, `check_type`, `current_value`, `recommended_value`,
  `monthly_savings`, `severity`, `reason`) — unlike most adapters'
  `CheckCategory` / `EstimatedSavings`. This matters for rendering (Phase 6).

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`. `network_cost` is **not** in the
    Live-Pricing table — its "rates" are module constants
    (`CROSS_AZ_SAVINGS_FACTOR=0.5`, `CLOUDFRONT_SAVINGS_FACTOR=0.40`,
    `TGW_ATTACHMENT_COST_PER_GB=0.05`, `TGW_PROCESSING_COST_PER_GB=0.02`) and an
    inline `0.30` cross-region factor. Reconcile the doc against reality; add the
    row if the audit lands fixes.
0b. Confirm `network_cost` is **not** in `core/scan_orchestrator.py`
    `_HUB_SERVICES` and has no `type_map` bucket — so no CoH/CO axis applies.
0c. The savings here are **percentages of a Cost-Explorer bill**, not
    per-resource price deltas. Focus on: (1) whether the usage-type → bucket
    classification is correct, (2) whether the reduction factors are defensible
    or fabricated, (3) the TGW unit/derivation math, (4) the cross-adapter
    overlap with `network`, and (5) silent CE/EC2 failures.

### Phase 1 — Understand the code (read before judging)
1. Read in full: `services/adapters/network_cost.py` (every method);
   `services/_savings.py` (`mark_zero_savings_advisory`, `parse_dollar_savings`);
   `core/contracts.py` (`ServiceFindings`, `SourceBlock`, `StatCardSpec`,
   `GroupingSpec`); `core/result_builder.py` (how `monthly_savings` /
   `check_category` grouping is serialized); the reporter
   (`reporter_phase_b.py` — `PHASE_B_HANDLERS`, `_GENERIC_SOURCE_TYPES`,
   `SOURCE_TYPE_MAP`, `_PHASE_B_SKIP_PER_REC`, `render_generic_per_rec` /
   `_render_generic_other_rec`); and `html_report_generator.py` (the
   `"network_cost"` stat-card descriptor, ~line 170, and the `_extract_*`
   synthetic-tab dispatch ~line 418).
2. Enumerate **every** check, with trigger, source, the exact rec dict, the
   factor/constant it embeds, and counted-vs-advisory status:
   - **`_analyze_cross_region`** — fires when `cross_region_spend > 0`;
     `monthly_savings = spend * 0.30`; severity HIGH if spend>100 else MEDIUM.
   - **`_analyze_cross_az`** — fires when `cross_az_spend > 0`;
     `monthly_savings = spend * CROSS_AZ_SAVINGS_FACTOR (0.5)`.
   - **`_analyze_internet_egress`** — fires when `egress_spend > 0`;
     `monthly_savings = spend * CLOUDFRONT_SAVINGS_FACTOR (0.40)`; explicitly
     discards `multiplier` (`_ = multiplier`) because CE dollars are already
     region-correct.
   - **`_analyze_tgw_vs_peering`** — three branches: (a) `tgw_count>0` → `$0`
     advisory; (b) `peering_count>0 and tgw_count==0` → `$0` advisory; (c)
     `tgw_count>0 and peering_count>0` → `monthly_savings =
     (transfer_spend/0.02) * TGW_PROCESSING_COST_PER_GB(0.02) * 0.20`, emitted
     only if > $1. **Note the algebra:** `/0.02 × 0.02` cancels, so (c) is just
     `transfer_spend × 0.20` where `transfer_spend = cross_region + cross_az` —
     the same spend already scored by the cross-region and cross-AZ recs.
3. Trace the classifier in `_fetch_transfer_spend` precisely (lines ~212–228):
   for each CE group, `usage_type = keys[0].lower()`, then
   - `cross_region` if any of `("interregion","cross-region","region")` in it,
   - **elif** `cross_az` if any of `("transfer-region","az","availability-zone")`,
   - **elif** `egress` if any of `("egress","internet","data-transfer-out")`,
   - **else** → `egress` (catch-all).
   Order matters: the `"region"` substring in the **first** branch swallows any
   usage type containing it.

### Phase 2 — Accuracy of every number (validate with MCP)
4. **Classification correctness (validate usage-type strings via MCP / CE).**
   AWS's real cross-AZ usage type is typically
   `USE1-DataTransfer-Regional-Bytes` — which contains `"regional"` → matches the
   `"region"` substring in the **cross_region** branch and is therefore bucketed
   as **cross-region (×0.30)** before the `cross_az` branch is ever reached. The
   `cross_az` keywords (`"transfer-region"`, `"az"`, `"availability-zone"`) are
   largely **dead** against real usage-type names, and `"az"` is dangerously
   broad (matches any string containing "az"). Confirm the canonical
   data-transfer usage-type strings (`get_pricing_attribute_values` on the
   relevant service `usagetype`, and/or a real CE response) and prove the
   misclassification: genuine cross-AZ spend gets the wrong factor and the wrong
   tab. This is the headline accuracy bug.
5. **Reduction factors are unbacked percentages of the bill.** `0.30`
   (cross-region), `0.50` (cross-AZ), `0.40` (CloudFront egress) are applied to
   raw CE spend with **no per-resource evidence, no target, no metric** — the
   universal "reduction factor instead of exact delta" + "savings from a config
   dimension alone" anti-patterns, and all three are **counted**
   (`monthly_savings > 0` → `Counted=True`). Decide which must become `$0`
   advisory and which can be defended with a calibrated, labelled factor + an
   **AuditBasis**.
6. **TGW constants — validate units with MCP.** `TGW_ATTACHMENT_COST_PER_GB =
   0.05` is a **unit error**: the AWS Pricing API (`AmazonVPC`,
   `usagetype="USE1-TransitGateway-Hours"`) prices the VPC attachment at
   **$0.05 per attachment-HOUR (~$36.50/mo)**, not per GB; only
   `USE1-TransitGateway-Bytes` is per-GB, at **$0.02/GB** (validated). The
   string `"${tgw_total_per_gb:.2f}/GB"` = `"$0.07/GB"` conflates an hourly
   attachment fee with a per-GB rate. Confirm both rates and flag the
   constant name + the conflated string.
7. **TGW branch (c) is circular and double-counts.** Re-derive
   `(transfer_spend/0.02) × 0.02 × 0.20` → `transfer_spend × 0.20`, where
   `transfer_spend = cross_region_spend + cross_az_spend`. Those same dollars
   are already scored by `_analyze_cross_region` (×0.30) and `_analyze_cross_az`
   (×0.50). So the same spend is counted up to **twice**. Confirm and quantify.
8. **No double-multiply (confirm as NOT a bug):** `_analyze_cross_region` /
   `_analyze_cross_az` accept `multiplier` but never use it, and
   `_analyze_internet_egress` explicitly discards it — correct, because CE
   returns real region-priced dollars (matches the L2.3.1 comment). Verify and
   document; don't "fix" it by reintroducing a multiplier.
9. Record a structured **AuditBasis** (rate / region / metric-window / formula)
   on each surviving counted finding so the number is defensible from the report
   alone. A flat percentage of a 30-day CE bill with no usage breakdown is not
   defensible as-is.

### Phase 3 — Duplication (no dollar counted twice)
10. **Intra-adapter:** the TGW branch (c) restacks cross-region + cross-AZ spend
    that the dedicated recs already counted (Phase 2.7). Also confirm the CE
    catch-all `else → egress` doesn't sweep a usage type that another branch
    should own, inflating `internet_egress`.
11. **Cross-adapter (the big one) — `network` vs `network_cost`.** The
    `network` adapter surfaces **NAT cross-AZ ($0.045/GB) and NAT
    data-processing** savings from its own heuristics; `network_cost` surfaces
    **cross-AZ transfer and internet egress** from CE. NAT-routed bytes and
    cross-AZ bytes can appear in **both** tabs — the same dollars counted twice
    across two tabs. Determine the correct single owner (CE-measured spend is the
    more authoritative dollar; the `network` NAT heuristic is a rate estimate)
    and confirm `core/result_builder.py` does not blindly sum across tabs.
    Name this overlap explicitly in the deliverable.
12. **Synthetic tabs:** confirm no `_extract_*` helper in
    `html_report_generator.py` pulls `network_cost` resources into a synthetic
    tab (the `_EXTRACTORS` map covers ec2/ebs/rds/file_systems only — confirm
    network_cost is absent and that's intended).

### Phase 4 — Coverage (works for ALL transfer, not a subset)
13. **`USAGE_TYPE_GROUP = ["AWS Data Transfer"]` only.** Transfer billed under
    other groups (e.g. `EC2: Other`, S3 transfer-out, inter-region replication)
    is excluded — confirm the scope is intentional and documented, not a silent
    gap. The CE call paginates correctly (`NextPageToken` loop) — good.
14. **EC2 topology calls are NOT paginated.**
    `describe_vpc_peering_connections` and `describe_transit_gateways` are
    single calls with no `NextToken` loop — large accounts truncate
    `peering_count` / `tgw_count`. Also `tgw_count` counts **all** TGWs,
    including shared/cross-account and `deleting`-state — confirm the state
    filter.
15. Whole-class behavior: a `cost <= 0` group is skipped (good — no $0 cards),
    but confirm credits/refunds (negative cost) and Savings-Plan-covered
    transfer don't distort the bucket totals.

### Phase 5 — Silent failures (nothing fails quietly)
16. **No `ctx.warn` / `ctx.permission_issue` anywhere in this adapter.**
    - `_fetch_transfer_spend` wraps the CE query in `except Exception as e:
      logger.warning(...)` and returns zeros — a CE `AccessDenied` /
      `OptInRequired` / throttle vanishes from the report (and wastes the $0.01
      call). Classify → `ctx.permission_issue` (Access/Unauthorized/OptIn) else
      `ctx.warn`.
    - `_fetch_network_topology` swallows both EC2 describe failures with
      `logger.warning` only → `peering_count`/`tgw_count` silently 0.
    - `scan()` returns `_empty_findings()` when the CE client is missing — a
      silent empty tab. Confirm whether that should warn.
17. **Pricing/È classification miss → $0.** A misclassified or empty CE response
    yields zero-spend buckets → no recs → a silent empty tab even when real
    transfer spend exists. Tie this back to the Phase 2.4 classifier bug.
18. **`mark_zero_savings_advisory` coverage.** Confirm it correctly demotes the
    two `$0` TGW advisories to `Counted=False` and that branch (c)'s `>1.0`
    floor doesn't emit a sub-dollar counted rec. There is no CloudWatch /
    fast-mode path here (correct — `requires_cloudwatch=False`); confirm no
    hidden CW read exists.

### Phase 6 — Reporting (counted == rendered, four sources)
19. **Source-name vs handler.** The adapter emits `cross_region_transfer`,
    `cross_az_transfer`, `internet_egress`, `tgw_vs_peering`. **None** has a
    `PHASE_B_HANDLERS` entry; only `tgw_vs_peering` has a `_GENERIC_SOURCE_TYPES`
    **badge** ("Metric Backed"). `network_cost` is **not** in
    `_PHASE_B_SKIP_PER_REC`, so it renders via `render_generic_per_rec` →
    `_render_generic_other_rec`. Confirm that path works given the snake_case
    keys: `_render_generic_other_rec` reads `rec.get("resource_id")` first (✓
    snake_case present) but `rec.get("CheckCategory", source_name.title())` —
    the recs only have `check_category` (snake_case) → the card heading falls
    back to the source name, and `monthly_savings`/`reason` print via the
    generic key loop. Verify each of the four sources actually renders a card.
20. **Badge inconsistency / confidence.** `tgw_vs_peering` is labelled "Metric
    Backed" but is a CE/heuristic estimate, not metric-backed; the other three
    sources have **no** badge entry at all. Decide consistent confidence labels
    (these are "Audit Based" / CE-derived, not "Metric Backed").
21. **Stat cards: adapter vs html descriptor.** The adapter declares
    `stat_cards` (Transfer Spend 30d / Cross-Region Spend / Monthly Savings) AND
    `html_report_generator.py` defines a `"network_cost"` `multi_source_cards`
    descriptor (30-Day Transfer Spend / Cross-Region Spend / VPC Peerings / TGW
    Attachments). Determine which one actually renders and whether they
    conflict/duplicate.
22. **Counted == rendered.** Reconcile `total_recommendations = len(all_recs)`
    (includes the `Counted=False` advisories) vs `total_monthly_savings`
    (sum over `Counted=True`). Confirm the grouping (`by="check_category"`) keys
    on the snake_case field the recs actually carry, the per-tab headline shows
    the counted/advisory split, and the executive-summary contribution
    reconciles.

### Phase 7 — Tooling & evidence
23. Run a real scan scoped to data transfer:
    `.venv/bin/python cli.py <region> --scan-only network_cost`
    (or the `data_transfer` alias), then pass the JSON through
    `.venv/bin/python tools/scan_doctor.py <json> --service network_cost`.
    Use `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`.
    Triage every: silent CE/EC2 failure, fabricated/percentage savings, and
    resource appearing in >1 source/tab (esp. the `network` overlap).
    CE data may be sparse or permission-gated; if so, drive
    `NetworkCostModule.scan` and `_fetch_transfer_spend` with a `SimpleNamespace`
    ctx + a fake `ce` client returning crafted `ResultsByTime/Groups` (include a
    `DataTransfer-Regional-Bytes` group to prove the misclassification) and a
    fake `ec2` with peering + TGW.
24. Prove the accuracy/duplication claims: show the AWS Pricing API values
    (TGW attachment `$0.05/attachment-hr` vs processing `$0.02/GB`; cross-AZ
    `$0.01/GB` each direction) next to the scanner's constants; and show the
    same cross-AZ/cross-region dollars counted in both a transfer rec and the
    TGW branch-(c) rec, and (if present) the same NAT bytes in both the
    `network` and `network_cost` tabs.

### Deliverable
- The complete check list (Phase 1.2), per source, with counted-vs-advisory
  marked and the embedded factor/constant for each.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file
  + line, evidence (code excerpt and/or AWS Pricing API / CE value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add `tests/test_network_cost_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: test the
  classifier directly (feed `DataTransfer-Regional-Bytes` and assert it lands in
  cross-AZ, not cross-region), the TGW unit/derivation, the
  `mark_zero_savings_advisory` gating, the silent-failure classification
  (CE `AccessDenied` → `ctx.permission_issue`), and the cross-adapter dedup with
  `network`. Drive `NetworkCostModule.scan` with a `SimpleNamespace` ctx + fake
  `ce`/`ec2` clients/paginators.
- Fix the usage-type classifier (anchor on full AWS usage-type tokens, not the
  bare `"region"`/`"az"` substrings; reorder so cross-AZ is tested before the
  broad `"region"` match). Validate the token set via MCP / a real CE response.
- Replace the circular TGW branch (c) and the unbacked reduction factors with
  defensible math or `$0` advisories; fix the `TGW_ATTACHMENT_COST_PER_GB` unit
  error. Record a structured **AuditBasis** on each counted finding.
- Resolve the `network` ↔ `network_cost` overlap (single owner + a `covered`
  set), and reconcile the stat-card duplication.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for `network_cost` first. Refresh reporter
  snapshots (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional,
  and say so.
- If you find the same classifier / reduction-factor / silent-failure bug in a
  sibling adapter out of scope (e.g. `network`), note it as a follow-up (don't
  fix unprompted).
- Update the `network_cost` row in `services/adapters/CLAUDE.md` to match
  reality. Stage ONLY the files you changed when committing.

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

**network_cost-specific (found in this code):**
- **Classifier substring bug:** the `"region"` token in the **first**
  (cross-region) branch swallows the real cross-AZ usage type
  `DataTransfer-Regional-Bytes` ("regional" contains "region"), so cross-AZ
  spend is mis-bucketed as cross-region and gets ×0.30 instead of ×0.50; the
  `cross_az` keywords (`transfer-region`/`az`/`availability-zone`) are largely
  dead and `"az"` is over-broad.
- **Unbacked reduction factors** counted as real savings: cross-region ×0.30,
  cross-AZ ×0.50, egress ×0.40 — flat percentages of a 30-day CE bill with no
  per-resource evidence, target, or metric.
- **TGW unit error:** `TGW_ATTACHMENT_COST_PER_GB = 0.05` — the AWS VPC
  attachment is **$0.05 per attachment-HOUR (~$36.50/mo)**, not per GB; only
  data processing is per-GB at `$0.02`. The `"$0.07/GB"` string conflates them.
- **Circular, double-counting TGW math:** branch (c)'s
  `(transfer_spend/0.02) × 0.02 × 0.20` reduces to `transfer_spend × 0.20`, and
  `transfer_spend` is the cross-region + cross-AZ spend already counted by the
  other two recs.
- **Cross-adapter overlap with `network`:** NAT-routed cross-AZ / data-processing
  dollars can be counted in both the `network` tab (NAT heuristics) and the
  `network_cost` tab (CE cross-AZ / egress) — same dollars, two tabs.
- **Silent failures:** CE and both EC2 topology calls swallow exceptions with
  `logger.warning` only; no `ctx.warn` / `ctx.permission_issue` anywhere; a CE
  `AccessDenied` produces a silent empty tab (and wastes the $0.01 CE call).
- **Coverage gaps:** scoped to `USAGE_TYPE_GROUP="AWS Data Transfer"` only;
  `describe_vpc_peering_connections` / `describe_transit_gateways` are not
  paginated (truncate on large accounts).
- **Render / badge wiring:** four sources, none with a `PHASE_B_HANDLERS` entry;
  only `tgw_vs_peering` carries a badge ("Metric Backed", wrong confidence); the
  recs' snake_case `check_category` defeats the generic renderer's
  `CheckCategory` lookup; adapter `stat_cards` vs the html `multi_source_cards`
  descriptor may duplicate.

## PROMPT (end)
