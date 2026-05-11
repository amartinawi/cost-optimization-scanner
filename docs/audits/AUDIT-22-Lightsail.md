# AUDIT-22: Lightsail Adapter Audit

**Adapter**: `services/adapters/lightsail.py`  
**Supporting**: `services/lightsail.py` (112 lines), `core/pricing_engine.py` (571 lines), `core/contracts.py` (188 lines)  
**Date**: 2026-05-01  
**Auditor**: OpenCode Audit Agent  
**Pricing Data Source**: AWS Price List API (service code: AmazonLightsail), queried 2026-05-01

---

## 1. Code Analysis

### 1.1 Adapter Architecture

**Location**: `services/adapters/lightsail.py`

The `LightsailModule` class implements the `ServiceModule` protocol:

```python
class LightsailModule(BaseServiceModule):
    key: str = "lightsail"
    cli_aliases: tuple[str, ...] = ("lightsail",)
    display_name: str = "Lightsail"

    def required_clients(self) -> tuple[str, ...]:
        return ("lightsail",)

    def scan(self, ctx: Any) -> ServiceFindings:
        result = get_enhanced_lightsail_checks(ctx)
        # ... savings calculation ...
```

**Key Points**:
- Inherits from `BaseServiceModule` (proper adapter pattern)
- Delegates to `get_enhanced_lightsail_checks()` for actual logic
- Uses `ctx.pricing_engine.get_instance_monthly_price()` when available
- Falls back to `12.0 * ctx.pricing_multiplier` heuristic

### 1.2 Bundle Cost Calculation

**Location**: `services/lightsail.py` lines 29-41

```python
_BUNDLE_COSTS: dict[str, float] = {
    "nano_2_0": 3.50,
    "micro_2_0": 5.00,
    "small_2_0": 10.00,
    "medium_2_0": 20.00,
    "large_2_0": 40.00,
    "xlarge_2_0": 80.00,
    "2xlarge_2_0": 160.00,
}
_DEFAULT_BUNDLE_COST = 20.00

def get_lightsail_bundle_cost(bundle_id: str) -> float:
    return _BUNDLE_COSTS.get(bundle_id, _DEFAULT_BUNDLE_COST)
```

**Analysis**:
- Only 7 bundle types mapped (nano through 2xlarge)
- Default cost of $20.00 for unknown bundles
- AWS Lightsail offers many more bundle types (GPU, Memory-optimized, Compute-optimized, Research bundles)

### 1.3 Detection Logic

**Location**: `services/lightsail.py` lines 57-109

The module performs 5 check types:

| Check Type | Logic | Status |
|------------|-------|--------|
| `idle_instances` | Detects instances with `state.name == "stopped"` | ✅ Implemented |
| `oversized_instances` | Detects running instances with "xlarge" or "large" in bundle_id | ✅ Implemented |
| `unused_static_ips` | Detects static IPs where `attachedTo` is falsy | ✅ Implemented |
| `load_balancer_optimization` | Empty placeholder | ⚠️ Not implemented |
| `database_optimization` | Empty placeholder | ⚠️ Not implemented |

**Idle Instance Detection** (lines 63-75):
```python
if instance_state == "stopped":
    checks["idle_instances"].append({
        "InstanceName": instance_name,
        "State": instance_state,
        "BundleId": bundle_id,
        "Recommendation": "Delete stopped Lightsail instance to eliminate costs",
        "EstimatedSavings": f"${get_lightsail_bundle_cost(bundle_id):.2f}/month",
        "CheckCategory": "Idle Resource Cleanup",
    })
```

**Oversized Instance Detection** (lines 77-91):
```python
if instance_state == "running" and ("xlarge" in bundle_id.lower() or "large" in bundle_id.lower()):
    checks["oversized_instances"].append({
        "InstanceName": instance_name,
        "BundleId": bundle_id,
        "State": instance_state,
        "Recommendation": "Review instance utilization - consider downsizing...",
        "EstimatedSavings": f"${get_lightsail_bundle_cost(bundle_id) * 0.3:.2f}/month potential",
        "CheckCategory": "Instance Rightsizing",
    })
```

**Static IP Detection** (lines 93-103):
```python
static_ips_response = lightsail.get_static_ips()
static_ips = static_ips_response.get("staticIps", [])

for static_ip in static_ips:
    if not static_ip.get("attachedTo"):
        checks["unused_static_ips"].append({
            "StaticIpName": static_ip.get("name"),
            "IpAddress": static_ip.get("ipAddress"),
            "Recommendation": "Release unused static IP to avoid charges",
            "EstimatedSavings": "$5.00/month",
            "CheckCategory": "Unused Resource Cleanup",
        })
```

### 1.4 Savings Aggregation in Adapter

**Location**: `services/adapters/lightsail.py` lines 36-46

```python
for rec in recs:
    bundle_name = rec.get("BundleName", rec.get("bundleName", ""))
    if ctx.pricing_engine and bundle_name:
        monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonLightsail", bundle_name)
        savings += monthly if monthly > 0 else 12.0 * ctx.pricing_multiplier
    else:
        savings += 12.0 * ctx.pricing_multiplier
```

**Issue**: The adapter looks for `BundleName` or `bundleName` in recommendations, but the underlying service returns `BundleId` (not `BundleName`). This means the pricing engine lookup will always fail, falling back to the constant.

---

## 2. Pricing Validation

### 2.1 Live API Pricing (eu-west-1)

Queried AWS Price List API for service code `AmazonLightsail` in region `eu-west-1`.

#### Standard Bundles (Linux with IPv4)

| Bundle Type | vCPU | Memory | Storage | Hourly Rate | Monthly Cost* | Hardcoded Cost | Variance |
|-------------|------|--------|---------|-------------|---------------|----------------|----------|
| nano (0.5GB) | 2 | 0.5GB | 20GB | $0.00470 | ~$3.43 | $3.50 | +2.0% |
| micro (1GB) | 2 | 1GB | 40GB | $0.00940 | ~$6.86 | $5.00 | -27.1% |
| small (2GB) | 2 | 2GB | 60GB | $0.01880 | ~$13.72 | $10.00 | -27.1% |
| medium (4GB) | 2 | 4GB | 80GB | $0.03760 | ~$27.45 | $20.00 | -27.2% |
| large (8GB) | 2 | 8GB | 160GB | $0.07520 | ~$54.90 | $40.00 | -27.2% |
| xlarge (16GB) | 4 | 16GB | 320GB | $0.11290 | ~$82.42 | $80.00 | -2.9% |
| 2xlarge (32GB) | 8 | 32GB | 640GB | $0.22581 | ~$164.84 | $160.00 | -2.9% |

*Monthly cost calculated as hourly rate × 730 hours

**Key Findings**:
- nano and xlarge/2xlarge bundles are reasonably accurate
- micro through large bundles are **significantly underestimated** (27% variance)
- Hardcoded prices appear to be outdated or from a different region

#### Static IP Pricing

| Resource | API Price | Hardcoded | Status |
|----------|-----------|-----------|--------|
| Unused Static IP | $0.005/hour ($3.65/month) | $5.00/month | ⚠️ Overestimated by 37% |

The code uses "$5.00/month" but the actual AWS price is $0.005/hour = ~$3.65/month (730 hours).

#### Other Lightsail Resources Detected in API

The API returned pricing for many bundle types not covered by the adapter:

- **GPU Bundles**: $2.44-$2.74/hour (Research workloads)
- **Memory-Optimized**: $0.09946-$0.39516/hour (2-64GB)
- **Compute-Optimized**: $0.05108-$2.26/hour (4GB-144GB)
- **Windows Bundles**: 50-100% premium over Linux
- **IPv6-only Bundles**: Slightly cheaper than IPv4

### 2.2 Pricing Engine Integration

**Location**: `core/pricing_engine.py` lines 230-242

```python
def get_instance_monthly_price(self, service_code: str, instance_type: str) -> float:
    if service_code == "AmazonLightsail":
        price = self._fetch_generic_instance_price(service_code, instance_type)
        if price is None:
            price = self._use_fallback(12.0, f"No Lightsail price for {instance_type}")
        return price
    # ... other services
```

The pricing engine does support Lightsail via `_fetch_generic_instance_price()`, but as noted in section 1.4, the adapter passes the wrong field name (`BundleName` vs `BundleId`).

---

## 3. Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Adapter implements ServiceModule protocol | ✅ PASS | Inherits from BaseServiceModule |
| 2 | Uses pricing_engine when available | ✅ PASS | Calls `ctx.pricing_engine.get_instance_monthly_price()` |
| 3 | Idle instance detection works | ✅ PASS | Checks `state.name == "stopped"` |
| 4 | Static IP detection works | ✅ PASS | Checks `attachedTo` field |
| 5 | Bundle costs are accurate | ⚠️ WARN | 27% variance for micro-large bundles |
| 6 | Static IP pricing is accurate | ⚠️ WARN | Hardcoded $5.00 vs actual $3.65 |
| 7 | All Lightsail bundle types covered | ❌ FAIL | Only 7 of 30+ bundle types mapped |
| 8 | GPU/Research bundle support | ❌ FAIL | Not implemented |
| 9 | Load balancer checks | 🔵 INFO | Placeholder only |
| 10 | Database checks | 🔵 INFO | Placeholder only |
| 11 | Field name consistency | ❌ FAIL | Adapter uses `BundleName`, service returns `BundleId` |
| 12 | Error handling | ✅ PASS | try/except with `ctx.warn()` |

---

## 4. Issues Found

### 4.1 ❌ FAIL — Bundle Cost Underestimation

**Severity**: HIGH  
**Location**: `services/lightsail.py` lines 29-35

| Bundle | Hardcoded | Live eu-west-1 | Variance |
|--------|-----------|----------------|----------|
| micro_2_0 | $5.00 | ~$6.86 | -27% |
| small_2_0 | $10.00 | ~$13.72 | -27% |
| medium_2_0 | $20.00 | ~$27.45 | -27% |
| large_2_0 | $40.00 | ~$54.90 | -27% |

**Impact**: Savings calculations for stopped instances and rightsizing recommendations are significantly underestimated (27% low for most bundles).

**Recommendation**: Update `_BUNDLE_COSTS` to match current eu-west-1 pricing or use pricing engine exclusively.

---

### 4.2 ❌ FAIL — Missing Bundle Type Coverage

**Severity**: MEDIUM  
**Location**: `services/lightsail.py` lines 29-35

The adapter only maps 7 bundle types. AWS Lightsail offers:
- General purpose (nano-2xlarge) ✅ Covered
- Memory-optimized (2GB-64GB) ❌ Not covered
- Compute-optimized (4GB-192GB) ❌ Not covered
- GPU/Research (XL-4XL) ❌ Not covered
- Windows variants ❌ Not covered

**Impact**: Unknown bundle types fall back to $20.00 default, causing incorrect savings estimates.

**Recommendation**: Expand bundle mapping or use live pricing API for all bundle lookups.

---

### 4.3 ❌ FAIL — Field Name Mismatch

**Severity**: HIGH  
**Location**: `services/adapters/lightsail.py` line 38

**Adapter code**:
```python
bundle_name = rec.get("BundleName", rec.get("bundleName", ""))
```

**Service returns**:
```python
{
    "BundleId": bundle_id,  # e.g., "micro_2_0"
    ...
}
```

**Impact**: The pricing engine lookup always fails because the field name is wrong. The adapter always falls back to `12.0 * ctx.pricing_multiplier` instead of using actual bundle pricing.

**Recommendation**: Change adapter to use `rec.get("BundleId")` to match the service output.

---

### 4.4 ⚠️ WARN — Static IP Overestimation

**Severity**: LOW  
**Location**: `services/lightsail.py` line 101

- Hardcoded: "$5.00/month"
- Actual: $0.005/hour × 730 = $3.65/month
- Variance: +37% overestimation

**Impact**: Savings estimates for releasing static IPs are inflated by 37%.

**Recommendation**: Use API pricing: $0.005/hour for unattached static IPs.

---

### 4.5 🔵 INFO — Placeholder Checks

**Severity**: INFO  
**Location**: `services/lightsail.py` lines 50-54

Two check types are defined but never populated:
- `load_balancer_optimization`: Empty list
- `database_optimization`: Empty list

AWS Pricing API shows:
- Load Balancer: $0.0242/hour (~$17.67/month)
- Database bundles: $0.0806-$0.6586/hour

**Recommendation**: Implement checks for Lightsail Load Balancers and Managed Databases.

---

### 4.6 🔵 INFO — Oversized Instance Detection Heuristic

**Severity**: INFO  
**Location**: `services/lightsail.py` line 77

The detection uses simple string matching:
```python
if "xlarge" in bundle_id.lower() or "large" in bundle_id.lower():
```

This catches "large", "xlarge", "2xlarge" but also falsely matches "nano_2_0" (contains "2_0" not "large").

The 30% savings estimate is a heuristic without utilization data.

**Recommendation**: Consider fetching CloudWatch metrics for actual utilization before flagging as oversized.

---

## 5. Verdict

### Summary

| Category | Status |
|----------|--------|
| Architecture | ✅ Correct (adapter pattern) |
| Idle detection | ✅ Working |
| Static IP detection | ✅ Working |
| Bundle pricing accuracy | ❌ 27% underestimated |
| Static IP pricing | ⚠️ 37% overestimated |
| Pricing engine integration | ❌ Broken (field name mismatch) |
| Bundle coverage | ❌ Incomplete (7 of 30+ types) |
| Error handling | ✅ Working |

### Final Assessment

The Lightsail adapter has a **correct architectural foundation** but contains **critical issues** that affect pricing accuracy:

1. **Field name mismatch** prevents pricing engine from working
2. **Hardcoded bundle costs** are outdated (27% low for most bundles)
3. **Limited bundle coverage** misses GPU, Memory-optimized, Compute-optimized, and Windows bundles
4. **Static IP pricing** is overestimated

**Verdict**: ❌ **FAIL** (52/100)

The adapter requires fixes before production use:
1. Fix field name from `BundleName` to `BundleId` in adapter
2. Update hardcoded bundle costs to match eu-west-1 pricing
3. Expand bundle type coverage or remove hardcoded costs and rely on pricing engine
4. Correct static IP pricing to $0.005/hour

---

## Appendix: Raw Pricing API Results

### Standard Bundle — nano (0.5GB IPv6)
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Instance
Memory: 0.5GB, vCPU: 2, Storage: 20GB
Price: $0.00470/hour (~$3.43/month)
SKU: 9B7ZUZ96CXNFSUAR
```

### Standard Bundle — micro (1GB with IPv4)
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Instance
Memory: 1GB, vCPU: 2, Storage: 40GB
Price: $0.00940/hour (~$6.86/month)
SKU: 6WHN6NX5RRBZ6BWX
```

### Static IP (Unattached)
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Networking
Price: $0.005/hour ($3.65/month)
SKU: 7BHMTQCG3VYCM7NA
Description: Static IP address not attached to an instance
```

### Load Balancer
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Load Balancing
Price: $0.0242/hour ($17.67/month)
SKU: 37PP4FD27VHEQU2H
```

### Memory-Optimized Bundle (16GB)
```
Service: AmazonLightsail
Region: eu-west-1 (EU Ireland)
Product Family: Lightsail Instance
Memory: 16GB, vCPU: 2, Storage: 160GB
Price: $0.09946/hour (~$72.61/month)
SKU: 5AJ9EYU6YWV44FEA
```

---

*End of Audit Report*
