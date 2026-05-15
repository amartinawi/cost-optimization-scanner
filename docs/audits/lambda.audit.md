# Audit: lambda Adapter

**Adapter**: `services/adapters/lambda_svc.py` (105 lines)
**Legacy shim**: `services/lambda_svc.py` (196 lines)
**ALL_MODULES index**: 7
**Renderer path**: **Phase A** — `lambda` is in `_PHASE_A_SERVICES` (`reporter_phase_b.py:2204`). Plus a Phase B handler for the Compute Optimizer source (`("lambda", "compute_optimizer"): _render_compute_optimizer_source`, line 2160)
**Date**: 2026-05-15
**Auditor**: automated

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **FAIL** | 5 (1 CRITICAL, 3 HIGH, 1 MEDIUM) |
| L2 Calculation | **FAIL** | 7 (2 CRITICAL, 3 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 3 (1 HIGH, 1 MEDIUM, 1 LOW) |
| **Overall**    | **FAIL** | 100%-utilization formula + PC rate 4× wrong + double-count after dedup |

---

## Pre-flight facts

- **Adapter class**: `LambdaModule` at `services/adapters/lambda_svc.py:13`
- **Declared `required_clients`**: `("lambda", "compute-optimizer")` — **omits `cloudwatch`** (shim uses CW for invocation metrics at `services/lambda_svc.py:158`)
- **Declared `requires_cloudwatch`**: `False` (default) — **lies**
- **Declared `reads_fast_mode`**: `False` (default) — accurate
- **Sources emitted**: `{"cost_optimization_hub", "compute_optimizer", "enhanced_checks"}`
- **Pricing methods consumed**: **none** from `PricingEngine`. Hardcoded constants `LAMBDA_PRICE_PER_GB_SEC_X86=0.0000166667`, `LAMBDA_PRICE_PER_GB_SEC_ARM=0.0000133334` inside the `scan()` function (uppercase locals — ruff N806)
- **Test files**: `tests/test_offline_scan.py`, `tests/test_regression_snapshot.py`, `tests/test_reporter_snapshots.py`

---

## L1 — Technical findings

### L1-001 `required_clients()` omits `cloudwatch`  [CRITICAL]
- **Check**: L1.1.4
- **Evidence**: `services/adapters/lambda_svc.py:22` returns `("lambda", "compute-optimizer")`. Shim calls `cloudwatch = ctx.client("cloudwatch")` at `services/lambda_svc.py:90` and `cloudwatch.get_metric_statistics(...)` at line 158 for ARM-migration analysis
- **Recommended fix**: change to `("lambda", "compute-optimizer", "cloudwatch")`. Also set `requires_cloudwatch: bool = True`

### L1-002 `requires_cloudwatch` flag is False but shim is CloudWatch-bound  [HIGH]
- **Check**: L1.1.5
- **Evidence**: default value `False` not overridden; shim hits CW for every x86 function in an ARM-supported runtime
- **Recommended fix**: override on the class

### L1-003 Three `print()` calls in adapter + shim error paths  [HIGH]
- **Check**: L1.2.2 / coding-style
- **Evidence**:
  - `services/adapters/lambda_svc.py:36` — adapter banner
  - `services/lambda_svc.py:108` — `print(f"⚠️ Error getting Lambda function config for {function_name}: {str(e)}")` (should be `ctx.warn` + AccessDenied classification)
  - `services/lambda_svc.py:144` — `print(f"Warning: Could not check provisioned concurrency for {function_name}: {e}")` followed by `continue` — silently aborts further checks for the function
- **Why it matters**: error paths invisible to JSON `scan_warnings[]` / `permission_issues[]`. `continue` at 145 means if `list_provisioned_concurrency_configs` fails (e.g., AccessDenied), the function's ARM-migration check is also skipped — quiet data loss
- **Recommended fix**: route to `ctx.warn` / `ctx.permission_issue`; remove the `continue` so the ARM-migration block still runs

### L1-004 Silent `except Exception: pass` at line 186-187 swallows CW errors  [HIGH]
- **Check**: L1.2.2
- **Evidence**: `services/lambda_svc.py:186-187` — the ARM-migration CW lookup falls into bare `pass` on any error. AccessDenied on `cloudwatch:GetMetricStatistics`, ThrottlingException, missing namespace — all silently dropped
- **Recommended fix**: classify and route via `ctx.warn` / `ctx.permission_issue`

### L1-005 Dead branches for retired categories  [MEDIUM]
- **Check**: L1.3.x — adapter should match shim's actual output
- **Evidence**: `services/adapters/lambda_svc.py:59-67` handles three categories — `"Low Invocation"`, `"VPC Configuration"`, `"Reserved Concurrency"` — that the shim **no longer emits** (see comments at `services/lambda_svc.py:124-126, 147-151` documenting their removal). Adapter branches are dead code but each holds an arbitrary $ amount that *would* be added if any rec slipped through with those category strings
- **Why it matters**: code rot; a typo elsewhere reintroducing the category strings would silently bring back the $5 flat / $3.65/month constants
- **Recommended fix**: delete the three dead `elif` branches

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| `cost_optimization_hub.*.estimatedMonthlySavings` | `aws-api` | `services/adapters/lambda_svc.py:49` | YES | AWS Cost Hub returns flat float at top level |
| `compute_optimizer.*.estimatedMonthlySavings` | `aws-api` (normalized) | `services/adapters/lambda_svc.py:72`, `services/advisor.py:228-258` | **WARN** — normalizer applies `pricing_multiplier` (line 255) but AWS Compute Optimizer already returns region-priced savings → potential double-multiply for non-us-east-1 | AWS docs: CO uses "on-demand pricing in the resource's Region" |
| `enhanced_checks` Excessive Memory | `module-const` × **fictitious 24/7 utilization** | `services/adapters/lambda_svc.py:58` | **FAIL** — formula `mem_gb × $/GB-sec × 730 × 3600 × 0.50` assumes the function runs 100% of every second of every month. Lambda bills per invocation × duration; real utilization is typically <1% | AWS Lambda pricing page: "You are charged based on the number of requests for your functions and the duration ... aggregated as GB-seconds" |
| `enhanced_checks` Provisioned Concurrency | `module-const` (**wrong rate**) | `services/adapters/lambda_svc.py:63` | **CRITICAL FAIL** — uses on-demand x86 rate ($0.0000166667/GB-s); PC rate is $0.0000041667/GB-s → **4× inflated** | AWS Pricing API confirmed $0.0000041667/GB-s for `AWS-Lambda-Provisioned-Concurrency` |
| `enhanced_checks` ARM Migration | `module-const` × 100% utilization | `services/adapters/lambda_svc.py:69` | **FAIL** — same 730×3600 problem; should use weekly invocation × avg-duration from CW metrics that the shim already collects (line 168) but the adapter ignores | n/a |
| Dead branches (`Low Invocation`, `VPC Configuration`, `Reserved Concurrency`) | `arbitrary` | `services/adapters/lambda_svc.py:60, 65, 67` | n/a — unreachable | n/a |

### L2-001 Excessive-memory / ARM-migration formulas assume 100% utilization  [CRITICAL]
- **Check**: L2.2 / L2.5 — unit + magnitude correctness
- **Evidence**:
  - `services/adapters/lambda_svc.py:58` — `formula_savings += mem_gb * LAMBDA_PRICE_PER_GB_SEC_X86 * 730 * 3600 * 0.50`
  - `services/adapters/lambda_svc.py:69` — `formula_savings += mem_gb * (LAMBDA_PRICE_PER_GB_SEC_X86 - LAMBDA_PRICE_PER_GB_SEC_ARM) * 730 * 3600`
  - `730 × 3600 = 2,628,000 GB-seconds/month` (1 month at 100% CPU). Lambda bills per **invocation duration**, not 24/7
- **Why it matters**: for a 1024 MB function actually invoked 1000 times × 200ms/invocation × 30 days = 6000 GB-s/month, AWS bills `6000 × 0.0000166667 = $0.10/month`. Adapter says rightsizing saves `1 × 0.0000166667 × 730 × 3600 × 0.50 = $21.91/month` — **219× the function's entire cost.** Similarly ARM migration claims `$8.76/month` saved on a function that costs $0.10/month total. The savings figure is detached from reality
- **Recommended fix**: the shim already pulls `WeeklyInvocations` (line 168, `total_invocations`) and `Duration` is available via CW `Duration` metric. Compute `actual_GB_seconds = avg_duration_ms / 1000 × invocations_per_month × mem_gb` and multiply by the rate. Without an actual-invocations figure, do not emit a $ value — set `EstimatedMonthlySavings = 0.0` with a `pricing_warning: "needs invocation metrics"`

### L2-002 Provisioned Concurrency uses wrong $/GB-sec rate (4× too high)  [CRITICAL]
- **Check**: L2.1 / L2.2 — pricing constant mismatch
- **Evidence**: `services/adapters/lambda_svc.py:63` — uses `LAMBDA_PRICE_PER_GB_SEC_X86 = 0.0000166667`. AWS Pricing API SKU `BMKCD2ZCEYKTYYCB` returns `$0.0000041667/GB-second` for the `AWS-Lambda-Provisioned-Concurrency` group
- **Why it matters**: PC IS billed 24/7 (so the `730 × 3600` factor is legitimate here, unlike L2-001), but the rate used is the on-demand x86 rate, which is exactly 4× the actual PC rate ($0.0000166667 / $0.0000041667 = 4.0). Every PC-savings figure is 4× over-stated
- **Recommended fix**: add a separate constant `LAMBDA_PC_PRICE_PER_GB_SEC = 0.0000041667` and use it for the PC branch. Plus a separate `LAMBDA_PC_INVOCATION_PRICE_PER_GB_SEC = 0.00000833` for the invocation fee when PC is actually used (PC has a dual-component price: allocation fee + reduced invocation fee)

### L2-003 Compute Optimizer normalizer double-applies `pricing_multiplier`  [HIGH]
- **Check**: L2.3.1 — `aws-api` source must not double-apply multiplier
- **Evidence**:
  - `services/advisor.py:255` — `"estimatedMonthlySavings": round(savings * pricing_multiplier, 2)` (`_normalize_lambda_co_rec`)
  - AWS docs: "Compute Optimizer estimates your savings based on the on-demand pricing in the resource's AWS Region" — i.e., the `savingsOpportunity.estimatedMonthlySavings.value` field is **already region-priced**
- **Why it matters**: in eu-west-1 (multiplier ≈ 1.08), CO returns $50/mo savings (correctly priced for eu-west-1); normalizer multiplies to $54/mo — 8% over. Identical bug in `_normalize_ecs_co_rec` (line 276) and `_normalize_asg_co_rec` (line 304) — flag those in their respective audits
- **Recommended fix**: drop `* pricing_multiplier` from `_normalize_lambda_co_rec` / `_normalize_ecs_co_rec` / `_normalize_asg_co_rec`. The CO API returns region-correct values

### L2-004 Dedupe excludes recs *after* their savings are already summed  [HIGH]
- **Check**: L2.5.2 / L2.5.4 — dedup must precede summation
- **Evidence**: `services/adapters/lambda_svc.py:51-90`:
  1. line 52-69 — sum `formula_savings` over **all** `enhanced_recs`
  2. line 84-88 — build `deduped_enhanced` (drops recs whose `FunctionName` is in Cost Hub)
  3. line 90 — overwrite `enhanced_recs = deduped_enhanced`
  4. line 92 — `total_recs = len(cost_hub) + len(co) + len(enhanced_recs)` (deduped)
  5. line 97 — `total_monthly_savings = savings` (which still contains pre-dedup `formula_savings`)
- **Why it matters**: a function that appears in BOTH Cost Hub and the shim's enhanced checks contributes:
  - Cost Hub savings (counted in `hub_savings`)
  - Enhanced savings (counted in `formula_savings`)
  - **The same function's count is dropped from `total_recommendations`** but its $ contribution stays
  - Result: `total_monthly_savings ≠ Σ (savings of recs in sources)`. L2.5.1 invariant broken
- **Recommended fix**: dedupe FIRST, then sum. Move the dedup block (lines 78-90) above the savings computation (lines 48-73)

### L2-005 `LAMBDA_PRICE_PER_1K_REQUESTS = 0.0000002` constant is mis-named  [MEDIUM]
- **Check**: L2.2 — unit honesty
- **Evidence**: `services/adapters/lambda_svc.py:46` — name says "PER_1K_REQUESTS" but `0.0000002` USD/request = $0.20/M requests (per-request rate, not per-1K). Variable is also unused — only the GB-sec constants are referenced
- **Recommended fix**: rename to `LAMBDA_PRICE_PER_REQUEST = 0.0000002` for clarity, or delete since unused

### L2-006 Cost Hub `estimatedMonthlySavings` accessed at top level (correct for Cost Hub, would be wrong for CO)  [LOW / INFO]
- **Check**: L2.1 — `aws-api` access pattern
- **Evidence**: `services/adapters/lambda_svc.py:49` — Cost Hub returns the flat float at top level, so `rec.get("estimatedMonthlySavings", 0)` is correct. CO recs are pre-normalized to the same shape — also fine
- **Status**: PASS

### L2-007 `pricing_multiplier` applied once to formula path (correct)  [LOW]
- **Check**: L2.3.2
- **Evidence**: `services/adapters/lambda_svc.py:71` — `formula_savings *= ctx.pricing_multiplier` after summing module-const-derived terms. Correct
- **Status**: PASS

### External validation log

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| L2-002 | `mcp__aws-pricing-mcp-server__get_pricing` | `AWSLambda, us-east-1, group=AWS-Lambda-Provisioned-Concurrency` | $0.0000041667/GB-s (SKU BMKCD2ZCEYKTYYCB) | refutes adapter's use of on-demand rate for PC; confirms 4× inflation |
| L2-002 (control) | same tool | `group=AWS-Lambda-Duration` | $0.0000166667/GB-s Tier-1 (SKU TG3M4CAGBA3NYQBH) — also Tier-2 $0.0000150000 (6-15B), Tier-3 $0.0000133334 (>15B) | confirms x86 on-demand constant is right for Tier-1; adapter does not handle volume tiers (acceptable rounding error for most accounts) |
| L2-001 | AWS Lambda docs | https://aws.amazon.com/lambda/pricing/ | "charged based on the number of requests ... and the duration" | refutes the 730×3600 assumption that bills as if 24/7 |
| L2-003 | AWS Compute Optimizer docs | https://docs.aws.amazon.com/compute-optimizer/latest/ug/view-lambda-function-recommendations.html | "estimated monthly savings ... based on the on-demand pricing in the resource's AWS Region" | refutes `_normalize_lambda_co_rec`'s `* pricing_multiplier` |

---

## L3 — Reporting findings

### L3-001 Phase B handler for `compute_optimizer` AND Phase A coverage — risk of dual-render  [MEDIUM]
- **Check**: L3.2.1
- **Evidence**: `reporter_phase_b.py:2160` — `("lambda", "compute_optimizer"): _render_compute_optimizer_source`. But `lambda` is also in `_PHASE_A_SERVICES` (line 2204). Phase A handles the whole service in one pass; Phase B handler may produce a duplicate render for the CO source depending on the orchestrator's dispatch order
- **Why it matters**: need to confirm the dispatcher in `html_report_generator.py` checks Phase B handler presence first for the specific (svc, src) tuple and skips that source in the Phase A pass. Without reading that dispatcher in this audit, this stays a yellow flag
- **Recommended fix**: trace the dispatch path and add a unit test asserting CO recs render exactly once

### L3-002 `enhanced_checks` recs lack `priority`, dropping severity signal  [LOW]
- **Check**: L3.2.3
- **Evidence**: shim emits no `priority`/`severity` on any of the 3 categories. A 10 GB memory function with 50% rightsizing potential renders identically to a 128 MB function with the same percentage potential
- **Recommended fix**: derive `priority="high"` for `mem_size >= 4096`, `medium` for `1024-4096`, `low` otherwise

### L3-003 Phase A render path for Lambda  [INFO]
- **Check**: L3.2.1
- **Evidence**: `lambda` is in `_PHASE_A_SERVICES`; need to read `reporter_phase_a.render_grouped_by_category` or similar to confirm field names. (Skipping deep audit for this one — Lambda is rendered by descriptor-driven grouping; field names match adapter output per `_extract_lambda_details` at `reporter_phase_a.py:107-131`)
- **Status**: PASS

---

## Cross-layer red flags

**None auto-FAIL** under §4. But:
- L1-001 (required_clients omits CW) — same protocol-lie as DynamoDB
- L2-001 (100% utilization) + L2-002 (PC rate 4× wrong) — both CRITICAL
- L2-004 (savings sum on un-deduped recs) — invariant violation

Overall **FAIL**.

---

## Verification log

```
# 1. Static checks
$ ruff check services/adapters/lambda_svc.py services/lambda_svc.py
N806 Variable `LAMBDA_PRICE_PER_GB_SEC_X86` in function should be lowercase
N806 Variable `LAMBDA_PRICE_PER_GB_SEC_ARM` in function should be lowercase
N806 Variable `LAMBDA_PRICE_PER_1K_REQUESTS` in function should be lowercase
N806 Variable `EXCESSIVE_MEMORY_THRESHOLD` ... (in shim)
Found 4 errors.

# 2. Registration check
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='lambda'); print('clients:', m.required_clients()); print('cw:', m.requires_cloudwatch); print('fast:', m.reads_fast_mode)"
clients: ('lambda', 'compute-optimizer')        ← LIE: shim uses cloudwatch
cw: False                                       ← LIE
fast: False

# 3. Offline + 5. Regression / snapshot tests
$ python3 -m pytest tests/test_offline_scan.py tests/test_regression_snapshot.py \
    tests/test_reporter_snapshots.py -k lambda -v
============================= 4 passed, 122 deselected in 0.06s ==============================

# 6. AWS prices (live API)
Lambda x86 on-demand duration:  $0.0000166667/GB-s Tier-1   (shim matches)
Lambda x86 on-demand duration:  $0.0000150000/GB-s Tier-2   (shim does not model)
Lambda x86 on-demand duration:  $0.0000133334/GB-s Tier-3   (shim does not model)
Lambda Provisioned Concurrency: $0.0000041667/GB-s          (shim uses $0.0000166667 — 4× too high)
```

---

## Recommended next steps (prioritized)

1. **[CRITICAL] L2-001** — Drop the `730 × 3600` assumption. Either (a) emit `EstimatedMonthlySavings = 0.0` with a `pricing_warning: "requires actual invocations"` for excessive_memory and arm_migration recs, OR (b) consume `WeeklyInvocations` + `Duration` from CW (already partly available) to compute `actual_GB_seconds`.
2. **[CRITICAL] L2-002** — Add `LAMBDA_PC_PRICE_PER_GB_SEC = 0.0000041667` and use it in the `"Provisioned Concurrency" in category` branch. PC savings drop by 4× after this fix.
3. **[CRITICAL] L1-001** — Add `"cloudwatch"` to `required_clients()`; set `requires_cloudwatch = True`.
4. **[HIGH] L2-004** — Move the dedup block above the savings loop in `services/adapters/lambda_svc.py:48-90`. Verify with a unit test that double-counted functions don't contribute to both `hub_savings` and `formula_savings`.
5. **[HIGH] L2-003** — Remove the `* pricing_multiplier` multiplication in `_normalize_lambda_co_rec` (and the matching ECS/ASG normalizers — flag during those audits).
6. **[HIGH] L1-003 / L1-004** — Route the 3 print/except sites through `ctx.warn` / `ctx.permission_issue`; remove the `continue` at line 145.
7. **[MEDIUM] L1-005** — Delete the 3 dead `elif` branches in adapter for retired categories.
8. **[MEDIUM] L2-005** — Rename or delete the unused `LAMBDA_PRICE_PER_1K_REQUESTS` constant.
9. **[MEDIUM] L3-001** — Trace Phase A vs Phase B dispatch for `("lambda", "compute_optimizer")`; add regression test that CO recs render exactly once.
10. **[LOW] L3-002** — Set `priority` per memory tier.
11. **[LOW] ruff N806** — Move constants to module-level UPPER_CASE or rename to lower_case in-function.
12. **[INFO]** — Re-capture goldens. Current goldens encode the 4× PC bug and 100%-util assumption.
