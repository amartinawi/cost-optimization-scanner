# Cost Optimization Scanner — Audit Action Plan

**Date:** 2026-05-01  
**Scope:** 28 service adapters (AUDIT-01 through AUDIT-28)  
**Status:** All audits read and validated

---

## Priority 0 — Critical Bugs (Wrong Numbers / Silent Failures)

These produce materially wrong output or completely broken pricing paths. Fix before the next scan.

---

### P0-1 · DMS: Wrong service code + instance prefix + narrow detection
**Files:** `services/adapters/dms.py`, `services/dms.py`  
**Audit:** AUDIT-25 (38/100)

| # | Fix | Location |
|---|-----|----------|
| DMS-01 | Change `"AmazonDMS"` → `"AWSDatabaseMigrationSvc"` | `adapters/dms.py:40` |
| DMS-02 | Strip `dms.` prefix before pricing lookup: `instance_class.replace("dms.", "")` | `adapters/dms.py:~40` |
| DMS-03 | Remove `"large" in instance_class` filter — scan all running instances | `dms.py:47` |
| DMS-05 | Add `"cloudwatch"` to `required_clients()` return tuple | `adapters/dms.py:~20` |
| DMS-09 | Multiply savings by `ctx.pricing_multiplier` | `adapters/dms.py:42` |
| DMS-04 | Remove hardcoded `"$100/month potential"` — use computed savings | `dms.py:88` |

---

### P0-2 · DynamoDB: Key mismatch makes pricing path dead code
**Files:** `services/adapters/dynamodb.py`, `services/dynamodb.py`  
**Audit:** AUDIT-07 (FAIL)

| # | Fix | Location |
|---|-----|----------|
| DYN-01 | Read `rcu = rec.get("ReadCapacityUnits", 0)` and `wcu = rec.get("WriteCapacityUnits", 0)` directly (not from nested `ProvisionedThroughput`) | `adapters/dynamodb.py:51-54` |
| DYN-02 | Fix shim constants: `_PROVISIONED_RCU_COST = 0.107`, `_PROVISIONED_WCU_COST = 0.537` (currently 2.33× actual) | `dynamodb.py:70-71` |
| DYN-03 | Update adapter hourly constants: `DYNAMODB_RCU_HOURLY = 0.000147`, `DYNAMODB_WCU_HOURLY = 0.000735` (currently 11.6% low) | `adapters/dynamodb.py:47-48` |
| DYN-04 | Populate `EstimatedMonthlyCost` for on-demand tables in shim (currently stays 0) | `dynamodb.py:~122` |
| DYN-05 | Apply `ctx.pricing_multiplier` in savings calculation | `adapters/dynamodb.py:~57` |

---

### P0-3 · Monitoring: CloudWatch log cost uses $0.03 instead of $0.57 (19× error)
**Files:** `services/monitoring.py`  
**Audit:** AUDIT-13 (FAIL on this check)

| # | Fix | Location |
|---|-----|----------|
| MON-01 | Change log ingestion rate from `0.03` to `0.57` in `EstimatedSavings` f-string | `monitoring.py:64` |

```python
# FROM:
"EstimatedSavings": f"${stored_bytes * 0.03 / (1024**3):.2f}/month"
# TO:
"EstimatedSavings": f"${stored_bytes * 0.57 / (1024**3):.2f}/month"
```

---

### P0-4 · S3: `total_monthly_savings` = current cost, not savings delta
**Files:** `services/adapters/s3.py`, `services/s3.py`  
**Audit:** AUDIT-04 (CONDITIONAL PASS)

| # | Fix | Location |
|---|-----|----------|
| S3-F001 | Sum `(current_cost - projected_optimized_cost)` per bucket, not `EstimatedMonthlyCost` (which is the current cost) | `adapters/s3.py:42` |
| S3-F002 | Add lifecycle transition savings: `size_gb × (STANDARD_price - target_class_price)` for buckets without lifecycle | `s3.py:562-761` |

---

### P0-5 · MSK: EC2 proxy pricing underestimates actual MSK cost by 2.2×
**Files:** `services/adapters/msk.py`, `core/pricing_engine.py`  
**Audit:** AUDIT-17 (P2-FAIL)

| # | Fix | Location |
|---|-----|----------|
| MSK-01 | Add `get_msk_broker_hourly_price(instance_type)` to PricingEngine using `AmazonMSK` service code | `pricing_engine.py` |
| MSK-02 | Replace `get_ec2_hourly_price()` with `get_msk_broker_hourly_price()` in adapter | `adapters/msk.py:44` |
| MSK-03 | Add broker EBS storage cost: `volume_size × 0.10/GB/month` | `adapters/msk.py` |

> **Impact:** 3-node kafka.m5.large: adapter currently calculates $234/mo; actual MSK cost is $513/mo. Savings estimates are off by 54%.

---

### P0-6 · Lambda: Keyword matching is dead code for 4 of 6 check types
**Files:** `services/adapters/lambda_svc.py`  
**Audit:** AUDIT-06 (FAIL)

| # | Fix | Location |
|---|-----|----------|
| LAM-01 | Replace keyword matching with function-level savings calculation using Lambda pricing constants | `adapters/lambda_svc.py:43-50` |
| LAM-02 | Add dedup by `FunctionName` between Cost Hub and enhanced checks | `adapters/lambda_svc.py` |

Dead keyword paths currently:
- `"memory optimization"` → never emitted by shim → $10 branch is dead code
- `"20% cost reduction with ARM architecture"` → $0 (no match)
- `"Reduce ENI costs"` → $0 (no match)
- `"Review actual concurrency needs"` → $0 (no match)

Suggested constants:
```python
LAMBDA_PRICE_PER_GB_SEC_X86 = 0.0000166667
LAMBDA_PRICE_PER_GB_SEC_ARM = 0.0000133334
LAMBDA_PRICE_PER_1K_REQUESTS = 0.0000002
```

---

### P0-7 · QuickSight: Field mismatch — adapter reads fields service module never sets
**Files:** `services/adapters/quicksight.py`, `services/quicksight.py`  
**Audit:** AUDIT-26 (WARN 45/100, from SUMMARY)

| # | Fix | Location |
|---|-----|----------|
| QS-01 | Service module must set `UnusedSpiceCapacityGB` and `Edition` in recommendation dicts — or adapter must be updated to read the fields actually set | `services/quicksight.py` + `adapters/quicksight.py` |

---

### P0-8 · ElastiCache: Multiple FAIL-level inaccuracies
**Files:** `services/adapters/elasticache.py`, `services/elasticache.py`  
**Audit:** AUDIT-08 (FAIL)

| # | Fix | Location |
|---|-----|----------|
| EC-01 | Fix Graviton rate: `0.25` → `0.05` (actual r5→r6g price delta is 5%, not 25%) | `adapters/elasticache.py:45-46` |
| EC-02 | Fix Valkey rate: `0.15` → `0.20` (actual 20% OD discount confirmed by pricing API) | `adapters/elasticache.py:47-48` |
| EC-03 | Fix Valkey recommendation text: "Same pricing as Redis" → "Valkey is ~20% cheaper than Redis" | `services/elasticache.py:84-85` |
| EC-04 | Fix Reserved fallback: `$200/node` → `~$60/node` (currently exceeds monthly instance cost of $167) | `adapters/elasticache.py:61-62` |
| EC-05 | Add `cacheEngine` filter to `_fetch_generic_instance_price()` for ElastiCache to avoid Valkey SKU contamination | `pricing_engine.py:414-422` |

---

## Priority 1 — High Impact Fixes

---

### P1-1 · Lightsail: Field name mismatch + outdated bundle costs
**Files:** `services/adapters/lightsail.py`, `services/lightsail.py`  
**Audit:** AUDIT-22 (52/100)

| # | Fix | Location |
|---|-----|----------|
| LS-01 | Change `rec.get("BundleName", rec.get("bundleName", ""))` → `rec.get("BundleId", "")` | `adapters/lightsail.py:38` |
| LS-02 | Update bundle costs for eu-west-1 actual prices (27% delta for micro-large): micro $5→$6.86, small $10→$13.72, medium $20→$27.45, large $40→$54.90 | `lightsail.py:29-41` |
| LS-03 | Fix static IP price: `"$5.00/month"` → `"$3.65/month"` ($0.005/hr × 730) | `lightsail.py:101` |

---

### P1-2 · Batch: No Fargate distinction + savings mismatch
**Files:** `services/adapters/batch.py`, `services/batch_svc.py`  
**Audit:** AUDIT-28 (68/100)

| # | Fix | Location |
|---|-----|----------|
| BAT-01 | Detect Fargate vs EC2: check `computeEnvironmentType` or `instanceTypes` contains "FARGATE"; skip Graviton checks for Fargate CEs | `batch_svc.py:30-50` |
| BAT-02 | Add Fargate Spot recommendation with 70% savings for Fargate CEs | `batch_svc.py` |
| BAT-03 | Use type-specific savings multipliers: Spot=0.70, Graviton=0.05-0.10 (not flat 0.30) | `adapters/batch.py:44` |
| BAT-04 | Apply `ctx.pricing_multiplier` to savings | `adapters/batch.py:44` |

---

### P1-3 · WorkSpaces: Pricing filter broken (always falls back to flat rate)
**Files:** `services/adapters/workspaces.py`, `services/workspaces.py`  
**Audit:** AUDIT-21 (45/100)

| # | Fix | Location |
|---|-----|----------|
| WS-01 | Replace `instanceType` filter with `usagetype` pattern: `{region_prefix}-AW-HW-{bundle_id}` | `pricing_engine.py` or `adapters/workspaces.py` |
| WS-02 | Add `ComputeTypeName` → bundle ID mapping: `{"STANDARD": "2", "PERFORMANCE": "3", "POWER": "8", "POWERPRO": "19"}` | `adapters/workspaces.py` |
| WS-03 | Implement `bundle_rightsizing` check (currently empty placeholder) | `workspaces.py` |

---

### P1-4 · AMI: `unused_amis` computed but never returned
**Files:** `services/ami.py`, `services/adapters/ami.py`  
**Audit:** AUDIT-12 (62/100)

| # | Fix | Location |
|---|-----|----------|
| AMI-01 | Include `unused_amis` in return: `{"recommendations": checks["old_amis"] + checks["unused_amis"], ...}` | `ami.py:128` |
| AMI-02 | Add `"autoscaling"` to `required_clients()` | `adapters/ami.py:24` |

---

### P1-5 · Step Functions: Wrong billing unit (executions vs transitions)
**Files:** `services/adapters/step_functions.py`  
**Audit:** AUDIT-20 (WARN)

| # | Fix | Location |
|---|-----|----------|
| SF-01 | Multiply `monthly_executions` by average state count (minimum: fetch definition and count states; conservative fallback: `AVG_STATES = 5`) | `adapters/step_functions.py:55-72` |
| SF-02 | Only recommend Standard→Express migration when `state_count > 25` AND `avg_duration_sec < 60` (otherwise Express is more expensive) | `adapters/step_functions.py` |

> **Impact:** For a 10-state workflow, current code underestimates transitions by 90%.

---

### P1-6 · MediaStore: Storage size never passed, always uses flat $20
**Files:** `services/mediastore.py`, `services/adapters/mediastore.py`  
**Audit:** AUDIT-27 (62/100)

| # | Fix | Location |
|---|-----|----------|
| MED-01 | Set `EstimatedStorageGB` from CloudWatch `BucketSizeBytes` metric in `get_enhanced_mediastore_checks()` | `mediastore.py:45-68` |
| MED-02 | Add ingest optimization cost: `$0.02/GB × monthly_ingest_gb` from `BytesUploaded` metric | `mediastore.py` |
| MED-04 | Align hardcoded `"$25/month"` in recommendation with calculated savings | `mediastore.py:60` |

---

### P1-7 · Redshift: 35% rate contradicts documented 24%; fallback ignores multiplier
**Files:** `services/adapters/redshift.py`  
**Audit:** AUDIT-16 (PARTIAL)

| # | Fix | Location |
|---|-----|----------|
| RED-01 | Change `0.35` → `0.24` (or split: `0.30` for 1-year No Upfront, `0.24` conservative) | `adapters/redshift.py:45` |
| RED-02 | Apply `ctx.pricing_multiplier` to flat fallback: `200 * ctx.pricing_multiplier` | `adapters/redshift.py:47-49` |

---

## Priority 2 — Medium Impact Improvements

---

### P2-1 · API Gateway: Add CloudWatch-based savings calculation
**Files:** `services/adapters/api_gateway.py`, `services/api_gateway.py`  
**Audit:** AUDIT-19 (FAIL)

| # | Fix | Location |
|---|-----|----------|
| AG-01 | Add `"cloudwatch"` to `required_clients()` | `adapters/api_gateway.py` |
| AG-02 | Query CloudWatch `Count` metric per REST API for 30 days; compute savings: `(REST_price - HTTP_price) × monthly_requests` | `api_gateway.py` |
| AG-03 | Replace `savings = 15 * len(recs)` with metric-based calculation | `adapters/api_gateway.py:41-43` |

---

### P2-2 · CloudFront: $25/rec flat rate is fundamentally wrong
**Files:** `services/adapters/cloudfront.py`, `services/cloudfront.py`  
**Audit:** AUDIT-18 (58/100)

| # | Fix | Location |
|---|-----|----------|
| CF-01 | Replace flat `$25 × len(recs)` with `BytesDownloaded` × regional price differential | `adapters/cloudfront.py:~27` |
| CF-03 | Disabled distributions already cost $0 — fix recommendation text | `cloudfront.py:74-85` |
| CF-04 | Only flag Origin Shield if cache hit ratio < threshold or origin request rate is high | `cloudfront.py:87-115` |

---

### P2-3 · OpenSearch: Storage costs omitted; RI rate optimistic
**Files:** `services/adapters/opensearch.py`, `core/pricing_engine.py`  
**Audit:** AUDIT-09 (65/100)

| # | Fix | Location |
|---|-----|----------|
| OS-01 | Add EBS storage cost to OpenSearch cost calculation (storage can be 10% of instance cost) | `adapters/opensearch.py` |
| OS-02 | Reduce RI rate from `0.40` to `0.35` (1-year No Upfront is 31%, 3-year is 48%) | `adapters/opensearch.py:27` |

---

### P2-4 · AppRunner: 24/7 active assumption overestimates 4-5×
**Files:** `services/adapters/apprunner.py`  
**Audit:** AUDIT-23 (40/100)

| # | Fix | Location |
|---|-----|----------|
| AP-01 | Model provisioned state: `monthly = memory_gb × $0.007 × 730 + (vcpus × $0.064 + mem × $0.007) × estimated_active_hours` | `adapters/apprunner.py:~34` |
| AP-02 | Replace arbitrary 30% with 10-15% conservative or CloudWatch-based actual | `adapters/apprunner.py:35` |

---

### P2-5 · Transfer: Savings assumes 100% protocol removal
**Files:** `services/adapters/transfer.py`  
**Audit:** AUDIT-24 (65/100)

| # | Fix | Location |
|---|-----|----------|
| TRANS-01 | Fix savings calc: `(num_protocols - 1) × $0.30 × 730` not `num_protocols × $0.30 × 730` | `adapters/transfer.py:46` |
| TRANS-02 | Add data transfer cost awareness via CloudWatch `BytesUploaded`/`BytesDownloaded` | `transfer_svc.py` |

---

### P2-6 · FileSystems: FSx has no live pricing; EFS fallback outdated
**Files:** `services/efs_fsx.py`, `core/pricing_engine.py`  
**Audit:** AUDIT-11 (PARTIAL)

| # | Fix | Location |
|---|-----|----------|
| EFS-01 | Update `FALLBACK_EFS_GB_MONTH = 0.30` → `0.33` | `pricing_engine.py` |
| EFS-02 | The 30% flat savings is overly conservative (73% achievable with 20/80 Standard/IA split) — either document or fix | `adapters/file_systems.py:52-66` |
| FSX-01 | Replace hardcoded FSx prices with PricingEngine calls (Lustre HDD is `$0.040` hardcoded vs `$0.025` actual — 60% high; Lustre SSD `$0.145` vs `$0.154+` actual) | `efs_fsx.py:152-164` |

---

## Priority 3 — Pricing Engine Constants

One-line fixes in `core/pricing_engine.py`:

| Constant | Current | Should Be | Delta |
|----------|---------|-----------|-------|
| `FALLBACK_NAT_MONTH` | `32.00` | `35.04` | −8.7% |
| `FALLBACK_VPC_ENDPOINT_MONTH` | `7.30` | `8.03` | −9.1% |
| `FALLBACK_ALB_MONTH` | `16.20` | `20.44` | −20.7% |
| `FALLBACK_EFS_GB_MONTH` | `0.30` | `0.33` | −9.1% |

---

## Priority 4 — Low Impact / Documentation

These are non-blocking but should be addressed for correctness.

| Service | Issue | Action |
|---------|-------|--------|
| Athena | 75% savings factor undocumented | Add comment citing AWS example (actual benchmarks show 70-91%) |
| Athena | Fallback `$50` not labeled as estimate | Add `"is_estimate": True` flag when falling back |
| Glue | 160 hours/month assumption undocumented | Add comment: `# 8 hrs/day × 20 workdays` |
| Glue | $316/endpoint dev endpoint calc unclear | Document or compute from actual DPU config |
| Containers | No ARM/Graviton pricing (20% cheaper) | Add `cpuArchitecture` check, ARM pricing constants |
| Redshift | RA3 managed storage costs omitted | Add RMS query for ra3.* node types ($0.024/GB/mo) |
| AMI | Snapshot cost uses full volume size (not incremental) | Document limitation |
| AMI | CreationDate parsing fragile (single strptime format) | Use `dateutil.parser.parse()` or handle `+00:00` suffix |
| Step Functions | Docstring claims "state transitions" but uses executions metric | Update docstring after fixing billing unit |
| Monitoring | CloudTrail savings strings non-parseable | Convert to `"$X.XX/month"` format |
| Monitoring | Missing first-trail-free logic for CloudTrail | Skip the first trail in paid trail detection |
| MSK | Serverless migration has no cost comparison | Add DCU-hour pricing lookup for comparison |
| DMS | No Multi-AZ detection | Flag Multi-AZ instances and recommend single-AZ for dev/test |

---

## Summary Table

| Priority | Count | Services |
|----------|-------|---------|
| P0 — Critical bugs | 8 | DMS, DynamoDB, Monitoring (CW logs), S3, MSK, Lambda, QuickSight, ElastiCache |
| P1 — High impact | 7 | Lightsail, Batch, WorkSpaces, AMI, Step Functions, MediaStore, Redshift |
| P2 — Medium impact | 6 | API Gateway, CloudFront, OpenSearch, AppRunner, Transfer, FileSystems |
| P3 — Constants | 4 | core/pricing_engine.py (NAT, VPC Endpoint, ALB, EFS) |
| P4 — Documentation | 12 | Various |

---

## Regression Gate

After each batch of fixes:
```bash
pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v
```

Both must pass. If `test_regression_snapshot` fails due to JSON shape change, confirm with user before updating golden file.

---

## What's Already PASS (No Action Needed)

| Service | Audit | Notes |
|---------|-------|-------|
| EBS | AUDIT-01 | Reference implementation — PricingEngine + live pricing |
| EC2 | AUDIT-02 | Reference implementation — PricingEngine + CloudWatch |
| RDS | AUDIT-03 | Reference implementation — PricingEngine |
| Athena | AUDIT-15 | CONDITIONAL-PASS — $5/TB verified, minor doc gaps only |
| Glue (DPU rate) | AUDIT-14 | $0.44/DPU-hour correct for all job types |
| Containers (Fargate) | AUDIT-10 | Fargate vCPU/memory constants exact match eu-west-1 |
| Route53 | AUDIT-13 | $0.50/zone exact match |
| CloudWatch metrics infra | AUDIT-13 | Parse logic correct, routing logic sound |
| MSK (detection logic) | AUDIT-17 | Recommendations are correct; only pricing method wrong |
| Transfer (protocol rate) | AUDIT-24 | $0.30/protocol-hour confirmed |

---

*Generated 2026-05-01 from all 28 AUDIT-* reports + SUMMARY.md*
