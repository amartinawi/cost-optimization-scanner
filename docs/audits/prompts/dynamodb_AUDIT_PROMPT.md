# DynamoDB Adapter Cost-Audit Prompt

A deep, DynamoDB-specific audit brief in the same structure as the Network /
RDS / EC2 audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* DynamoDB code path so the auditor starts from
facts, not a blind find-replace. Scope is **strictly cost**: every emitted
recommendation must produce a concrete, account-specific dollar saving.

---

## PROMPT (copy from here)

You are auditing the **`dynamodb`** adapter of this AWS cost-optimization
scanner. Scope is strictly cost: every emitted recommendation must produce a
concrete, account-specific dollar saving. Work read-only first (understand +
validate), then propose fixes grouped by severity, and only implement after I
confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, metrics,
units, and billing codes — never trust hardcoded rates or memory. Use the
codebase/search tools to trace actual code paths. Treat the **RDS** adapter
(`services/adapters/rds.py`, `services/rds_logic.py`) as the canonical model for
cross-source dedup, the **Reserved-Instance→advisory demotion** rule, and
Cost-Hub consumption; treat the **EC2** adapter (`services/adapters/ec2.py`) as
the model for the `$0`-placeholder→warning pattern and the exact-price-delta
(not reduction-factor) discipline. The recently-audited **Lambda** adapter
(`services/adapters/lambda_svc.py`) and `tests/test_lambda_audit_fixes.py` are
the worked examples for the `mark_zero_savings_advisory` pattern and the test
style I expect.

### NOTE on structure (DynamoDB is a three-source aggregator with module-constant pricing)
- The adapter `services/adapters/dynamodb.py` → `DynamoDbModule.scan` fuses
  **three** savings sources and de-dups across them by `TableName`:
  - **Table analysis** (`services.dynamodb.get_dynamodb_table_analysis`) —
    per-table billing-mode/capacity baseline; source block
    `dynamodb_table_analysis` (the base layer).
  - **Enhanced heuristics** (`services.dynamodb.get_enhanced_dynamodb_checks`) —
    unused/over-provisioned/reserved-capacity/billing-mode/data-lifecycle
    checks; source block `enhanced_checks` (the higher-fidelity override).
  - **Cost Optimization Hub** (consumed from `ctx.cost_hub_splits["dynamodb"]`,
    the bucket the orchestrator fills from the `DynamoDBTable`
    `currentResourceType`); source block `cost_optimization_hub`.
- There is **NO Compute Optimizer** for DynamoDB (it does not cover the service),
  so a "missing CO source" finding is not fair game — capacity savings are
  derived locally from module constants + the shim's CloudWatch reads.
- Pricing is **module constants**, NOT `PricingEngine`. The adapter holds
  `_DYNAMODB_RCU_HOURLY = 0.00013`, `_DYNAMODB_WCU_HOURLY = 0.00065`,
  `_HOURS_PER_MONTH = 730`, region-scaled by `ctx.pricing_multiplier` at the emit
  site. The shim mirrors these (`_PROVISIONED_RCU_COST = 0.00013*730`,
  `_PROVISIONED_WCU_COST = 0.00065*730`) plus on-demand per-request rates
  (`_ON_DEMAND_RCU_PER_REQUEST = 0.125/1e6`, `_ON_DEMAND_WCU_PER_REQUEST =
  0.625/1e6`). Every one of these is a hardcoded us-east-1 constant — validate
  each against the live Pricing API and confirm `pricing_multiplier` is applied
  exactly once (RCU/WCU path) and **not** double-applied on the on-demand path.
- Savings are factor-based via `DYNAMODB_SAVINGS_FACTORS` (reserved 0.66,
  rightsize_provisioned 0.40, billing_mode_switch 0.40, unused 1.00,
  data_lifecycle 0.60, default 0.30). The adapter's `_enhanced_savings_factor`
  maps a rec's `CheckCategory` → factor; the factor is applied to the table's
  **whole** current monthly cost from RCU/WCU. A rec with no rcu/wcu (on-demand /
  unused / data-lifecycle) gets `rec_savings = 0.0`.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`, find the `dynamodb.py` row ("RCU/WCU
    hourly rates × 730 | Module constants"), and reconcile it against reality:
    confirm the rates, the three source blocks, the CoH consumption, and that
    there is no live `PricingEngine` method for DynamoDB. Flag any drift.
0b. Confirm module identity in `services/adapters/dynamodb.py`: `key="dynamodb"`,
    `cli_aliases=("dynamodb",)`, `display_name="DynamoDB"`,
    `requires_cloudwatch=True`, `required_clients()=("dynamodb","cloudwatch")`.
    Note that `reads_fast_mode` is **not** declared even though both shim
    functions hit `cloudwatch.get_metric_statistics` — flag if the CloudWatch
    reads are not gated on `ctx.fast_mode`.
0c. DynamoDB has CoH but no CO and no `PricingEngine` method. Focus on:
    constant accuracy, the reserved-capacity-as-counted question, the
    config-dimension-without-usage-evidence question, GSI coverage, and the
    table-analysis↔enhanced↔CoH dedup.

### Phase 1 — Understand the code (read before judging)
1. Read the full DynamoDB path: `services/adapters/dynamodb.py`
   (`DynamoDbModule.scan`, `_enhanced_savings_factor`, the module constants),
   `services/dynamodb.py` (`get_dynamodb_table_analysis`,
   `get_enhanced_dynamodb_checks`, `get_dynamodb_optimization_descriptions`,
   `DYNAMODB_SAVINGS_FACTORS`, all `_*_THRESHOLD`/buffer constants),
   `services/_savings.py` (`parse_dollar_savings`, `mark_zero_savings_advisory`),
   `core/contracts.py`, `core/scan_orchestrator.py` (`type_map`:
   `DynamoDBTable`→`dynamodb`), `core/result_builder.py`, and the reporter
   (`reporter_phase_b.py:_render_dynamodb_enhanced_checks` ~864,
   `_render_generic_dynamodb_rec` ~1702; `dynamodb` is in `_PHASE_B_SKIP_PER_REC`,
   handlers `("dynamodb","enhanced_checks")` and
   `("dynamodb","dynamodb_table_analysis")` both → `_render_dynamodb_enhanced_checks`,
   `("dynamodb","cost_optimization_hub")` → `_render_cost_hub_source`).
2. List **every** cost check, and for each give: trigger condition, data source
   (CloudWatch `Consumed{Read,Write}CapacityUnits` / describe-API / CoH), the
   savings formula, the factor/constant, whether it is **counted or contributes
   $0**, and the source block it lands in. Confirm the known inventory:
   - `dynamodb_table_analysis`: every provisioned table → `default` factor 0.30 ×
     (RCU×rate + WCU×rate)×730×multiplier; every on-demand table →
     `EstimatedMonthlyCost`×multiplier×0.30 (EstimatedMonthlyCost from CW
     consumed-capacity × per-request rate). Dedup: a table also in
     `enhanced_checks` is skipped here.
   - `enhanced_checks` "Unused DynamoDB Tables" (ItemCount==0): factor 1.00, but
     rcu/wcu drive the base — so an empty on-demand table contributes $0.
   - `enhanced_checks` "DynamoDB Over-Provisioned Capacity" (RCU>100 or WCU>100):
     factor `rightsize_provisioned` 0.40 × full table cost — emitted for ANY
     high-capacity table regardless of measured utilization.
   - `enhanced_checks` "DynamoDB Reserved Capacity" (RCU≥100 AND WCU≥100): factor
     `reserved_capacity` 0.66 × full table cost — **counted**.
   - `enhanced_checks` "DynamoDB Billing Mode - Metric-Backed" /
     "DynamoDB Monitoring Required" / "DynamoDB CloudWatch Required" (on-demand):
     no rcu/wcu → $0.
   - `enhanced_checks` "DynamoDB Data Lifecycle" (size > 10 GiB): factor 0.60 but
     no rcu/wcu → $0.
   - `cost_optimization_hub`: CoH `estimatedMonthlySavings` summed for tables not
     already covered by the two heuristic sources.

### Phase 2 — Accuracy of every number (validate with MCP)
3. For each **counted** figure, re-derive it from the live AWS Pricing API:
   - **Provisioned RCU/WCU**: validate `_DYNAMODB_RCU_HOURLY = 0.00013`/RCU-hr and
     `_DYNAMODB_WCU_HOURLY = 0.00065`/WCU-hr (service code `AmazonDynamoDB`,
     usagetype `…ReadCapacityUnit-Hrs` / `…WriteCapacityUnit-Hrs`). Confirm the
     `×730` and that `pricing_multiplier` is applied exactly once.
   - **On-demand per-request**: validate `0.125`/M reads, `0.625`/M writes
     (`PayPerRequestThroughput` ReadRequestUnits/WriteRequestUnits). Confirm the
     shim's `EstimatedMonthlyCost` uses the **Average** CloudWatch
     `Consumed{Read,Write}CapacityUnits` × per-request rate × 730 — scrutinise the
     unit conflation: `ConsumedReadCapacityUnits` is a capacity-unit metric, not a
     request count, and `Average` over a 1-hour `Period` is not a monthly volume.
     Confirm the `×730` math actually yields monthly request units, and that the
     on-demand path is **not** double-region-scaled (shim has no multiplier;
     adapter multiplies once).
   - **Reserved capacity factor 0.66**: confirm the 53–76% AWS range and the
     midpoint, and whether reserved capacity should be **counted at all** (see
     Phase 3.6 — it is a commitment lever).
   - **Reduction factors vs exact delta**: 0.40 over-provisioned / 0.30 default
     are blanket factors over the **whole** table cost, not a delta to a concrete
     target capacity. The over-provisioned shim already computes a
     measured-utilization `read_utilization`/`write_utilization` — prefer
     `current − target` (target = rightsized RCU/WCU at the observed utilization)
     to a flat 0.40.
4. Record a structured **AuditBasis** (rate / region / metric-window / formula)
   on each counted finding. The number must be defensible from the report alone:
   a 0.30-factor "saving" on a provisioned table with no utilization signal is
   not defensible.

### Phase 3 — Duplication (no dollar counted twice)
5. **Intra-adapter / intra-table:** confirm the `enhanced_table_names` skip in
   the adapter prevents a table being counted in both `dynamodb_table_analysis`
   and `enhanced_checks`. Then check **within** `enhanced_checks`: a single table
   with RCU≥100 AND WCU≥100 fires **both** "DynamoDB Over-Provisioned Capacity"
   (0.40) **and** "DynamoDB Reserved Capacity" (0.66) — the adapter applies the
   factor per rec, so the same table's savings stack to 1.06× its cost. Prove or
   disprove the stack; fix by demotion/removal, not double-count.
6. **Reserved-capacity = commitment lever (the RI rule):** RDS treats
   `Reserved Instance Opportunities` as **advisory** (`ADVISORY_CATEGORIES`,
   `Counted=False`) because RI/reserved-capacity purchases overlap rightsizing on
   the same resource and belong to the commitment view. DynamoDB reserved
   capacity is the identical lever (and CoH would surface
   `*ReservedCapacity`/`*ReservedInstances` to `commitment_analysis`). Determine
   whether "DynamoDB Reserved Capacity" should be demoted to advisory rather than
   counted, and whether it double-counts against the over-provisioned rightsizing
   rec on the same table.
7. **Cross-source (CoH):** confirm the CoH dedup — `covered_tables` is the union
   of `dynamodb_table_analysis` + `enhanced_checks` TableNames, and a CoH
   `resourceId` whose last `/`-segment matches is skipped. Verify the ARN→table
   parsing (`resourceId.split("/")[-1]`) survives real DynamoDB ARNs
   (`…:table/Name`, and index ARNs `…:table/Name/index/GSI`). Confirm authority
   order is CoH > heuristic for the kept set, and that `result_builder.py` doesn't
   blindly re-sum.

### Phase 4 — Coverage (works for ALL resources, not a subset)
8. **GSIs (the big coverage gap):** `get_dynamodb_table_analysis` /
   `get_enhanced_dynamodb_checks` read only the **table-level**
   `ProvisionedThroughput`. Global Secondary Indexes carry their **own**
   provisioned RCU/WCU (`GlobalSecondaryIndexes[].ProvisionedThroughput`) that is
   billed separately and never summed — a table with small base throughput and
   large GSIs is materially under-costed (and its over-provisioned GSIs are
   invisible). Confirm and quantify.
9. **Autoscaling, global tables, storage, on-demand:** Application Auto Scaling
   target tracking means provisioned RCU/WCU is a ceiling, not steady cost —
   confirm whether autoscaled tables are mis-rightsized. Global Tables replicated
   writes (rWCU) and per-region replicas are not modeled (the
   `global_tables_optimization` bucket is defined but never populated). Storage
   ($/GB-mo) and the data-lifecycle saving (0.60 factor) are emitted as $0 —
   confirm intentional. Confirm full pagination of `list_tables` and that the
   `TableStatus != "ACTIVE"` skip in enhanced checks doesn't silently drop
   `UPDATING`/`CREATING` tables that still bill. Confirm `ItemCount==0` (a value
   updated only ~every 6h) isn't flagging an actively-written table as unused.

### Phase 5 — Silent failures (nothing fails quietly)
10. Find every `except`, `ctx.warn`-only, and `0.0` fallback. Both shim functions
    wrap per-table work in `try/except → ctx.warn` and the CloudWatch reads in a
    bare `except Exception: → recommendation_text` fallback (no `ctx`
    classification). Confirm a CloudWatch `AccessDenied`/throttle is routed to
    `ctx.permission_issue`/`ctx.warn` (action `cloudwatch:GetMetricStatistics`),
    not swallowed into a "validate with CloudWatch metrics" string. The on-demand
    `EstimatedMonthlyCost` falls back to `0.0` with a `PricingWarning` — confirm a
    $0 on-demand cost never produces a counted card.
11. Confirm `requires_cloudwatch=True` but **`reads_fast_mode` is not declared** —
    the CloudWatch reads in both shim functions are not gated on `ctx.fast_mode`,
    so a fast scan still pays for `get_metric_statistics`. Mirror the Lambda/RDS
    fast-mode fix.
12. The "DynamoDB Monitoring Required" / "DynamoDB CloudWatch Required" recs are
    pure **"enable monitoring" $0 nudges** — confirm they land as advisory
    (`Counted=False` via `mark_zero_savings_advisory` or equivalent), rendered but
    excluded from the count, not inflating `total_recommendations` as counted
    findings.

### Phase 6 — Reporting (one tab, counted == rendered)
13. DynamoDB is in `_PHASE_B_SKIP_PER_REC` with three registered handlers
    (`enhanced_checks`, `dynamodb_table_analysis`, `cost_optimization_hub`).
    Confirm every emitted source has a handler (no silent unrendered source) and
    that the renderer surfaces each rec's `EstimatedSavings` string. Note the
    desync risk: the shim emits human strings like `"53-76% vs On-Demand"`,
    `"Variable based on actual usage"`, `"100% of table costs"` while the **counted**
    number is the factor×cost computed in the adapter — confirm the card's
    displayed savings string and the parsed counted number do not contradict each
    other (a card reading "Variable" while the tab total counts a concrete 0.40×
    figure is a desync).
14. **Counted == rendered:** `total_monthly_savings` sums the per-rec factor math;
    `total_recommendations = len(opt_opps)+len(enhanced_recs)+len(coh_kept)`.
    Verify the per-tab total equals the sum of COUNTED rendered findings (advisory
    $0 recs rendered but not summed), and reconcile the executive-summary headline
    + `_calculate_service_savings` against the per-service total. Confirm no
    finding is counted but dropped from the table (or vice-versa).

### Phase 7 — Tooling & evidence
15. Run a real scan scoped to DynamoDB: `python3 cli.py <region> --scan-only
    dynamodb`, then `python3 tools/scan_doctor.py <json> --service dynamodb`.
    Triage every silent failure, `$0`/missing-savings finding (separate genuine
    advisory from leakage), and table appearing in >1 source. Reconcile the
    headline against the per-source sum. Caveats: exercise a provisioned table
    with RCU≥100 AND WCU≥100 (the over-provisioned + reserved stack), a table with
    large GSIs (coverage gap), an on-demand table with and without CloudWatch
    data, and an empty table. Use `.venv/bin/python` (3.14) — system `python3`
    lacks `datetime.UTC`.
16. For any duplication claim, prove it (same TableName in two sources, or
    over-provisioned + reserved on one table). For any accuracy claim, show the
    AWS Pricing API value (RCU/WCU-hr, per-million request unit) next to the
    scanner's constant.

### Deliverable
- The complete check list (Phase 1.2), with counted-vs-$0 marked and the source
  block named.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with an ID'd fix plan (C1/H1/M1…) so a subset can be approved.

### Implementation (only after I approve)
- Add `tests/test_dynamodb_audit_fixes.py` mirroring
  `tests/test_rds_audit_fixes.py` / `tests/test_lambda_audit_fixes.py`: unit-test
  the pure helpers directly (`_enhanced_savings_factor` category mapping,
  `parse_dollar_savings` boundaries, the over-provisioned/reserved dedup, the CoH
  ARN→table parse) and drive `DynamoDbModule.scan` with a `SimpleNamespace` ctx +
  monkeypatched shim functions + fake boto3/CloudWatch clients/paginators. Cover
  every fix: GSI coverage, reserved-capacity demotion, over-provisioned vs
  reserved stack, fast-mode skip, $0-nudge advisory gating, region scaling,
  counted == rendered.
- For any factor that assumes a usage target with no usage evidence, replace it
  with a CloudWatch-backed delta (target capacity at observed utilization) or keep
  it a $0 advisory — never fabricate a `$`.
- Record a structured **AuditBasis** (rate / region / metric-window / formula) on
  each counted finding.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the DynamoDB golden fixture first; refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) only when a rendering change is intentional, and say so.
- If you find the same bug class in `rds`/`redshift`/`elasticache`, note it as a
  follow-up (don't fix unprompted). Update the `dynamodb.py` row in
  `services/adapters/CLAUDE.md`. Stage ONLY the files you changed.

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

**DynamoDB-specific items discovered in the code (verify each):**
- **Config-dimension savings with no usage evidence (HIGH):** `dynamodb_table_analysis`
  applies the `default` 0.30 factor to **every** provisioned table's full RCU/WCU
  cost, and "DynamoDB Over-Provisioned Capacity" applies 0.40 to any table with
  RCU>100 or WCU>100 — both with **no required utilization signal** (the
  over-provisioned rec is emitted even when its own CloudWatch text says
  "Utilization acceptable"). These are fabricated $ from a capacity dimension.
  Gate on measured low utilization and compute the exact `current − target` delta.
- **Over-provisioned + Reserved double-count on one table (HIGH/CRITICAL):** a
  table with RCU≥100 AND WCU≥100 fires both "DynamoDB Over-Provisioned Capacity"
  (0.40) and "DynamoDB Reserved Capacity" (0.66); the adapter sums both → up to
  1.06× the table's cost "saved". They are alternative remediations — keep one.
- **Reserved capacity counted, not advisory (HIGH):** reserved capacity is a
  commitment lever (RDS demotes the equivalent to `ADVISORY_CATEGORIES`; CoH routes
  `*ReservedInstances`/`*SavingsPlans` to `commitment_analysis`). It is currently
  `Counted=True` here and overlaps the rightsizing lever — demote to advisory.
- **GSI throughput invisible (HIGH):** only table-level `ProvisionedThroughput` is
  read; `GlobalSecondaryIndexes[].ProvisionedThroughput` (separately billed) is
  never summed — base cost and over-provisioning on GSI-heavy tables are
  under-counted.
- **On-demand cost from `Average` consumed-capacity (MEDIUM):** the on-demand
  `EstimatedMonthlyCost` multiplies the CloudWatch `Average ConsumedReadCapacityUnits`
  by a per-**request**-unit rate and ×730 — a unit/semantics mismatch (capacity
  units vs request units; hourly average vs monthly volume). Validate or replace
  with the per-request `Sum` metric.
- **`reads_fast_mode` not declared (MEDIUM):** both shim functions call
  `cloudwatch.get_metric_statistics` but the adapter does not gate on
  `ctx.fast_mode`, so fast scans still pay for CloudWatch.
- **"Enable monitoring" $0 nudges (LOW):** "DynamoDB Monitoring Required" /
  "DynamoDB CloudWatch Required" / "DynamoDB Billing Mode - Metric-Backed" on
  on-demand tables carry no rcu/wcu → $0; confirm they are explicitly advisory
  (`Counted=False`), not counted placeholders inflating the rec count.
- **Empty `global_tables_optimization` / storage coverage (LOW):** the
  `global_tables_optimization` bucket is declared but never populated, and
  per-region replica writes + storage $/GB are unmodeled — document as intentional
  gaps or fill them.

## PROMPT (end)
