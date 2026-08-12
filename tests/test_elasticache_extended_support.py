"""ElastiCache Extended Support surcharge — measured from Cost Explorer.

level-Shoes-prod live regression (2026-08-12, eu-west-1): the account pays
$725.62/mo of ElastiCache Extended Support on four Redis 5.0.6 nodes and the
report showed **$0** of it — 34% of that report's entire headline, unreported,
while the SAME report invented a $365/mo EKS surcharge AWS was not billing.

Measured, never inferred from engine-version numbers (that is precisely the
EKS Extended-Support bug, twice over). AWS bills the surcharge as its own
usage type, which — unlike OpenSearch's — embeds the NODE TYPE:

    EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.r6g.xlarge   $544.61 / 1488 hr
    EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.m6g.large    $194.93 / 1488 hr

so the charge is attributable to the clusters running that node type without
needing CE resource-level granularity.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.adapters.elasticache import (
    _extended_support_breakdown,
    _is_elasticache_extended_support_usage_type,
    _node_type_from_usage_type,
)


# Real level-Shoes-prod eu-west-1 rows, trailing 7 days (July run rate / 744 * 168).
_ROWS = [
    ("EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.r6g.xlarge", "122.98"),
    ("EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.m6g.large", "44.02"),
    ("EU-NodeUsage:cache.r6g.xlarge", "153.88"),      # base node cost — NOT a surcharge
    ("EU-NodeUsage:cache.m5.large", "57.79"),         # Redis 7.1.0 — no surcharge
]


def _ce(rows=_ROWS):
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [{"Groups": [
            {"Keys": [ut], "Metrics": {"UnblendedCost": {"Amount": amt}}} for ut, amt in rows
        ]}]
    }
    return ce


def _ctx(ce=None, region="eu-west-1") -> Any:
    ns = SimpleNamespace(region=region, pricing_multiplier=1.0, fast_mode=False)
    ns.client = lambda name, region=None: ce if name == "ce" else MagicMock()
    ns.warn = MagicMock()
    ns.permission_issue = MagicMock()
    return ns


# --------------------------------------------------------------------------- #
# Usage-type matcher
# --------------------------------------------------------------------------- #
def test_matches_both_extended_support_tiers_only():
    assert _is_elasticache_extended_support_usage_type(
        "EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.r6g.xlarge")
    assert _is_elasticache_extended_support_usage_type(
        "EU-ExtendedSupportYr3-NodeUsage:cache.m6g.large")
    # Base node usage must NOT match — it is the cost, not the surcharge.
    assert not _is_elasticache_extended_support_usage_type("EU-NodeUsage:cache.r6g.xlarge")
    assert not _is_elasticache_extended_support_usage_type("EU-SyncDurability-NodeUsage:cache.m6g.large")


def test_node_type_extracted_from_usage_type():
    assert _node_type_from_usage_type(
        "EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.r6g.xlarge") == "cache.r6g.xlarge"
    assert _node_type_from_usage_type("EU-ExtendedSupportYr3-NodeUsage:cache.m6g.large") == "cache.m6g.large"
    assert _node_type_from_usage_type("EU-NodeUsage") == ""


# --------------------------------------------------------------------------- #
# Breakdown
# --------------------------------------------------------------------------- #
def test_breakdown_totals_and_attributes_by_node_type():
    ctx = _ctx(_ce())
    total, per_type = _extended_support_breakdown(ctx)
    # 7d -> 30d run rate.
    assert total == pytest.approx((122.98 + 44.02) * 30 / 7, abs=0.01)
    assert set(per_type) == {"cache.r6g.xlarge", "cache.m6g.large"}
    assert per_type["cache.r6g.xlarge"] == pytest.approx(122.98 * 30 / 7, abs=0.01)


def test_breakdown_is_region_scoped():
    """OS-1: an account-wide read re-counts the surcharge in every scanned region."""
    ce = _ce()
    _extended_support_breakdown(_ctx(ce))
    flt = ce.get_cost_and_usage.call_args.kwargs["Filter"]
    keys = {d["Dimensions"]["Key"] for d in flt["And"]}
    assert keys == {"SERVICE", "REGION"}


def test_no_region_fails_closed():
    ce = _ce()
    ctx = _ctx(ce, region="")
    assert _extended_support_breakdown(ctx) == (0.0, {})
    assert ce.get_cost_and_usage.call_count == 0
    assert ctx.warn.called


def test_ce_failure_fails_closed():
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = RuntimeError("AccessDeniedException")
    ctx = _ctx(ce)
    assert _extended_support_breakdown(ctx) == (0.0, {})
    assert ctx.warn.called


def test_no_surcharge_billed_returns_zero():
    ctx = _ctx(_ce([("EU-NodeUsage:cache.m5.large", "57.79")]))
    assert _extended_support_breakdown(ctx) == (0.0, {})


# --------------------------------------------------------------------------- #
# End-to-end through the adapter
# --------------------------------------------------------------------------- #
def _scan(monkeypatch, checks, ce):
    import services.adapters.elasticache as mod

    monkeypatch.setattr(mod, "get_enhanced_elasticache_checks", lambda ctx: {"recommendations": checks})
    ctx = _ctx(ce)
    ctx.cost_hub_splits = {}
    ctx.pricing_engine = None
    return mod.ElasticacheModule().scan(ctx), ctx


# The four Redis 5.0.6 nodes that actually pay the surcharge, plus two 7.1.0
# nodes that do not — level-Shoes-prod exactly.
_CLUSTERS = [
    {"ClusterId": "levelshoes-prod-cache-redis-001", "Engine": "redis", "EngineVersion": "5.0.6",
     "NodeType": "cache.r6g.xlarge", "NumNodes": 1, "CheckCategory": "Valkey Migration",
     "Recommendation": "x", "EstimatedMonthlySavings": 0.0, "Counted": False},
    {"ClusterId": "levelshoes-prod-cache-redis-002", "Engine": "redis", "EngineVersion": "5.0.6",
     "NodeType": "cache.r6g.xlarge", "NumNodes": 1, "CheckCategory": "Valkey Migration",
     "Recommendation": "x", "EstimatedMonthlySavings": 0.0, "Counted": False},
    {"ClusterId": "levelshoes-prod-session-001", "Engine": "redis", "EngineVersion": "5.0.6",
     "NodeType": "cache.m6g.large", "NumNodes": 1, "CheckCategory": "Valkey Migration",
     "Recommendation": "x", "EstimatedMonthlySavings": 0.0, "Counted": False},
    {"ClusterId": "ls-ms-redis-prod-001", "Engine": "redis", "EngineVersion": "7.1.0",
     "NodeType": "cache.m5.large", "NumNodes": 1, "CheckCategory": "Valkey Migration",
     "Recommendation": "x", "EstimatedMonthlySavings": 0.0, "Counted": False},
]


def test_surcharge_counted_and_named_per_node_type(monkeypatch):
    findings, _ = _scan(monkeypatch, list(_CLUSTERS), _ce())
    recs = list(findings.sources["extended_support"].recommendations)
    assert len(recs) == 2
    by_type = {r["NodeType"]: r for r in recs}
    assert by_type["cache.r6g.xlarge"]["EstimatedMonthlySavings"] == pytest.approx(122.98 * 30 / 7, abs=0.01)
    assert all(r["Counted"] is True for r in recs)
    # Attributed WITHOUT resource-level CE granularity: the usage type carries
    # the node type, and only EOL-engine clusters on it are named.
    named = by_type["cache.r6g.xlarge"]["Clusters"]
    assert named == ["levelshoes-prod-cache-redis-001", "levelshoes-prod-cache-redis-002"]
    # The 7.1.0 cluster pays no surcharge and must never be implicated.
    assert "ls-ms-redis-prod-001" not in str(recs)
    assert findings.total_monthly_savings == pytest.approx((122.98 + 44.02) * 30 / 7, abs=0.02)


def test_surcharge_absent_emits_no_source(monkeypatch):
    findings, _ = _scan(monkeypatch, list(_CLUSTERS), _ce([("EU-NodeUsage:cache.m5.large", "57.79")]))
    assert "extended_support" not in findings.sources
    assert findings.total_monthly_savings == 0.0


def test_surcharge_on_unmatched_node_type_still_counted(monkeypatch):
    """Billed is billed: a node type no live cluster reports is still a real
    charge, so count it and say the cluster could not be named."""
    ce = _ce([("EU-ExtendedSupportYr1_Yr2-NodeUsage:cache.r5.4xlarge", "70.00")])
    findings, _ = _scan(monkeypatch, list(_CLUSTERS), ce)
    recs = list(findings.sources["extended_support"].recommendations)
    assert len(recs) == 1 and recs[0]["Counted"] is True
    assert recs[0]["Clusters"] == []
    assert findings.total_monthly_savings == pytest.approx(70.0 * 30 / 7, abs=0.01)
