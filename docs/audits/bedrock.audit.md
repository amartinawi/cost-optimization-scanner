# Audit: bedrock Adapter

**Adapter**: `services/adapters/bedrock.py` (392 lines — self-contained)
**ALL_MODULES index**: 30
**Date**: 2026-05-15

---

## Verdict

| Layer | Verdict | Findings |
|-------|---------|----------|
| L1 Technical   | **FAIL** | 4 (1 CRITICAL, 1 HIGH, 2 MEDIUM) |
| L2 Calculation | **FAIL** | 5 (1 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW) |
| L3 Reporting   | **PASS** | 0 |
| **Overall**    | **FAIL** | CW Period invalid (every CW lookup fails silently) + KB savings overstate by assuming 100% idle + on-demand flat $3/M token wrong for most models |

**Notable**: scope-rule cleanup happened (line 278-282 — idle agents finding removed because "agents accrue no AWS charge"). Good.

---

## Pre-flight facts

- **Required clients**: `("bedrock", "cloudwatch")` — **omits `bedrock-agent`** (used at line 213, 259)
- **Flags**: `requires_cloudwatch=True` ✓, `reads_fast_mode=True` ✓
- **Sources**: 4 (`idle_provisioned_throughput`, `pt_breakeven_analysis`, `idle_knowledge_bases`, `idle_agents` empty)
- **Pricing**: 6 hardcoded PT-hourly + $0.20/OCU-hr + $0.000003/token

---

## L1 — Technical findings

### L1-001 `required_clients()` omits `bedrock-agent`  [HIGH]
- **Evidence**: line 310 returns `("bedrock", "cloudwatch")`; `_check_idle_knowledge_bases` (line 213) and `_check_idle_agents` (line 259) use `ctx.client("bedrock-agent")`
- **Recommended fix**: `("bedrock", "bedrock-agent", "cloudwatch")`

### L1-002 CloudWatch Period exceeds maximum  [CRITICAL]
- **Evidence**: lines 61, 83 — `period = CW_LOOKBACK_DAYS * 86400 = 2,592,000`. AWS CloudWatch `Period` max is 86400 for queries within 15 days, OR must be one of `60, 300, 3600` (and stay ≤86400). 2.6M-second period is rejected → every `get_metric_statistics` call returns an error → silently swallowed → both `_check_idle_pt` and `_check_pt_breakeven` produce zero recs
- **Why it matters**: same bug as Aurora L1-002. Adapter emits ZERO Bedrock PT recommendations on any account because the CW call always fails. Only Knowledge Bases path produces recs (which itself overstates — see L2-002)
- **Recommended fix**: set `Period=86400` and aggregate datapoints in code; OR `Period=3600` with multi-page aggregation

### L1-003 Adapter banner `print()` + 8 `except: pass` swallows  [MEDIUM]
- Lines 49, 53, 75, 99, 114, 214, 225, 229, 260, 271, 275 — multiple silent swallows
- **Recommended fix**: route through `ctx.warn` / `ctx.permission_issue`

### L1-004 `_check_idle_agents` retained but returns empty  [MEDIUM]
- **Evidence**: line 255-283 — function still calls `bedrock-agent list_agents` (paginated + retry) but discards result. Wasted API calls
- **Recommended fix**: stub function to `return []` immediately

---

## L2 — Calculation findings

### Source-classification table

| Recommendation | Source | Evidence | Acceptable? | Notes |
|---|---|---|---|---|
| Idle PT | `module-const` (`PT_HOURLY_PRICE[model_id]` × units × 730 × multiplier) | line 141 | **WARN** — hardcoded 5-model dict; PT_HOURLY_DEFAULT=$1.0 for unmapped models |
| PT breakeven | `derived` (`pt_monthly - od_estimate`) where od = `tokens × $0.000003` | line 185-190 | **FAIL** — flat $3/M token wrong; Bedrock OD varies $0.25-$75/M tokens by model |
| Idle KB | `module-const` (`$0.20 × 730 × multiplier`) | line 236 | **FAIL** — assumes 100% idle for every KB; emits rec without checking utilization |
| Idle Agents | scope-rule REMOVED | line 278 | YES |

### L2-001 KB savings overstate by assuming 100% idle  [CRITICAL]
- **Evidence**: line 236-237 — `monthly_cost = KB_OCU_HOURLY × HOURS_PER_MONTH × multiplier = $146/month/KB`. The rec text at line 247-248 says "may have idle OCU hours — estimated if fully idle". But adapter emits the full $146 as `monthly_savings` regardless
- **Why it matters**: an account with 10 active KBs reports `$1,460/month savings` — fictitious. KB OCU pricing scales with actual query/ingest activity, not constant 730 hr/month
- **Recommended fix**: query CW `KnowledgeBaseQueryCount` or `IngestionJobCount` per KB; emit rec only if zero queries in 30 days; compute savings from actual idle-time × OCU rate

### L2-002 On-demand $3/M token assumed for all models  [HIGH]
- **Evidence**: line 185 — `od_monthly_estimate = (input_tokens + output_tokens) * 0.000_003`. AWS Bedrock on-demand:
  - Claude 3 Haiku: $0.25 input / $1.25 output per 1M tokens
  - Claude 3.5 Sonnet: $3 / $15 per 1M tokens
  - Claude 3 Opus: $15 / $75 per 1M tokens
  - Titan Text Lite: $0.30 / $0.40 per 1M tokens
- The flat $0.000003/token ($3/M, single bucket for input+output) is mid-range Sonnet pricing; underestimates Opus by 5×, overestimates Haiku by 4×
- **Recommended fix**: model-specific input vs output token rates from AWS pricing page

### L2-003 PT hourly prices need live verification  [HIGH]
- **Evidence**: lines 18-24 — hardcoded values for 5 models. Need MCP validation
- **Recommended fix**: live AWS Pricing API via `mcp__aws-pricing-mcp-server__get_pricing` for `AmazonBedrock`

### L2-004 `PT_HOURLY_DEFAULT = 1.0` for unmapped models  [MEDIUM]
- **Evidence**: line 25, 140. Bedrock supports dozens of foundation models; the 5-entry dict covers the most popular but `$1/hour` default underestimates Opus ($21.50/hr) and overestimates lite models
- **Recommended fix**: use `mcp__aws-pricing-mcp-server__get_bedrock_patterns` (server-mandated for Bedrock per MCP instructions) for accurate dispatch

### L2-005 `pricing_multiplier` correctly applied to module-const path  [LOW / POSITIVE]
- **Evidence**: lines 141, 236 — `* pricing_multiplier` on hardcoded constants
- **Status**: PASS

---

## L3 — Reporting findings

**None** — strong `stat_cards`, `grouping`, descriptions.

---

## Recommended next steps

1. **[CRITICAL] L1-002** — Fix CW Period (set to 86400 max, aggregate in code).
2. **[CRITICAL] L2-001** — Query CW for KB utilization; don't assume 100% idle.
3. **[HIGH] L1-001** — Add `"bedrock-agent"` to `required_clients()`.
4. **[HIGH] L2-002** — Model-specific input/output token pricing.
5. **[HIGH] L2-003** — Validate PT hourly prices via Pricing API + Bedrock patterns MCP.
6. **[MEDIUM] L2-004** — Use Bedrock patterns MCP for model-specific defaults.
7. **[MEDIUM] L1-003 / L1-004** — Route prints through `ctx.warn`; stub `_check_idle_agents`.
