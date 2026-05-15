# Audit: eks_cost Adapter

**Adapter**: `services/adapters/eks.py` (555 lines — self-contained)
**ALL_MODULES index**: 33 (final adapter)
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 2 |
| L2 Calculation | **FAIL** | 6 (2 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | node-group Graviton + Spot savings computed from **control-plane cost** not node cost — formulas are nonsense |

---

## Pre-flight facts

- **Required clients**: `("eks", "ec2", "cloudwatch")` — declares but only uses `eks` and CoH-pre-fetched data. `ec2` + `cloudwatch` unused in current code
- **Flags**: `requires_cloudwatch=False`, `reads_fast_mode=False`
- **Sources**: 5 (`cluster_costs`, `node_group_optimization`, `fargate_analysis`, `addon_costs`, `cost_hub_recommendations`)
- **Pricing**: `EKS_CONTROL_PLANE_HOURLY = 0.10` ✓ AWS list

---

## L1 — Technical findings

### L1-001 6 `print()` statements in error paths  [MEDIUM]
- Lines 75, 196, 212, 302, 360, 391, 449, 467
- **Recommended fix**: `ctx.warn` / `ctx.permission_issue`

### L1-002 `required_clients` declares `ec2` + `cloudwatch` but neither is used  [MEDIUM]
- Adapter doesn't reference `ctx.client("ec2")` or `ctx.client("cloudwatch")` anywhere
- **Recommended fix**: drop unused declarations, OR add the actual EC2 calls (needed for L2-001 fix)

---

## L2 — Calculation findings

### Source-classification table

| Recommendation | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| Inactive cluster | `module-const` ($0.10/hr × 730 × multiplier) | line 234, 244 | YES — accurate |
| Graviton node-group savings | `derived` from **CONTROL PLANE cost** × 2 × 0.30 | line 329-332 | **CRITICAL FAIL** — uses wrong base; should be from EC2 instance cost |
| Spot node-group savings | `derived` from **CONTROL PLANE cost** × 0.70 | line 349-352 | **CRITICAL FAIL** — same wrong base |
| Fargate analysis | `derived` (`estimated_pods × typical_vcpu × $0.04048 × 730 × 0.20`) | line 398-412 | **HIGH FAIL** — `estimated_pods = len(profiles) * 3` invented; `typical_vcpu = 0.25`, `typical_mem = 0.5` invented |
| Add-ons | scope-rule REMOVED | line 460-463 | YES |
| Cost Hub | `aws-api` | line 492 | YES — direct from CoH |

### L2-001 Node-group Graviton savings computed from EKS control-plane cost  [CRITICAL]
- **Evidence**: line 329-332:
  ```python
  "monthly_savings": round(
      EKS_CONTROL_PLANE_HOURLY * HOURS_PER_MONTH * 2 * GRAVITON_SAVINGS_FACTOR * multiplier,
      2,
  ),
  ```
- The control plane is **$0.10/hr** (a flat cluster fee). Migrating a node group from `m4.large` to `m6g.large` saves money on the **EC2 instance cost** (`m4.large = $0.10/hr`, `m6g.large = $0.077/hr` → ~23% delta), NOT on the control plane fee. The formula computes `$0.10 × 730 × 2 × 0.30 = $43.80/month` for ANY node group with previous-gen instances, regardless of:
  - Instance count
  - Instance type
  - Cluster size
- **Why it matters**: a 50-node m4.large group reports the same $43.80/month savings as a 2-node group. Real savings for 50 nodes: `50 × $0.023/hr delta × 730 ≈ $840/month` — adapter under-reports by 20×
- **Recommended fix**: shim should query EC2 describe_instance_types for the node group's instance types + autoscaling describe_auto_scaling_groups for desired count, then compute `instance_count × (old_hourly - new_hourly) × 730`

### L2-002 Node-group Spot savings computed from control-plane cost  [CRITICAL]
- **Evidence**: line 349-352 — same shape as L2-001 but with `SPOT_SAVINGS_FACTOR = 0.70`. Computes `$0.10 × 730 × 0.70 = $51.10/month` per node group flat
- **Why it matters**: a node group with 100 c5.4xlarge instances ($0.68/hr each) reports $51.10/month Spot savings; real Spot savings: `100 × $0.68 × 730 × 0.70 ≈ $34,748/month`. Under-reports by **680×**
- **Recommended fix**: same as L2-001 — derive from actual instance cost

### L2-003 Graviton factor 30% too aggressive  [HIGH]
- **Evidence**: line 27 — `GRAVITON_SAVINGS_FACTOR = 0.30`. AWS list-price delta x86→Graviton is ~20% (m5 → m6g, c5 → c6g). The 30% might reflect price-performance gain but as a $-savings factor, 20% is the correct constant
- **Recommended fix**: set to 0.20

### L2-004 Fargate analysis invents 3 pods/profile + typical pod size  [HIGH]
- **Evidence**: line 395-397 — `estimated_pods = max(len(profile_names) * 3, 1)`, `typical_vcpu = 0.25`, `typical_mem_gb = 0.5`. None of these come from actual data
- **Why it matters**: an account with 1 Fargate profile running 100 large pods reports the same savings as 1 profile running 1 small pod
- **Recommended fix**: query `eks describe_fargate_profile` for selectors, then enumerate matching Kubernetes pods via the K8s API (requires extra IAM); OR emit recs only when CW metrics provide actual pod count

### L2-005 `pricing_multiplier` applied to module-const path (correct)  [LOW / POSITIVE]
- **Status**: PASS per L2.3.2

### L2-006 `cost_hub_recs` doesn't apply `pricing_multiplier` (correct)  [LOW / POSITIVE]
- Line 492 — `estimatedMonthlySavings` from CoH is region-correct; no multiplier needed per L2.3.1
- **Status**: PASS

---

## L3 — Reporting findings

**None** — strong `stat_cards`, `grouping`, descriptions, empty-state handler.

---

## Recommended next steps

1. **[CRITICAL] L2-001 / L2-002** — Rewrite node-group savings to derive from actual EC2 instance cost (instance type + count) instead of control-plane cost. Same formula shape, completely wrong base.
2. **[HIGH] L2-003** — Set `GRAVITON_SAVINGS_FACTOR = 0.20`.
3. **[HIGH] L2-004** — Use actual pod count + size from K8s API or CW metrics instead of 3/0.25/0.5 defaults.
4. **[MEDIUM] L1-001 / L1-002** — Classify prints; drop unused `ec2`/`cloudwatch` declarations (or add the calls L2-001/L2-002 need).
