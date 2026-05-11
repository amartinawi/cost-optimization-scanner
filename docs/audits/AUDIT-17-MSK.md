# AUDIT-17: MSK (Managed Streaming for Kafka) Service Adapter

**Date:** 2026-05-01  
**Auditor:** Automated Audit  
**Adapter File:** `services/adapters/msk.py`  
**Legacy Logic File:** `services/msk.py`

---

## Executive Summary

The MSK adapter uses **EC2 proxy pricing** instead of actual MSK broker pricing, does **not include storage costs**, and applies an **arbitrary 30% savings rate** that is not grounded in MSK pricing mechanics.

**VERDICT: P2-FAIL** - Significant pricing inaccuracies require correction.

---

## 1. Code Analysis

### 1.1 Adapter Structure
```python
# services/adapters/msk.py lines 39-46
for rec in recs:
    instance_type = rec.get("InstanceType")
    num_brokers = rec.get("NumberOfBrokerNodes", 3)
    if instance_type:
        hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type)
        monthly_cluster = hourly * 730 * num_brokers
        savings += monthly_cluster * 0.30
```

### 1.2 Issues Identified

| Line | Issue | Severity |
|------|-------|----------|
| 44 | Uses `get_ec2_hourly_price()` instead of MSK broker pricing | **Critical** |
| 46 | Arbitrary 30% multiplier without MSK-specific justification | **High** |
| 46 | Storage costs completely omitted | **High** |

---

## 2. Pricing Analysis

### 2.1 MSK vs EC2 Pricing Comparison (eu-west-1)

| Instance Type | MSK Broker Price/hr | EC2 Price/hr | MSK Premium |
|---------------|---------------------|--------------|-------------|
| kafka.m5.large | **$0.234** | $0.107 | **2.19x** |
| kafka.m5.xlarge | **$0.468** | $0.214 | **2.19x** |
| kafka.m7g.large | **$0.2275** | ~$0.10 | **2.28x** |
| kafka.m7g.xlarge | **$0.4548** | ~$0.20 | **2.27x** |

**Finding:** MSK charges approximately **2.2x the EC2 price** for the same underlying instance type. Using EC2 pricing dramatically **underestimates** MSK costs by ~55%.

### 2.2 Storage Pricing (Not Included)

According to AWS MSK pricing documentation:
- MSK Standard brokers charge for **EBS storage in GB-months**
- Storage is a significant cost component (typically $0.10-$0.12/GB-month for gp3)
- The adapter completely omits storage costs in its calculations

**Example Impact:**
- 3 brokers × 1000 GB each = 3000 GB-month
- At $0.10/GB-month = **$300/month storage cost** (not counted)

---

## 3. The 30% Savings Rate Problem

### 3.1 Current Implementation
```python
savings += monthly_cluster * 0.30  # Arbitrary 30% flat rate
```

### 3.2 Analysis

**Question:** Where does 30% come from?

**Answer:** The adapter uses a flat 30% rate without MSK-specific justification:

1. **MSK does NOT offer Reserved Instances** - Unlike EC2/RDS, MSK has no RI program
2. **Rightsizing is the primary optimization** - Moving from larger to smaller broker types
3. **Serverless migration** - Converting provisioned to MSK Serverless (variable savings)
4. **Storage optimization** - gp2→gp3 migration (~20% savings)

**Realistic Savings Potential:**
- **Rightsizing (one size down):** ~50% compute savings (e.g., m5.xlarge → m5.large)
- **Storage (gp2→gp3):** ~20% storage savings
- **Serverless conversion:** Variable (can be higher or lower depending on usage)

**Verdict:** The 30% flat rate is **not grounded in MSK pricing mechanics** and will produce inaccurate estimates.

---

## 4. Pass Criteria Assessment

| Criterion | Status | Notes |
|-----------|--------|-------|
| Uses MSK-specific pricing | ❌ FAIL | Uses EC2 proxy pricing |
| Includes storage costs | ❌ FAIL | Storage omitted entirely |
| Savings rate justified | ❌ FAIL | Arbitrary 30% rate |
| Recommendation context accurate | ✅ PASS | Recommendations are reasonable |
| No code errors | ✅ PASS | Code executes without errors |

---

## 5. Detailed Issues

### Issue #1: EC2 Proxy Pricing (CRITICAL)
**Location:** `services/adapters/msk.py:44`
```python
hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type)
```

**Problem:** MSK brokers cost ~2.2x more than equivalent EC2 instances. Using EC2 pricing significantly underestimates actual costs.

**Impact:** For a 3-node kafka.m5.large cluster:
- Adapter calculates: $0.107 × 730 × 3 = **$234.33/month**
- Actual MSK cost: $0.234 × 730 × 3 = **$512.46/month**
- **Underestimation: $278.13/month (54%)**

### Issue #2: Missing Storage Costs (HIGH)
**Location:** `services/adapters/msk.py`

**Problem:** The adapter only calculates broker hourly costs and ignores EBS storage pricing entirely.

**Evidence:** AWS documentation states MSK charges:
> "a charge for the amount of storage provisioned in the cluster, calculated in GB-months"

**Impact:** Storage can be 30-50% of total MSK cost for high-volume clusters.

### Issue #3: Arbitrary 30% Savings Rate (HIGH)
**Location:** `services/adapters/msk.py:46`
```python
savings += monthly_cluster * 0.30
```

**Problem:** 
- MSK has no Reserved Instance program
- Rightsizing (the main optimization) can yield 50%+ savings for one size reduction
- 30% is not based on actual optimization potential

---

## 6. Recommendations

### 6.1 Short-Term Fixes

1. **Use MSK Broker Pricing**
   - Add `get_msk_broker_hourly_price(instance_type)` to PricingEngine
   - Use `AmazonMSK` service code instead of `AmazonEC2`

2. **Add Storage Cost Calculation**
   - Query `VolumeSize` from cluster info (already available in line 61)
   - Add EBS storage pricing at ~$0.10/GB-month

3. **Revise Savings Methodology**
   - Replace flat 30% with rightsizing-based calculation
   - Calculate savings as: `(current_instance_price - target_instance_price) × hours × brokers`

### 6.2 Example Corrected Calculation

```python
# Current (incorrect)
hourly = ctx.pricing_engine.get_ec2_hourly_price("kafka.m5.large")  # $0.107
savings = hourly * 730 * 3 * 0.30  # $70.30/month

# Corrected
msk_hourly = ctx.pricing_engine.get_msk_broker_hourly_price("kafka.m5.large")  # $0.234
target_hourly = ctx.pricing_engine.get_msk_broker_hourly_price("kafka.m5.large")  # Next size down
storage_cost = volume_size * 0.10  # EBS storage
actual_savings = (msk_hourly - target_hourly) * 730 * 3  # Real rightsizing savings
```

---

## 7. Verdict Codes

| Category | Code | Description |
|----------|------|-------------|
| **Overall** | **P2-FAIL** | Significant pricing inaccuracies require correction |
| Pricing Accuracy | E1-CRITICAL | Uses wrong pricing source (EC2 instead of MSK) |
| Completeness | E2-HIGH | Storage costs omitted |
| Savings Logic | E3-HIGH | Arbitrary 30% rate not justified |
| Code Quality | P1-PASS | No execution errors |

---

## 8. References

- [AWS MSK Pricing](https://aws.amazon.com/msk/pricing/)
- [MSK Broker Instance Sizes](https://docs.aws.amazon.com/msk/latest/developerguide/broker-instance-sizes.html)
- AWS Pricing API: `AmazonMSK` service code exists with broker-specific pricing
- Adapter method: `get_ec2_hourly_price()` in `core/pricing_engine.py`

---

## 9. Appendix: Pricing Data

### MSK Broker Pricing (eu-west-1, from AWS Pricing API)

| SKU | Instance Type | Price/Hour |
|-----|---------------|------------|
| 89K6M3Q8CNUUMRTV | kafka.m5.large | $0.2340 |
| 75HMKTEZEPX8UJT9 | kafka.m5.xlarge | $0.4680 |
| 5MAR6NZW479TGG67 | kafka.m7g.large | $0.2275 |
| 2JBW4Q6HH85RPBXS | kafka.m7g.xlarge | $0.4548 |

### EC2 Pricing (eu-west-1, for comparison)

| Instance Type | Price/Hour |
|---------------|------------|
| m5.large | $0.1070 |
| m5.xlarge | $0.2140 |

**MSK Premium Factor: 2.18x - 2.27x**

---

*End of Audit Report*
