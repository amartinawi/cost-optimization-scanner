# Cost Optimization Scanner — Cross-Service Audit Summary

**Date**: 2026-05-01  
**Scope**: 28 service adapters  
**Auditor**: Automated audit pipeline  

---

## Executive Summary

Of the 28 service adapters audited:

| Verdict | Count |
|---------|-------|
| PASS | 3 |
| CONDITIONAL-PASS | 1 |
| WARN | 22 |
| INFO | 1 |
| FAIL | 1 |

**Three adapters** (EBS, EC2, RDS) fully integrate with `PricingEngine` for live regional pricing and calculate savings from actual cost deltas. These represent the gold standard for the codebase.

**Twenty-two adapters** pass hard-coded or arbitrarily-derived savings percentages through the `ServiceFindings` pipeline. While the recommendation logic (detecting waste, idle resources, rightsizing opportunities) is sound, the dollar-amount savings are often estimates rather than computed from live pricing.

**One adapter** (API Gateway, AUDIT-19) fails because it uses a flat `$15/api` migration cost not sourced from CloudWatch or PricingEngine, and omits dollar amounts for caching savings entirely.

**One adapter** (DMS, AUDIT-25) uses the wrong AWS Price List service code (`AmazonDMS` instead of `AWSDatabaseMigrationSvc`), causing pricing lookups to silently fail.

**One adapter** (QuickSight, AUDIT-26) has a field mismatch between its service module and adapter — the adapter reads metadata fields the service module never populates.

---

## Service-by-Service Verdicts

| # | Service | Verdict | Pricing Source | Savings Basis | Key Issue |
|---|---------|---------|----------------|---------------|-----------|
| 01 | EBS | PASS | PricingEngine (live) | Computed from price delta | None — reference implementation |
| 02 | EC2 | PASS | PricingEngine (live) | Computed from rightsizing/scheduling | None — reference implementation |
| 03 | RDS | PASS | PricingEngine (live) | Instance-class cost lookup | None — reference implementation |
| 04 | S3 | WARN | PricingEngine + tier logic | Lifecycle savings approximate | Lifecycle $ savings not fully derived from pricing API |
| 05 | Network | WARN | PricingEngine (NAT) + flat (EIP/VPC) | Mixed live and flat-rate | EIP and VPC savings use static rates |
| 06 | Lambda | WARN | CloudWatch + simplified $/GB-s | Memory rightsizing | $/GB-second pricing simplified, ignores tier |
| 07 | DynamoDB | WARN | Partial RCU/WCU pricing | On-demand vs provisioned | RCU/WCU pricing incomplete |
| 08 | ElastiCache | WARN | PricingEngine (nodes) | Reserved/Graviton flat % | Reserved and Graviton savings are arbitrary % |
| 09 | OpenSearch | WARN | PricingEngine (instances) | UltraWarm/cold estimated | UltraWarm and cold storage savings not from API |
| 10 | Containers (ECS/EKS) | WARN | PricingEngine (Fargate) | Savings % arbitrary | Fargate pricing correct but savings % unsubstantiated |
| 11 | FileSystems (EFS) | WARN | PricingEngine (per-GB) | IA savings estimated | Infrequent Access savings not calculated |
| 12 | AMI | INFO | None | None | Detection only — no pricing or savings calculation |
| 13 | Monitoring | WARN | PricingEngine (metrics) | Dashboard/alarm flat-rate | Custom metrics pricing OK; dashboard savings flat |
| 14 | Glue | WARN | PricingEngine (DPU) | Bookmarks/session estimated | DPU pricing matches API; other savings guessed |
| 15 | Athena | CONDITIONAL-PASS | Hardcoded $5/TB (verified) | 75% savings undocumented | Hardcoded value is correct; savings % has no source |
| 16 | Redshift | WARN | PricingEngine (RA3 nodes) | Reserved/Spectrum flat % | Node pricing live; reserved/Spectrum savings flat % |
| 17 | MSK | WARN | PricingEngine (instances) | Broker optimization flat | Instance pricing live; broker savings arbitrary |
| 18 | CloudFront | WARN | PricingEngine (transfer) | Cache hit/L@Edge estimated | Transfer pricing correct; cache/Lambda@Edge guessed |
| 19 | API Gateway | FAIL | Flat $15 (not from CW/PE) | Caching savings missing $ | $15/REST→HTTP not sourced; caching has no dollar figure |
| 20 | Step Functions | WARN | Hardcoded $0.025/1K (verified) | 60% Standard→Express arbitrary | No PricingEngine; savings % unsubstantiated |
| 21 | WorkSpaces | WARN | Pricing API (mismatched fields) | Static $50/workspace | Bundle rightsizing empty; pricing field names wrong |
| 22 | Lightsail | WARN | 7 bundle types, $20 default | Flat monthly savings | Incomplete pricing coverage (7 of many bundles) |
| 23 | AppRunner | WARN | PricingEngine (vCPU/memory) | 30% rightsizing arbitrary | Dual-mode pricing not modeled; savings % flat |
| 24 | Transfer | WARN | PricingEngine (hourly rates) | Data transfer/connector omitted | Protocol rates correct; major cost categories missing |
| 25 | DMS | WARN | Wrong service code | No pricing_multiplier | `AmazonDMS` should be `AWSDatabaseMigrationSvc` |
| 26 | QuickSight | WARN | SPICE pricing OK | Field mismatch adapter↔module | Adapter reads fields service module never sets |
| 27 | MediaStore | WARN | S3-equivalent pricing | Ingest/API fees omitted | Flat $20 fallback; incomplete cost model |
| 28 | Batch | WARN | PricingEngine (EC2 hourly) | 30% contradicts 60-90% claim | Flat multiplier inconsistent with claim text |

---

## Cross-Service Findings

### X1: pricing_multiplier Consistency

The `pricing_multiplier` in `ScanContext` is intended to scale all savings by a regional cost factor.

**Findings**:
- EBS, EC2, RDS apply it correctly through `PricingEngine`
- ElastiCache, Redshift, MSK, Batch reference it for base pricing but then apply flat % for savings
- **DMS (AUDIT-25) does not apply `pricing_multiplier` at all** — savings will be wrong for non-us-east-1 regions
- Network adapter applies it to NAT Gateway savings but not EIP/VPC savings

**Impact**: Regional deployments (eu-west-1, ap-southeast-1, etc.) will show inaccurate savings for ~12 adapters.

### X2: PricingEngine Coverage

| Category | Adapters | Count |
|----------|----------|-------|
| Full PricingEngine (live) | EBS, EC2, RDS | 3 |
| PricingEngine + flat savings | ElastiCache, OpenSearch, Redshift, MSK, CloudFront, Containers, Glue, Monitoring, FileSystems, AppRunner, Transfer, Batch | 12 |
| Hardcoded (verified correct) | Athena ($5/TB), Step Functions ($0.025/1K) | 2 |
| Hardcoded (unverified/flat) | API Gateway ($15), WorkSpaces ($50), Lightsail ($20), MediaStore ($20) | 4 |
| Wrong service code | DMS | 1 |
| Field mismatch | QuickSight | 1 |
| No pricing needed | AMI | 1 |
| Partial/missing | DynamoDB, Lambda, S3, Network | 4 |

**46% of adapters (13/28)** derive at least some savings from hard-coded percentages rather than computed cost deltas.

### X3: Savings Overflow Risk

Several adapters could report savings exceeding actual spend:

1. **API Gateway**: `$15/api × N apis` could exceed actual monthly spend for low-traffic APIs
2. **AppRunner**: 30% rightsizing savings applied uniformly regardless of actual utilization
3. **Batch**: 30% flat savings contradicts the 60-90% Spot savings claim in recommendation text
4. **WorkSpaces**: Static `$50/workspace` may exceed actual bundle cost differentials
5. **Lightsail**: Default `$20/month` savings applied when bundle pricing is incomplete

### X4: Grand Total Consistency

The `ServiceFindings` → `ResultBuilder` → `HTMLReportGenerator` pipeline requires all adapters to:
1. Set `total_recommendations` count
2. Populate `estimated_savings` as a dollar figure
3. Return findings with consistent JSON shape

**Issues found**:
- **QuickSight (AUDIT-26)**: Adapter reads `user_type` and `session_type` fields the service module never sets — findings will have empty metadata
- **AMI (AUDIT-12)**: Sets `estimated_savings = 0` correctly (detection only), but `total_recommendations` counts unused AMIs which have no dollar value
- All other adapters correctly flow savings through the pipeline

### X5: Regional Pricing Accuracy

Hardcoded values were verified against `eu-west-1` pricing:

| Adapter | Hardcoded Value | Verified? | Delta |
|---------|----------------|-----------|-------|
| Athena | $5.00/TB scanned | Yes | $0 (global service) |
| Step Functions | $0.025/1K transitions | Yes | $0 (correct for us-east-1) |
| API Gateway | $15.00/api migration | No | N/A — not from any API |
| WorkSpaces | $50.00/workspace | Partial | Varies by bundle ±$20 |
| Lightsail | $20.00/month default | Partial | Only 7 of 20+ bundles |
| MediaStore | $20.00 flat fallback | No | Actual varies by region |

**Note**: Services using `PricingEngine` automatically get correct regional pricing. The 6 hardcoded adapters above will be wrong in non-us-east-1 regions unless `pricing_multiplier` compensates.

---

## Top 10 Critical Issues

| Rank | Severity | Service | Issue | Impact |
|------|----------|---------|-------|--------|
| 1 | **CRITICAL** | API Gateway (19) | Flat `$15/api` not sourced from CloudWatch or PricingEngine; caching savings have no dollar amount | Savings figures unreliable; FAIL verdict |
| 2 | **CRITICAL** | DMS (25) | Wrong AWS Price List service code (`AmazonDMS` vs `AWSDatabaseMigrationSvc`) | Pricing lookups silently fail; no pricing_multiplier applied |
| 3 | **HIGH** | QuickSight (26) | Adapter reads fields service module never populates | Findings contain empty metadata; misleading report |
| 4 | **HIGH** | Batch (28) | 30% flat savings contradicts 60-90% Spot claim in recommendation text | User-facing inconsistency erodes trust |
| 5 | **HIGH** | WorkSpaces (21) | Pricing API field name mismatches; `bundle_rightsizing` always empty | Rightsizing recommendations never generated |
| 6 | **HIGH** | Network (5) | EIP and VPC savings use flat rates instead of live pricing | Savings inaccurate for all regions |
| 7 | **MEDIUM** | 12 adapters | Savings derived from arbitrary percentages (30%, 40%, 60%, 75%) rather than computed deltas | Savings over/underestimated; cross-service total unreliable |
| 8 | **MEDIUM** | Transfer (24) | Major cost categories omitted (data transfer, connectors, Web App) | Significant costs invisible to scanner |
| 9 | **MEDIUM** | Lightsail (22) | Only 7 of 20+ bundle types mapped; $20 default for unmapped | Many instances get wrong savings estimate |
| 10 | **LOW** | DynamoDB (7) | RCU/WCU pricing incomplete for provisioned capacity | Provisioned vs on-demand comparison partially accurate |

---

## Recommendations

### Priority 1 — Fix Broken Adapters

1. **API Gateway**: Source REST→HTTP migration cost from CloudWatch metrics (actual request counts × price difference). Add caching savings calculation from cache hit rate metrics.

2. **DMS**: Change service code from `AmazonDMS` to `AWSDatabaseMigrationSvc`. Add `pricing_multiplier` to savings calculation. Broaden instance filter beyond just "large" instances.

3. **QuickSight**: Align adapter field names with service module output. Remove reads of `user_type` and `session_type` if service module cannot provide them.

### Priority 2 — Convert Flat Savings to Computed Savings

4. **Batch**: Replace 30% flat multiplier with actual Spot vs On-Demand price delta from PricingEngine. Align recommendation text with computed savings.

5. **WorkSpaces**: Fix pricing API field names. Implement actual bundle cost comparison for rightsizing instead of static $50/workspace.

6. **Network**: Source EIP and VPC costs from PricingEngine instead of flat rates.

7. **AppRunner**: Compute actual rightsizing savings from utilization metrics instead of flat 30%.

### Priority 3 — Expand Pricing Coverage

8. **Transfer**: Add data transfer, SFTP connector, and Web App cost models.

9. **Lightsail**: Map all available bundle types from AWS pricing API.

10. **MediaStore**: Add ingest and API request pricing from PricingEngine; remove $20 flat fallback.

### Priority 4 — Improve Savings Accuracy Across All WARN Adapters

11. For the 12 adapters using flat savings percentages (ElastiCache, OpenSearch, Redshift, MSK, CloudFront, Containers, Glue, Monitoring, FileSystems, Athena, Step Functions, DynamoDB):
    - Replace `% savings` with actual cost delta calculations
    - Document the source/justification for any remaining percentages
    - Add integration tests verifying savings ≤ actual current spend

---

## Statistics

| Metric | Value |
|--------|-------|
| Total adapters audited | 28 |
| PASS | 3 (10.7%) |
| CONDITIONAL-PASS | 1 (3.6%) |
| WARN | 22 (78.6%) |
| INFO | 1 (3.6%) |
| FAIL | 1 (3.6%) |
| **Adapters using PricingEngine (full or partial)** | **18 (64.3%)** |
| **Adapters with hardcoded pricing** | **8 (28.6%)** |
| **Adapters with pricing bugs** | **2 (7.1%)** |
| Critical issues | 2 |
| High issues | 4 |
| Medium issues | 3 |
| Low issues | 1 |
| Total issues | 10 |
| Cross-service findings (X1-X5) | 5 |

### Savings Accuracy Distribution

```
Computed from live pricing  ████████████░░░░░░░░  3/28 (10.7%)
Partial live + flat %       ████████████████████  12/28 (42.9%)
Hardcoded (verified)        ████░░░░░░░░░░░░░░░░  2/28 (7.1%)
Hardcoded (unverified)      ████████░░░░░░░░░░░░  4/28 (14.3%)
Broken/missing              █████░░░░░░░░░░░░░░░  2/28 (7.1%)
Detection only              ██░░░░░░░░░░░░░░░░░░  1/28 (3.6%)
Partial/other               ████████░░░░░░░░░░░░  4/28 (14.3%)
```

---

## Methodology

Each audit report evaluated:
1. **Pricing source**: Whether adapter uses `PricingEngine`, hardcoded values, or AWS Price List API directly
2. **Savings basis**: Whether savings are computed from actual cost deltas or derived from flat percentages
3. **Edge cases**: How adapters handle missing pricing data, empty responses, and regional variations
4. **Pipeline integration**: Whether `ServiceFindings` → `ResultBuilder` flow is correct
5. **Regional accuracy**: Whether hardcoded values match actual AWS pricing in target regions

Cross-service checks (X1-X5) examined systemic patterns across all 28 adapters simultaneously.

---

*Generated from 28 individual audit reports (AUDIT-01 through AUDIT-28).*
