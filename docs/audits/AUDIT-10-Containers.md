# AUDIT-10: Containers (ECS/ECR/EKS) Service Adapter

**File:** `services/adapters/containers.py`  
**Date:** 2026-05-01  
**Auditor:** Automated via AWS Pricing API + Documentation  
**Verdict Codes:** PASS, FLAG, WARN, FAIL

---

## Executive Summary

| Check | Status | Severity |
|-------|--------|----------|
| Fargate Pricing Constants | **FLAG** | High |
| Fargate Spot Discount (70%) | **PASS** | — |
| Rightsizing Discount (30%) | **WARN** | Medium |
| ECR Storage Pricing | **PASS** | — |
| Code Quality | **WARN** | Low |

---

## 1. Fargate Pricing Constants (Lines 34-35)

### Adapter Code
```python
FARGATE_VCPU_HOURLY = 0.04048
FARGATE_MEM_GB_HOURLY = 0.004445
```

### AWS Pricing API Verification

| Region | vCPU/hr (Actual) | Memory/hr (Actual) | Adapter Value | Match |
|--------|------------------|-------------------|---------------|-------|
| eu-west-1 | $0.04048 | $0.004445 | $0.04048 / $0.004445 | ✅ **MATCH** |
| us-east-1 | $0.04048 | $0.004445 | — | — |

### Finding
The adapter uses **eu-west-1 pricing constants**. This is correct when the scanner runs in eu-west-1, but the code comment implies these are global values. The values match exactly with AWS's eu-west-1 Linux Fargate pricing:

- vCPU: `EU-Fargate-vCPU-Hours:perCPU` = $0.04048/hr
- Memory: `EU-Fargate-GB-Hours` = $0.004445/hr

### Regional Pricing Comparison

The pricing is **identical** between us-east-1 and eu-west-1 for Linux x86 Fargate:
- Both regions: vCPU $0.04048/hr
- Both regions: Memory $0.004445/hr

However, **ARM (Graviton) pricing differs**:
- eu-west-1 ARM vCPU: $0.03238/hr (vs $0.04048 x86)
- eu-west-1 ARM Memory: $0.00356/hr (vs $0.004445 x86)

**Verdict:** FLAG — While values are correct for eu-west-1, the adapter:
1. Only supports x86 pricing (no ARM/Graviton path)
2. Hardcodes values instead of using regional lookup
3. Relies on pricing_multiplier for regional adjustment, which is applied AFTER the base calculation

---

## 2. Fargate Spot Discount (Line 70)

### Adapter Code
```python
if "spot" in recommendation or "spot instances" in savings_str.lower():
    savings += monthly_ondemand * 0.70 if monthly_ondemand > 0 else 150.0 * ctx.pricing_multiplier
```

### AWS Documentation Verification

Per AWS documentation:
> "Spot Instances offer significant discounts compared to On-Demand Instances, with an **average savings of 70%** on compute costs."
> — AWS Well-Architected Framework, ADVCOST02-BP04

Fargate Spot provides similar discounts to EC2 Spot for interruption-tolerant workloads. The 70% figure is the documented **average** savings across all Spot usage.

**Verdict:** PASS — 70% discount is the AWS-documented average for Spot capacity.

---

## 3. Rightsizing Discount (Line 73-78)

### Adapter Code
```python
elif (
    "rightsizing" in savings_str.lower()
    or "rightsize" in recommendation
    or "over-provisioned" in recommendation
):
    savings += monthly_ondemand * 0.30 if monthly_ondemand > 0 else 75.0 * ctx.pricing_multiplier
```

### Analysis
The 30% discount for rightsizing is a **rule-of-thumb estimate** rather than an official AWS figure. AWS Compute Optimizer provides specific rightsizing recommendations but does not publish a standard discount percentage.

**Verdict:** WARN — 30% is a reasonable heuristic but:
1. Not an official AWS discount rate
2. Actual savings depend on specific task size reduction (e.g., 2 vCPU → 1 vCPU = 50% compute savings)
3. Should be documented as "estimated" or "heuristic"

---

## 4. ECR Storage Pricing

### AWS Pricing API Verification

```
Product: EU-TimedStorage-ByteHrs
Unit: GB-Mo
Price: $0.10 per GB-month of data storage
Location: EU (Ireland)
```

**Verdict:** PASS — ECR storage at $0.10/GB/month in eu-west-1 is correct.

---

## 5. Code Quality Issues

### 5.1 Dead Code: `savings_str` Dollar Extraction

**Lines:** 37, 73, 78  
**Pattern:** `savings_str` is assigned but used only for keyword matching, not for extracting dollar values.

```python
savings_str = rec.get("EstimatedSavings", "")  # Line 37 - Assigned but never parsed for $

# Line 73, 78 - Only used for keyword matching:
"rightsizing" in savings_str.lower()
"lifecycle" in savings_str.lower()
```

**Expected Behavior:** If `EstimatedSavings` contains a string like "$150/month", the code should extract the numeric value `$150` rather than ignoring it and calculating from task specs.

**Verdict:** WARN — This is intentional design (calculating from task specs rather than trusting string values), but should be documented.

### 5.2 Missing ARM/Graviton Support

The adapter only uses x86 Fargate pricing constants:
- vCPU: $0.04048 (x86)
- Memory: $0.004445 (x86)

ARM (Graviton) pricing is 20% cheaper:
- vCPU: $0.03238 (ARM) — **20% savings**
- Memory: $0.00356 (ARM) — **20% savings**

**Impact:** If tasks run on ARM, the adapter overestimates costs by ~20%.

**Verdict:** WARN — No platform detection (cpuArchitecture field not checked).

---

## 6. Calculation Formula Verification

### Monthly Cost Calculation (Lines 51-57)

```python
monthly_ondemand = (
    (task_vcpu * FARGATE_VCPU_HOURLY + task_mem_gb * FARGATE_MEM_GB_HOURLY)
    * 730              # Hours in month (365 * 24 / 12 = 730)
    * task_count       # Number of tasks
    * ctx.pricing_multiplier  # Regional adjustment
)
```

**Verification:**
- 730 hours/month is correct (365 days ÷ 12 months × 24 hours)
- Formula: (vCPU_cost + Memory_cost) × hours × tasks × regional_multiplier
- This matches AWS Fargate billing: per-vCPU-hour + per-GB-hour

**Verdict:** PASS — Formula is correct.

---

## 7. Recommendations

| Priority | Issue | Recommendation |
|----------|-------|----------------|
| **P1** | Regional pricing hardcoded | Add regional lookup table or fetch from AWS Pricing API at runtime |
| **P2** | No ARM support | Add platform detection and ARM pricing path |
| **P3** | Rightsizing % heuristic | Document that 30% is an estimate, not an AWS guarantee |
| **P4** | savings_str purpose | Add code comment explaining why string values are ignored |

---

## Appendix: Raw AWS Pricing Data

### eu-west-1 Linux Fargate (x86)
```
SKU: K4EXFQ5YFQCP98EN
Usage: EU-Fargate-vCPU-Hours:perCPU
Price: $0.04048/hr

SKU: UP5ETC3QSW2QZGEY
Usage: EU-Fargate-GB-Hours
Price: $0.004445/hr
```

### eu-west-1 Linux Fargate (ARM/Graviton)
```
SKU: KXC9CEUQGJQPNZPY
Usage: EU-Fargate-ARM-vCPU-Hours:perCPU
Price: $0.03238/hr (20% cheaper)

SKU: MGJ5EB5GSHR8QZDF
Usage: EU-Fargate-ARM-GB-Hours
Price: $0.00356/hr (20% cheaper)
```

### eu-west-1 ECR Storage
```
SKU: 4NWTEXUZBWQH8MDW
Usage: EU-TimedStorage-ByteHrs
Price: $0.10/GB-Mo
```

---

## Final Verdict

**Overall:** FLAG

The adapter functions correctly for eu-west-1 x86 Fargate workloads but has significant limitations:
1. ✅ Pricing constants match eu-west-1 AWS prices exactly
2. ✅ Spot discount (70%) matches AWS documented average
3. ✅ ECR pricing correct at $0.10/GB/month
4. ⚠️ No ARM/Graviton support (20% pricing difference)
5. ⚠️ Hardcoded values don't adapt to other regions automatically
6. ⚠️ Rightsizing discount is a heuristic, not an AWS-published rate

**Action Required:** Document the regional and architecture limitations, or implement dynamic pricing lookup.
