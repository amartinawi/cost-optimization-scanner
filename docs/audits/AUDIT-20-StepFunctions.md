# AUDIT-20: Step Functions Service Adapter

**Adapter File:** `/Users/amartinawi/Desktop/Cost_OptV1_dev/services/adapters/step_functions.py`  
**Legacy Module:** `/Users/amartinawi/Desktop/Cost_OptV1_dev/services/step_functions.py`  
**Audit Date:** 2026-05-01  
**Auditor:** AI Agent  
**Verdict:** ⚠️ WARN — Multiple Issues Detected

---

## Executive Summary

The Step Functions adapter has **3 confirmed issues** affecting cost accuracy:

| Issue | Severity | Finding |
|-------|----------|---------|
| Billing Unit Mismatch | 🔴 HIGH | Uses `ExecutionsStarted` metric but billing is per state **transition**, not execution |
| Hardcoded Pricing | 🟡 MED | $0.025/1K transitions hardcoded, no PricingEngine integration |
| Arbitrary Savings % | 🟡 MED | Flat 60% savings for Standard→Express without workflow analysis |

**Verdict Codes:** `WARN-001` (underestimation), `WARN-002` (hardcoded), `WARN-003` (unverified)

---

## 1. Pricing Verification

### 1.1 Hardcoded Pricing Constant

**Location:** `services/adapters/step_functions.py:40`

```python
STEP_FUNCTIONS_PER_1K_TRANSITIONS = 0.025  # Hardcoded
```

**AWS Pricing API Results:**
- Service Code: `AWSStepFunctions` (not found in API)
- Alternative attempted: No matches for "step" pattern
- eu-west-1: Unable to verify via API (empty results)
- us-east-1: Unable to verify via API (empty results)

**Documentation Verification:**
Per [AWS Step Functions Pricing](https://aws.amazon.com/step-functions/pricing/):
- **Standard Workflows:** $0.000025 per state transition ($0.025 per 1,000)
- **Free Tier:** 4,000 state transitions/month (indefinite)
- **Express Workflows:** $1.00 per million requests + $0.00001667 per GB-second duration

**Finding:** The hardcoded $0.025/1K matches documented us-east-1 pricing, but:
1. No regional pricing verification via PricingEngine
2. Unlike EC2/EBS/RDS adapters, does not use `ctx.pricing_engine`
3. No fallback mechanism for pricing API failures

**Status:** ⚠️ `WARN-002` — Hardcoded pricing without API verification

---

## 2. Critical Issue: ExecutionsStarted vs State Transitions

### 2.1 The Problem

**Billing Model (per AWS docs):**
- Step Functions Standard charges per **state transition**, NOT per execution
- Each state in a workflow = 1 transition
- A 10-state workflow executed 1,000 times = 10,000 state transitions
- Cost = (10,000 transitions × $0.000025) = $0.25

**Adapter Implementation (line 55-72):**
```python
# Gets EXECUTIONS count, not transitions
resp = cw.get_metric_statistics(
    Namespace="AWS/States",
    MetricName="ExecutionsStarted",  # ← WRONG METRIC
    ...
)
monthly_executions = sum(dp["Sum"] for dp in resp.get("Datapoints", []))

# Calculates as if 1 execution = 1 transition
savings += (
    (monthly_executions / 1000) * STEP_FUNCTIONS_PER_1K_TRANSITIONS * 0.60
)
```

### 2.2 Impact Analysis

| Workflow Type | States/Exec | Actual Transitions | Adapter Calc | Error |
|---------------|-------------|-------------------|--------------|-------|
| Simple | 2 | 2,000 (for 1K execs) | 1,000 | **-50%** |
| Medium | 10 | 10,000 (for 1K execs) | 1,000 | **-90%** |
| Complex | 50 | 50,000 (for 1K execs) | 1,000 | **-98%** |

**Real-World Example:**
- Image processing workflow (from AWS docs): 9 state transitions per execution
- 100,000 executions = 900,000 transitions
- **Actual cost:** 900,000 × $0.000025 = $22.50
- **Adapter estimates:** 100,000 × $0.000025 = $2.50
- **Underestimation:** $20.00 (89% error)

### 2.3 Root Cause

The adapter uses `ExecutionsStarted` CloudWatch metric but does not:
1. Query the state machine definition to count states
2. Use `ExecutionsSucceeded` + average state count per execution
3. Apply a conversion factor (avg_states × executions)

**Available CloudWatch Metrics for Step Functions:**
- `ExecutionsStarted` — Number of workflow executions (what adapter uses)
- `ExecutionsSucceeded` — Successful completions
- `ExecutionsFailed` — Failed executions
- No direct "state transitions" metric available in CloudWatch

**Status:** 🔴 `WARN-001` — Severe cost underestimation for multi-step workflows

---

## 3. 60% Savings Calculation Analysis

### 3.1 Standard vs Express Pricing

| Workflow Type | Pricing Model | Unit Cost |
|---------------|---------------|-----------|
| **Standard** | Per state transition | $0.000025/transition |
| **Express** | Per request + duration | $0.000001/request + $0.00001667/GB-second |

### 3.2 Break-Even Analysis

Express has **two cost components:**
1. **Requests:** $1.00 per million = $0.000001 per execution
2. **Duration:** $0.00001667 per GB-second (=$0.06000/GB-hour for first 1K)

**Scenario Comparison** (1,000 executions/month):

| States | Standard Cost | Express Cost* | Actual Savings | Adapter Claims |
|--------|---------------|---------------|----------------|----------------|
| 2 states | $0.05 | ~$1.00+ | **-1900%** (more expensive) | 60% |
| 10 states | $0.25 | ~$1.00+ | **-300%** (more expensive) | 60% |
| 50 states | $1.25 | ~$1.00+ | ~20% | 60% |
| 100 states | $2.50 | ~$1.00+ | ~60% | 60% |

*Express estimate: $1.00 request fee + minimal duration for short workflows

### 3.3 The Issue

The adapter applies a **flat 60% savings** (`* 0.60`) without:
1. Analyzing the state machine definition (number of states)
2. Measuring workflow duration
3. Estimating memory utilization
4. Considering request volume

For many workflows (especially simple ones), Express is **more expensive** than Standard, not 60% cheaper.

**Status:** 🟡 `WARN-003` — Arbitrary savings percentage without workflow analysis

---

## 4. Code Review Findings

### 4.1 Missing PricingEngine Integration

**What other adapters do (e.g., EC2, EBS, RDS):**
```python
# Live pricing lookup
price = ctx.pricing_engine.get_ec2_instance_monthly_price(instance_type)
```

**What step_functions adapter does:**
```python
# Hardcoded constant
STEP_FUNCTIONS_PER_1K_TRANSITIONS = 0.025
```

### 4.2 Inaccurate Docstring

**Line 27-28:**
```python
"""...Savings calculated via CloudWatch ExecutionsStarted metrics 
with $0.025/1K state transitions pricing..."""
```

**Issue:** Claims to use "state transitions pricing" but uses `ExecutionsStarted` metric (executions, not transitions).

### 4.3 Fast Mode Handling

**Lines 44, 76-77:**
```python
if not ctx.fast_mode:
    # ... CloudWatch lookup
else:
    savings += 150.0 * ctx.pricing_multiplier
```

The flat $150/rec estimate in fast mode is reasonable as a fallback, but the non-fast mode calculation is flawed.

---

## 5. Recommendations

### 5.1 Immediate (High Priority)

1. **Add State Count Estimation**
   ```python
   # Query state machine definition
   sfn = ctx.client("stepfunctions")
   definition = sfn.describe_state_machine(stateMachineArn=arn)["definition"]
   # Parse JSON, count states (excluding Choice branches for conservative estimate)
   state_count = count_states_in_definition(definition)
   total_transitions = monthly_executions * state_count
   ```

2. **Use Conservative Multiplier**
   If parsing definitions is complex, apply a conservative multiplier:
   ```python
   AVG_STATES_PER_WORKFLOW = 5  # Conservative industry average
   total_transitions = monthly_executions * AVG_STATES_PER_WORKFLOW
   ```

### 5.2 Medium Priority

3. **Implement PricingEngine Method**
   Add `get_stepfunctions_transition_price(region)` to `core/pricing_engine.py`

4. **Fix Express Savings Calculation**
   Require workflow analysis before claiming Express savings:
   ```python
   if state_count > 25 and duration_seconds < 60:
       estimated_savings = calculate_express_savings(...)
   else:
       estimated_savings = 0  # Cannot recommend without analysis
   ```

### 5.3 Documentation

5. **Update Docstring**
   Document the limitation:
   ```python
   """...Note: Cost estimates assume average 5 states per workflow. 
   Actual costs vary based on workflow complexity."""
   ```

---

## 6. Test Impact

**Golden File:** `tests/fixtures/golden_scan_results.json` (line 424)
- Current: `total_monthly_savings: 0` (no recommendations)
- Impact: No change required (zero baseline)

**Regression Gate:**
```bash
pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -v
```
- Status: No impact (no Step Functions test fixtures with non-zero savings)

---

## 7. Appendix: AWS Pricing Documentation

### Standard Workflow Example (from AWS docs)

> An application workflow has four state transitions:
> 1. Start
> 2. Upload RAW File
> 3. Delete RAW File
> 4. End
>
> 100,000 executions = 400,000 transitions
> Cost = 400,000 × $0.000025 = $10.00 (after free tier)

### Express Workflow Example (from AWS docs)

> 1 million workflows, 30 seconds average duration:
> - Request charges: 1M × $1.00/M = $1.00
> - Duration charges: 30M seconds × 64MB / 1024MB × $0.00001667 = $31.26
> - Total: $32.26

---

## 8. Verdict

| Check | Result | Notes |
|-------|--------|-------|
| $0.025/1K verified vs eu-west-1 API | ❌ FAIL | API returned empty results |
| Pricing from AWS PricingEngine | ❌ FAIL | Uses hardcoded constant |
| Billing unit is transitions | ❌ FAIL | Uses executions metric |
| 60% savings justified | ❌ FAIL | Arbitrary percentage |
| Code quality | ⚠️ WARN | Docstring inaccurate |
| Test coverage | ✅ PASS | No regressions |

**Final Verdict:** `WARN-001 WARN-002 WARN-003`

The adapter significantly underestimates Step Functions costs for multi-step workflows and applies arbitrary savings percentages. Recommendation: Fix billing unit calculation before production use.

---

*End of Audit Report*
