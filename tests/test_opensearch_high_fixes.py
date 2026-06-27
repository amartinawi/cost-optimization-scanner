"""Unit tests for the OpenSearch adapter HIGH cost-audit fixes (H1, H3, C3).

Mirrors the SimpleNamespace-ctx + monkeypatched-enhanced-checks +
fake-boto3-client style of ``tests/test_audit_fixes_counted_dollars.py`` and
``tests/test_lambda_audit_fixes.py``. Every counted dollar (or advisory $0) is
proven with an explicit assertion, not inferred from a golden fixture.

Findings covered:

  - H1  Graviton saving scales by the real data-node count: the shim carries
        ``ClusterConfig.InstanceCount`` onto the graviton rec and the adapter
        multiplies the per-node price by it (a 6-node domain is no longer priced
        as 1 node).
  - H3  gp2 -> gp3 storage saving is the exact ``(gp2_rate - gp3_rate)`` migration
        delta (region-scaled), NOT a flat 20% of the gp3 base. Rates validated
        live against the AWS Pricing API (AmazonES "Amazon OpenSearch Service
        Volume": GP3 $0.122/GB-Mo, GP2 $0.135/GB-Mo).
  - C3  Underutilized Domain is priced as a concrete current -> one-size-down node
        price delta; when the downsize target cannot be priced it is rendered as
        an explicit $0 advisory (Counted=False), never silently dropped.

The OpenSearch shim uses the non-paginated ``list_domain_names`` /
``describe_domain`` / ``get_metric_statistics`` APIs, so the shim tests drive
fake boto3 clients directly rather than paginators.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.opensearch as opensearch_adapter
import services.opensearch as opensearch_shim
from services.adapters.opensearch import (
    GP2_PRICE_PER_GB_MONTH,
    GP3_PRICE_PER_GB_MONTH,
    GRAVITON_RATE,
    OpensearchModule,
    _downsize_node_delta,
    _one_size_down,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePricing:
    """Returns OpenSearch (AmazonES) monthly prices keyed by instance type."""

    def __init__(self, prices: dict[str, float] | None = None, default: float = 100.0) -> None:
        self._prices = prices or {}
        self._default = default

    def get_instance_monthly_price(self, service_code: str, instance_type: str, *, engine: Any = None) -> float:
        if service_code != "AmazonES":
            return 0.0
        return self._prices.get(instance_type, self._default)


def _ctx(*, pricing_multiplier: float = 1.0, pricing_engine: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        pricing_engine=pricing_engine if pricing_engine is not None else _FakePricing(),
        pricing_multiplier=pricing_multiplier,
        region="us-east-1",
        account_id="123456789012",
        fast_mode=False,
        cost_hub_splits={},
        warnings=[],
        warn=lambda message, service=None: None,
    )


def _scan_with(recs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, **ctx_kw: Any):
    monkeypatch.setattr(
        opensearch_adapter,
        "get_enhanced_opensearch_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )
    return OpensearchModule().scan(_ctx(**ctx_kw))


def _by_category(findings) -> dict[str, dict[str, Any]]:
    return {r["CheckCategory"]: r for r in findings.sources["enhanced_checks"].recommendations}


# --------------------------------------------------------------------------- #
# Pure logic — one-size-down ladder
# --------------------------------------------------------------------------- #
def test_one_size_down_steps_the_search_suffix_ladder() -> None:
    assert _one_size_down("r6g.2xlarge.search") == "r6g.xlarge.search"
    assert _one_size_down("m5.xlarge.search") == "m5.large.search"
    assert _one_size_down("c5.large.search") == "c5.medium.search"
    # legacy .elasticsearch suffix is preserved
    assert _one_size_down("r5.4xlarge.elasticsearch") == "r5.2xlarge.elasticsearch"


def test_one_size_down_returns_none_at_floor_or_unparseable() -> None:
    assert _one_size_down("t3.micro.search") is None  # smallest rung
    assert _one_size_down("weird") is None
    assert _one_size_down("") is None
    assert _one_size_down(None) is None
    assert _one_size_down("r6g.bogussize.search") is None  # size not on ladder


# --------------------------------------------------------------------------- #
# Pure logic — concrete downsize delta (C3)
# --------------------------------------------------------------------------- #
def test_downsize_node_delta_is_current_minus_target() -> None:
    pricing = _FakePricing({"r6g.2xlarge.search": 200.0, "r6g.xlarge.search": 100.0})
    delta, target = _downsize_node_delta(_ctx(pricing_engine=pricing), "r6g.2xlarge.search")
    assert target == "r6g.xlarge.search"
    assert delta == pytest.approx(100.0)


def test_downsize_node_delta_abstains_when_target_unpriceable() -> None:
    # target size priced 0 (does not exist for the family) -> fail safe.
    pricing = _FakePricing({"r6g.large.search": 100.0}, default=0.0)
    delta, target = _downsize_node_delta(_ctx(pricing_engine=pricing), "r6g.large.search")
    assert (delta, target) == (0.0, None)
    # no pricing engine / no type -> abstain.
    assert _downsize_node_delta(SimpleNamespace(pricing_engine=None), "r6g.large.search") == (0.0, None)
    assert _downsize_node_delta(_ctx(), None) == (0.0, None)


# --------------------------------------------------------------------------- #
# H1 — Graviton saving scales by the real data-node count
# --------------------------------------------------------------------------- #
def test_graviton_scales_by_instance_count(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "DomainName": "logs",
            "InstanceType": "r5.large.search",
            "InstanceCount": 6,  # carried from ClusterConfig by the shim (H1)
            "CheckCategory": "Graviton Migration",
        }
    ]
    findings = _scan_with(recs, monkeypatch, pricing_engine=_FakePricing(default=120.0))
    rec = _by_category(findings)["Graviton Migration"]
    # 120 * 6 nodes * 0.25 = 180.00 (NOT 120 * 1 * 0.25 = 30.00, the 1-node bug).
    assert rec["EstimatedMonthlySavings"] == pytest.approx(120.0 * 6 * GRAVITON_RATE)
    assert rec["EstimatedMonthlySavings"] == pytest.approx(180.0)
    assert rec["Counted"] is True
    assert findings.total_monthly_savings == pytest.approx(180.0)
    assert rec["AuditBasis"]["instance_count"] == 6


def test_graviton_default_count_is_one_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [{"DomainName": "d", "InstanceType": "r5.large.search", "CheckCategory": "Graviton Migration"}]
    findings = _scan_with(recs, monkeypatch, pricing_engine=_FakePricing(default=120.0))
    rec = _by_category(findings)["Graviton Migration"]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(120.0 * GRAVITON_RATE)  # 30.00


# --------------------------------------------------------------------------- #
# H3 — gp2 -> gp3 storage saving is the exact rate delta, region-scaled
# --------------------------------------------------------------------------- #
def test_storage_uses_gp2_gp3_delta_not_flat_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "DomainName": "d",
            "StorageType": "gp2",
            "EBSVolumeSize": 1000,
            "CheckCategory": "Storage Optimization",
        }
    ]
    findings = _scan_with(recs, monkeypatch)
    rec = _by_category(findings)["Storage Optimization"]
    expected = 1000 * (GP2_PRICE_PER_GB_MONTH - GP3_PRICE_PER_GB_MONTH)  # 1000 * 0.013 = 13.00
    assert rec["EstimatedMonthlySavings"] == pytest.approx(expected)
    assert rec["EstimatedMonthlySavings"] == pytest.approx(13.0)
    # Must NOT be the old flat-20%-of-gp3-base figure (1000 * 0.122 * 0.20 = 24.40).
    assert rec["EstimatedMonthlySavings"] != pytest.approx(1000 * GP3_PRICE_PER_GB_MONTH * 0.20)
    assert rec["Counted"] is True
    ab = rec["AuditBasis"]
    assert ab["gp2_rate_per_gb_month"] == GP2_PRICE_PER_GB_MONTH
    assert ab["gp3_rate_per_gb_month"] == GP3_PRICE_PER_GB_MONTH
    assert ab["delta_rate_per_gb_month"] == pytest.approx(0.013)


def test_storage_delta_is_region_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [{"DomainName": "d", "EBSVolumeSize": 1000, "CheckCategory": "Storage Optimization"}]
    findings = _scan_with(recs, monkeypatch, pricing_multiplier=1.25)
    rec = _by_category(findings)["Storage Optimization"]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(1000 * 0.013 * 1.25)  # 16.25


# --------------------------------------------------------------------------- #
# C3 — Underutilized Domain: concrete downsize delta OR explicit $0 advisory
# --------------------------------------------------------------------------- #
def test_underutilized_priced_as_concrete_downsize_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "DomainName": "slow",
            "InstanceType": "r6g.2xlarge.search",
            "InstanceCount": 2,
            "CheckCategory": "Underutilized Domain",
        }
    ]
    pricing = _FakePricing({"r6g.2xlarge.search": 200.0, "r6g.xlarge.search": 100.0})
    findings = _scan_with(recs, monkeypatch, pricing_engine=pricing)
    rec = _by_category(findings)["Underutilized Domain"]
    # (200 - 100) per node * 2 nodes = 200.00 (NOT a 0.30 reduction factor of cost).
    assert rec["EstimatedMonthlySavings"] == pytest.approx(200.0)
    assert rec["Counted"] is True
    assert findings.total_monthly_savings == pytest.approx(200.0)
    ab = rec["AuditBasis"]
    assert ab["current_type"] == "r6g.2xlarge.search"
    assert ab["target_type"] == "r6g.xlarge.search"
    assert ab["per_node_delta_monthly"] == pytest.approx(100.0)
    assert ab["instance_count"] == 2


def test_underutilized_unpriceable_target_is_zero_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    # large -> medium target priced 0 (does not exist for r6g) => $0 advisory.
    recs = [
        {
            "DomainName": "slow",
            "InstanceType": "r6g.large.search",
            "InstanceCount": 3,
            "CheckCategory": "Underutilized Domain",
        }
    ]
    pricing = _FakePricing({"r6g.large.search": 100.0}, default=0.0)
    findings = _scan_with(recs, monkeypatch, pricing_engine=pricing)
    rec = _by_category(findings)["Underutilized Domain"]
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["Counted"] is False
    assert rec["EstimatedSavings"].startswith("$0.00/month — advisory")
    assert "AuditBasis" not in rec  # no defensible delta -> no basis
    assert findings.total_monthly_savings == 0.0
    # Still rendered (advisory, not a silent drop).
    assert rec in findings.sources["enhanced_checks"].recommendations


def test_underutilized_no_instance_type_is_zero_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [{"DomainName": "slow", "InstanceType": None, "CheckCategory": "Underutilized Domain"}]
    findings = _scan_with(recs, monkeypatch)
    rec = _by_category(findings)["Underutilized Domain"]
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["Counted"] is False


def test_underutilized_beats_graviton_in_per_domain_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same domain: a concrete 50%-style downsize delta should outrank the 25%
    # graviton proxy and be the single counted instance lever.
    recs = [
        {
            "DomainName": "slow",
            "InstanceType": "r6g.2xlarge.search",
            "InstanceCount": 1,
            "CheckCategory": "Graviton Migration",
        },
        {
            "DomainName": "slow",
            "InstanceType": "r6g.2xlarge.search",
            "InstanceCount": 1,
            "CheckCategory": "Underutilized Domain",
        },
    ]
    pricing = _FakePricing({"r6g.2xlarge.search": 200.0, "r6g.xlarge.search": 100.0})
    findings = _scan_with(recs, monkeypatch, pricing_engine=pricing)
    cats = _by_category(findings)
    assert cats["Underutilized Domain"]["Counted"] is True  # delta 100 wins
    assert cats["Graviton Migration"]["Counted"] is False  # 200*0.25=50 superseded
    assert findings.total_monthly_savings == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# Shim — H1/C3: the recs carry the fields the adapter needs to price them
# --------------------------------------------------------------------------- #
class _FakeOpenSearchClient:
    def __init__(self, domain_status: dict[str, Any]) -> None:
        self._status = domain_status

    def list_domain_names(self) -> dict[str, Any]:
        return {"DomainNames": [{"DomainName": "d1"}]}

    def describe_domain(self, DomainName: str) -> dict[str, Any]:  # noqa: N803 - boto3 shape
        return {"DomainStatus": self._status}


class _FakeCloudWatch:
    def __init__(self, avg_cpu: float) -> None:
        self._avg = avg_cpu

    def get_metric_statistics(self, **_kw: Any) -> dict[str, Any]:
        return {"Datapoints": [{"Average": self._avg}]}


def _shim_ctx(domain_status: dict[str, Any], avg_cpu: float) -> SimpleNamespace:
    clients = {
        "opensearch": _FakeOpenSearchClient(domain_status),
        "cloudwatch": _FakeCloudWatch(avg_cpu),
    }
    return SimpleNamespace(
        client=lambda name: clients[name],
        account_id="123456789012",
        warn=lambda message, service=None: None,
    )


def test_shim_carries_instance_count_on_graviton_and_underutilized() -> None:
    status = {
        "EngineVersion": "OpenSearch_2.11",
        "ClusterConfig": {"InstanceType": "r5.large.search", "InstanceCount": 6},
        "EBSOptions": {"VolumeType": "gp2", "VolumeSize": 500},
    }
    # avg_cpu 12 => underutilized (5 <= cpu < 20).
    result = opensearch_shim.get_enhanced_opensearch_checks(_shim_ctx(status, avg_cpu=12.0))
    by_cat = {r["CheckCategory"]: r for r in result["recommendations"]}

    grav = by_cat["Graviton Migration"]
    assert grav["InstanceCount"] == 6  # H1: count carried from ClusterConfig
    assert grav["InstanceType"] == "r5.large.search"

    under = by_cat["Underutilized Domain"]
    assert under["InstanceType"] == "r5.large.search"  # C3: type carried
    assert under["InstanceCount"] == 6  # C3: count carried

    storage = by_cat["Storage Optimization"]
    assert storage["EBSVolumeSize"] == 500  # H3: GB carried for the delta


def test_shim_to_adapter_end_to_end_prices_every_lever(monkeypatch: pytest.MonkeyPatch) -> None:
    status = {
        "EngineVersion": "OpenSearch_2.11",
        "ClusterConfig": {"InstanceType": "r5.2xlarge.search", "InstanceCount": 4},
        "EBSOptions": {"VolumeType": "gp2", "VolumeSize": 1000},
    }
    monkeypatch.setattr(
        opensearch_adapter,
        "get_enhanced_opensearch_checks",
        lambda ctx: opensearch_shim.get_enhanced_opensearch_checks(_shim_ctx(status, avg_cpu=12.0)),
    )
    pricing = _FakePricing({"r5.2xlarge.search": 400.0, "r5.xlarge.search": 200.0})
    findings = OpensearchModule().scan(_ctx(pricing_engine=pricing))
    cats = _by_category(findings)

    # Underutilized downsize delta: (400 - 200) * 4 = 800 (the counted instance lever).
    assert cats["Underutilized Domain"]["EstimatedMonthlySavings"] == pytest.approx(800.0)
    assert cats["Underutilized Domain"]["Counted"] is True
    # Graviton: 400 * 4 * 0.25 = 400 -> superseded by the downsize lever.
    assert cats["Graviton Migration"]["Counted"] is False
    # Storage delta is a separate axis: 1000 * 0.013 = 13.00, counted.
    assert cats["Storage Optimization"]["EstimatedMonthlySavings"] == pytest.approx(13.0)
    assert cats["Storage Optimization"]["Counted"] is True
    # Total = downsize 800 + storage 13 = 813.00.
    assert findings.total_monthly_savings == pytest.approx(813.0)
