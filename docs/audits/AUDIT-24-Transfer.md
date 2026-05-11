# AUDIT-24: Transfer Adapter Audit

**Adapter:** AWS Transfer Family  
**File:** `services/adapters/transfer.py`  
**Service Module:** `services/transfer_svc.py`  
**Audit Date:** 2026-05-01  
**Auditor:** OpenCode Agent  
**Region Tested:** eu-west-1 (EU Ireland)

---

## Executive Summary

The AWS Transfer Family adapter implements protocol-based cost optimization detection for SFTP/FTPS/FTP/AS2 servers. The pricing calculation is **accurate for protocol hourly rates** but has **significant gaps** in covering the full pricing model, including data transfer costs, connector fees, and Web App pricing.

---

## Code Analysis

### Adapter Structure (`transfer.py` - 56 lines)

```python
class TransferModule(BaseServiceModule):
    key: str = "transfer"
    cli_aliases: tuple[str, ...] = ("transfer",)
    display_name: str = "Transfer Family"

    def required_clients(self) -> tuple[str, ...]:
        return ("transfer",)
```

**Architecture:**
- ✅ Follows `BaseServiceModule` pattern correctly
- ✅ Implements `required_clients()` and `scan()` methods
- ✅ Uses `SourceBlock` for structured findings
- ✅ Properly integrates with `ServiceFindings` contract

### Pricing Calculation Logic

```python
TRANSFER_PER_PROTOCOL_HOUR = 0.30
savings = 0.0
for rec in recs:
    protocols = rec.get("Protocols", ["SFTP"])
    num_protocols = len(protocols) if isinstance(protocols, list) else 1
    endpoint_monthly = TRANSFER_PER_PROTOCOL_HOUR * num_protocols * 730 * ctx.pricing_multiplier
    savings += endpoint_monthly
```

**Formula:** `$0.30 × protocols × 730 hours × pricing_multiplier`

### Service Module (`transfer_svc.py` - 82 lines)

**Detection Logic:**
1. **Protocol Optimization** - Flags servers with >1 enabled protocol
2. **Unused Servers** - Identifies STOPPED/OFFLINE servers

**Checks Dictionary Structure:**
```python
checks: dict[str, list[dict[str, Any]]] = {
    "unused_servers": [],
    "protocol_optimization": [],
    "endpoint_optimization": [],  # Empty - not implemented
}
```

---

## Pricing Validation

### AWS Pricing API Data (eu-west-1)

| Component | Usage Type | Price | Adapter Coverage |
|-----------|------------|-------|------------------|
| **SFTP Protocol** | EU-ProtocolHours | $0.30/hour | ✅ Correct |
| **FTPS Protocol** | EU-ProtocolHours | $0.30/hour | ✅ Correct |
| **FTP Protocol** | EU-ProtocolHours | $0.30/hour | ✅ Correct |
| **AS2 Protocol** | EU-ProtocolHours | $0.30/hour | ✅ Correct |
| **Data Upload** | EU-UploadBytes | $0.04/GB | ❌ Missing |
| **Data Download** | EU-DownloadBytes | $0.04/GB | ❌ Missing |
| **SFTP Connector Send** | EU-SFTPConnector-SendBytes | $0.40/GB | ❌ Missing |
| **SFTP Connector Retrieve** | EU-SFTPConnector-RetrieveBytes | $0.40/GB | ❌ Missing |
| **Web App** | EU-WebAppHours | $0.50/hour | ❌ Missing |
| **PGP Decryption** | EU-DecryptBytes-PGP | $0.10/GB | ❌ Missing |
| **AS2 Messages (inbound)** | EU-InboundMessages | $0.01-$0.001/msg | ❌ Missing |
| **AS2 Messages (outbound)** | EU-OutboundMessages | $0.01-$0.001/msg | ❌ Missing |
| **Connector Calls** | EU-SFTPConnectorCall-* | $0.001/call | ❌ Missing |

### Pricing Accuracy Assessment

| Metric | Value | Status |
|--------|-------|--------|
| Protocol Hour Rate | $0.30 | ✅ Accurate |
| Hours/Month | 730 | ✅ Accurate |
| Regional Multiplier Support | Yes | ✅ Implemented |
| Data Transfer Pricing | $0.04/GB | ❌ Not Covered |
| Connector Pricing | $0.40/GB | ❌ Not Covered |

---

## Pass Criteria Checklist

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Correct protocol hourly pricing ($0.30) | ✅ PASS | Matches AWS Pricing API |
| 2 | Proper client registration | ✅ PASS | Returns `("transfer",)` tuple |
| 3 | Follows adapter pattern | ✅ PASS | Extends `BaseServiceModule` |
| 4 | Uses `ServiceFindings` contract | ✅ PASS | Correct structure |
| 5 | Error handling with `ctx.warn()` | ✅ PASS | Try/catch around API calls |
| 6 | Supports regional pricing multiplier | ✅ PASS | Uses `ctx.pricing_multiplier` |
| 7 | Protocol detection logic | ✅ PASS | Flags servers with >1 protocol |
| 8 | Unused server detection | ✅ PASS | Detects STOPPED/OFFLINE state |
| 9 | Data transfer cost estimation | ❌ FAIL | Not implemented |
| 10 | SFTP Connector cost tracking | ❌ FAIL | Not implemented |
| 11 | Web App pricing support | ❌ FAIL | Not implemented |
| 12 | AS2 message pricing | ❌ FAIL | Not implemented |
| 13 | Realistic savings calculation | ⚠️ WARN | Assumes 100% protocol removal |
| 14 | Endpoint type optimization | ❌ FAIL | VPC endpoint costs not analyzed |
| 15 | Storage backend cost awareness | ❌ FAIL | S3 vs EFS costs not differentiated |

**Score:** 8/15 criteria passed (53%)

---

## Issues Found

| ID | Severity | Description | Impact |
|----|----------|-------------|--------|
| TRANS-001 | 🔴 HIGH | Savings calculation assumes complete protocol removal | Reports inflated savings that may not be achievable |
| TRANS-002 | 🟡 MEDIUM | Missing data transfer cost analysis ($0.04/GB) | Underestimates total cost of ownership |
| TRANS-003 | 🟡 MEDIUM | No SFTP Connector pricing ($0.40/GB) | Misses connector-related optimization opportunities |
| TRANS-004 | 🟡 MEDIUM | No Web App pricing ($0.50/hour) | Incomplete for Web App-enabled servers |
| TRANS-005 | 🟢 LOW | `endpoint_optimization` category is empty placeholder | Dead code - should be removed or implemented |
| TRANS-006 | 🟢 LOW | Generic "Variable" savings for protocol optimization | Reduces actionability of recommendations |
| TRANS-007 | 🟡 MEDIUM | No VPC endpoint cost analysis | VPC-hosted endpoints have different pricing |
| TRANS-008 | 🟡 MEDIUM | No PGP decryption cost tracking ($0.10/GB) | Missing encryption-related costs |

### Issue Details

#### TRANS-001: Inflated Savings Calculation

**Current Logic:**
```python
endpoint_monthly = TRANSFER_PER_PROTOCOL_HOUR * num_protocols * 730 * ctx.pricing_multiplier
```

**Problem:** This calculates the cost of ALL protocols, but the recommendation is to "review if all protocols are needed" - not to remove them. A server with SFTP+FTPS (2 protocols) reports $438/month savings, but removing one protocol only saves $219/month.

**Recommendation:** Calculate savings as cost of redundant protocols only (protocols - 1).

#### TRANS-002: Missing Data Transfer Costs

Data transfer is often the largest cost component for Transfer Family:
- Upload: $0.04/GB
- Download: $0.04/GB
- SFTP Connector: $0.40/GB (10x higher!)

**Impact:** For a server transferring 1TB/month, data costs ($40) exceed protocol costs ($30).

#### TRANS-003: Missing Connector Pricing

SFTP Connectors are priced at $0.40/GB - 10x the standard data transfer rate. The adapter does not detect or price connector usage.

---

## Verdict

### ⚠️ WARN — 65/100

**Assessment:** The Transfer Family adapter has **accurate protocol hourly pricing** ($0.30/hour confirmed by AWS Pricing API) but has **significant gaps** in cost coverage. The adapter is functional for basic protocol optimization detection but underestimates total costs and overestimates potential savings.

### Strengths
1. ✅ Protocol hourly rate ($0.30) is accurate for eu-west-1
2. ✅ Follows established adapter patterns correctly
3. ✅ Proper error handling with context warnings
4. ✅ Supports regional pricing multipliers
5. ✅ Detects unused servers (STOPPED/OFFLINE)

### Weaknesses
1. ❌ Savings calculation assumes 100% protocol removal
2. ❌ Missing data transfer cost analysis ($0.04/GB)
3. ❌ Missing SFTP Connector pricing ($0.40/GB)
4. ❌ Missing Web App pricing ($0.50/hour)
5. ❌ Missing PGP decryption costs ($0.10/GB)
6. ❌ Missing AS2 message pricing
7. ❌ Empty `endpoint_optimization` category

### Recommendations

1. **Fix TRANS-001:** Change savings calculation to `(num_protocols - 1) × $0.30 × 730` for protocol optimization recommendations

2. **Add Data Transfer Estimation:** Query CloudWatch metrics for `BytesUploaded` and `BytesDownloaded` to estimate transfer costs

3. **Add Connector Detection:** Query `list_connectors` API and include connector transfer costs

4. **Remove or Implement:** Either implement `endpoint_optimization` checks or remove the empty placeholder

5. **Enhanced Recommendations:** 
   - Detect idle servers (low transfer volume relative to protocol hours)
   - Identify servers suitable for consolidation
   - Flag VPC endpoint vs Public endpoint cost differences

---

## AWS Documentation References

- [AWS Transfer Family Pricing](https://aws.amazon.com/aws-transfer-family/pricing/)
- [AWS Transfer Family User Guide](https://docs.aws.amazon.com/transfer/latest/userguide/what-is-aws-transfer-family.html)
- Pricing validated against AWS Price List API (service code: `AWSTransfer`)

---

## Appendix: Pricing API Raw Data

**Service Code:** AWSTransfer  
**Region:** eu-west-1 (EU Ireland)  
**API Call:** `get_pricing(service_code="AWSTransfer", region="eu-west-1")`

**Key Price Points:**
```json
{
  "EU-ProtocolHours": "$0.30/hour (SFTP, FTPS, FTP, AS2)",
  "EU-UploadBytes": "$0.04/GB",
  "EU-DownloadBytes": "$0.04/GB",
  "EU-WebAppHours": "$0.50/hour",
  "EU-SFTPConnector-SendBytes": "$0.40/GB",
  "EU-SFTPConnector-RetrieveBytes": "$0.40/GB",
  "EU-DecryptBytes-PGP": "$0.10/GB"
}
```

---

*End of Audit Report*
