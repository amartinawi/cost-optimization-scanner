# Audit: lightsail Adapter

**Adapter**: `services/adapters/lightsail.py` (56 lines)
**Legacy shim**: `services/lightsail.py` (~112 lines)
**ALL_MODULES index**: 16
**Renderer path**: **Phase A** (`lightsail` in `_PHASE_A_SERVICES`)
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **WARN** | 3 (1 HIGH, 2 MEDIUM) |
| L2 Calculation | **FAIL** | 6 (1 CRITICAL, 3 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **WARN** | 1 (MEDIUM) |
| **Overall**    | **FAIL** | live-pricing path is structurally broken (Lightsail has no `instanceType` filter); every rec falls back to $12 invented constant |

---

## Pre-flight facts

- **Required clients**: `("lightsail",)`
- **Flags**: defaults (False, False)
- **Sources**: `result.get("checks", {})` mapped to per-category `SourceBlock`s (5: `idle_instances`, `oversized_instances`, `unused_static_ips`, `load_balancer_optimization`, `database_optimization`)
- **Pricing**:
  - Shim: `_BUNDLE_COSTS` dict (7 entries) + `_DEFAULT_BUNDLE_COST = 20.00`
  - Adapter: `ctx.pricing_engine.get_instance_monthly_price("AmazonLightsail", bundle_name)` → **structurally returns 0** (Lightsail Pricing API doesn't expose `instanceType` filter)
  - Adapter fallback: $12 × multiplier

---

## L1 — Technical findings

### L1-001 Module-level + adapter `print()` (2 sites)  [HIGH]
- `services/lightsail.py:13`, `services/adapters/lightsail.py:35`
- **Recommended fix**: `logger.debug`

### L1-002 No error handling around shim call  [MEDIUM]
- **Evidence**: adapter line 36 — `result = get_enhanced_lightsail_checks(ctx)` with no try/except. Any shim crash (which currently swallows internally via `try/except` at shim line 51-onwards — needs check) would propagate
- **Recommended fix**: wrap in try/except; route to `ctx.warn`

### L1-003 Shim's `except Exception` blocks not classified for AccessDenied  [MEDIUM]
- Lightsail API requires `lightsail:GetInstances`, `lightsail:GetStaticIps`, etc.
- **Recommended fix**: classify and route to `ctx.permission_issue`

---

## L2 — Calculation findings

### Source-classification table

| Recommendation type | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| `idle_instances` (shim string) | `module-const` via `_BUNDLE_COSTS` dict in shim | `services/lightsail.py:70` `f"${get_lightsail_bundle_cost(bundle_id):.2f}/month"` | **WARN** — 7 bundles mapped; Lightsail offers 20+ bundles (cache, container, database) — unmapped fall to `_DEFAULT_BUNDLE_COST = $20` |
| `oversized_instances` | `module-const × 0.3` factor | shim line 84 | **FAIL** — 30% rightsize factor is invented; actual savings depend on target bundle |
| `unused_static_ips` | hardcoded "$3.65/month" string | shim line 100 | **WARN** — matches AWS EIP rate (Lightsail static IPs charge $0.005/hr when unattached = $3.65/mo) ✓ |
| Adapter savings rollup | `live` (`get_instance_monthly_price`) OR `$12 fallback` | adapter line 42-45 | **CRITICAL FAIL** — live path returns 0 because Lightsail has no `instanceType` Pricing API attribute; every rec falls through to $12 |

### L2-001 Live-pricing path is structurally dead  [CRITICAL]
- **Evidence**: adapter line 42 — `monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonLightsail", bundle_name)`. `_fetch_generic_instance_price` at `core/pricing_engine.py:661-669` uses `{"Field": "instanceType", ...}` filter. AWS Lightsail's pricing attributes (verified via `mcp__aws-pricing-mcp-server__get_pricing_service_attributes`) are: `memory`, `vcpu`, `storage`, `instancefamily`, `operatingSystem` — **no `instanceType`**. The filter never matches anything; the call returns `None` → `0.0` → adapter falls through to $12
- **Why it matters**: 100% of Lightsail savings come from the $12 fallback constant. The shim has its own correct $3.50-$160 bundle table at `_BUNDLE_COSTS`, but the adapter ignores it entirely
- **Recommended fix**: either (a) add a `_fetch_lightsail_bundle_price(bundle_id)` method to PricingEngine that uses `instancefamily` + `memory` filters, or (b) make the adapter consume the shim's `get_lightsail_bundle_cost()` helper directly. Option (b) is the cheap fix

### L2-002 $12 fallback constant invented  [HIGH]
- **Evidence**: adapter lines 43, 45 — `12.0 * ctx.pricing_multiplier`. Doesn't correspond to any bundle:
  - Lightsail smallest: $3.50/mo
  - micro: ~$5/mo
  - small: ~$10/mo
  - medium: ~$20/mo
  - $12 between small and medium — arbitrary
- **Recommended fix**: replace with `get_lightsail_bundle_cost(bundle_name)` (shim helper); apply `pricing_multiplier`

### L2-003 Adapter does not multiply live `monthly` by `pricing_multiplier`  [HIGH]
- **Evidence**: adapter line 43 — `monthly if monthly > 0 else 12.0 * ctx.pricing_multiplier`. The live path returns `monthly` WITHOUT multiplier (correct per L2.3.1 since live is region-correct); the fallback path applies multiplier (correct per L2.3.2). BUT in real-world the live path always returns 0 (L2-001), so this is moot today
- **Status**: per-line logic correct; dead in practice

### L2-004 Oversized 30% rightsize factor invented  [HIGH]
- **Evidence**: `services/lightsail.py:84` — `f"${get_lightsail_bundle_cost(bundle_id) * 0.3:.2f}/month potential"`. 30% has no AWS basis
- **Recommended fix**: compute `(current_bundle_cost − target_bundle_cost)` using actual smaller bundle from `_BUNDLE_COSTS` mapped to measured CPU/memory utilization

### L2-005 `_DEFAULT_BUNDLE_COST = $20` for unmapped bundles  [MEDIUM]
- **Evidence**: `services/lightsail.py:33` — applies $20 to any bundle not in the 7-entry dict. Lightsail offers ~24 bundle types (Linux/Windows, Cache, Container, Database, Web App, Object Storage). Unmapped → invented $20
- **Recommended fix**: complete the dict from AWS list price OR fetch live

### L2-006 Static IP $3.65/month is correct but hardcoded string  [LOW / POSITIVE]
- Evidence: `services/lightsail.py:100` — matches AWS list (Lightsail unattached static IP: $0.005/hr × 730 = $3.65/mo). Same number as EIP
- **Status**: PASS — but should use `PricingEngine.get_eip_monthly_price()` (live)

### External validation

| Finding | Tool | Result |
|---|---|---|
| L2-001 | `mcp__aws-pricing-mcp-server__get_pricing_service_attributes("AmazonLightsail")` | returns `["countsAgainstQuota","dataTransferQuota","engine",...,"memory","vcpu",...]` — confirms no `instanceType` attribute |
| L2-001 | `mcp__aws-pricing-mcp-server__get_pricing` with `instanceType=nano_2_0` | `empty_results` — confirms the filter doesn't match Lightsail SKUs |

---

## L3 — Reporting findings

### L3-001 5 sources emitted but `optimization_descriptions` only covers 1 (`idle_instances`)  [MEDIUM]
- **Evidence**: shim `LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS` at `services/lightsail.py:15-21` defines only `idle_instances`. The other 4 sources (`oversized_instances`, `unused_static_ips`, `load_balancer_optimization`, `database_optimization`) lack descriptions
- **Recommended fix**: add 4 missing entries

---

## Recommended next steps

1. **[CRITICAL] L2-001** — Replace adapter's `get_instance_monthly_price("AmazonLightsail", ...)` with `get_lightsail_bundle_cost()` (shim helper). Live-PricingEngine path is structurally dead.
2. **[HIGH] L2-002** — Drop the $12 fallback; use bundle dict.
3. **[HIGH] L2-004** — Compute oversized savings from actual bundle delta, not 30%.
4. **[MEDIUM] L2-005** — Complete `_BUNDLE_COSTS` for all 24 Lightsail bundles, or fetch live with corrected filter.
5. **[MEDIUM] L3-001** — Add missing `optimization_descriptions`.
6. **[MEDIUM] L1-002 / L1-003** — Wrap shim call, add AccessDenied classification.
7. **[LOW] L1-001** — Drop the 2 prints.
