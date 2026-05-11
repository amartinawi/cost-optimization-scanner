# AUDIT-23: AppRunner Adapter Audit

**Date**: 2026-05-01  
**Auditor**: OpenCode Agent  
**Scope**: `services/adapters/apprunner.py` and underlying `services/apprunner.py`  
**Region Tested**: eu-west-1 (Ireland)

---

## Code Analysis

### Architecture
The AppRunner adapter follows the standard two-layer architecture:

1. **Adapter Layer** (`services/adapters/apprunner.py`): Implements `ServiceModule` Protocol
   - Hardcoded pricing constants for vCPU and memory
   - Applies 30% arbitrary rightsizing discount to all recommendations
   - Uses `ctx.pricing_multiplier` for regional pricing adjustments

2. **Service Layer** (`services/apprunner.py`): Contains actual detection logic
   - Lists App Runner services via `apprunner:ListServices`
   - Describes service configuration via `apprunner:DescribeService`
   - Very basic detection logic (triggers only on "large" in config or non-1GB memory)

### Pricing Implementation

```python
# From services/adapters/apprunner.py lines 40-59
APP_RUNNER_VCPU_HOURLY = 0.064      # $/vCPU-hour
APP_RUNNER_MEM_GB_HOURLY = 0.007    # $/GB-hour

monthly_active = (
    (vcpus * APP_RUNNER_VCPU_HOURLY + mem_gb * APP_RUNNER_MEM_GB_HOURLY) * 730 * ctx.pricing_multiplier
)
savings += monthly_active * 0.30    # 30% arbitrary discount
```

---

## Pricing Validation

### AWS Pricing API Results (eu-west-1)

| Component | API Price | Adapter Constant | Match |
|-----------|-----------|------------------|-------|
| vCPU-hour (x86) | $0.064 | $0.064 | ✅ |
| GB-hour | $0.007 | $0.007 | ✅ |
| Build-mins | $0.005/min | Not used | N/A |
| Automatic Deployment | $1.00/pipeline | Not used | N/A |
| Provisioned GB-hour | $0.007 | Not used | N/A |

**Pricing API Verification**: ✅ Constants match AWS Pricing API for eu-west-1

### App Runner Pricing Model (from AWS Documentation)

App Runner uses a **dual-mode pricing model**:

1. **Provisioned Container Instances** (Idle State):
   - Charged: $0.007/GB-hour (memory only)
   - Purpose: Keep application warm, eliminate cold starts
   - Default: 1 provisioned instance minimum

2. **Active Container Instances** (Processing State):
   - Charged: $0.064/vCPU-hour + $0.007/GB-hour
   - Automatically scales up/down based on request volume
   - One-minute minimum vCPU charge when transitioning from provisioned

3. **Additional Costs**:
   - Build fee: $0.005/minute (when deploying from source)
   - Automatic deployments: $1.00/pipeline/month

---

## Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Pricing constants match AWS API | ✅ PASS | $0.064/vCPU-hr and $0.007/GB-hr verified |
| 2 | Regional pricing multiplier applied | ✅ PASS | Uses `ctx.pricing_multiplier` |
| 3 | Required boto3 clients declared | ✅ PASS | Returns `("apprunner",)` |
| 4 | Returns proper ServiceFindings | ✅ PASS | Correct dataclass structure |
| 5 | Error handling for API failures | ⚠️ WARN | Silent pass on exceptions (line 60-61, 63-64) |
| 6 | CloudWatch metrics for rightsizing | ❌ FAIL | No utilization data collected |
| 7 | Dual-mode pricing model honored | ❌ FAIL | Assumes 24/7 active, ignores provisioned state |
| 8 | All cost components included | ❌ FAIL | Build fees and deployment fees omitted |
| 9 | Detection logic comprehensive | ❌ FAIL | Only triggers on "large" or non-1GB memory |
| 10 | Unused service detection | ❌ FAIL | Services not checked for actual traffic |

**Score: 4/10 criteria passed**

---

## Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| AP-001 | **HIGH** | Assumes 24/7 active usage; ignores provisioned idle state | Savings estimates 3-5x too high for typical low-traffic services |
| AP-002 | **HIGH** | 30% rightsizing discount is arbitrary, not based on metrics | Recommendations lack factual basis; could mislead users |
| AP-003 | **MEDIUM** | No CloudWatch integration for CPU/memory utilization | Cannot detect actually idle or over-provisioned services |
| AP-004 | **MEDIUM** | Build fees ($0.005/min) not included in savings calc | Missing cost optimization opportunity |
| AP-005 | **MEDIUM** | Automatic deployment fees ($1/mo) not included | Incomplete cost picture for CI/CD workflows |
| AP-006 | **MEDIUM** | Detection logic too narrow (only "large" or non-1GB) | Many optimization opportunities missed |
| AP-007 | **LOW** | No check for services that could be paused | Missed savings for dev/test environments |
| AP-008 | **LOW** | Silent exception handling masks failures | Debug difficulty if App Runner APIs fail |
| AP-009 | **LOW** | No concurrency optimization analysis | Key cost driver in App Runner not analyzed |

---

## Detailed Analysis

### Issue AP-001: Incorrect Pricing Model Assumption

**Current Behavior**: Adapter calculates costs assuming services are always in "active" state:
```
Monthly = (vCPU × $0.064 + Memory × $0.007) × 730 hours
```

**Actual App Runner Behavior**:
- Most services spend majority of time in "provisioned" state (memory only)
- Only pay vCPU costs when actively processing requests
- Example: A 1vCPU/2GB service active 2 hours/day:
  - **Actual cost**: $10.22/month (provisioned) + $0.13/day × 30 = $14.12/month
  - **Adapter calculates**: $56.94/month (as if 24/7 active)
  - **Overestimation**: 4× too high

**Recommendation**: Model both provisioned and active states; use conservative traffic assumptions.

### Issue AP-002: Arbitrary 30% Discount

**Current Behavior**: All savings multiplied by 0.30 without justification.

**Comparison with Other Adapters**:
- EC2 adapter: Uses CloudWatch CPU metrics, Compute Optimizer recommendations
- Lambda adapter: CloudWatch invocation metrics, memory analysis
- RDS adapter: CloudWatch connection metrics

**Recommendation**: Implement CloudWatch metric collection or use conservative fixed percentage based on industry benchmarks.

### Issue AP-003: Missing CloudWatch Integration

**Gap**: No `requires_cloudwatch = True` flag, no metric collection.

**Relevant Metrics** (from App Runner console):
- `RequestCount` — total requests (detect unused services)
- `ActiveInstances` — current active container count
- `MemoryUtilization` — percentage of memory used
- `CPUUtilization` — percentage of CPU used

**Recommendation**: Add CloudWatch metric queries to detect:
1. Services with zero requests (pausable)
2. Services with low CPU/memory utilization (rightsizable)
3. Over-provisioned concurrency settings

### Issue AP-004 & AP-005: Missing Cost Components

**Build Fees**: When deploying from source code, App Runner charges $0.005/minute for build time. For teams with frequent deployments, this is significant.

**Automatic Deployments**: $1.00/pipeline/month. Services with CI/CD integration incur this fee.

**Recommendation**: Add detection for:
- Source-based deployments (vs image-based)
- Automatic deployment configuration
- Calculate build frequency impact

---

## Correctness Verification

### Pricing Constants
```bash
# AWS Pricing API verified on 2026-05-01
Service: AWSAppRunner
Region: eu-west-1
- vCPU-hour (x86): $0.064 ✅
- GB-hour: $0.007 ✅
```

### Calculation Example
For a 1 vCPU, 2 GB service:
```python
# Adapter calculation (always active assumption)
(1 * 0.064 + 2 * 0.007) * 730 * 1.0 = $56.94/month
Savings at 30% = $17.08/month

# Realistic calculation (2 hours active/day)
# Provisioned: 2 GB * $0.007 * 730 = $10.22/month
# Active: (1 * $0.064 + 2 * $0.007) * 2 hrs/day * 30 days = $4.68/month
# Total: $14.90/month
# Realistic savings (pause dev): $14.90/month (not $17.08)
```

---

## Comparison with Best Practices

| Aspect | AppRunner Adapter | Best Practice (EC2 Adapter) | Gap |
|--------|-------------------|----------------------------|-----|
| Pricing source | Hardcoded constants | AWS Pricing API via PricingEngine | Medium |
| Rightsizing basis | Arbitrary 30% | CloudWatch CPU metrics | High |
| Idle detection | None | 14-day CloudWatch analysis | High |
| State awareness | Single (active) | Multiple (running/stopped) | High |
| Error handling | Silent pass | Warning emission | Low |

---

## Recommendations

### Immediate (P0)
1. **Fix pricing model**: Calculate based on provisioned + realistic active hours
2. **Remove arbitrary 30%**: Use actual CloudWatch metrics or conservative 10-15%

### Short-term (P1)
3. **Add CloudWatch integration**: Collect RequestCount, CPU, Memory metrics
4. **Detect pausable services**: Zero requests over 14 days = pause recommendation
5. **Include build/deployment costs**: Detect source-based deployments

### Long-term (P2)
6. **Concurrency analysis**: Review auto-scaling concurrency settings
7. **Configuration optimization**: Recommend optimal vCPU/memory combinations
8. **Add to pricing engine**: Implement `get_apprunner_monthly_price()` method

---

## Verdict

**⚠️ WARN — 40/100**

The AppRunner adapter functions correctly from a code perspective but contains significant methodological flaws in its cost modeling:

1. **Pricing Model**: The assumption of 24/7 active usage leads to **4-5× overestimation** of potential savings for typical low-traffic services.

2. **Detection Logic**: The underlying service module's detection is too narrow, only triggering on superficial string matching ("large") rather than analyzing actual resource utilization.

3. **Missing Features**: Unlike EC2, Lambda, and RDS adapters, AppRunner lacks CloudWatch metric integration, making it impossible to provide data-driven recommendations.

4. **Cost Completeness**: Build fees and automatic deployment costs are significant for App Runner but completely omitted.

**Recommendation**: The adapter should be marked as **beta quality** in documentation until CloudWatch integration and proper dual-mode pricing are implemented. Current savings estimates should be treated as theoretical maximums, not realistic projections.

---

## Appendix: AWS Documentation References

- [App Runner Pricing](https://aws.amazon.com/apprunner/pricing/)
- [App Runner InstanceConfiguration API](https://docs.aws.amazon.com/apprunner/latest/api/API_InstanceConfiguration.html)
- [App Runner Architecture](https://docs.aws.amazon.com/apprunner/latest/dg/architecture.html)
- Pricing verified via AWS Price List API: `AWSAppRunner` service code

---

*Generated by OpenCode Agent | Audit ID: AUDIT-23*
