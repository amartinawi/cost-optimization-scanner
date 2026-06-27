"""Tests for the network_cost HIGH remediation (H1 reduction factors, H2 TGW circular).

network_cost operates on **blended Cost Explorer dollars** with no per-flow GB,
co-location, or topology signal, so no fixed fraction of a transfer bill is
defensibly recoverable. These tests pin that every transfer/TGW lever is now a
``$0.00`` ``Counted=False`` advisory — rendered for action, never summed:

  H1 — cross-region (was ×0.30), cross-AZ (was ×0.50), egress (was ×0.40) recs
       carry $0 and ``Counted=False``; the measured spend is still surfaced.
  H2 — the mixed TGW/peering "route optimization" rec (which re-derived GB from
       the same dollars already scored) is $0 advisory, never a ~20% double-count.
  Net — a full scan with real spend in every bucket nets ``total_monthly_savings``
       of $0.00 while still rendering each opportunity.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.adapters.network_cost import NetworkCostModule


# --------------------------------------------------------------------------- #
# H1 — the three reduction-factor analyzers now emit $0 advisories
# --------------------------------------------------------------------------- #
def test_cross_region_is_zero_advisory() -> None:
    recs = NetworkCostModule()._analyze_cross_region(500.0, multiplier=1.0)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["monthly_savings"] == 0.0  # was 500 × 0.30 = 150
    assert rec["Counted"] is False
    # The real spend is still surfaced so a FinOps reader can act on it.
    assert "$500.00" in rec["current_value"]


def test_cross_az_is_zero_advisory() -> None:
    recs = NetworkCostModule()._analyze_cross_az(200.0, multiplier=1.0)
    assert len(recs) == 1
    assert recs[0]["monthly_savings"] == 0.0  # was 200 × 0.50 = 100
    assert recs[0]["Counted"] is False


def test_internet_egress_is_zero_advisory() -> None:
    recs = NetworkCostModule()._analyze_internet_egress(300.0, multiplier=1.0)
    assert len(recs) == 1
    assert recs[0]["monthly_savings"] == 0.0  # was 300 × 0.40 = 120
    assert recs[0]["Counted"] is False


def test_reduction_factors_ignore_multiplier() -> None:
    # A non-1.0 multiplier must not resurrect a counted dollar (CE returns real $).
    for analyze in ("_analyze_cross_region", "_analyze_cross_az", "_analyze_internet_egress"):
        recs = getattr(NetworkCostModule(), analyze)(1000.0, multiplier=1.5)
        assert recs[0]["monthly_savings"] == 0.0


def test_zero_spend_emits_no_rec() -> None:
    mod = NetworkCostModule()
    assert mod._analyze_cross_region(0.0, 1.0) == []
    assert mod._analyze_cross_az(0.0, 1.0) == []
    assert mod._analyze_internet_egress(0.0, 1.0) == []


# --------------------------------------------------------------------------- #
# H2 — the circular TGW route-optimization branch is a $0 advisory
# --------------------------------------------------------------------------- #
def test_tgw_route_optimization_is_zero_advisory() -> None:
    usage = {"cross_region": 400.0, "cross_az": 100.0}
    recs = NetworkCostModule()._analyze_tgw_vs_peering(
        peering_count=2, tgw_count=1, usage_breakdown=usage, multiplier=1.0
    )
    route = [r for r in recs if r["resource_id"] == "tgw-route-optimization"]
    assert len(route) == 1
    # Old behaviour: (500 / 0.02) × 0.02 × 0.20 = $100 counted (20% of the same
    # cross-region+cross-AZ dollars already scored). Must now be $0.
    assert route[0]["monthly_savings"] == 0.0
    assert route[0]["Counted"] is False


def test_tgw_advisory_cards_never_counted() -> None:
    # The two pre-existing TGW/peering advisory cards already carried $0.
    recs = NetworkCostModule()._analyze_tgw_vs_peering(
        peering_count=0, tgw_count=3, usage_breakdown={}, multiplier=1.0
    )
    assert all(r["monthly_savings"] == 0.0 for r in recs)


# --------------------------------------------------------------------------- #
# Net — a full scan with real spend in every bucket nets $0 counted savings
# --------------------------------------------------------------------------- #
def _ce_with_spend() -> MagicMock:
    """A Cost Explorer client whose transfer query hits all three buckets."""
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "Groups": [
                    {"Keys": ["USE1-DataTransfer-Regional-Bytes"], "Metrics": {"UnblendedCost": {"Amount": "200"}}},
                    {"Keys": ["USE1-USW2-AWS-Out-Bytes"], "Metrics": {"UnblendedCost": {"Amount": "400"}}},
                    {"Keys": ["USE1-DataTransfer-Out-Bytes"], "Metrics": {"UnblendedCost": {"Amount": "300"}}},
                ]
            }
        ]
    }
    return ce


def _ec2_with_topology() -> MagicMock:
    ec2 = MagicMock()
    ec2.describe_vpc_peering_connections.return_value = {"VpcPeeringConnections": [{"id": "pcx-1"}, {"id": "pcx-2"}]}
    ec2.describe_transit_gateways.return_value = {"TransitGateways": [{"id": "tgw-1"}]}
    return ec2


def test_full_scan_nets_zero_counted_but_renders_opportunities() -> None:
    ce, ec2 = _ce_with_spend(), _ec2_with_topology()
    ctx = SimpleNamespace(
        pricing_multiplier=1.0,
        client=lambda name, region=None: {"ce": ce, "ec2": ec2}.get(name),
        warn=lambda *a, **k: None,
        permission_issue=lambda *a, **k: None,
    )
    findings = NetworkCostModule().scan(ctx)

    # Real spend is surfaced (200 + 400 + 300 = 900) but NONE of it is counted:
    # the old factors would have totalled 100 + 120 + 90 + 100(tgw) = $410.
    assert findings.total_monthly_savings == 0.0
    assert findings.extras["total_data_transfer_spend_30d"] == 900.0

    # counted == rendered: each opportunity still renders as an advisory.
    assert findings.sources["cross_region_transfer"].count == 1
    assert findings.sources["cross_az_transfer"].count == 1
    assert findings.sources["internet_egress"].count == 1
    every_rec = [
        r
        for block in findings.sources.values()
        for r in block.recommendations
    ]
    assert every_rec, "scan should surface advisory recs"
    assert all(r.get("Counted") is False for r in every_rec)
    assert all(r.get("monthly_savings", 0.0) == 0.0 for r in every_rec)
