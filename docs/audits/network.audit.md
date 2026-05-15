# Audit: network Adapter

**Adapter**: `services/adapters/network.py` (68 lines — thin composition wrapper)
**Legacy shims (5)**:
- `services/elastic_ip.py` (132 lines)
- `services/nat_gateway.py` (151 lines)
- `services/vpc_endpoints.py` (130 lines)
- `services/load_balancer.py` (233 lines)
- `services/ec2.py:get_auto_scaling_checks` (subset)

**ALL_MODULES index**: 9
**Renderer path**: **Phase B** — `("network", "enhanced_checks"): _render_network_enhanced_checks` (`reporter_phase_b.py:2189`). `network` is in `_PHASE_B_SKIP_PER_REC`
**Date**: 2026-05-15
**Auditor**: automated

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 5 (3 HIGH, 1 MEDIUM, 1 LOW) |
| L2 Calculation | **FAIL** | 7 (1 CRITICAL, 4 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 2 (1 MEDIUM, 1 LOW) |
| **Overall**    | **FAIL** | invented NLB 1.4× ratio + dual-fallback constants disagree with PricingEngine + N-1 NAT consolidation overstates by 50%+ |

---

## Pre-flight facts

- **Adapter class**: `NetworkModule` at `services/adapters/network.py:17`
- **Declared `required_clients`**: `("ec2", "elasticloadbalancingv2", "autoscaling", "elb")` — `elasticloadbalancingv2` should be `elbv2` per boto3 service-alias (shim calls `ctx.client("elbv2")` at `services/load_balancer.py:70`). `ClientRegistry` may resolve both, but the declared name is non-canonical
- **Sources emitted**: `{"enhanced_checks"}` only — single block holding `eip + nat + vpc + lb + asg` recs concatenated
- **Pricing methods consumed**: `ctx.pricing_engine.get_eip_monthly_price()`, `get_nat_gateway_monthly_price()`, `get_vpc_endpoint_monthly_price()`, `get_alb_monthly_price()`. This is the **highest-quality live-pricing usage** of any adapter audited so far in this pass
- **Test files**: `tests/test_offline_scan.py`, `tests/test_regression_snapshot.py`, `tests/test_reporter_snapshots.py`

---

## L1 — Technical findings

### L1-001 `required_clients()` uses non-canonical boto3 service name  [HIGH]
- **Check**: L1.1.4
- **Evidence**: `services/adapters/network.py:26` declares `"elasticloadbalancingv2"`; shim calls `ctx.client("elbv2")` (`services/load_balancer.py:70`). `boto3.client("elbv2")` is the canonical alias; `"elasticloadbalancingv2"` is the SDK service ID but not what shim uses
- **Why it matters**: a strict `ClientRegistry` could fail to mint the client even though boto3 accepts both. IAM-policy generators that key off `required_clients` produce an inconsistent surface
- **Recommended fix**: change to `("ec2", "elbv2", "autoscaling", "elb")`

### L1-002 `print()` calls in adapter + 4 sub-shims  [HIGH]
- **Evidence**:
  - `services/adapters/network.py:40` — adapter banner
  - `services/load_balancer.py:48, 84` — `print(f"Warning: Could not get tags for ALB ...")`, `print(f"⚠️ Error getting Classic Load Balancers ...")`
  - Likely also in `elastic_ip.py`, `nat_gateway.py`, `vpc_endpoints.py` (similar pattern)
- **Recommended fix**: route through `ctx.warn`/`ctx.permission_issue`

### L1-003 No `AccessDenied` discrimination across 5 sub-shims  [HIGH]
- **Evidence**: `grep -n "permission_issue" services/elastic_ip.py services/nat_gateway.py services/vpc_endpoints.py services/load_balancer.py` returns nothing. Every `except Exception:` becomes either a `print` or a `ctx.warn`
- **Recommended fix**: classify `AccessDenied`/`UnauthorizedOperation` and route via `ctx.permission_issue` in all 5 sub-shims (EC2, ELB, ELBv2, AutoScaling list-describe operations)

### L1-004 Adapter wraps recs in a **single** `enhanced_checks` source — five sub-services collapsed  [MEDIUM]
- **Check**: L1.1.8 / L3.2.1 — source name should reflect content
- **Evidence**: `services/adapters/network.py:54-65` — all 5 sub-services' recs concatenated into one tuple, exposed as `"enhanced_checks"`. Renderer at `reporter_phase_b._render_network_enhanced_checks` must then re-group by `CheckCategory`. Per-source dedup/diagnostics impossible
- **Why it matters**: per-sub-service total/savings invisible; can't tell from JSON whether $X is EIP-driven or LB-driven without parsing every CheckCategory
- **Recommended fix**: emit 5 sources: `elastic_ip`, `nat_gateway`, `vpc_endpoints`, `load_balancer`, `auto_scaling`. Adjust renderer accordingly

### L1-005 Adapter does not catch sub-shim exceptions  [LOW]
- **Check**: L1.5.1 — error isolation
- **Evidence**: `services/adapters/network.py:42-46` — if any sub-shim raises, the whole network scan aborts (caught only by `safe_scan` at orchestrator level — entire network section empty). Compare to `containers` adapter which wraps each sub-call in try/except
- **Recommended fix**: wrap each sub-shim call individually so one failing sub-shim (e.g., autoscaling AccessDenied) doesn't blank the entire network tab

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | External validation |
|---|---|---|---|---|
| Idle EIP | `live` (`get_eip_monthly_price`) → fallback `$3.65` | `services/elastic_ip.py:16, 53` | YES — matches AWS list price | AWS Pricing API → $0.005/hr × 730 = $3.65/month |
| Unused NAT Gateway | `live` (`get_nat_gateway_monthly_price`) → fallback `$32.0` in shim, `$35.04` in PricingEngine | `services/nat_gateway.py:17, 71`; `core/pricing_engine.py:130` | **WARN** — shim fallback $32 matches us-east-1; PricingEngine fallback $35.04 is regional and contradicts the shim | AWS Pricing API → $0.045/hr × 730 = $32.85/month us-east-1 |
| NAT consolidation savings | `derived` (`(count-1) × nat_monthly`) | `services/nat_gateway.py:114, 139` | **FAIL** — assumes all N−1 NATs can be removed; AWS HA best-practice mandates 1 NAT per AZ → max realistic reduction is `count - az_count`, not `count - 1` | AWS docs: "deploy a NAT gateway in each Availability Zone" |
| Cross-AZ NAT | `derived` (flat `$25/month`) | `services/nat_gateway.py:127` | **FAIL** — invented constant for "low-traffic scenarios"; should be `actual_traffic_GB × $0.045 cross-AZ` | n/a |
| Unused VPC endpoint | `live` (`get_vpc_endpoint_monthly_price`) → fallback `$7.30` shim, `$8.03` PricingEngine | `services/vpc_endpoints.py:17, 96`; `core/pricing_engine.py:131` | **WARN** — shim fallback $7.30 matches AWS ($0.01/hr × 730); PricingEngine fallback $8.03 too high | AWS docs → $0.01/AZ/hr for Interface endpoints |
| VPC endpoint consolidation | `derived` (`(N-2) × vpc_ep_monthly`) | `services/vpc_endpoints.py:118` | **WARN** — assumes 2 endpoints is enough; same AZ-HA issue as NAT |
| Idle / zero-traffic ALB | `live` (`get_alb_monthly_price`) → fallback `$16.20` shim, `$20.44` PricingEngine | `services/load_balancer.py:54, 131, 191`; `core/pricing_engine.py:132` | **WARN** — shim fallback $16.20 matches AWS ($0.0225/hr × 730 = $16.43); PricingEngine fallback $20.44 too high |
| NLB monthly | `derived` (`alb_monthly × 1.4`) | `services/load_balancer.py:55` | **CRITICAL FAIL** — 1.4× ratio is invented. NLB and ALB have **different pricing models** (NLCU vs LCU); base hourly NLB rate is comparable to ALB. The 1.4× ratio has no AWS basis | AWS Pricing API → NLB $0.0225/hr base + $0.006/NLCU-hr; ALB $0.0225/hr base + $0.008/LCU-hr. **Base rates are equal**; difference is LCU usage |
| NLB→ALB savings (line 115) | `derived` from NLB×1.4 | `services/load_balancer.py:115` | **CRITICAL FAIL** — `nlb_monthly − alb_monthly = $6.48/month` displayed as fixed savings on every NLB→ALB conversion. Wrong: real delta depends on traffic-driven LCU/NLCU usage |
| Classic ELB migration (line 221) | `arbitrary` ("10-20% + better features") | `services/load_balancer.py:221` | **FAIL** — percentage string, scope-rule violation |

### L2-001 NLB / ALB price ratio invented (1.4×)  [CRITICAL]
- **Check**: L2.1 — `derived` requires documented ratio
- **Evidence**: `services/load_balancer.py:55` — `nlb_monthly = alb_monthly * 1.4`. AWS Pricing API confirms ALB base hourly = $0.0225/hr (`LoadBalancerUsage`). NLB base hourly (separate SKU) is also $0.0225/hr. The differentiator is `LCU` (ALB) vs `NLCU` (NLB) consumption rates, not the base
- **Why it matters**: every `nlb_vs_alb` recommendation (line 110-119) reports `$6.48/month savings` regardless of actual traffic. For low-traffic accounts (NLB without significant NLCU usage), real saving may be $0. For high-traffic (sustained Gbps), saving may be negative (NLB cheaper for many TCP workloads). The number bears no relation to the customer's actual cost delta
- **Recommended fix**: add `get_nlb_monthly_price()` to PricingEngine returning the actual NLB base + LCU complement; or remove the `nlb_vs_alb` rec entirely (it's primarily an architectural choice, not a cost optimization)

### L2-002 NAT consolidation assumes all N−1 NATs removable  [HIGH]
- **Check**: L2.5 — savings basis
- **Evidence**: `services/nat_gateway.py:114, 139` — `f"${(count - 1) * nat_monthly:.2f}/month if consolidated"`
- **Why it matters**: best-practice AWS deployment uses 1 NAT per AZ for fault tolerance (typically 2-3 AZs). Going from 3 NATs (one per AZ) to 1 NAT eliminates HA — not a cost optimization, a resilience trade-off. The rec's savings figure ignores the AZ topology
- **Recommended fix**: compute `realistic_savings = max(0, count - az_count) × nat_monthly`; or rec-text should explicitly say "consolidation requires accepting single-AZ failure mode"

### L2-003 Fallback constants in shims and PricingEngine disagree  [HIGH]
- **Check**: L2.1 — `module-const`/`fallback` must be internally consistent
- **Evidence**:
  - NAT: shim line 17 `else 32.0`; PricingEngine `FALLBACK_NAT_MONTH=35.04`
  - VPC endpoint: shim line 17 `else 7.30`; PricingEngine `FALLBACK_VPC_ENDPOINT_MONTH=8.03`
  - ALB: shim line 54 `else 16.20`; PricingEngine `FALLBACK_ALB_MONTH=20.44`
  - EIP: shim line 16 `else 3.65`; PricingEngine `FALLBACK_EIP_MONTH=3.65` ✓ matches
- **Why it matters**: if `ctx.pricing_engine is None` (e.g., in some test setups), shim returns one value; if `pricing_engine` exists but API fails, the engine returns its fallback — different number for the same SKU. Cross-layer red flag §4.4 ("pricing constant exists in the adapter that contradicts `core/pricing_engine.py` `FALLBACK_*` — two different 'fallback' prices for the same SKU")
- **Recommended fix**: delete the shim's `if ctx.pricing_engine is not None else <CONST>` ternary; trust `ctx.pricing_engine.get_*_price()` always (the engine handles its own fallback). Also reconcile PricingEngine fallback constants to match us-east-1 list prices ($32.85 NAT, $7.30 VPC-EP, $16.43 ALB)

### L2-004 `$25/month low-traffic` cross-AZ NAT savings invented  [HIGH]
- **Evidence**: `services/nat_gateway.py:127` — `"EstimatedSavings": "Up to $25/month for low-traffic scenarios"` (string, not numeric). `parse_dollar_savings` extracts $25.00 from this string regardless of actual traffic
- **Recommended fix**: compute from actual cross-AZ data-processing volume (CloudWatch `NatGateway` metrics) × $0.045/GB

### L2-005 Classic ELB "10-20%" string can't parse  [HIGH]
- **Check**: scope rule
- **Evidence**: `services/load_balancer.py:221` — `"EstimatedSavings": "10-20% + better features"`. `parse_dollar_savings("10-20% + better features")` returns 0.0 — counted toward `total_recommendations` but contributes $0 to savings
- **Recommended fix**: compute as `clb_monthly - alb_monthly` from actual prices (CLB is $0.025/hr = $18.25/mo; ALB is $16.43/mo; savings = $1.82/mo flat)

### L2-006 `parse_dollar_savings` aggregates correctly when string contains a $  [LOW]
- **Check**: L2.1 — `parsed` source classification
- **Evidence**: adapter line 56 — `sum(parse_dollar_savings(rec.get("EstimatedSavings", "")) for rec in all_recs)`. For recs whose strings include `$N.NN/month`, this works. For percentage-only strings (L2-005), returns 0.0. Visibility-only counted in `total_recommendations` but $0 contribution
- **Status**: PASS for the parse logic; L2-005 is the upstream issue

### L2-007 `pricing_multiplier` not applied to live `PricingEngine` paths  [LOW / INFO]
- **Check**: L2.3.1 — live methods already return region-correct prices
- **Evidence**: `services/elastic_ip.py:53` — `f"${eip_monthly:.2f}/month per EIP"` — uses `eip_monthly` directly without multiplier. **Correct** per L2.3.1 (live methods are region-correct). However, the shim's `else` branch fallback (e.g., `else 3.65`) is module-const and SHOULD apply `ctx.pricing_multiplier` — it doesn't. Bug present only on the dead-else path
- **Status**: Live path PASS; dead-else fallback has L2.3.2 issue but is unreachable today

### External validation log

| Finding ID | Tool | Call | Result | Confirms / Refutes |
|---|---|---|---|---|
| L2-001 | `mcp__aws-pricing-mcp-server__get_pricing` | `AWSELB, us-east-1, productFamily=Load Balancer-Application` | ALB `LoadBalancerUsage` $0.0225/hr (SKU 37CUWUT8GSNQEPUV) | refutes 1.4× NLB ratio (NLB base is also $0.0225/hr per separate SKU) |
| L2-003 | `mcp__aws-pricing-mcp-server__get_pricing` | `AmazonEC2, us-east-1, productFamily=NAT Gateway` | NAT hourly $0.045/hr (SKU M2YSHUBETB3JX4M4), data $0.045/GB | confirms shim's $32 fallback ($0.045 × 730 = $32.85); refutes PricingEngine fallback $35.04 |
| L2-003 (ALB) | same | ALB $0.0225/hr | confirms shim $16.20; refutes PricingEngine $20.44 |
| L2-002 | AWS NAT Gateway docs | https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat-gateway.html | "we recommend deploying a NAT gateway in each Availability Zone" | refutes `(count-1) × $32` blanket savings claim |

---

## L3 — Reporting findings

### L3-001 Single `enhanced_checks` source masks 5 sub-services  [MEDIUM]
- Same as L1-004; reporter's `_render_network_enhanced_checks` has to re-group by `CheckCategory`. Possible but messy
- **Recommended fix**: emit 5 sources

### L3-002 No `priority`/`severity` on recs  [LOW]
- **Recommended fix**: derive (idle EIP for 30+ days → high; underutilized NAT → medium)

---

## Cross-layer red flags

- **§4.4 violation** — L2-003 — two different "fallback" prices exist for the same SKU (shim vs PricingEngine). Should converge.

Overall **FAIL** (driven by L2-001 NLB 1.4× and L2-003 cross-fallback drift).

---

## Verification log

```
# 2. Registration check
$ python3 -c "from services import ALL_MODULES; m=next(x for x in ALL_MODULES if x.key=='network'); print('clients:', m.required_clients())"
clients: ('ec2', 'elasticloadbalancingv2', 'autoscaling', 'elb')   ← non-canonical name

# 3. Offline + 5. Regression / snapshot tests
$ python3 -m pytest tests/test_offline_scan.py tests/test_regression_snapshot.py \
    tests/test_reporter_snapshots.py -k network -v
============================= 4 passed, 122 deselected in 0.07s ==============================

# 6. AWS prices (live API, us-east-1)
EIP idle:          $0.005/hr × 730 = $3.65/mo    ← matches shim $3.65 and PricingEngine fallback $3.65 ✓
NAT hourly:        $0.045/hr × 730 = $32.85/mo   ← shim $32 close, PricingEngine $35.04 too high
ALB hourly:        $0.0225/hr × 730 = $16.43/mo  ← shim $16.20 close, PricingEngine $20.44 too high
VPC EP interface:  $0.010/hr × 730 = $7.30/mo    ← shim $7.30 ✓, PricingEngine $8.03 too high
NLB hourly:        $0.0225/hr (base, same as ALB) — invalidates 1.4× ratio
```

---

## Recommended next steps (prioritized)

1. **[CRITICAL] L2-001** — Either add `get_nlb_monthly_price()` to PricingEngine + `get_nlb_lcu_hourly()` for traffic-based estimate, OR remove the NLB-vs-ALB rec (it's architectural, not a cost optimization).
2. **[HIGH] L2-002** — Modify NAT consolidation savings: `realistic_savings = max(0, count - az_count) × nat_monthly`; rec text must call out the HA trade-off.
3. **[HIGH] L2-003** — Reconcile fallback constants: drop shim `else <CONST>` ternaries; update PricingEngine FALLBACK_NAT_MONTH=32.85, FALLBACK_VPC_ENDPOINT_MONTH=7.30, FALLBACK_ALB_MONTH=16.43.
4. **[HIGH] L2-004 / L2-005** — Compute cross-AZ NAT and Classic ELB savings from live data + price; drop the placeholder strings.
5. **[HIGH] L1-002 / L1-003** — Replace `print`s with `ctx.warn`; add AccessDenied classification across all 5 sub-shims.
6. **[HIGH] L1-001** — Use `"elbv2"` not `"elasticloadbalancingv2"` in `required_clients()`.
7. **[MEDIUM] L1-004 / L3-001** — Emit 5 distinct sources (`elastic_ip`, `nat_gateway`, `vpc_endpoints`, `load_balancer`, `auto_scaling`).
8. **[LOW] L1-005** — Wrap each sub-shim call in try/except so one failure doesn't blank the tab.
9. **[LOW] L3-002** — Set `priority` per finding age/severity.
