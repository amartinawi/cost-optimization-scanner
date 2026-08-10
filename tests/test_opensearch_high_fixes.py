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

import pytest

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
    OpensearchModule,
    _downsize_node_delta,
    _graviton_equivalent,
    _graviton_node_delta,
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
    # c5.medium.search is NOT a real SKU (c5 floors at large) — the old
    # assertion pinned a nonexistent downsize target (bnc live audit).
    assert _one_size_down("c5.large.search") is None
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
    # H4: counted dollar is the exact x86->Graviton node delta, per node.
    # r5.large.search $120 - r6g.large.search $110 = $10/node x 6 = $60.
    pricing = _FakePricing({"r5.large.search": 120.0, "r6g.large.search": 110.0})
    findings = _scan_with(recs, monkeypatch, pricing_engine=pricing)
    rec = _by_category(findings)["Graviton Migration"]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(60.0)
    assert rec["Counted"] is True
    assert findings.total_monthly_savings == pytest.approx(60.0)
    assert rec["AuditBasis"]["instance_count"] == 6
    assert rec["AuditBasis"]["target_type"] == "r6g.large.search"
    assert rec["AuditBasis"]["per_node_delta_monthly"] == pytest.approx(10.0)


def test_graviton_default_count_is_one_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [{"DomainName": "d", "InstanceType": "r5.large.search", "CheckCategory": "Graviton Migration"}]
    pricing = _FakePricing({"r5.large.search": 120.0, "r6g.large.search": 110.0})
    findings = _scan_with(recs, monkeypatch, pricing_engine=pricing)
    rec = _by_category(findings)["Graviton Migration"]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(10.0)  # delta x 1 node


def test_graviton_equivalent_maps_x86_family_same_size() -> None:
    assert _graviton_equivalent("r5.xlarge.search") == "r6g.xlarge.search"
    assert _graviton_equivalent("m5.large.search") == "m6g.large.search"
    assert _graviton_equivalent("c5.2xlarge.search") == "c6g.2xlarge.search"
    assert _graviton_equivalent("t3.medium.search") == "t4g.medium.search"
    # already-Graviton or unmappable -> None (caller emits $0 advisory).
    assert _graviton_equivalent("r6g.large.search") is None
    assert _graviton_equivalent("i3.large.search") is None
    assert _graviton_equivalent(None) is None
    assert _graviton_equivalent("weird") is None


def test_graviton_node_delta_abstains_when_target_unpriceable() -> None:
    # x86 with no priced Graviton counterpart -> (0.0, None) fail safe.
    pricing = _FakePricing({"r5.large.search": 120.0}, default=0.0)
    assert _graviton_node_delta(_ctx(pricing_engine=pricing), "r5.large.search") == (0.0, None)
    # already-Graviton -> no x86->Graviton mapping -> abstain.
    assert _graviton_node_delta(_ctx(pricing_engine=_FakePricing()), "r6g.large.search") == (0.0, None)
    assert _graviton_node_delta(SimpleNamespace(pricing_engine=None), "r5.large.search") == (0.0, None)


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


def test_storage_delta_multiplies_data_node_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """OS-2: EBSOptions.VolumeSize is PER DATA NODE (AWS EBSOptions docs); a
    6-node domain bills 6x the per-node volume. Pricing one volume under-counted
    the gp2->gp3 delta by the node count."""
    recs = [
        {
            "DomainName": "d",
            "StorageType": "gp2",
            "EBSVolumeSize": 500,
            "InstanceCount": 6,
            "CheckCategory": "Storage Optimization",
        }
    ]
    findings = _scan_with(recs, monkeypatch)
    rec = _by_category(findings)["Storage Optimization"]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(500 * 6 * 0.013)  # 39.00
    assert rec["AuditBasis"]["instance_count"] == 6


def test_idle_domain_storage_leg_multiplies_data_node_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """OS-2 (idle branch): the instance leg already multiplies count; the
    storage leg must too — the recoverable EBS on delete is per-node volume x
    node count."""
    recs = [
        {
            "DomainName": "idle",
            "InstanceType": "r5.large.search",
            "InstanceCount": 6,
            "EBSVolumeSize": 500,
            "IdleCorroborated": True,
            "CheckCategory": "Idle Domain",
        }
    ]
    pricing = _FakePricing({"r5.large.search": 100.0})
    findings = _scan_with(recs, monkeypatch, pricing_engine=pricing)
    rec = _by_category(findings)["Idle Domain"]
    expected = 100.0 * 6 + 500 * 6 * GP3_PRICE_PER_GB_MONTH  # 600 + 366 = 966
    assert rec["EstimatedMonthlySavings"] == pytest.approx(expected)


def test_extended_support_ce_read_is_region_scoped():
    """OS-1: the ce client is account-global; without a REGION dimension the
    account-wide surcharge is re-counted in EVERY scanned region, including
    regions with zero domains. Assert on the REQUEST kwargs, not the mocked
    response (the C8-corollary trap: a wrong filter returns plausible data)."""
    from types import SimpleNamespace

    from services.adapters.opensearch import _extended_support_breakdown

    ce = _ce_with(
        [("EUW1-OpenSearchExtendedSupport", 10.0)],
        resource_error=RuntimeError("resource granularity disabled"),
    )
    ctx = SimpleNamespace(client=lambda _n: ce, warn=lambda *a, **k: None, region="eu-west-1")
    _extended_support_breakdown(ctx)

    f = ce.get_cost_and_usage.call_args.kwargs["Filter"]
    clauses = f.get("And", [f])
    region_clauses = [c for c in clauses if c.get("Dimensions", {}).get("Key") == "REGION"]
    assert region_clauses, f"CE read must be region-scoped; Filter was {f}"
    assert region_clauses[0]["Dimensions"]["Values"] == ["eu-west-1"]


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
    # Same domain: the larger concrete downsize delta outranks the smaller
    # Graviton node delta and is the single counted instance lever.
    recs = [
        {
            "DomainName": "slow",
            "InstanceType": "r5.2xlarge.search",
            "InstanceCount": 1,
            "CheckCategory": "Graviton Migration",
        },
        {
            "DomainName": "slow",
            "InstanceType": "r5.2xlarge.search",
            "InstanceCount": 1,
            "CheckCategory": "Underutilized Domain",
        },
    ]
    # downsize r5.2xlarge $200 -> r5.xlarge $100 = $100 delta;
    # graviton r5.2xlarge $200 -> r6g.2xlarge $190 = $10 delta (loses).
    pricing = _FakePricing(
        {"r5.2xlarge.search": 200.0, "r5.xlarge.search": 100.0, "r6g.2xlarge.search": 190.0}
    )
    findings = _scan_with(recs, monkeypatch, pricing_engine=pricing)
    cats = _by_category(findings)
    assert cats["Underutilized Domain"]["Counted"] is True  # delta 100 wins
    assert cats["Graviton Migration"]["Counted"] is False  # delta 10 superseded
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
    # r6g.2xlarge.search priced just below the x86 node so the Graviton delta is
    # small and the downsize lever wins.
    pricing = _FakePricing(
        {"r5.2xlarge.search": 400.0, "r5.xlarge.search": 200.0, "r6g.2xlarge.search": 380.0}
    )
    findings = OpensearchModule().scan(_ctx(pricing_engine=pricing))
    cats = _by_category(findings)

    # Underutilized downsize delta: (400 - 200) * 4 = 800 (the counted instance lever).
    assert cats["Underutilized Domain"]["EstimatedMonthlySavings"] == pytest.approx(800.0)
    assert cats["Underutilized Domain"]["Counted"] is True
    # counted == rendered: the rendered EstimatedSavings string is the counted
    # dollar, NOT a stale "30-50%" price-performance figure (opensearch fix).
    assert cats["Underutilized Domain"]["EstimatedSavings"] == "$800.00/month"
    # Graviton node delta: (400 - 380) * 4 = 80 -> superseded by the downsize lever.
    assert cats["Graviton Migration"]["Counted"] is False
    assert cats["Graviton Migration"]["EstimatedSavings"].startswith("$0.00/month")
    # Storage delta is a separate axis, PER DATA NODE (OS-2): VolumeSize=1000
    # is per node, so 4 nodes bill 4,000 GB -> 1000 * 4 * 0.013 = 52.00, counted.
    assert cats["Storage Optimization"]["EstimatedMonthlySavings"] == pytest.approx(52.0)
    assert cats["Storage Optimization"]["Counted"] is True
    # Total = downsize 800 + storage 52 = 852.00.
    assert findings.total_monthly_savings == pytest.approx(852.0)


# --------------------------------------------------------------------------- #
# bnc live regression (2026-07-09): Extended Support surcharge is MEASURED
# --------------------------------------------------------------------------- #
def _ce_with(usage_types, resources=None, resource_error=None):
    from unittest.mock import MagicMock
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [{"Groups": [
            {"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v)}}} for k, v in usage_types
        ]}]
    }
    if resource_error is not None:
        ce.get_cost_and_usage_with_resources.side_effect = resource_error
    else:
        ce.get_cost_and_usage_with_resources.return_value = {
            "ResultsByTime": [{"Groups": [
                {"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v)}}} for k, v in (resources or [])
            ]}]
        }
    return ce


def test_extended_support_measured_from_billing_and_scaled():
    from types import SimpleNamespace
    from services.adapters.opensearch import _extended_support_breakdown

    ce = _ce_with([("APS1-ESInstance:m5.xlarge", 350.46),
                   ("APS1-OpenSearchExtendedSupport", 61.78),
                   ("APS1-ES:GP3-Storage", 19.48)],
                  resource_error=RuntimeError("resource granularity disabled"))
    ctx = SimpleNamespace(client=lambda _n: ce, warn=lambda *a, **k: None, region="ap-southeast-1")
    total, per_domain = _extended_support_breakdown(ctx)
    assert total == pytest.approx(61.78 / 7 * 30, rel=1e-6)   # ~$264.77/mo (bnc)
    assert per_domain == {}          # unattributed -> caller must name no domain


def test_extended_support_attributes_to_the_billed_domain():
    # bnc: production-bnc (OpenSearch_2.9) is the payer; staging-bnc-2 (3.3) is not.
    from types import SimpleNamespace
    from services.adapters.opensearch import _extended_support_breakdown

    ce = _ce_with(
        [("APS1-OpenSearchExtendedSupport", 61.78)],
        resources=[("arn:aws:es:ap-southeast-1:1:domain/production-bnc", 61.78),
                   ("NoResourceId", 0.0)],
    )
    ctx = SimpleNamespace(client=lambda _n: ce, warn=lambda *a, **k: None, region="ap-southeast-1")
    total, per_domain = _extended_support_breakdown(ctx)
    assert total == pytest.approx(61.78 / 7 * 30, rel=1e-6)
    assert list(per_domain) == ["production-bnc"]            # innocent domain not named
    assert per_domain["production-bnc"] == pytest.approx(61.78 / 7 * 30, rel=1e-6)


def test_extended_support_zero_when_not_billed():
    from types import SimpleNamespace
    from services.adapters.opensearch import _extended_support_breakdown

    ce = _ce_with([("APS1-ESInstance:m5.xlarge", 350.46)])
    ctx = SimpleNamespace(client=lambda _n: ce, warn=lambda *a, **k: None, region="ap-southeast-1")
    assert _extended_support_breakdown(ctx) == (0.0, {})


def test_extended_support_fails_closed_on_ce_error():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from services.adapters.opensearch import _extended_support_breakdown

    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = RuntimeError("AccessDenied")
    warns = []
    ctx = SimpleNamespace(client=lambda _n: ce, warn=lambda m, s=None: warns.append(m), region="ap-southeast-1")
    assert _extended_support_breakdown(ctx) == (0.0, {})   # never invent a charge
    assert warns


def test_one_size_down_respects_large_floor():
    """r5-class OpenSearch families have no size below large — probing
    r5.medium.search wasted a Pricing API call before failing closed (bnc)."""
    from services.adapters.opensearch import _one_size_down

    assert _one_size_down("r5.large.search") is None
    assert _one_size_down("r5.xlarge.search") == "r5.large.search"
    assert _one_size_down("t3.medium.search") is not None  # t3 goes smaller


def test_extended_support_fails_closed_without_region():
    """Review: an account-wide CE read IS the OS-1 phantom — no region, no
    surcharge measurement (warn + (0.0, {}))."""
    from types import SimpleNamespace

    from services.adapters.opensearch import _extended_support_breakdown

    ce = _ce_with([("APS1-OpenSearchExtendedSupport", 61.78)])
    warns: list[str] = []
    ctx = SimpleNamespace(client=lambda _n: ce, warn=lambda m, s=None: warns.append(m))
    assert _extended_support_breakdown(ctx) == (0.0, {})
    assert warns and "region unset" in warns[0]
    ce.get_cost_and_usage.assert_not_called()


# --------------------------------------------------------------------------- #
# OS-5 / OS-4 — the two abstentions that hit the most expensive fleets
# --------------------------------------------------------------------------- #
def test_graviton_map_covers_current_generation_intel() -> None:
    """OS-5: the map stopped at m5/c5/r5/t3, so every current-gen Intel family
    had no Graviton target and the lever abstained on the newest fleets."""
    from services.adapters.opensearch import _graviton_family_for

    assert _graviton_family_for("m6i") == "m6g"
    assert _graviton_family_for("m7i") == "m7g"
    assert _graviton_family_for("r7i") == "r7g"
    assert _graviton_family_for("c7i") == "c7g"
    # Legacy families keep their existing targets.
    assert _graviton_family_for("m5") == "m6g"
    assert _graviton_family_for("t3") == "t4g"


def test_graviton_map_skips_families_that_are_already_graviton() -> None:
    from services.adapters.opensearch import _graviton_family_for

    for family in ("r6g", "m7g", "r6gd", "im4gn", "or1"):
        assert _graviton_family_for(family) is None, family


def test_smaller_sizes_walks_past_a_gap_in_the_family_ladder() -> None:
    """OS-4: OpenSearch m5 offers no 8xlarge, so a one-rung step from
    m5.12xlarge probed a nonexistent size and abstained - on the single most
    expensive node in the domain."""
    from services.adapters.opensearch import _smaller_sizes

    ladder = _smaller_sizes("m5.12xlarge.search")
    assert ladder[0] == "m5.8xlarge.search"
    assert "m5.4xlarge.search" in ladder
    assert ladder[-1] == "m5.large.search"  # size floor honoured


def test_smaller_sizes_honours_the_large_floor() -> None:
    from services.adapters.opensearch import _smaller_sizes

    assert all(".medium." not in t for t in _smaller_sizes("r5.xlarge.search"))
    # A family with no floor still reaches the small rungs.
    assert "t3.micro.search" in _smaller_sizes("t3.medium.search")


def test_downsize_delta_skips_an_unpriceable_rung() -> None:
    from types import SimpleNamespace

    from services.adapters.opensearch import _downsize_node_delta

    prices = {"m5.12xlarge.search": 2000.0, "m5.4xlarge.search": 700.0}

    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(
            get_instance_monthly_price=lambda code, itype, **kw: prices.get(itype, 0.0)
        )
    )
    delta, target = _downsize_node_delta(ctx, "m5.12xlarge.search")
    # 8xlarge does not price, so the walk continues to 4xlarge.
    assert target == "m5.4xlarge.search"
    assert delta == pytest.approx(1300.0)


def test_downsize_delta_abstains_when_nothing_below_prices() -> None:
    from types import SimpleNamespace

    from services.adapters.opensearch import _downsize_node_delta

    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(
            get_instance_monthly_price=lambda code, itype, **kw: 2000.0 if "12xlarge" in itype else 0.0
        )
    )
    assert _downsize_node_delta(ctx, "m5.12xlarge.search") == (0.0, None)


def test_downsize_delta_abstains_when_the_smaller_size_is_not_cheaper() -> None:
    from types import SimpleNamespace

    from services.adapters.opensearch import _downsize_node_delta

    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(get_instance_monthly_price=lambda code, itype, **kw: 900.0)
    )
    assert _downsize_node_delta(ctx, "m5.12xlarge.search") == (0.0, None)


def _scan_idle_with(mod, ctx, checks: dict):
    """Drive OpensearchModule.scan with a stubbed shim result (OS-7/OS-9 tests).

    Named distinctly from the file's existing ``_scan_with`` helper, which has a
    different signature.
    """
    recs = [r for v in checks.values() for r in v]
    original = mod.get_enhanced_opensearch_checks
    try:
        mod.get_enhanced_opensearch_checks = lambda _c: {"recommendations": recs, "checks": checks}
        return mod.OpensearchModule().scan(ctx)
    finally:
        mod.get_enhanced_opensearch_checks = original



# --------------------------------------------------------------------------- #
# OS-7 / OS-9 — the idle-domain legs
# --------------------------------------------------------------------------- #
def _idle_rec(**kw):
    rec = {
        "DomainName": "logs",
        "InstanceType": "r6g.large.search",
        "InstanceCount": 3,
        "EBSVolumeSize": 100,
        "IdleCorroborated": True,
        "CheckCategory": "Idle Domain",
        "Recommendation": "Delete idle domain",
        "EstimatedSavings": "100% of domain cost",
    }
    rec.update(kw)
    return rec


def test_idle_domain_price_includes_master_and_warm_nodes() -> None:
    """OS-7: master and UltraWarm nodes bill on top of the data nodes and are
    deleted with the domain. A master tier is a common production default."""
    from types import SimpleNamespace

    import services.adapters.opensearch as mod

    rates = {"r6g.large.search": 100.0, "m6g.large.search": 90.0, "ultrawarm1.medium.search": 50.0}
    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(
            get_instance_monthly_price=lambda code, itype, **kw: rates.get(itype, 0.0)
        ),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=True,
        cost_hub_splits={},
        commitment_coverage=None,
    )
    ctx.client = lambda name, region=None: None
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None

    rec = _idle_rec(
        DedicatedMasterType="m6g.large.search",
        DedicatedMasterCount=3,
        WarmType="ultrawarm1.medium.search",
        WarmCount=2,
    )
    findings = _scan_idle_with(mod, ctx, {"idle_domains": [rec]})
    emitted = next(
        r
        for block in findings.sources.values()
        for r in block.recommendations
        if r["CheckCategory"] == "Idle Domain"
    )
    # 3x100 data + 100GB x 3 x gp3 storage + 3x90 master + 2x50 warm.
    assert emitted["AuditBasis"]["master_count"] == 3
    assert emitted["AuditBasis"]["warm_type"] == "ultrawarm1.medium.search"
    assert emitted["EstimatedMonthlySavings"] > 300 + 270 + 100


def test_master_leg_omitted_when_it_has_no_live_sku() -> None:
    from types import SimpleNamespace

    import services.adapters.opensearch as mod

    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(
            get_instance_monthly_price=lambda code, itype, **kw: 100.0 if itype == "r6g.large.search" else 0.0
        ),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=True,
        cost_hub_splits={},
        commitment_coverage=None,
    )
    ctx.client = lambda name, region=None: None
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None

    rec = _idle_rec(DedicatedMasterType="mystery.search", DedicatedMasterCount=3)
    findings = _scan_idle_with(mod, ctx, {"idle_domains": [rec]})
    emitted = next(
        r
        for block in findings.sources.values()
        for r in block.recommendations
        if r["CheckCategory"] == "Idle Domain"
    )
    assert "no live SKU" in emitted["AuditBasis"]["master_leg"]


def test_reservation_covered_idle_domain_keeps_its_storage_leg() -> None:
    """OS-9: an OpenSearch RI covers instance HOURS. Deleting the domain still
    frees every provisioned GB, so demoting the whole rec threw money away."""
    from types import SimpleNamespace

    import services.adapters.opensearch as mod
    from services.commitment_coverage import CommitmentCoverage

    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(
            get_instance_monthly_price=lambda code, itype, **kw: 100.0 if "r6g" in itype else 0.0
        ),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=True,
        cost_hub_splits={},
        commitment_coverage=CommitmentCoverage(
            # normalize_type strips the .search suffix, as the RI resolver does on the way in.
            opensearch_ri_types=frozenset({"r6g.large"})
        ),
    )
    ctx.client = lambda name, region=None: None
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None

    findings = _scan_idle_with(mod, ctx, {"idle_domains": [_idle_rec()]})
    emitted = next(
        r
        for block in findings.sources.values()
        for r in block.recommendations
        if r["CheckCategory"] == "Idle Domain"
    )
    storage = 100 * 3 * mod.GP3_PRICE_PER_GB_MONTH
    assert emitted["Counted"] is True
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(storage, abs=0.01)
    assert emitted["InstanceLegCoveredByReservation"] == pytest.approx(300.0, abs=0.01)
    assert "storage leg stays counted" in emitted["CommitmentCoverageNote"]
    assert findings.total_monthly_savings == pytest.approx(storage, abs=0.01)


def test_uncorroborated_idle_domain_stays_fully_advisory() -> None:
    """A rec demoted for lack of idle evidence has no evidence of idleness at
    all - the storage re-promotion must not resurrect it."""
    from types import SimpleNamespace

    import services.adapters.opensearch as mod

    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(get_instance_monthly_price=lambda code, itype, **kw: 100.0),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=True,
        cost_hub_splits={},
        commitment_coverage=None,
    )
    ctx.client = lambda name, region=None: None
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None

    findings = _scan_idle_with(mod, ctx, {"idle_domains": [_idle_rec(IdleCorroborated=False)]})
    emitted = next(
        r
        for block in findings.sources.values()
        for r in block.recommendations
        if r["CheckCategory"] == "Idle Domain"
    )
    assert emitted["Counted"] is False
    assert emitted["EstimatedMonthlySavings"] == 0.0


def test_repromoted_idle_domain_carries_no_stale_advisory_estimate() -> None:
    """The demotion parks the full gross in AdvisoryEstimate; on a rec that is
    counted again that would render as a property row claiming more than the
    card's own dollar."""
    from types import SimpleNamespace

    import services.adapters.opensearch as mod
    from services.commitment_coverage import CommitmentCoverage

    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(
            get_instance_monthly_price=lambda code, itype, **kw: 100.0 if "r6g" in itype else 0.0
        ),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=True,
        cost_hub_splits={},
        commitment_coverage=CommitmentCoverage(opensearch_ri_types=frozenset({"r6g.large"})),
    )
    ctx.client = lambda name, region=None: None
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None

    findings = _scan_idle_with(mod, ctx, {"idle_domains": [_idle_rec()]})
    emitted = next(
        r
        for block in findings.sources.values()
        for r in block.recommendations
        if r["CheckCategory"] == "Idle Domain"
    )
    assert "AdvisoryEstimate" not in emitted
    # counted == rendered at the field level.
    from services._savings import parse_dollar_savings

    assert parse_dollar_savings(emitted["EstimatedSavings"]) == pytest.approx(
        emitted["EstimatedMonthlySavings"], abs=0.01
    )
