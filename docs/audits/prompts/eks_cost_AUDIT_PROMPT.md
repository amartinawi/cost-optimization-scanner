# EKS Cost Adapter Cost-Audit Prompt

A deep, EKS-specific audit brief in the same structure as the Network / Lambda /
RDS audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* EKS code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`eks_cost`** adapter (`EksCostModule`) of this AWS
cost-optimization scanner. Scope is strictly cost: every emitted recommendation
must produce a concrete, account-specific dollar saving. Work read-only first
(understand + validate), then propose fixes grouped by severity, and only
implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools (CodeGraph if present) to trace actual code paths. EKS
deliberately emits node-group/Fargate findings as **advisory (`Counted=False`)**
because those instances belong to the **EC2** adapter
(`services/adapters/ec2.py`) — so EC2 is the canonical reference for the
"don't double-count compute" boundary. Treat the **Lambda**
(`services/adapters/lambda_svc.py`) and **RDS** (`services/adapters/rds.py`)
adapters as the worked examples for the `Counted=False` advisory pattern,
`AuditBasis`, and the test style I expect (`tests/test_lambda_audit_fixes.py`,
`tests/test_rds_audit_fixes.py`, `tests/test_ec2_audit_fixes.py`).

### NOTE on structure (EKS is single-file, counts ONLY EKS-owned cost)
- The whole adapter is `services/adapters/eks.py` (`EksCostModule`) — there is
  **no `services/eks.py` shim and no `eks_logic.py`**; all logic is inline.
- Module key is **`eks_cost`** (NOT `eks`); `cli_aliases=("eks_cost",
  "eks_cost_visibility","eks")`; `display_name="EKS Cost Visibility"`. This name
  mismatch matters for CoH bucketing and rendering — see below.
- It emits **five SourceBlocks**: `cluster_costs`, `node_group_optimization`,
  `fargate_analysis`, `addon_costs`, `cost_hub_recommendations`. Recs use a
  **lowercase snake_case schema** (`resource_id`, `check_type`, `check_category`,
  `current_value`, `recommended_value`, `monthly_savings`, `severity`, `reason`,
  `audit_basis`, `Counted`) — DIFFERENT from the EC2/EBS rec shape; the renderer
  `_render_eks_source` must read these keys.
- **Counted vs advisory:** `total_monthly_savings = sum(monthly_savings for r if
  r.get("Counted", True))`. COUNTED: Extended-Support surcharge, idle/empty
  control plane, failed-cluster control plane, and CoH recs. ADVISORY
  (`Counted=False`, dollars excluded): node-group Spot (×0.70) and Graviton
  (×0.20), and Fargate presence (`monthly_savings=0`). `addon_costs` emits NO
  findings (inventory only).
- **Cost Optimization Hub**: consumed via `ctx.cost_hub_splits["eks_cost"]`
  (`type_map["EksCluster"]="eks_cost"`, and `_HUB_SERVICES` includes `eks_cost` —
  the bucket name MUST equal the module key or `bucket in selected` fails and
  `EksCluster` recs are silently dropped; this was a real prior bug, see the
  orchestrator comment).
- **No Compute Optimizer** for EKS (node instances are covered by EC2 CO). EKS
  declares `requires_cloudwatch=False`, `reads_fast_mode=True`,
  `required_clients()=("eks","ec2","cloudwatch")`. It has `stat_cards` and
  `grouping=GroupingSpec(by="check_category")`.
- EKS is Phase B and in `_PHASE_B_SKIP_PER_REC`; all five sources map to
  `_render_eks_source` in `PHASE_B_HANDLERS`.

### Phase 0 — Orient (5-minute map before judging)
0a. Confirm identity and the **bucket-name == key** invariant in
    `core/scan_orchestrator.py` (`_HUB_SERVICES` contains `"eks_cost"`;
    `type_map["EksCluster"]="eks_cost"`). A regression to `"eks"` silently drops
    all EKS CoH recs.
0b. Read the module constants: `HOURS_PER_MONTH=730`, `GRAVITON_SAVINGS_FACTOR=0.20`,
    `SPOT_SAVINGS_FACTOR=0.70`, `PREV_GEN_PREFIXES` (declared but confirm whether
    actually used). Pricing: `get_eks_control_plane_hourly` (fallback
    `FALLBACK_EKS_CONTROL_PLANE_HOURLY=0.10`), `get_eks_extended_support_hourly`
    (fallback `FALLBACK_EKS_EXTENDED_SUPPORT_HOURLY=0.50`), and
    `get_ec2_hourly_price` for node-group cost.
0c. The counted dollars are small and structural (control plane + Extended
    Support). Most of the risk is: Extended-Support/control-plane rate accuracy,
    the **idle-cluster false positive** (Karpenter/self-managed nodes invisible
    to describe APIs), CoH-vs-cluster dedup, and the advisory node-group math.

### Phase 1 — Understand the code (read before judging)
1. Read `services/adapters/eks.py` in full (`scan`, `_list_clusters`,
   `_describe_cluster`, `_check_cluster_cost`, `_analyze_node_groups`,
   `_node_group_monthly_cost`, `_analyze_fargate`, `_analyze_addons`,
   `_build_cost_hub_recs`, `_is_graviton`, `_is_access_denied`,
   `_optimization_descriptions`, `_empty_findings`); `core/contracts.py`
   (`StatCardSpec`, `GroupingSpec`); `core/pricing_engine.py`
   (`get_eks_control_plane_hourly`, `get_eks_extended_support_hourly`,
   `get_ec2_hourly_price`, and the `FALLBACK_EKS_*` constants);
   `core/scan_orchestrator.py` (`_HUB_SERVICES`/`type_map`);
   `core/result_builder.py`; and `reporter_phase_b.py:_render_eks_source`.
2. List **every** finding with: trigger, data source (`describe_cluster` /
   `list_nodegroups` / `describe_nodegroup` / `list_fargate_profiles` /
   `list_addons` / CoH), savings formula, constant, and counted-vs-advisory.
   Known inventory to confirm — **counted**: `extended_support`
   (`extended_support_rate × 730`, gated on `cluster.upgradePolicy.supportType ==
   "EXTENDED"`), `idle_cluster` (`control_plane_rate × 730`, gated on `ACTIVE` &
   0 node groups & 0 Fargate profiles), `failed_cluster` (`control_plane_rate ×
   730` on `FAILED`), `cost_hub` (flat `estimatedMonthlySavings`). **Advisory**:
   node-group Spot (`ng_monthly × 0.70`), node-group Graviton (`ng_monthly ×
   0.20`), Fargate presence ($0).

### Phase 2 — Accuracy of every number (validate with MCP)
3. Re-derive each rate from the live AWS Pricing API and confirm:
   - **Control plane**: `get_eks_control_plane_hourly` should resolve the EKS
     `perCluster` SKU (`AmazonEKS`, ≈ $0.10/hr ⇒ ~$73/mo). Validate the live
     value and that the $0.10 fallback is region-scaled on the fallback path
     (`× _fallback_multiplier`) with no double-multiply on the engine path.
   - **Extended Support**: `get_eks_extended_support_hourly` (`extendedSupport`
     SKU). The code fallback is **$0.50/hr**, but AWS has published **$0.60/hr**
     for EKS Extended Support in several regions — VALIDATE the current rate via
     the Pricing MCP and flag if the fallback is stale. This surcharge is billed
     ON TOP of the base fee, so it equals the saving from upgrading off it.
   - **Node-group advisory cost**: `_node_group_monthly_cost` calls
     `get_ec2_hourly_price(instance_types[0], quiet=True)` with **no OS/license
     arg** → defaults to Linux. A Windows/Bottlerocket-Windows node group is
     underpriced, and it uses `desiredSize` (not running instance count) ×
     first-type only. Even though these are `Counted=False`, the displayed
     magnitude should be honest — flag the OS omission and the desired-vs-actual
     basis.
   - **Advisory factors**: Spot `×0.70` and Graviton `×0.20` are reduction
     factors on the node-group on-demand cost, NOT exact deltas. Confirm they are
     labelled advisory and validate they're not wildly off (Graviton list-price
     delta ~20% is reasonable; Spot ~70% is optimistic — confirm).
   - **`_is_graviton`** uses a regex (`[0-9]g` in the family + `a1`). Confirm it
     correctly classifies `m6g`/`c7g`/`t4g`/`x2gd`/`im4gn` as Graviton and does
     NOT misclassify x86 families, so the Graviton advisory only fires on x86.
4. Confirm every COUNTED finding carries an `audit_basis` (rate/unit/formula/
   evidence) — `extended_support` and `idle_cluster` do; **`failed_cluster` does
   NOT** (inconsistent). Add the missing basis.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter:** a single node group emits BOTH a Spot advisory and a
   Graviton advisory (`×0.70` + `×0.20` = 0.90 of node cost). They are both
   `Counted=False` so they don't sum into the total today — but confirm they are
   mutually-exclusive *alternatives* in the text (they are) and that a future
   change flipping `Counted` would not silently double-count. Confirm
   `extended_support` and `idle_cluster` cannot both fire on one cluster (idle
   requires 0 node groups; an Extended-Support cluster may still be idle — check
   whether a cluster could legitimately count BOTH the surcharge AND the full
   control-plane idle cost, which would be additive on the same control plane).
6. **Cross-source (CoH vs cluster findings):** `_build_cost_hub_recs` adds CoH
   `EksCluster` recs with NO normalization or dedup against `cluster_costs`. If
   CoH returns a recommendation for the SAME cluster the adapter already flagged
   (idle / Extended Support), both count → double-count. There is **no authority
   dedup (CoH > heuristic) by cluster name** here, unlike EC2/EBS. Flag this and
   propose normalizing `resource_id`/cluster name and deduping.
7. **Cross-adapter (EKS ↔ EC2 — the designed boundary):** node-group instances
   are EC2 resources. EKS emits them `Counted=False`; the EC2 adapter skips
   `_is_eks_managed_instance`-tagged instances and surfaces them via EC2 Compute
   Optimizer / ASG CO. Confirm the boundary holds both ways and that no EKS
   node-group dollars are counted in the EKS tab.

### Phase 4 — Coverage (works for ALL clusters, not a subset)
8. Confirm pagination: `_list_clusters` (paginated), `list_nodegroups`
   (paginated), `list_fargate_profiles` (paginated). Note `list_addons` is a
   single call (no paginator) — confirm that's acceptable (add-on counts are
   small) and that it is inventory-only (no findings).
9. **Idle-cluster false-positive (highest stakes):** `is_idle = (ng_count == 0
   and fp_count == 0)` counts the FULL control-plane cost as recoverable. But
   **self-managed nodes and Karpenter-provisioned nodes are invisible to
   `list_nodegroups`/`list_fargate_profiles`** — a busy Karpenter cluster with 0
   managed node groups would be flagged "idle → delete the cluster", a dangerous
   false positive. The code documents the assumption ("no self-managed/Karpenter
   nodes") but does not corroborate it. Propose a corroborating signal (e.g.
   CloudWatch `cluster_node_count` / EC2 instances tagged
   `kubernetes.io/cluster/<name>`) before counting idle, or demote to advisory.
10. Node groups scaled to 0 (`desiredSize == 0`) correctly yield `ng_monthly <= 0`
    and are **skipped** (no $0 Spot/Graviton noise) — confirm, and confirm a
    pricing miss (`get_ec2_hourly_price` returns 0) is also skipped.

### Phase 5 — Silent failures (nothing fails quietly)
11. EKS uses `_is_access_denied` to classify nearly every call
    (`ctx.permission_issue` vs `ctx.warn`) — confirm coverage across
    `_list_clusters`, `_describe_cluster`, `_analyze_node_groups`,
    `_analyze_fargate`, `_analyze_addons`. Find the gaps:
    - **Pricing lookup failure** (`scan`, ~line 98-104) → `ctx.warn` then rates
      set to `0.0`. With rate 0, control-plane/Extended-Support findings are
      guarded by `> 0` and silently skipped — confirm that's intended and not a
      silent loss of a real saving when the Pricing API is briefly down.
    - **`_node_group_monthly_cost` `except` → `0.0`** silently (no trace) — a
      pricing error makes a node group look free and skips its advisory.
    - **`_build_cost_hub_recs` `except` → `[]`** silently — a malformed CoH split
      drops all EKS CoH recs with no `ctx.warn`.
12. Does a pricing miss fall back to `0.0` and still emit a finding? Confirm every
    counted finding is guarded by `monthly_*_> 0` so no `$0` rec is counted.
13. **Fast-mode:** `reads_fast_mode=True` but the adapter makes no CloudWatch
    reads (only `eks`/`ec2` pricing). Confirm `ctx.fast_mode` only changes the
    Fargate advisory note text and that there is no ungated expensive read.

### Phase 6 — Reporting (one tab, counted == rendered)
14. Confirm all five sources render under the single EKS tab via
    `_render_eks_source` (`PHASE_B_HANDLERS`), and that `_render_eks_source` reads
    the **lowercase rec schema** (`monthly_savings`, `check_category`,
    `current_value`, `recommended_value`, `reason`, `Counted`). Confirm advisory
    (`Counted=False`) cards are RENDERED (visible) but contribute `$0` to the
    headline.
15. **Counted == rendered:** `total_monthly_savings` sums only `Counted=True`
    `monthly_savings`; `total_recommendations = len(all_recs)` (includes
    advisory). Confirm the per-tab headline shows the counted/advisory split and
    that the stat cards (`extras.cluster_count`,
    `extras.monthly_control_plane_cost`, `total_monthly_savings`) are consistent —
    note `monthly_control_plane_cost` is the FULL control-plane spend (all
    clusters), NOT a saving, so it must not be read as recoverable.
16. Reconcile the EKS per-service total against the executive-summary headline.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to EKS:
    `.venv/bin/python cli.py <region> --scan-only eks_cost`
    then `.venv/bin/python tools/scan_doctor.py <json> --service eks_cost`.
    Triage every: silent failure, `$0`/missing-savings finding, and cluster
    counted by both CoH and a cluster finding. Caveats: exercise an
    Extended-Support cluster (set/find `supportType == EXTENDED`); an idle
    cluster (0 node groups, 0 Fargate); a Karpenter cluster to expose the
    idle false positive; a Windows node group to expose the OS-omission
    advisory mispricing. Use `.venv/bin/python` (3.14) — system `python3` lacks
    `datetime.UTC`.
18. For any duplication claim, prove it: show one cluster name in both a CoH rec
    and a `cluster_costs` finding. For any accuracy claim, show the AWS Pricing
    API value (EKS control plane / Extended Support per-hour) next to the
    scanner's constant.

### Deliverable
- The complete check list (Phase 1.2), counted-vs-advisory marked.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact (and, for the idle false positive, the **safety**
  impact). Separate **confirmed bugs** from **known limitations**. End with an
  ID'd fix plan (C1/H1/M1…).

### Implementation (only after I approve)
- Add a `tests/test_eks_audit_fixes.py` mirroring `tests/test_lambda_audit_fixes.py`
  / `tests/test_rds_audit_fixes.py`: test the pure logic (`_is_graviton`
  classification, `_check_cluster_cost` extended/idle/failed branches,
  `_node_group_monthly_cost`, the `Counted=True` sum, the CoH-vs-cluster dedup if
  adopted) and drive `EksCostModule.scan` with a `SimpleNamespace` ctx +
  monkeypatched `eks` client (list/describe paginators) + fake pricing engine.
  Cover every fix: idle corroboration, CoH dedup, node-group OS pricing,
  failed-cluster `audit_basis`, pricing-failure tracing, counted==rendered.
- Record a structured `audit_basis` (rate/unit/formula/evidence) on EVERY counted
  finding (add the missing one on `failed_cluster`).
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for EKS first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- Update the EKS notes in `services/adapters/CLAUDE.md` if behaviour changes.
- Stage ONLY the files you changed.

### Known issue catalogue to check against (found in prior audits)
- Usage savings computed from a config dimension alone (node count/size) with no
  usage metric → fabricated $.
- Wrong architecture/OS pricing (node group priced as Linux when Windows; Spot/
  Graviton factor instead of exact delta).
- Non-deterministic pricing filter (multiple SKUs, `MaxResults=1`).
- Region: hardcoded fallback (0.10 control plane / 0.50 Extended Support) not
  region-scaled or stale, OR `pricing_multiplier` double-applied on the
  region-correct engine path.
- Free / no-cost resource recommended for a saving it cannot realize.
- Same cluster counted by Cost Hub + heuristic — authority dedup CoH > heuristic
  by normalized cluster name (ABSENT in EKS today).
- Two checks stacking on one resource (Spot + Graviton on one node group;
  Extended Support + idle on one control plane).
- Reduction factor instead of exact price delta (Spot ×0.70 / Graviton ×0.20).
- `$0` "enable X" placeholder counted instead of dropped.
- Metric-gated `$0` nudge rendered as COUNTED instead of advisory (Fargate $0 is
  correctly `Counted=False` — confirm).
- Cost Hub: `EksCluster` type bucketed to `eks_cost` (NOT `eks`) — the
  bucket-name == key invariant; a regression silently drops all EKS CoH recs.
- A source with no `PHASE_B` handler in a `_PHASE_B_SKIP_PER_REC` service →
  renders nothing (all five EKS sources DO map to `_render_eks_source` —
  confirm the renderer reads the lowercase schema).
- Coverage gated to managed node groups / Fargate only — self-managed/Karpenter
  nodes invisible → idle false positive; scaled-to-zero node group flagged.
- EKS API / pricing permission or failure logged via `logger`/silent `return []`/
  `0.0`, not recorded via `ctx.warn`/`ctx.permission_issue`.
- Spot/discounted priced at on-demand; Spot recommended without an interruptible
  signal (node-group Spot advisory has no fault-tolerance evidence — it stays
  advisory for this reason).
- Each counted finding must carry a structured `audit_basis`; counted == rendered.

#### EKS-specific items (found in this code)
- **Idle-cluster false positive**: `is_idle` from managed node groups + Fargate
  only; a Karpenter/self-managed cluster with 0 managed node groups is flagged
  "idle → delete" and counts the FULL control-plane cost. Corroborate before
  counting.
- **No CoH-vs-cluster dedup**: `_build_cost_hub_recs` adds `EksCluster` CoH recs
  with no normalization or authority dedup against `cluster_costs` — the same
  cluster can count twice.
- **Extended-Support fallback may be stale** ($0.50/hr in code; AWS has published
  $0.60/hr) — validate against the live Pricing API.
- **Node-group advisory priced as Linux** (`get_ec2_hourly_price` with no OS/
  license arg) and on `desiredSize` × first instance type only — magnitude
  misleading for Windows / multi-type / pending-capacity groups.
- **`failed_cluster` lacks `audit_basis`** while `extended_support`/`idle_cluster`
  carry one — inconsistent evidence trail.
- **Silent `0.0`/`[]` fallbacks** in `_node_group_monthly_cost` and
  `_build_cost_hub_recs` drop findings with no `ctx` trace.
- **Spot + Graviton advisory both emitted per node group** — safe today
  (`Counted=False`) but a latent double-count if `Counted` is ever flipped.

## PROMPT (end)
