# Bedrock Adapter Cost-Audit Prompt

A deep, Bedrock-specific audit brief in the same structure as the Lambda /
RDS / EC2 / Network audits. Paste the **PROMPT** section into a fresh session.

It is pre-grounded in the *actual* Bedrock code path so the auditor starts from
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
> Service-specific live-audit findings for `bedrock`:
> - Thread `ctx` + `record_aws_error` into the Provisioned-Throughput / knowledge-base / agent ENUMERATION paths (bare `except: pass` hid AccessDenied as 'no resources'); record only the genuine double-failure, not the paginator-unavailable fallback.
> - `_check_idle_knowledge_bases` emits `monthly_savings: 0.0` recs without `Counted: False`; unlike the idle-PT advisory path these inflate `total_recommendations` and evade the B1 advisory-hygiene sweep — confirm each KB rec carries `Counted: False` so D4 holds (advisory recs must not inflate the count).

You are auditing the **`bedrock`** adapter of this AWS cost-optimization scanner.
Scope is strictly cost: every emitted recommendation must produce a concrete,
account-specific dollar saving. Work read-only first (understand + validate),
then propose fixes grouped by severity, and only implement after I confirm.

Use the AWS Pricing MCP and AWS Knowledge MCP to validate real prices, model-unit
rates, token rates, metrics, and billing codes — never trust hardcoded rates or
memory. **Use `get_bedrock_patterns()` from the Pricing MCP** (mandatory for
Bedrock cost work) and the codebase/search tools to trace actual code paths.
Treat the recently-audited **Lambda** adapter
(`services/adapters/lambda_svc.py`) as the worked example for **CloudWatch-gated /
metric-gated `$0` advisory** findings (`mark_zero_savings_advisory`,
`Counted=False`), the **arch/model-aware module constant**, `requires_cloudwatch`
/ `reads_fast_mode` handling, the structured **AuditBasis**, and the test style I
expect; treat **`services/adapters/file_systems.py`** as the model for
**evidence-gating a saving on a real utilization metric** before counting it; and
treat the **EC2** adapter (`services/adapters/ec2.py`) as the canonical model for
the `$0`-placeholder→advisory discipline and cross-source dedup.

### NOTE on structure (Bedrock is NOT shaped like EC2/RDS/Lambda)
- The adapter is a **single self-contained file** `services/adapters/bedrock.py`
  (~409 lines, `BedrockModule`). There is **NO** `services/bedrock.py` legacy
  shim and **NO** `services/bedrock_logic.py` — all detection, pricing, and
  CloudWatch logic lives inline as module-level helpers (`_list_provisioned_
  throughputs`, `_get_pt_invocation_sum`, `_get_pt_token_counts`, `_check_idle_pt`,
  `_check_pt_breakeven`, `_check_idle_knowledge_bases`, `_check_idle_agents`).
- It emits **four SourceBlocks**: `idle_provisioned_throughput`,
  `pt_breakeven_analysis`, `idle_knowledge_bases`, `idle_agents`. None is
  registered in `PHASE_B_HANDLERS`; `bedrock` is NOT in `_PHASE_A_SERVICES` and
  NOT in `_PHASE_B_SKIP_PER_REC` — so `should_fallback_to_per_rec("bedrock")` is
  **True** and every source renders through the generic per-rec fallback
  `render_generic_per_rec("bedrock", …)` → `_render_generic_other_rec` in
  `reporter_phase_b.py`. There **IS** a `"bedrock"` descriptor in
  `html_report_generator.py` (~line 156) supplying three stat cards
  (`extras.pt_count` / `kb_count` / `agent_count`); the adapter also declares
  `stat_cards` + a `GroupingSpec(by="check_category")`. Remember this for Phase 6.
- Bedrock pricing is **module constants**, not a PricingEngine method:
  `PT_HOURLY_PRICE` (a dict keyed by model id, lines 18-24), `PT_HOURLY_DEFAULT =
  1.0` (line 25) for unknown models, `KB_OCU_HOURLY = 0.20` (line 26, currently
  unused), `HOURS_PER_MONTH = 730` (line 27), and a flat **`0.000_003` $/token**
  on-demand estimate hardcoded inside `_check_pt_breakeven` (line 193). All are
  multiplied by `ctx.pricing_multiplier` on the counted path (lines 149, 198).
- Bedrock consumes **neither Cost Optimization Hub nor Compute Optimizer**. It is
  NOT in `core/scan_orchestrator.py`'s `_HUB_SERVICES` set or `type_map`, and
  there is no `BedrockInstance`/`BedrockProvisionedThroughput` CoH resource type.
  So a "missing CoH/CO source" finding is **NOT fair game** here. It **does** read
  CloudWatch (`AWS/Bedrock` `Invocations`, `InputTokenCount`, `OutputTokenCount`)
  and correctly declares `requires_cloudwatch = True` / `reads_fast_mode = True`,
  and the two PT checks early-return when `fast_mode` (lines 134-135, 176-177).
  Focus on **PT-rate accuracy, the model-id key-match, the blended token rate, the
  CloudWatch dimension, the $0 advisory KB/agent paths, and render wiring**.

### Phase 0 — Orient (5-minute map before judging)
0a. Open `services/adapters/CLAUDE.md`. Bedrock has **no row** in the Live-Pricing
    table (it is a module-constant adapter not yet documented there) — note that
    gap and reconcile it: the adapter is module-constant priced
    (`PT_HOURLY_PRICE` + flat token rate), consumes no CoH/CO, reads CloudWatch.
    Add a correct row in Implementation.
0b. Confirm module identity in `services/adapters/bedrock.py`: `key="bedrock"`,
    `cli_aliases=("bedrock",)`, `display_name="Bedrock"`, `required_clients()` =
    `("bedrock", "bedrock-agent", "cloudwatch")`, `requires_cloudwatch=True`,
    `reads_fast_mode=True`, `stat_cards` (3) and
    `grouping=GroupingSpec(by="check_category")`. Note `scan` bails to
    `_empty_findings()` when the `bedrock` client is absent.
0c. Bedrock has **no AWS advisory source** (no CoH / CO). Drop that axis. The cost
    signal is: list Provisioned Throughputs → for each, (1) if 0 invocations in 30
    days → idle-delete waste, (2) breakeven PT-commitment vs estimated on-demand;
    plus list Knowledge Bases (emit $0 advisory) and Agents (emit nothing). The
    money is in **Provisioned Throughput model-unit hours** and **per-token
    on-demand inference** — verify the adapter prices both correctly.

### Phase 1 — Understand the code (read before judging)
1. Read the full file `services/adapters/bedrock.py` and: `core/contracts.py`
   (`ServiceFindings`, `SourceBlock`, `StatCardSpec`, `GroupingSpec`);
   `services/_base.py` (`BaseServiceModule`); `core/scan_context.py`
   (`ctx.client`, `ctx.pricing_multiplier`, `ctx.fast_mode`, `ctx.warn`);
   `core/result_builder.py`; and the reporter path `reporter_phase_b.py`
   (`render_generic_per_rec`, `_render_generic_other_rec`,
   `should_fallback_to_per_rec`) + `html_report_generator.py` (the `"bedrock"`
   descriptor ~line 156 and `_get_detailed_recommendations` ~lines 3300-3362).
2. Enumerate **every** cost check across the four sources, and for each give:
   trigger, data source (Bedrock describe-API field vs CloudWatch metric vs pure
   constant), the savings formula + constant, the savings key (these recs use
   snake_case **`monthly_savings`**, NOT `EstimatedSavings`/`estimatedMonthlySavings`),
   and whether it is **counted** or **$0 advisory**. Map each to its SourceBlock.
   The known inventory to confirm:
   - **`idle_provisioned_throughput`** (`_check_idle_pt`, lines 127-166):
     trigger = `invocations == 0` over 30 days (`_get_pt_invocation_sum`). Formula
     = `hourly * model_units * 730 * pricing_multiplier`, where `hourly =
     PT_HOURLY_PRICE.get(model_id, PT_HOURLY_DEFAULT=1.0)`. Counted (when
     `> $1.0`). Skipped entirely in `fast_mode`.
   - **`pt_breakeven_analysis`** (`_check_pt_breakeven`, lines 169-214): trigger =
     PT monthly cost > estimated on-demand. `pt_monthly = hourly * model_units *
     730`; `od_monthly_estimate = (input_tokens + output_tokens) * 0.000_003`
     (flat, model-agnostic, single rate for BOTH input and output). Saving =
     `(pt_monthly − od_monthly_estimate) * pricing_multiplier`. Counted (when
     `> $1.0`). Skipped in `fast_mode`.
   - **`idle_knowledge_bases`** (`_check_idle_knowledge_bases`, lines 217-269):
     lists KBs via `bedrock-agent`; emits `monthly_savings = 0.0` +
     `pricing_warning` for EVERY KB (deliberate **$0 advisory** — the comment
     explains the old fictitious $146/mo was removed). Confirm it stays $0.
   - **`idle_agents`** (`_check_idle_agents`, lines 272-300): lists agents but
     **emits nothing** — the comment (lines 295-298) states agents accrue no AWS
     charge (MCP-confirmed), so the old $5/mo placeholder was removed. Confirm the
     source is always empty and `agent_count` is informational only.

### Phase 2 — Accuracy of every number (validate with MCP)
3. **Provisioned-Throughput hourly rate (`PT_HOURLY_PRICE`).** Validate every
   constant against the live Pricing API (`AmazonBedrock`, usagetype pattern
   `…-ProvisionedThroughput-{NoCommit|1month|6months}-ModelUnits`, unit = hour).
   Confirm these grounded facts and decide the fix:
   - **The dict keys cannot match real model ids.** A live
     `list_provisioned_model_throughputs` entry's `modelId` is the **full**
     identifier/ARN (e.g. `anthropic.claude-3-haiku-20240307-v1:0`), but
     `PT_HOURLY_PRICE` keys are truncated (`anthropic.claude-3-haiku`,
     `anthropic.claude-3-sonnet`, `anthropic.claude-3-5-sonnet`,
     `anthropic.claude-3-opus`, `amazon.titan-text-lite`). So
     `PT_HOURLY_PRICE.get(model_id, 1.0)` will **almost always miss** and fall to
     `PT_HOURLY_DEFAULT = 1.0` — a fabricated $1/unit/hr. This silently
     mis-prices BOTH the idle-PT waste and the breakeven `pt_monthly`.
   - **`PT_HOURLY_DEFAULT = 1.0` is wildly low.** The Pricing API shows real
     NoCommit PT model-unit rates an order of magnitude higher — e.g.
     `USW2-Llama3-1-70B-ProvisionedThroughput-NoCommit-ModelUnits` = **$24.00/hr**.
     A $1/hr default under-states idle-PT waste by ~20-24×.
   - **Claude has no published PT model-unit SKU.** In the Pricing API, Anthropic
     Claude appears only as `…-input-tokens` / `…-output-tokens` (on-demand) — there
     is **no** `Claude…-ProvisionedThroughput-…-ModelUnits` usagetype. Only Nova,
     Titan, and Llama expose PT ModelUnits. So the four Claude entries in
     `PT_HOURLY_PRICE` cannot be validated against published PT pricing and likely
     reflect stale/guessed numbers. Confirm whether Claude PT is even orderable in
     the account; if a Claude PT exists, source its rate from the account's actual
     commitment, not a hardcoded guess.
   - **Commitment term is ignored.** PT has NoCommit / 1-month / 6-month rates
     (distinct SKUs). The adapter prices a single hourly with no term — confirm
     which the constants purport to be and whether the `provisionedModelSummaries`
     entry exposes the commitment to pin the right SKU.
   - Region scaling: the constants are multiplied by `ctx.pricing_multiplier`
     (lines 149, 198) — confirm that is the right single-multiply pattern for a
     us-east-1-anchored constant and not double-applied.
4. **Blended on-demand token rate (`0.000_003` $/token).** This single flat rate
   is applied to `input_tokens + output_tokens` for **every** model. Validate
   against the API: Claude 3 Sonnet on-demand is **$0.003/1K input** (=
   $0.000003/token) but **$0.015/1K output** (= $0.000015/token, 5×); Claude 3
   Haiku is **$0.00025/1K input** (= $0.00000025/token, ~12× *lower* than the
   constant) and ~$0.00125/1K output. So `0.000_003`:
   - conflates input and output at one rate (output is 5× input for Sonnet);
   - is model-agnostic (12× too high for Haiku input, right only for Sonnet input);
   - therefore the breakeven `od_monthly_estimate` is wrong in both directions
     depending on the model and the input/output mix. Re-derive per-model,
     per-direction from `…-input-tokens` / `…-output-tokens` SKUs and split the CW
     `InputTokenCount` / `OutputTokenCount` (the adapter already fetches both in
     `_get_pt_token_counts`) so each is priced at its own rate.
5. **CloudWatch dimension correctness.** `_get_pt_invocation_sum` /
   `_get_pt_token_counts` query `Dimensions=[{"Name":"ModelId","Value":model_id}]`
   using the base model id. For a Provisioned Throughput, AWS/Bedrock metrics are
   emitted under the model/inference identifier — confirm whether passing the base
   `modelId` (a) aggregates ALL invocations of that base model including on-demand
   traffic (so a busy on-demand model masks an idle PT → false negative on
   idle-delete), or (b) returns nothing for the PT (false "idle"). This is the
   Bedrock analogue of the universal "agent-metric dimension mismatch." Confirm
   the correct dimension for PT-scoped usage (e.g. the provisioned model ARN) via
   AWS Knowledge MCP, and confirm `Period = 86400` is valid for the 30-day window
   (the code comment claims larger Periods silently fail — verify).
6. Record a structured **AuditBasis** (rate / region / commitment-term /
   metric-window / formula) on EACH counted PT finding so the number is defensible
   from the report alone, as the Lambda/RDS audits did. The KB advisory already
   carries a `pricing_warning` — confirm it states exactly what metric is missing.

### Phase 3 — Duplication (no dollar counted twice)
7. **Intra-adapter stacking.** A single PT flows through BOTH `_check_idle_pt` and
   `_check_pt_breakeven`. Confirm they are mutually exclusive: idle fires only on
   `invocations == 0`; breakeven returns early when `input_tokens`/`output_tokens`
   are `None` or `od_monthly_estimate <= 0`. A 0-invocation PT should yield 0
   tokens → breakeven returns → no double count. **But** verify the edge where
   `Invocations` returns no datapoints (→ `None`, idle check returns) yet token
   metrics return stale `Sum > 0`, or vice-versa — a PT must never appear in both
   `idle_provisioned_throughput` and `pt_breakeven_analysis` for the same waste.
8. **Cross-adapter.** No other adapter prices Bedrock; KBs that use OpenSearch
   Serverless as a vector store accrue **OCU** cost that belongs to OpenSearch /
   the vector store, NOT to Bedrock — confirm the adapter does not (re)price KB OCU
   here (it correctly emits $0 advisory; `KB_OCU_HOURLY = 0.20` is defined but
   unused — confirm it stays unused so KB cost is not double-counted against an
   OpenSearch Serverless tab). Confirm no `_extract_*` helper pulls Bedrock
   resources into a synthetic tab.

### Phase 4 — Coverage (works for ALL resources, not a subset)
9. Pagination: `_list_provisioned_throughputs`, `list_knowledge_bases`, and
   `list_agents` all paginate with a non-paginator fallback — confirm full
   coverage and that the fallback `except Exception: pass` does not hide a
   permission error (Phase 5). Confirm the PT list is not filtered to a hardcoded
   model/status allowlist (it iterates all `provisionedModelSummaries`).
10. Whole-class gaps to weigh: **custom-model-import** copies (charged per
    model-copy/hour), **Marketplace** model endpoints, **model-customization**
    storage, and on-demand spend on **idle/duplicate** models are not covered.
    More importantly, the **30-day zero-invocation** gate misses a PT that is
    *under-utilized* but non-zero (1 invocation in 30 days still bills the full
    model-unit hours) — only `pt_breakeven_analysis` partially catches that, and
    only when token metrics exist. Confirm whether a near-idle PT escapes both
    checks. Also confirm a `fast_mode` scan deliberately skips BOTH PT checks
    (it does) and that this is the intended cost/coverage tradeoff.

### Phase 5 — Silent failures (nothing fails quietly)
11. Find every `except: pass`, bare `except`, `logger`-only, and `return []`
    fallback:
    - `_list_provisioned_throughputs` (lines 50-55): paginator failure →
      non-paginator call → on second failure `except Exception: pass` returns an
      empty list. A `bedrock:ListProvisionedModelThroughputs` AccessDenied makes
      the whole PT analysis **vanish silently** — classify
      AccessDenied/Unauthorized → `ctx.permission_issue`, other → `ctx.warn`.
    - `_get_pt_invocation_sum` / `_get_pt_token_counts` (lines 80-81, 107-108,
      122-123): CloudWatch `get_metric_statistics` failure → `except: pass` →
      `None`. A CW throttle or `cloudwatch:GetMetricStatistics` denial silently
      makes a PT look "no data" (idle check returns, breakeven returns) — record
      it on `ctx` and distinguish "no metrics" (legitimately idle) from "couldn't
      read metrics" (permission/throttle).
    - `_check_idle_knowledge_bases` / `_check_idle_agents` (lines 230-233,
      243-248, 275-278, 288-293): `ctx.client("bedrock-agent")` and the list calls
      swallow every exception and `return recs` — a `bedrock-agent` permission gap
      drops the KB advisory with no warning. Record it.
    - The per-PT loop in `scan` (lines 347-352) wraps both checks in
      `except Exception: continue` — a single malformed PT silently disappears.
      Record it.
12. Does a pricing miss still emit a counted finding? When `model_id` is unknown
    (the common case, per Phase 2), `hourly` falls to `1.0` and the idle/breakeven
    rec is **still counted** at a fabricated rate. A finding whose model-unit rate
    cannot be confirmed should be advisory (`Counted=False`) or resolved from the
    real PT commitment, not silently counted at $1/hr. Today these recs carry no
    `Counted` flag — counting is purely "monthly_savings > 1.0"; weigh whether the
    unknown-rate path should be demoted to advisory like the KB path.
13. `fast_mode` gating: confirm BOTH PT checks early-return under `ctx.fast_mode`
    (lines 134-135, 176-177) and that `reads_fast_mode=True` is declared so the
    orchestrator knows CW is skipped. Confirm the KB/agent listing (which does NOT
    read CloudWatch) is acceptable to run in fast_mode, or gate it too.

### Phase 6 — Reporting (four sources, counted == rendered)
14. **Source-name vs handler (verify carefully):** the adapter emits four sources,
    none registered in `PHASE_B_HANDLERS`. Because `bedrock` is NOT in
    `_PHASE_B_SKIP_PER_REC`, `should_fallback_to_per_rec("bedrock")` is **True**,
    so each source renders via `render_generic_per_rec("bedrock", recs,
    source_name)` → `_render_generic_other_rec`. Trace
    `html_report_generator._get_detailed_recommendations` and confirm all four
    actually render (contrast network, which IS in `_PHASE_B_SKIP_PER_REC` and so
    renders nothing without a handler). Confirm a PT rec is visible.
15. **snake_case vs PascalCase render desync (Bedrock-specific):**
    `_render_generic_other_rec` resolves the card's resource id from PascalCase
    keys (`ProvisionedModelId`, `KnowledgeBaseId`, `AgentId`) and the category
    from `CheckCategory`, but the Bedrock recs use **snake_case**
    (`provisioned_model_id`, `knowledge_base_id`, `check_category`,
    `monthly_savings`, `current_value`, `recommended_value`, `reason`). So the
    header falls back to the `source_name` title and the id likely resolves to
    `"Resource"`, while every snake_case key is dumped as a `<p>` line. The
    **counted total is still correct** (the adapter sums `monthly_savings` itself,
    the reporter does not re-sum), so this is a presentation desync, not a money
    desync — but confirm `monthly_savings` is shown intelligibly and that the
    `pricing_warning`/`reason` make the $0 KB advisory legible. Decide whether to
    add a proper `("bedrock", <source>)` handler or normalize the rec keys.
16. **Counted == rendered:** `total_recommendations = len(all_recs)` includes the
    $0 KB advisories; `total_monthly_savings` sums `monthly_savings` across all
    recs (KB = 0.0, agents = none). Confirm the per-tab headline equals the sum of
    the counted PT findings, that the three stat cards
    (`extras.pt_count`/`kb_count`/`agent_count`) reconcile with the rendered
    counts, and that the executive-summary headline
    (`_get_executive_summary_content` + `_calculate_service_savings`) matches the
    per-service total. Confirm KB advisories are visible but contribute $0 and are
    not dropped from the table.

### Phase 7 — Tooling & evidence
17. Run a real scan scoped to Bedrock:
    `python3 cli.py <region> --scan-only bedrock`
    then pass the JSON through
    `python3 tools/scan_doctor.py <json> --service bedrock`.
    Triage every: silent failure, `$0`/missing-savings finding (separate the
    deliberate KB advisory from leakage), unknown-model `$1/hr` default, and any PT
    in both PT sources. Caveats: Bedrock PTs are rare/expensive — most accounts
    have none (exercise the empty-PT path and the KB-only path); CloudWatch
    `AWS/Bedrock` metrics are often empty (exercise the "no data" branch and prove
    it is not misread as idle); try `us-east-1` / `us-west-2` where most models
    live. Use `.venv/bin/python` (3.14) — system `python3` lacks `datetime.UTC`
    (and this adapter is datetime-heavy: `_get_pt_invocation_sum` uses
    `datetime.now(timezone.utc)`).
18. For the accuracy claims, show the AWS Pricing API value next to the scanner's
    constant: (a) a real PT NoCommit ModelUnits hourly (e.g. Llama 3.1 70B
    $24.00/hr) vs `PT_HOURLY_DEFAULT = 1.0`; (b) Claude 3 Sonnet input $0.003/1K
    and output $0.015/1K vs the flat `0.000_003`/token; (c) the absence of any
    Claude `…-ProvisionedThroughput-…-ModelUnits` usagetype. For the dimension
    claim, show what `get_metric_statistics` returns for a PT'd model under
    `ModelId` vs the provisioned-model ARN.

### Deliverable
- The complete check list (Phase 1.2), per source, with counted-vs-advisory
  marked and the always-empty `idle_agents` source called out.
- Findings grouped by severity (CRITICAL / HIGH / MEDIUM / LOW), each with: file +
  line, evidence (code excerpt and/or AWS Pricing API value), and the
  dollar/coverage impact. Separate **confirmed bugs** from **known limitations /
  tradeoffs**. End with a short, ID'd fix plan (C1/H1/M1…) so a subset can be
  approved.

### Implementation (only after I approve)
- Add a `tests/test_bedrock_audit_fixes.py` mirroring
  `tests/test_lambda_audit_fixes.py` / `tests/test_rds_audit_fixes.py`: drive
  `BedrockModule.scan` (and each pure helper directly) with a `SimpleNamespace`
  ctx + fake `bedrock` / `bedrock-agent` / `cloudwatch` clients (stubbed
  paginators + `get_metric_statistics`). Cover every fix: full-model-id rate
  match (no silent fall to `$1/hr`), real PT model-unit rate, per-direction
  per-model token pricing for breakeven, CloudWatch dimension correctness,
  silent-failure classification (ListProvisionedModelThroughputs / GetMetricStatistics
  / bedrock-agent AccessDenied → `permission_issue`), fast_mode skip, idle-vs-breakeven
  mutual exclusion, KB $0 advisory stays $0, counted == rendered, and render
  wiring (snake_case keys render intelligibly).
- Resolve PT rates from the real model id + commitment term (Pricing API or the
  PT summary), demote unknown-rate PTs to advisory instead of counting at $1/hr,
  and split breakeven into per-model input/output token pricing using the already-
  fetched `InputTokenCount` / `OutputTokenCount`.
- Record a structured **AuditBasis** on each counted PT finding. Never fabricate a
  `$` for a KB/agent that accrues no measurable AWS charge.
- Keep the regression gate green:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.
  Inspect the golden fixture for bedrock first. Refresh reporter snapshots
  (`SNAPSHOT_UPDATE=1`) ONLY when a rendering change is intentional, and say so.
- If you find the same silent-failure / constant-rate / dimension bug in a sibling
  adapter out of scope, note it as a follow-up (don't fix unprompted).
- Add a `bedrock.py` row to `services/adapters/CLAUDE.md` (module-constant PT
  pricing + flat token rate, no CoH/CO, CloudWatch-gated, four sources rendered
  via generic per-rec).
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

### Known issue catalogue — Bedrock-specific (found while grounding this prompt)
- **PT model-id keys can't match real model ids.** `PT_HOURLY_PRICE` is keyed by
  truncated ids (`anthropic.claude-3-haiku`) but a live PT `modelId` is the full
  identifier (`anthropic.claude-3-haiku-20240307-v1:0`), so the lookup nearly
  always misses and falls to `PT_HOURLY_DEFAULT`.
- **`PT_HOURLY_DEFAULT = 1.0` is fabricated and ~20-24× too low.** Real NoCommit PT
  model-unit rates are an order of magnitude higher (Llama 3.1 70B = $24.00/hr via
  `USW2-Llama3-1-70B-ProvisionedThroughput-NoCommit-ModelUnits`), so idle-PT waste
  is grossly under-counted whenever the rate defaults.
- **Claude PT rates are unverifiable / likely wrong.** The Pricing API exposes NO
  `Claude…-ProvisionedThroughput-…-ModelUnits` usagetype (only Nova/Titan/Llama
  have PT ModelUnits); Claude appears only as on-demand input/output tokens. The
  four Claude entries in `PT_HOURLY_PRICE` are guesses with no public SKU, and the
  commitment term (NoCommit/1-month/6-month) is ignored entirely.
- **Blended flat token rate (`0.000_003`/token).** Applied to input+output for all
  models, it equals only Claude 3 Sonnet's *input* rate; it is ~12× too high for
  Haiku input and ignores that output bills ~5× input ($0.015 vs $0.003 per 1K for
  Sonnet) — breakeven `od_monthly_estimate` is wrong in both directions. Price
  `InputTokenCount`/`OutputTokenCount` separately per model.
- **CloudWatch dimension mismatch.** Querying `AWS/Bedrock` by `ModelId = <base
  model>` for a Provisioned Throughput either aggregates unrelated on-demand
  traffic of the same base model (masking an idle PT → false negative) or returns
  nothing (false idle). Confirm the PT-scoped dimension (provisioned-model ARN).
- **Unknown-rate PT still counted.** When the model rate defaults to $1/hr the
  idle/breakeven rec is counted at a fabricated rate with no `Counted=False`
  demotion — unlike the KB path which correctly emits $0 advisory.
- **snake_case recs vs PascalCase renderer.** Bedrock recs use
  `provisioned_model_id` / `check_category` / `monthly_savings`, but
  `_render_generic_other_rec` looks for `ProvisionedModelId` / `CheckCategory`, so
  the card id/category degrade to generic fallbacks (money total stays correct,
  presentation desyncs).
- **`KB_OCU_HOURLY = 0.20` is defined but unused** (KBs emit $0 advisory) — correct
  today, but confirm it is never wired to count KB OCU here, since OpenSearch
  Serverless OCU cost belongs to the vector store, not Bedrock.

## PROMPT (end)
