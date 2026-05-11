# Audit: ec2 Adapter (v3 — post-fix re-audit)

**Adapter**: `services/adapters/ec2.py` (72 lines)
**Legacy shim**: `services/ec2.py` (819 lines after fixes)
**ALL_MODULES index**: 0
**Renderer path**: Phase B — 4 dedicated handlers
**Date**: 2026-05-12
**Auditor**: automated re-audit (CRITICAL + HIGH findings from v2)

## Verdict

| Layer | v2 verdict | v3 verdict | Δ |
|-------|------------|------------|----|
| L1 Technical   | WARN  | **WARN**           | L1-001 fixed (HIGH); 4 MEDIUM/LOW remain |
| L2 Calculation | FAIL  | **CONDITIONAL-PASS** | All CRITICAL + HIGH cleared; 1 MEDIUM (test fixture schema) + 1 LOW remain |
| L3 Reporting   | WARN  | **CONDITIONAL-PASS** | L3-001 fixed (HIGH); 2 MEDIUM remain |
| **Overall**    | FAIL  | **CONDITIONAL-PASS** | All CRITICAL + HIGH findings cleared; no FAIL-class regressions |

Conditional-pass because 8 MEDIUM/LOW findings from v2 remain open (intentionally — user scoped this round to CRITICAL + HIGH only).

---

## Pre-flight facts (verified post-fix)

- `EC2Module` at `services/adapters/ec2.py:14` — class definition unchanged
- `requires_cloudwatch = True` at `services/adapters/ec2.py:21` (newly declared) ← **L1-001 fix verified**
- `reads_fast_mode = True` at `services/adapters/ec2.py:20`
- `required_clients()` = `("ec2", "compute-optimizer", "autoscaling")`
- Adapter calls **only** `get_enhanced_ec2_checks` and `get_advanced_ec2_checks` from `services/ec2.py` (verified: `get_auto_scaling_checks` is **not** invoked from the adapter, so its leftover % strings never reach `total_monthly_savings`)
- `services/_savings.py` exports `parse_dollar_savings` and new `compute_optimizer_savings`

---

## CRITICAL — re-audit

### L2-002  Compute Optimizer field path  ✅ FIXED

- **v2 evidence**: adapter read `rec.get("estimatedMonthlySavings", 0)` at top level (AWS returns 0); confirmed by boto3 ref.
- **Fix delta** (`services/adapters/ec2.py:52`):
  ```python
  savings += sum(compute_optimizer_savings(rec) for rec in co_recs)
  ```
- **Helper** (`services/_savings.py:33-56`): iterates the 4 documented option-array keys (`recommendationOptions`, `volumeRecommendationOptions`, `instanceRecommendationOptions`, `storageRecommendationOptions`), picks rank-1 (else first), reads `savingsOpportunity.estimatedMonthlySavings.{value}`, defensively coerces dict-or-scalar, sums.
- **External re-validation (this turn)**:
  - boto3 `get_ec2_instance_recommendations` (re-checked): field still nested in `recommendationOptions[N].savingsOpportunity.estimatedMonthlySavings`. ✓
  - Behaviour test:
    ```
    EC2 rank-1=$42.5, rank-2=$999  → compute_optimizer_savings = $42.50  ✓ (picks rank-1)
    Legacy-wrong-schema {estimatedMonthlySavings: 9999} → $0.00  ✓ (no longer leaks)
    Empty rec → $0.00  ✓
    ```
- **Verdict**: **RESOLVED**.

### L2-004  Arbitrary $50 percentage fallback  ✅ FIXED

- **v2 evidence**: `services/_savings.py:19-20` returned `50.0` for any string containing `%`; 6 EC2 categories emitted percentage-only strings.
- **Fix delta**:
  - `services/_savings.py:9-22` — `if "%" in savings_str: return 50.0` deleted. Now returns `0.0` for any string without an explicit `$`.
  - `services/ec2.py:18-30` — new `EC2_SAVINGS_FACTORS` constant with 8 categories.
  - `services/ec2.py:36-50` — new `_compute_ec2_savings(ctx, instance_type, category)` helper using `ctx.pricing_engine.get_ec2_hourly_price() * 730 * factor`.
  - 8 percentage-only `EstimatedSavings` strings rewritten to `f"${X.YY}/month"`. Categories that genuinely have $0 (Auto Scaling Missing, Stopped Instances, Monitoring-Only, Monitoring Required) explicitly emit `"$0.00/month - …"` so headline ↔ table parity is preserved.
- **Re-validation (this turn)**:
  | string | parse_dollar_savings → |
  |---|---|
  | `"$70.08/month if terminated"` | **$70.08** ✓ |
  | `"$28.03/month if rightsized"` | **$28.03** ✓ |
  | `"$0.00/month - enable CloudWatch monitoring to quantify"` | **$0.00** ✓ |
  | `"30-50% cost reduction"` (legacy form) | **$0.00** ✓ (was $50) |
- **AWS Pricing API (m5.large × idle factor 1.0)**:  $0.096/h × 730h × 1.0 = **$70.08/mo** — matches the new emission.
- **Verdict**: **RESOLVED**. The flat-$50 inflator is gone everywhere; real-instance pricing now drives every quantified rec.

---

## HIGH — re-audit

### L1-001  `requires_cloudwatch=False` lie  ✅ FIXED

- `services/adapters/ec2.py:21` — `requires_cloudwatch: bool = True`.
- Probe: `python3 -c "from services import ALL_MODULES; m = next(x for x in ALL_MODULES if x.key == 'ec2'); print(m.requires_cloudwatch)"` → `True` ✓
- **Verdict**: **RESOLVED**.

### L2-005  Hardcoded `$0.10/GB` for oversized root volumes  ✅ FIXED

- **Fix delta** (`services/ec2.py:722-735`):
  ```python
  volume_type = volume.get("VolumeType", "gp2")
  gb_rate = (
      ctx.pricing_engine.get_ebs_monthly_price_per_gb(volume_type)
      if ctx.pricing_engine
      else 0.10 * ctx.pricing_multiplier
  )
  root_savings = max(size_gb - 20, 0) * gb_rate
  ...
  "VolumeType": volume_type,
  ```
- **External re-validation**:
  - AWS Pricing API us-east-1: gp2 = **$0.10/GB-mo**, gp3 = **$0.08/GB-mo**. Both match what the new path would compute.
  - 200 GB gp3 root volume: `(200 - 20) × 0.08 = $14.40/month`. Previously hardcoded path would have reported `$18.00` (gp2 rate applied to a gp3 volume — wrong).
- **Verdict**: **RESOLVED**. Now respects volume type and region; falls back gracefully when `pricing_engine` is None.

### L2-006  Duplicate `underutilized_instance_store` append  ✅ FIXED

- Removed in the v2-CRITICAL commit (`services/ec2.py` batch-job branch). Re-grep confirms only one append site for the category.
- **Verdict**: **RESOLVED**.

### L2-002 in EBS + RDS sibling adapters  ✅ FIXED

- **External evidence (this round)**:
  - boto3 `get_ebs_volume_recommendations`: `estimatedMonthlySavings` lives in `volumeRecommendationOptions[N].savingsOpportunity.estimatedMonthlySavings`. ✓
  - boto3 `get_rds_database_recommendations`: split into `instanceRecommendationOptions` + `storageRecommendationOptions`, each with `savingsOpportunity.estimatedMonthlySavings`. ✓
- **Fix delta**:
  - `services/_savings.py:25-30` — `_CO_OPTION_KEYS` covers all 4 documented variants.
  - `services/adapters/ebs.py:66` — `sum(compute_optimizer_savings(r) for r in co_recs)`.
  - `services/adapters/rds.py:63` — same.
- **Behaviour test**:
  - EBS rec with `volumeRecommendationOptions[{rank:1, …value:12.30}]` → **$12.30** ✓
  - RDS rec with both instance + storage option arrays ($100 + $20) → **$120.00** ✓ (correctly sums)
- **Verdict**: **RESOLVED** across EC2, EBS, RDS.

### L3-001  Renderer drops Spot recommendations the adapter counts  ✅ FIXED

- Three filter blocks removed from `reporter_phase_b.py`:
  - `_render_ec2_enhanced_checks` (was line 46-49)
  - `_render_ec2_cost_hub` (was line 96)
  - `render_generic_per_rec` (was line 987-989)
- `grep -n '"spot instance"\|"Spot" in rec' reporter_phase_b.py` → **no matches**. ✓
- **Verdict**: **RESOLVED**. Adapter is now the single source of truth for what counts and renders.

---

## What did NOT change (remaining MEDIUM/LOW open from v2)

These are unchanged by this round of fixes; tracking them only — no new evidence required:

| ID | Severity | Summary |
|---|---|---|
| L1-002 | MEDIUM | `--fast` not honored by `get_enhanced_ec2_checks` / `get_advanced_ec2_checks` |
| L1-003 | MEDIUM | Silent `except: pass` blocks in `services/ec2.py` |
| L1-004 | LOW    | `print()` statements pollute stdout |
| L1-005 | LOW    | `CPUCreditBalance` queried at `Period=3600` despite 5-min publication |
| L2-001 | MEDIUM | Test fixtures (not real API) encode wrong Cost Hub schema |
| L2-003 | LOW    | Compute Optimizer opt-in placeholder uses inconsistent capital-E key |
| L2-007 | MEDIUM | `services/adapters/CLAUDE.md` Live-Pricing table needs to claim EC2 now that PricingEngine is actually wired |
| L3-002 | MEDIUM | Phase B handler collapses per-instance details to `recs[0]` |
| L3-003 | MEDIUM | Format-consistency mostly resolved by L2-004; one ASG-non-reachable string in `get_auto_scaling_checks` still emits a percentage but doesn't reach `total_monthly_savings` |

---

## Verification log

```
# 1. Static checks
$ ruff check services/adapters/ec2.py services/adapters/ebs.py services/adapters/rds.py services/_savings.py services/ec2.py reporter_phase_b.py
(touched lines clean; ruff's 149 reported errors are pre-existing project-wide
issues in unaffected long files, not regressions from this round)

# 2. Module registration (after fix)
idx: 0
class: EC2Module
aliases: ('ec2',)
clients: ('ec2', 'compute-optimizer', 'autoscaling')
cw: True              ← FIXED (was False)
fast_mode: True

# 3. CLI alias resolution
resolve_cli_keys({'ec2'}) → {'ec2'}

# 4. parse_dollar_savings post-fix behavior
'30-50% cost reduction'                                    → 0.0   (was 50.0)
'Up to 100% if terminated, 50-70% if downsized'           → 0.0   (was 50.0)
'Variable - up to 25% on EC2 instances'                   → 0.0   (was 50.0)
'$70.08/month if terminated'                              → 70.08
'$0.00/month - enable CloudWatch monitoring to quantify'  → 0.0
'$15.20/month'                                            → 15.20

# 5. compute_optimizer_savings against all 4 AWS schemas
EC2 (recommendationOptions, picks rank-1)                 → 42.50
EBS (volumeRecommendationOptions)                          → 12.30
RDS (instanceRecommendationOptions + storageRecommendationOptions) → 120.00 (sums both)
Legacy wrong top-level estimatedMonthlySavings=9999       → 0.00  ✓ no leak

# 6. L2-005 verification (Oversized root volumes)
gp2 200GB: (200-20) × $0.10 = $18.00/month   (matches AWS gp2 price)
gp3 200GB: (200-20) × $0.08 = $14.40/month   (matches AWS gp3 price; was hardcoded wrong before)

# 7. L3-001 — Spot filters in renderer
$ grep -n '"spot instance"\|"Spot" in rec' reporter_phase_b.py
(no matches — all 3 filter blocks removed)

# 8. Regression gate
$ pytest tests/test_offline_scan.py tests/test_regression_snapshot.py tests/test_reporter_snapshots.py tests/test_result_builder.py tests/test_orchestrator.py tests/test_pricing_engine.py
152 passed, 1 skipped in 8.25s   (1 skip is the --live-gated test)
```

---

## External validation log (this round)

| Finding | Tool | Call | Result | Confirms |
|---|---|---|---|---|
| L2-005 (post-fix) | `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2 / us-east-1`, `volumeApiName=gp3 + productFamily=Storage` | gp3 = **$0.08/GB-month** | New `get_ebs_monthly_price_per_gb` path matches AWS truth for non-gp2 root volumes (where the old hardcoded `0.10` was wrong) |
| L2-002 (EBS) | `WebFetch` | `boto3 get_ebs_volume_recommendations` | Field in `volumeRecommendationOptions[N].savingsOpportunity.estimatedMonthlySavings` | Helper's new `volumeRecommendationOptions` key matches AWS schema |
| L2-002 (RDS) | `WebFetch` | `boto3 get_rds_database_recommendations` | Two separate arrays: `instanceRecommendationOptions` + `storageRecommendationOptions` | Helper sums both, RDS recs no longer drop storage savings |

---

## Cross-layer red flags

None remaining. (v2 had two — both tied to L2-002 and L2-004, both resolved.)

---

## Recommended next steps

The remaining items are all MEDIUM/LOW per v2 prioritization. None block release.

- If you keep going: **L1-002** (`--fast` plumbing), **L1-003** (silent `except: pass`), and **L2-001** (test fixture schema fix) are the highest-value follow-ups. Each is a small, scoped change.
- If you stop here: open a single tracking issue listing the 8 remaining items so v4 has a clear scope.
