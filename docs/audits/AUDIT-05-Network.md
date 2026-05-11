# AUDIT-05: Network Adapter Audit Report

**Date:** 2026-05-01  
**Auditor:** OpenCode Agent  
**Adapter:** `services/adapters/network.py`  
**Scope:** EIP, NAT Gateway, VPC Endpoints, Load Balancers (ALB/NLB), ASG  
**Region Tested:** eu-west-1 (EU Ireland)

---

## Executive Summary

| Component | Verdict | Notes |
|-----------|---------|-------|
| EIP Pricing | ✅ PASS | Uses live pricing API correctly |
| NAT Gateway Pricing | ⚠️ WARN | Fallback constant outdated ($32.00 vs $35.04) |
| VPC Endpoint Pricing | ✅ PASS | Uses live pricing API correctly |
| ALB Pricing | ⚠️ WARN | Fallback constant outdated ($16.20 vs $20.44) |
| Legacy Shims | ⚠️ WARN | Use live pricing but fallback values need updating |
| Documentation | ✅ PASS | AWS docs confirm VPC endpoint cost optimization |

**Overall Verdict: ⚠️ WARN** — Live pricing is correct, but fallback constants are outdated.

---

## 1. Code Analysis

### 1.1 Adapter Architecture

The `network.py` adapter is a **composite/aggregator** that delegates to five legacy shim modules:

```python
# services/adapters/network.py (lines 42-46)
eip_result = get_elastic_ip_checks(ctx)
nat_result = get_nat_gateway_checks(ctx)
vpc_result = get_vpc_endpoints_checks(ctx)
lb_result = get_load_balancer_checks(ctx)
asg_result = get_auto_scaling_checks(ctx)
```

**Called Functions:**
| Function | Source File | Lines |
|----------|-------------|-------|
| `get_elastic_ip_checks()` | `services/elastic_ip.py` | 15-131 |
| `get_nat_gateway_checks()` | `services/nat_gateway.py` | 16-150 |
| `get_vpc_endpoints_checks()` | `services/vpc_endpoints.py` | 16-129 |
| `get_load_balancer_checks()` | `services/load_balancer.py` | 53-260 |
| `get_auto_scaling_checks()` | `services/ec2.py` | (separate) |

### 1.2 Pricing Engine Integration Pattern

All legacy shims follow the same pattern:

```python
# Example from elastic_ip.py (line 16)
eip_monthly = ctx.pricing_engine.get_eip_monthly_price() if ctx.pricing_engine is not None else 3.65

# Example from nat_gateway.py (line 17)
nat_monthly = ctx.pricing_engine.get_nat_gateway_monthly_price() if ctx.pricing_engine is not None else 32.0

# Example from vpc_endpoints.py (line 17)
vpc_ep_monthly = ctx.pricing_engine.get_vpc_endpoint_monthly_price() if ctx.pricing_engine is not None else 7.30

# Example from load_balancer.py (line 54)
alb_monthly = ctx.pricing_engine.get_alb_monthly_price() if ctx.pricing_engine is not None else 16.20
```

**Pattern Analysis:**
- ✅ **Live pricing used when available**: All shims call `ctx.pricing_engine.*_price()` methods
- ✅ **Graceful fallback**: Hardcoded constants used when pricing engine is None
- ⚠️ **Fallback constants outdated**: Some fallback values don't match current AWS pricing

---

## 2. EIP (Elastic IP) Pricing Verification

### 2.1 AWS Pricing API Result

```json
{
  "service": "AmazonVPC",
  "filter": "group=VPCPublicIPv4Address",
  "hourly_rate": "$0.005/hr",
  "monthly_calculation": "$0.005 × 730 = $3.65/month"
}
```

**Pricing Confirmed:**
- **In-use EIP**: $0.005/hour (sku: CNZ4XSXPRJXGA4ZF)
- **Idle EIP**: $0.005/hour (sku: MHVKGXCT3GPZ4RVC)
- **Both types same price** since AWS IPv4 rebilling (2024)

### 2.2 Code Implementation

```python
# pricing_engine.py lines 289-300
def get_eip_monthly_price(self) -> float:
    key = ("eip_month",)
    if (cached := self._get_cached(key)) is not None:
        return cached
    price = self._fetch_eip_price()
    if price is None:
        price = self._use_fallback(
            FALLBACK_EIP_MONTH * self._fallback_multiplier,  # $3.65
            ...
        )
```

**Fallback Value:** `FALLBACK_EIP_MONTH = 3.65` (line 79) ✅ **CORRECT**

### 2.3 Usage in Recommendations

```python
# elastic_ip.py lines 47-56 (unassociated_eips)
{
    "Recommendation": "Release unassociated Elastic IP to avoid charges",
    "EstimatedSavings": f"${eip_monthly:.2f}/month per EIP",  # Uses live price
}
```

**Verdict: ✅ PASS** — EIP pricing is accurate and uses live pricing correctly.

---

## 3. NAT Gateway Pricing Verification

### 3.1 AWS Pricing API Result

```json
{
  "service": "AmazonEC2",
  "filters": [
    "productFamily=NAT Gateway",
    "operation=NatGateway"
  ],
  "results": [
    {
      "sku": "KFNSSCHWD43WZK2R",
      "description": "$0.048 per NAT Gateway Hour",
      "unit": "Hrs",
      "hourly_rate": "$0.048/hr",
      "monthly_calculation": "$0.048 × 730 = $35.04/month"
    },
    {
      "sku": "DTH8595YVPFXV226",
      "description": "$0.048 per GB Data Processed by NAT Gateways",
      "unit": "GB",
      "rate": "$0.048/GB"
    }
  ]
}
```

**Pricing Confirmed:**
- **Hourly charge**: $0.048/hour → **$35.04/month**
- **Data processing**: $0.048/GB

### 3.2 Code Implementation

```python
# pricing_engine.py lines 302-313
def get_nat_gateway_monthly_price(self) -> float:
    key = ("nat_month",)
    ...
    if price is None:
        price = self._use_fallback(
            FALLBACK_NAT_MONTH * self._fallback_multiplier,  # $32.0
            ...
        )

# Fallback constant (line 80)
FALLBACK_NAT_MONTH: float = 32.0  # ❌ OUTDATED (should be $35.04)
```

### 3.3 Discrepancy Analysis

| Metric | Expected | Fallback | Delta |
|--------|----------|----------|-------|
| NAT Gateway monthly | $35.04 | $32.00 | -$3.04 (-8.7%) |

**Impact:** When pricing API is unavailable, savings estimates will be **8.7% lower** than actual.

### 3.4 Usage in Recommendations

```python
# nat_gateway.py lines 64-73 (nat_in_dev_test)
{
    "EstimatedSavings": f"${nat_monthly + 0.85:.2f}/month base + data processing fees",
}

# nat_gateway.py lines 109-116 (multiple_nat_gateways)
{
    "EstimatedSavings": f"${(count - 1) * nat_monthly:.2f}/month if consolidated",
}
```

**Verdict: ⚠️ WARN** — Live pricing is correct, but fallback constant is outdated.

---

## 4. VPC Endpoint Pricing Verification

### 4.1 AWS Pricing API Result

```json
{
  "service": "AmazonVPC",
  "filters": [
    "productFamily=VpcEndpoint",
    "endpointType=PrivateLink"
  ],
  "results": [
    {
      "sku": "H37WJ97MHYGDXG2A",
      "description": "$0.011 per VPC Endpoint Hour",
      "unit": "Hrs",
      "hourly_rate": "$0.011/hr",
      "monthly_calculation": "$0.011 × 730 = $8.03/month"
    },
    {
      "sku": "S8CASMMQB4XR7YWZ",
      "description": "Data processing tiers",
      "tiers": [
        "$0.01/GB (up to 1 PB)",
        "$0.006/GB (1-5 PB)",
        "$0.004/GB (5+ PB)"
      ]
    }
  ]
}
```

**Pricing Confirmed:**
- **Interface endpoint**: $0.011/hour → **$8.03/month**
- **Data processing**: Tiered ($0.01/GB for first PB)

### 4.2 Code Implementation

```python
# pricing_engine.py lines 315-326
def get_vpc_endpoint_monthly_price(self) -> float:
    key = ("vpc_ep_month",)
    ...
    if price is None:
        price = self._use_fallback(
            FALLBACK_VPC_ENDPOINT_MONTH * self._fallback_multiplier,  # $7.30
            ...
        )

# Fallback constant (line 81)
FALLBACK_VPC_ENDPOINT_MONTH: float = 7.30  # ❌ OUTDATED (should be $8.03)
```

**Note:** Fallback shows $7.30 but actual is $8.03 — off by ~9%.

### 4.3 Usage in Recommendations

```python
# vpc_endpoints.py lines 89-98 (interface_endpoints_in_nonprod)
{
    "EstimatedSavings": f"${vpc_ep_monthly:.2f}/month per endpoint",
}

# vpc_endpoints.py lines 111-121 (duplicate_endpoints)
{
    "EstimatedSavings": f"${(len(service_endpoints) - 2) * vpc_ep_monthly:.2f}/month if some consolidated",
}
```

**Verdict: ⚠️ WARN** — Live pricing is correct, but fallback constant is slightly outdated.

---

## 5. ALB (Application Load Balancer) Pricing Verification

### 5.1 AWS Pricing API Result

```json
{
  "service": "AWSELB",
  "filter": "productFamily=Load Balancer",
  "results": [
    {
      "sku": "PMVAP4E245MPBY22",
      "description": "$0.028 per LoadBalancer-hour (or partial hour)",
      "unit": "Hrs",
      "hourly_rate": "$0.028/hr",
      "monthly_calculation": "$0.028 × 730 = $20.44/month"
    },
    {
      "sku": "Z2PBQTQYT4Z8CUQF",
      "description": "$0.008 per GB Data Processed by the LoadBalancer",
      "unit": "GB",
      "rate": "$0.008/GB"
    }
  ]
}
```

**Pricing Confirmed:**
- **ALB hourly**: $0.028/hour → **$20.44/month**
- **Data processing**: $0.008/GB
- **LCU charges**: Additional (not captured)

### 5.2 Code Implementation

```python
# pricing_engine.py lines 328-339
def get_alb_monthly_price(self) -> float:
    key = ("alb_month",)
    ...
    if price is None:
        price = self._use_fallback(
            FALLBACK_ALB_MONTH * self._fallback_multiplier,  # $16.20
            ...
        )

# Fallback constant (line 82)
FALLBACK_ALB_MONTH: float = 16.20  # ❌ OUTDATED (should be $20.44)
```

### 5.3 Discrepancy Analysis

| Metric | Expected | Fallback | Delta |
|--------|----------|----------|-------|
| ALB monthly | $20.44 | $16.20 | -$4.24 (-20.7%) |

**Impact:** When pricing API is unavailable, ALB savings estimates will be **20.7% lower** than actual.

### 5.4 NLB Pricing Calculation

```python
# load_balancer.py line 55
nlb_monthly = alb_monthly * 1.4  # NLB is ~40% more expensive than ALB
```

This relative calculation is reasonable: NLB ≈ $28.62/month.

### 5.5 Usage in Recommendations

```python
# load_balancer.py lines 122-130 (nlb_vs_alb)
{
    "EstimatedSavings": f"Estimated ${nlb_monthly - alb_monthly:.2f}/month savings (NLB: ${nlb_monthly:.2f} vs ALB: ${alb_monthly:.2f})",
}

# load_balancer.py lines 150-170 (single_service_albs)
{
    "EstimatedSavings": f"Up to ${alb_monthly:.2f}/month per ALB eliminated through consolidation",
}
```

**Verdict: ⚠️ WARN** — Live pricing is correct, but fallback constant is significantly outdated.

---

## 6. Legacy Shims Analysis

### 6.1 Do Legacy Shims Use Live Pricing?

| Shim File | Uses Live Pricing | Fallback Value | Fallback Correct? |
|-----------|-------------------|----------------|-------------------|
| `elastic_ip.py` | ✅ Yes | $3.65 | ✅ Yes |
| `nat_gateway.py` | ✅ Yes | $32.00 | ❌ No (should be $35.04) |
| `vpc_endpoints.py` | ✅ Yes | $7.30 | ❌ No (should be $8.03) |
| `load_balancer.py` | ✅ Yes | $16.20 | ❌ No (should be $20.44) |

### 6.2 EstimatedSavings Generation

All legacy shims use **formatted live pricing** in `EstimatedSavings`:

```python
# Pattern found across all shims
f"${monthly_price:.2f}/month ..."  # Uses live price with 2 decimal precision
```

**No hardcoded dollar strings found** in recommendation logic — all use the calculated `*_monthly` variables.

### 6.3 Exception: Non-Monetary Recommendations

Some recommendations use descriptive savings:

```python
# nat_gateway.py line 98
"EstimatedSavings": "$0.01/GB data processing savings",

# load_balancer.py line 249
"EstimatedSavings": "10-20% + better features",  # Classic ELB migration
```

These are informational and acceptable.

---

## 7. Documentation Review

### 7.1 AWS Documentation Findings

**Source:** AWS Well-Architected Framework — COST08-BP02

Key findings relevant to this adapter:

1. **VPC Endpoints reduce NAT Gateway costs**: Using VPC endpoints for S3 and DynamoDB avoids NAT Gateway data processing charges ($0.048/GB)

2. **Gateway Endpoints are FREE**: S3 and DynamoDB gateway endpoints have **no hourly charge**, only interface endpoints do

3. **Interface Endpoints have hourly charges**: $0.011/hour ($8.03/month) per AZ

4. **Cost optimization hierarchy**:
   - Gateway endpoints (free) → Preferred for S3/DynamoDB
   - Interface endpoints (paid) → Required for most other services
   - NAT Gateway (most expensive) → Use only when necessary

### 7.2 Adapter Alignment

The adapter correctly identifies:
- ✅ Missing gateway endpoints for S3/DynamoDB (`missing_gateway_endpoints`)
- ✅ Interface endpoints in non-prod environments (`interface_endpoints_in_nonprod`)
- ✅ NAT Gateway optimization opportunities (`nat_for_aws_services`)
- ✅ ALB consolidation opportunities (`shared_alb_opportunity`)

**Documentation Verdict: ✅ PASS**

---

## 8. Pass Criteria Verification

| Criteria | Status | Evidence |
|----------|--------|----------|
| EIP uses $0.005/hr × 730 = $3.65/month | ✅ PASS | AWS API confirms; fallback matches |
| NAT Gateway uses $0.048/hr × 730 = $35.04/month | ⚠️ WARN | Live pricing correct; fallback is $32.00 |
| VPC Endpoint uses $0.011/hr × 730 = $8.03/month | ⚠️ WARN | Live pricing correct; fallback is $7.30 |
| ALB uses ~$0.022-0.028/hr × 730 = $16-20/month | ⚠️ WARN | Live pricing shows $0.028/hr; fallback is $16.20 |
| Legacy shims use live pricing for EstimatedSavings | ✅ PASS | All use formatted live price variables |
| No hardcoded pricing strings in recommendations | ✅ PASS | All use `f"${price:.2f}"` pattern |

---

## 9. Issues Found

### 9.1 Issue #1: Outdated NAT Gateway Fallback (MEDIUM)

**Location:** `core/pricing_engine.py` line 80

```python
FALLBACK_NAT_MONTH: float = 32.0  # Should be 35.04
```

**Impact:** Underestimates NAT Gateway costs by 8.7% when API unavailable.

**Fix:**
```python
FALLBACK_NAT_MONTH: float = 35.04  # $0.048/hr × 730
```

### 9.2 Issue #2: Outdated VPC Endpoint Fallback (LOW)

**Location:** `core/pricing_engine.py` line 81

```python
FALLBACK_VPC_ENDPOINT_MONTH: float = 7.30  # Should be 8.03
```

**Impact:** Underestimates VPC endpoint costs by 9.1% when API unavailable.

**Fix:**
```python
FALLBACK_VPC_ENDPOINT_MONTH: float = 8.03  # $0.011/hr × 730
```

### 9.3 Issue #3: Outdated ALB Fallback (HIGH)

**Location:** `core/pricing_engine.py` line 82

```python
FALLBACK_ALB_MONTH: float = 16.20  # Should be 20.44
```

**Impact:** Underestimates ALB costs by 20.7% when API unavailable.

**Fix:**
```python
FALLBACK_ALB_MONTH: float = 20.44  # $0.028/hr × 730
```

---

## 10. Recommendations

1. **Update fallback constants** in `core/pricing_engine.py` to match current AWS pricing
2. **Add pricing validation tests** to detect drift in fallback values
3. **Consider regional pricing** — fallback constants assume us-east-1; document this
4. **Monitor AWS pricing changes** — set up quarterly audit schedule

---

## 11. Final Verdict

| Component | Verdict |
|-----------|---------|
| Code Quality | ✅ PASS |
| Live Pricing | ✅ PASS |
| Fallback Pricing | ⚠️ WARN (3 outdated constants) |
| Legacy Shim Integration | ✅ PASS |
| AWS Docs Alignment | ✅ PASS |

### Overall: ⚠️ WARN

**Rationale:**
- The adapter correctly implements live AWS Pricing API integration
- All legacy shims properly use live pricing for `EstimatedSavings` calculations
- **Three fallback constants are outdated** (NAT: -8.7%, VPC Endpoint: -9.1%, ALB: -20.7%)
- When pricing API is available (normal operation), all calculations are accurate
- Fallback values only affect scans when API is unavailable or in `--fast` mode

**Required Action:** Update fallback constants in `core/pricing_engine.py` lines 80-82.

---

## Appendix: Pricing API Raw Results

### EIP Pricing (AmazonVPC)
- Hourly: $0.005/hr
- Monthly: $3.65/month
- SKUs: CNZ4XSXPRJXGA4ZF (InUse), MHVKGXCT3GPZ4RVC (Idle)

### NAT Gateway Pricing (AmazonEC2)
- Hourly: $0.048/hr
- Monthly: $35.04/month
- Data Processing: $0.048/GB
- SKU: KFNSSCHWD43WZK2R

### VPC Endpoint Pricing (AmazonVPC)
- Hourly: $0.011/hr
- Monthly: $8.03/month
- Data Processing: $0.01/GB (first PB)
- SKU: H37WJ97MHYGDXG2A

### ALB Pricing (AWSELB)
- Hourly: $0.028/hr
- Monthly: $20.44/month
- Data Processing: $0.008/GB
- SKU: PMVAP4E245MPBY22

---

*End of Audit Report*
