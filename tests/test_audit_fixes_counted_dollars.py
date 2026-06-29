"""Targeted tests for the counted-dollar fixes (verifier follow-up).

Drives each adapter's ``scan()`` pricing/dedup logic with a fake ctx +
monkeypatched enhanced-checks helpers so the counted dollar is proven, not just
inferred from a green golden fixture. Covers the highest-value counted-dollar
fixes the verifier flagged as untested: apprunner, quicksight, transfer,
opensearch C2, dynamodb dedup+cap, and api_gateway advisory labeling.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.apprunner as apprunner_adapter
import services.adapters.api_gateway as api_gateway_adapter
import services.adapters.dynamodb as dynamodb_adapter
import services.adapters.opensearch as opensearch_adapter
import services.adapters.quicksight as quicksight_adapter
import services.adapters.transfer as transfer_adapter
from services.quicksight import quicksight_spice_rate


class _FakePricing:
    """Returns fixed monthly/hourly prices keyed by call signature."""

    def __init__(self, es_monthly: float = 100.0):
        self._es_monthly = es_monthly

    def get_instance_monthly_price(self, service_code, instance_type, *, engine=None):
        if service_code == "AmazonES":
            return self._es_monthly
        return 0.0


def _ctx(*, pricing_multiplier: float = 1.0, pricing_engine=None, fast_mode=False) -> SimpleNamespace:
    return SimpleNamespace(
        pricing_engine=pricing_engine or _FakePricing(),
        pricing_multiplier=pricing_multiplier,
        region="us-east-1",
        account_id="123456789012",
        fast_mode=fast_mode,
        cost_hub_splits={},
        warnings=[],
        permission_issues=[],
        client=lambda name, region=None: None,
        warn=lambda message, service=None: None,
        permission_issue=lambda message, service=None, action=None: None,
    )


# --------------------------------------------------------------------------- #
# apprunner C1 — idle service priced at recoverable provisioned-memory charge
# --------------------------------------------------------------------------- #
def test_apprunner_idle_service_priced_at_provisioned_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "ServiceName": "idle-svc",
            "InstanceConfiguration": {"Memory": "4 GB"},
            "CheckCategory": "Idle Service",
        }
    ]
    monkeypatch.setattr(
        apprunner_adapter,
        "get_enhanced_apprunner_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = apprunner_adapter.AppRunnerModule().scan(_ctx(pricing_multiplier=1.0))
    # 4 GB × $0.007/hr × 730 hr = $20.44/mo
    assert findings.total_monthly_savings == pytest.approx(4 * 0.007 * 730, abs=0.01)
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(20.44, abs=0.01)


# --------------------------------------------------------------------------- #
# quicksight C1 — edition-aware SPICE rate (Standard $0.25 vs Enterprise $0.38)
# --------------------------------------------------------------------------- #
def test_quicksight_standard_edition_uses_lower_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "DomainName": "n/a",
            "Edition": "STANDARD",
            "UnusedSpiceCapacityGB": 100,
            "CheckCategory": "SPICE Optimization",
        }
    ]
    monkeypatch.setattr(
        quicksight_adapter,
        "get_enhanced_quicksight_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = quicksight_adapter.QuicksightModule().scan(_ctx(pricing_multiplier=1.0))
    # Standard: 100 GB × $0.25 = $25.00 (NOT $38 — the old $0.38-for-both bug)
    assert findings.total_monthly_savings == pytest.approx(25.0, abs=0.01)


def test_quicksight_enterprise_edition_uses_higher_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "DomainName": "n/a",
            "Edition": "ENTERPRISE",
            "UnusedSpiceCapacityGB": 100,
            "CheckCategory": "SPICE Optimization",
        }
    ]
    monkeypatch.setattr(
        quicksight_adapter,
        "get_enhanced_quicksight_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = quicksight_adapter.QuicksightModule().scan(_ctx(pricing_multiplier=1.0))
    # Enterprise: 100 GB × $0.38 = $38.00
    assert findings.total_monthly_savings == pytest.approx(38.0, abs=0.01)


def test_quicksight_rate_helper_pins_by_edition() -> None:
    assert quicksight_spice_rate("STANDARD") == 0.25
    assert quicksight_spice_rate("ENTERPRISE") == 0.38
    assert quicksight_spice_rate("standard") == 0.25
    assert quicksight_spice_rate("") == 0.38  # unknown → Enterprise default


# --------------------------------------------------------------------------- #
# transfer C1 — region-flat ProtocolHours, no pricing_multiplier
# --------------------------------------------------------------------------- #
def test_transfer_drops_pricing_multiplier_from_flat_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    # transfer H2: protocol_optimization counts ONLY with per-protocol usage
    # evidence. With evidence, the region-flat ProtocolHours rate still ignores
    # pricing_multiplier (transfer C1) so card == counted.
    recs = [
        {
            "ServerId": "s-1",
            "Protocols": ["SFTP", "FTP", "FTPS"],
            "RemovableProtocols": 2,
            "PerProtocolUsageEvidence": True,
            "CheckCategory": "Protocol Optimization",
        }
    ]
    monkeypatch.setattr(
        transfer_adapter,
        "get_enhanced_transfer_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = transfer_adapter.TransferModule().scan(_ctx(pricing_multiplier=1.25))
    # 2 protocols × $0.30/hr × 730 = $438.00 (NOT $547.50 = 438 × 1.25)
    assert findings.total_monthly_savings == pytest.approx(438.0, abs=0.01)
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedSavings"].startswith("$438.00")


def test_transfer_protocol_optimization_advisory_without_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    # transfer H2: without per-protocol usage evidence, (len-1) is a fabricated
    # removable count → $0 advisory (Counted=False), never a counted dollar.
    recs = [
        {
            "ServerId": "s-1",
            "Protocols": ["SFTP", "FTP", "FTPS"],
            "RemovableProtocols": 2,
            "CheckCategory": "Protocol Optimization",
        }
    ]
    monkeypatch.setattr(
        transfer_adapter,
        "get_enhanced_transfer_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = transfer_adapter.TransferModule().scan(_ctx(pricing_multiplier=1.0))
    assert findings.total_monthly_savings == 0.0
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec.get("Counted") is False
    assert rec["EstimatedSavings"].startswith("$0.00/month")


# --------------------------------------------------------------------------- #
# opensearch C2 — idle domain priced at full domain cost, wins dedup
# --------------------------------------------------------------------------- #
def test_opensearch_idle_domain_full_cost_beats_graviton(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "DomainName": "idle-dom",
            "InstanceType": "r6g.large.search",
            "InstanceCount": 3,
            "CheckCategory": "Idle Domain",
            "IdleCorroborated": True,  # SearchRate + IndexingRate ~ 0 (truly idle)
        },
        {
            "DomainName": "idle-dom",
            "InstanceType": "r6g.large.search",
            "CheckCategory": "Graviton Migration",
        },
    ]
    monkeypatch.setattr(
        opensearch_adapter,
        "get_enhanced_opensearch_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    pricing = _FakePricing(es_monthly=150.0)
    findings = opensearch_adapter.OpensearchModule().scan(_ctx(pricing_engine=pricing))
    # Idle = full domain cost = 150 × 3 = $450 (Graviton = 150 × 0.25 = $37.50).
    # Idle wins the per-domain dedup; total = $450.
    assert findings.total_monthly_savings == pytest.approx(450.0, abs=0.01)
    idle = next(r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == "Idle Domain")
    graviton = next(
        r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == "Graviton Migration"
    )
    assert idle.get("Counted") is True
    assert graviton.get("Counted") is False


def test_opensearch_idle_domain_uncorroborated_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A domain at low CPU but WITHOUT request-level corroboration (the default,
    e.g. it is still serving searches, or the metric was unavailable) is rendered
    as a $0 advisory, never a counted DELETE — the irreversible-action safety gate."""
    recs = [
        {
            "DomainName": "busy-but-low-cpu",
            "InstanceType": "c5.2xlarge.search",
            "InstanceCount": 2,
            "EBSVolumeSize": 150,
            "CheckCategory": "Idle Domain",
            "IdleCorroborated": False,  # search/indexing activity present (or no metric)
        },
    ]
    monkeypatch.setattr(
        opensearch_adapter,
        "get_enhanced_opensearch_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = opensearch_adapter.OpensearchModule().scan(_ctx(pricing_engine=_FakePricing(es_monthly=400.0)))
    # The DELETE is not counted (no corroboration); headline stays $0.
    assert findings.total_monthly_savings == pytest.approx(0.0, abs=0.01)
    idle = next(r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == "Idle Domain")
    assert idle.get("Counted") is False  # rendered as advisory, not a counted delete


# --------------------------------------------------------------------------- #
# dynamodb C1 — one counted winner per table + cap at monthly_current
# --------------------------------------------------------------------------- #
def test_dynamodb_one_counted_winner_per_table(monkeypatch: pytest.MonkeyPatch) -> None:
    # DynamoDB H1/H2 (supersedes the prior C1 "reserved wins at 0.66" behavior):
    # Reserved Capacity is a commitment lever -> advisory, never counted; and an
    # over-provisioned rec WITHOUT CloudWatch low-utilization evidence is a $0
    # advisory, not a blanket-factor counted dollar. With no metric fields on
    # either hand-built rec, the headline must net $0.
    enhanced_recs = [
        {
            "TableName": "t1",
            "ReadCapacityUnits": 200,
            "WriteCapacityUnits": 200,
            "CheckCategory": "DynamoDB Reserved Capacity",
        },
        {
            "TableName": "t1",
            "ReadCapacityUnits": 200,
            "WriteCapacityUnits": 200,
            "CheckCategory": "DynamoDB Over-Provisioned Capacity",
        },
    ]
    monkeypatch.setattr(
        dynamodb_adapter,
        "get_dynamodb_table_analysis",
        lambda ctx: {"optimization_opportunities": []},
    )
    monkeypatch.setattr(
        dynamodb_adapter,
        "get_enhanced_dynamodb_checks",
        lambda ctx: {"recommendations": [dict(r) for r in enhanced_recs]},
    )
    findings = dynamodb_adapter.DynamoDbModule().scan(_ctx(pricing_multiplier=1.0))
    recs = findings.sources["enhanced_checks"].recommendations
    reserved = next(r for r in recs if r["CheckCategory"] == "DynamoDB Reserved Capacity")
    over = next(r for r in recs if r["CheckCategory"] == "DynamoDB Over-Provisioned Capacity")
    assert reserved.get("Counted") is False  # H2: commitment lever, never counted
    assert over.get("Counted") is False  # H1: no CloudWatch evidence -> advisory
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# api_gateway C1 — $0 rec labeled advisory (Counted=False)
# --------------------------------------------------------------------------- #
def test_api_gateway_zero_savings_rec_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "ApiId": "a1",
            "Recommendation": "REST→HTTP migration",
            "EstimatedMonthlySavings": 0.0,
            "CheckCategory": "REST to HTTP Migration",
        }
    ]
    monkeypatch.setattr(
        api_gateway_adapter,
        "get_enhanced_api_gateway_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    findings = api_gateway_adapter.ApiGatewayModule().scan(_ctx(pricing_multiplier=1.0))
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec.get("Counted") is False
    assert findings.total_monthly_savings == 0.0
