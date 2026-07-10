"""Unit tests for the ElastiCache HIGH cost-audit fixes (H1, H2, H3).

Mirrors the SimpleNamespace-ctx + fake-boto3 style of
``tests/test_lambda_audit_fixes.py`` / ``tests/test_audit_fixes_counted_dollars.py``.

Covers:
  - H1  Graviton AND Underutilized recs carry ``NumNodes`` so every lever prices
        on the same node count and the per-cluster dedup compares like-for-like.
  - H2  The Graviton counted dollar is the exact
        ``(current_node_price − graviton_node_price) × NumNodes`` delta, NOT a
        flat ``0.20`` of node cost (which overcounted ~4x). Validated live on the
        AWS Pricing API (us-east-1, NodeUsage, On-Demand, Redis, 2026-06-27):
        cache.r5.large $0.216/hr ($157.68/mo) vs cache.r6g.large $0.206/hr
        ($150.38/mo) → $7.30/mo/node (~4.6%).
  - H3  Fast mode skips the per-cluster CloudWatch CPUUtilization read entirely
        and suppresses the Underutilized lever (one warning); ``reads_fast_mode``
        is declared. A CloudWatch AccessDenied is classified as a permission
        issue, never swallowed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.elasticache as adapter_mod
import services.elasticache as shim_mod
from services.adapters.elasticache import (
    ElasticacheModule,
    downsize_target,
    graviton_equivalent,
)

# Live-validated monthly node prices (us-east-1, Redis, NodeUsage On-Demand).
R5_LARGE_MONTHLY = 157.68  # cache.r5.large  $0.216/hr × 730
R6G_LARGE_MONTHLY = 150.38  # cache.r6g.large $0.206/hr × 730
GRAVITON_DELTA_PER_NODE = round(R5_LARGE_MONTHLY - R6G_LARGE_MONTHLY, 2)  # $7.30


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePricing:
    """Returns fixed ElastiCache node monthly prices keyed by node type."""

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices

    def get_instance_monthly_price(self, service_code: str, instance_type: str, *, engine: str | None = None) -> float:
        assert service_code == "AmazonElastiCache"
        return self._prices.get(instance_type, 0.0)


def _ctx(*, pricing_engine: Any = None, fast_mode: bool = False, cost_hub_splits: Any = None) -> SimpleNamespace:
    """Adapter-level ctx (the shim helper is monkeypatched, so no boto clients)."""
    ctx = SimpleNamespace(
        pricing_engine=pricing_engine,
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=fast_mode,
        cost_hub_splits=cost_hub_splits or {},
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append((service, action, msg))
    return ctx


class _FakeElasticachePaginator:
    def __init__(self, clusters: list[dict[str, Any]]) -> None:
        self._clusters = clusters

    def paginate(self, **_kwargs: Any):  # noqa: ANN201 - boto3 shape
        return iter([{"CacheClusters": self._clusters}])


class _FakeElasticacheClient:
    def __init__(self, clusters: list[dict[str, Any]]) -> None:
        self._clusters = clusters

    def get_paginator(self, _name: str) -> _FakeElasticachePaginator:
        return _FakeElasticachePaginator(self._clusters)


class _FakeCloudWatch:
    """Returns one canned CPU datapoint, or raises a canned error."""

    def __init__(self, avg_cpu: float | None = None, error: Exception | None = None) -> None:
        self._avg_cpu = avg_cpu
        self._error = error

    def get_metric_statistics(self, **_kwargs: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        if self._avg_cpu is None:
            return {"Datapoints": []}
        return {"Datapoints": [{"Average": self._avg_cpu}]}


def _cluster(
    cluster_id: str,
    *,
    node_type: str,
    num_nodes: int,
    engine: str = "redis",
    status: str = "available",
) -> dict[str, Any]:
    return {
        "CacheClusterId": cluster_id,
        "Engine": engine,
        "EngineVersion": "7.0",
        "CacheNodeType": node_type,
        "NumCacheNodes": num_nodes,
        "CacheClusterStatus": status,
    }


def _shim_ctx(clusters: list[dict[str, Any]], cw: _FakeCloudWatch, *, fast_mode: bool = False) -> SimpleNamespace:
    """Shim-level ctx with fake elasticache + cloudwatch clients."""
    ctx = SimpleNamespace(fast_mode=fast_mode, warnings=[], permissions=[])
    ec = _FakeElasticacheClient(clusters)
    ctx.client = lambda name, **_k: {"elasticache": ec, "cloudwatch": cw}[name]
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append((service, action, msg))
    return ctx


# --------------------------------------------------------------------------- #
# graviton_equivalent helper (H2 mapping)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "node_type,expected",
    [
        ("cache.r5.large", "cache.r6g.large"),
        ("cache.r5.xlarge", "cache.r6g.xlarge"),
        ("cache.m5.large", "cache.m6g.large"),
        ("cache.m6i.2xlarge", "cache.m6g.2xlarge"),
        ("cache.t3.micro", "cache.t4g.micro"),
        ("cache.c5.large", "cache.c6g.large"),
        ("cache.m3.large", None),  # old x86, no Graviton counterpart → advisory
        ("cache.r6g.large", None),  # already Graviton (not in x86 map)
        ("cache.r5", None),  # malformed (no size)
        ("r5.large", None),  # missing cache. prefix
    ],
)
def test_graviton_equivalent_mapping(node_type: str, expected: str | None) -> None:
    assert graviton_equivalent(node_type) == expected


# --------------------------------------------------------------------------- #
# H1 — every lever carries NumNodes (shim)
# --------------------------------------------------------------------------- #
def test_shim_all_levers_carry_numnodes() -> None:
    clusters = [_cluster("c1", node_type="cache.r5.xlarge", num_nodes=3, engine="redis")]
    cw = _FakeCloudWatch(avg_cpu=5.0)  # low CPU → underutilized lever fires
    ctx = _shim_ctx(clusters, cw)

    recs = shim_mod.get_enhanced_elasticache_checks(ctx)["recommendations"]
    by_cat = {r["CheckCategory"]: r for r in recs}

    # All four levers present for a multi-node Redis cluster…
    for cat in (
        "Valkey Migration",
        "Graviton Migration",
        "Reserved Nodes Opportunity",
        "Underutilized Cluster",
    ):
        assert cat in by_cat, f"missing lever: {cat}"
        # …and every one prices on the SAME node count (H1).
        assert by_cat[cat]["NumNodes"] == 3


# --------------------------------------------------------------------------- #
# H2 — exact Graviton node-price delta × NumNodes (not a flat 0.20)
# --------------------------------------------------------------------------- #
def _graviton_rec(node_type: str, num_nodes: int, engine: str = "redis") -> dict[str, Any]:
    return {
        "ClusterId": "c1",
        "Engine": engine,
        "NodeType": node_type,
        "NumNodes": num_nodes,
        "Recommendation": "Migrate to Graviton instances",
        "EstimatedSavings": "Estimated: 20-40% price-performance improvement",
        "CheckCategory": "Graviton Migration",
    }


def test_graviton_counts_exact_node_delta_times_numnodes(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _graviton_rec("cache.r5.large", num_nodes=3)
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_elasticache_checks", lambda c: {"recommendations": [dict(rec)]}
    )
    pricing = _FakePricing({"cache.r5.large": R5_LARGE_MONTHLY, "cache.r6g.large": R6G_LARGE_MONTHLY})

    findings = ElasticacheModule().scan(_ctx(pricing_engine=pricing))

    expected = round(GRAVITON_DELTA_PER_NODE * 3, 2)  # 7.30 × 3 = 21.90
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.01)

    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(expected, abs=0.01)
    assert emitted["Counted"] is True
    # counted == rendered: the single-sourced string matches the counted dollar.
    assert emitted["EstimatedSavings"].startswith(f"${expected:.2f}")
    # The old flat-0.20 path would have counted 0.20 × 157.68 × 3 = $94.61 (~4.3x).
    assert findings.total_monthly_savings < 0.20 * R5_LARGE_MONTHLY * 3
    # AuditBasis defends the dollar from the report alone.
    ab = emitted["AuditBasis"]
    assert ab["num_nodes"] == 3
    assert ab["engine"] == "redis"
    assert ab["region"] == "us-east-1"


def test_graviton_scales_linearly_with_numnodes(monkeypatch: pytest.MonkeyPatch) -> None:
    pricing = _FakePricing({"cache.r5.large": R5_LARGE_MONTHLY, "cache.r6g.large": R6G_LARGE_MONTHLY})

    def _scan(num_nodes: int) -> float:
        rec = _graviton_rec("cache.r5.large", num_nodes=num_nodes)
        monkeypatch.setattr(
            adapter_mod, "get_enhanced_elasticache_checks", lambda c: {"recommendations": [dict(rec)]}
        )
        return ElasticacheModule().scan(_ctx(pricing_engine=pricing)).total_monthly_savings

    one = _scan(1)
    four = _scan(4)
    assert one == pytest.approx(GRAVITON_DELTA_PER_NODE, abs=0.01)
    assert four == pytest.approx(GRAVITON_DELTA_PER_NODE * 4, abs=0.01)


def test_graviton_unmappable_family_is_zero_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    # An x86 family with no Graviton counterpart cannot be quantified → $0 advisory.
    rec = _graviton_rec("cache.m3.large", num_nodes=2)
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_elasticache_checks", lambda c: {"recommendations": [dict(rec)]}
    )
    pricing = _FakePricing({"cache.m3.large": 120.0})  # priced, but no graviton target

    findings = ElasticacheModule().scan(_ctx(pricing_engine=pricing))

    assert findings.total_monthly_savings == 0.0
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["Counted"] is False
    assert emitted["EstimatedMonthlySavings"] == 0.0
    assert "advisory" in emitted["EstimatedSavings"].lower()


@pytest.mark.parametrize(
    ("node_type", "expected"),
    [
        ("cache.r5.xlarge", "cache.r5.large"),
        ("cache.m6g.2xlarge", "cache.m6g.xlarge"),
        ("cache.r6g.large", "cache.r6g.medium"),
        ("cache.t3.micro", None),  # smallest size — no target
        ("cache.r5", None),  # unparseable (no size)
        ("r5.large", None),  # not a cache. node type
        ("cache.c5.18xlarge", None),  # size token off the ladder
    ],
)
def test_downsize_target(node_type: str, expected: str | None) -> None:
    assert downsize_target(node_type) == expected


def test_underutilized_lever_prices_every_node(monkeypatch: pytest.MonkeyPatch) -> None:
    # H3: the downsize lever is the live current→one-size-down node-price delta
    # across NumNodes, NOT a flat 0.30 factor. cache.r5.xlarge -> cache.r5.large.
    rec = {
        "ClusterId": "c1",
        "Engine": "redis",
        "NodeType": "cache.r5.xlarge",
        "NumNodes": 2,
        "AvgCPU": 5.0,
        "Recommendation": "Downsize node type or consider smaller instance family",
        "EstimatedSavings": "30-50%",
        "CheckCategory": "Underutilized Cluster",
        # Memory headroom proven: this test is about pricing every node (H1).
        "MemoryHeadroomOk": True,
        "PeakMemoryUsagePct": 5.0,
        "Evictions": 0.0,
    }
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_elasticache_checks", lambda c: {"recommendations": [dict(rec)]}
    )
    pricing = _FakePricing({"cache.r5.xlarge": 100.0, "cache.r5.large": 50.0})

    findings = ElasticacheModule().scan(_ctx(pricing_engine=pricing))

    # (100 − 50) × 2 nodes = $100.00 (the real one-size-down delta, priced per node).
    assert findings.total_monthly_savings == pytest.approx(100.0, abs=0.01)
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["Counted"] is True
    assert emitted["AuditBasis"]["lever"] == "Underutilized cluster downsizing"
    assert emitted["AuditBasis"]["num_nodes"] == 2


def test_underutilized_lever_advisory_when_target_unpriceable(monkeypatch: pytest.MonkeyPatch) -> None:
    # H3: no priceable one-size-down target → $0 advisory, never a fabricated
    # delta against an unknown (here, missing cache.r5.large) target price.
    rec = {
        "ClusterId": "c1",
        "Engine": "redis",
        "NodeType": "cache.r5.xlarge",
        "NumNodes": 2,
        "AvgCPU": 5.0,
        "Recommendation": "Downsize node type",
        "EstimatedSavings": "30-50%",
        "CheckCategory": "Underutilized Cluster",
        # Memory headroom proven: this test is about pricing every node (H1).
        "MemoryHeadroomOk": True,
        "PeakMemoryUsagePct": 5.0,
        "Evictions": 0.0,
    }
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_elasticache_checks", lambda c: {"recommendations": [dict(rec)]}
    )
    pricing = _FakePricing({"cache.r5.xlarge": 100.0})  # target price absent

    findings = ElasticacheModule().scan(_ctx(pricing_engine=pricing))

    assert findings.total_monthly_savings == 0.0
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["Counted"] is False
    assert emitted["EstimatedMonthlySavings"] == 0.0


def test_per_cluster_dedup_compares_like_for_like(monkeypatch: pytest.MonkeyPatch) -> None:
    # Valkey (0.20 × node × nodes) and Graviton (delta × nodes) on the SAME
    # multi-node cluster: with both now priced across NumNodes the higher-$ lever
    # (Valkey) wins and Graviton is demoted — no double count, like-for-like.
    valkey = {
        "ClusterId": "c1",
        "Engine": "redis",
        "NodeType": "cache.r5.large",
        "NumNodes": 4,
        "Recommendation": "Consider migrating to ElastiCache for Valkey",
        "EstimatedSavings": "Valkey is ~20% cheaper",
        "CheckCategory": "Valkey Migration",
    }
    graviton = _graviton_rec("cache.r5.large", num_nodes=4)
    monkeypatch.setattr(
        adapter_mod,
        "get_enhanced_elasticache_checks",
        lambda c: {"recommendations": [dict(valkey), dict(graviton)]},
    )
    pricing = _FakePricing({"cache.r5.large": R5_LARGE_MONTHLY, "cache.r6g.large": R6G_LARGE_MONTHLY})

    findings = ElasticacheModule().scan(_ctx(pricing_engine=pricing))

    expected_valkey = round(R5_LARGE_MONTHLY * 4 * 0.20, 2)  # 126.14
    assert findings.total_monthly_savings == pytest.approx(expected_valkey, abs=0.01)

    emitted = {r["CheckCategory"]: r for r in findings.sources["enhanced_checks"].recommendations}
    assert emitted["Valkey Migration"]["Counted"] is True
    assert emitted["Graviton Migration"]["Counted"] is False  # superseded, not summed


# --------------------------------------------------------------------------- #
# H3 — fast mode skips CloudWatch + suppresses the Underutilized lever
# --------------------------------------------------------------------------- #
def test_module_declares_fast_mode_contract() -> None:
    module = ElasticacheModule()
    assert module.reads_fast_mode is True
    assert module.requires_cloudwatch is True


def test_fast_mode_skips_cloudwatch_and_suppresses_underutilized() -> None:
    clusters = [_cluster("c1", node_type="cache.r5.xlarge", num_nodes=2, engine="redis")]
    # If the shim touches CloudWatch in fast mode this raises.
    cw = _FakeCloudWatch(error=AssertionError("CloudWatch must not be called in fast mode"))
    ctx = _shim_ctx(clusters, cw, fast_mode=True)

    recs = shim_mod.get_enhanced_elasticache_checks(ctx)["recommendations"]

    # No underutilized lever without a measured CPU signal…
    assert not [r for r in recs if r["CheckCategory"] == "Underutilized Cluster"]
    # …but the metric-free levers still fire.
    assert [r for r in recs if r["CheckCategory"] == "Graviton Migration"]
    assert [r for r in recs if r["CheckCategory"] == "Valkey Migration"]
    # Exactly one fast-mode warning.
    assert sum("fast mode" in msg.lower() for _svc, msg in ctx.warnings) == 1


def test_cloudwatch_access_denied_is_permission_issue() -> None:
    clusters = [_cluster("c1", node_type="cache.r5.xlarge", num_nodes=2, engine="redis")]
    cw = _FakeCloudWatch(error=Exception("AccessDeniedException: no cloudwatch metrics"))
    ctx = _shim_ctx(clusters, cw)

    shim_mod.get_enhanced_elasticache_checks(ctx)

    assert ctx.permissions, "CloudWatch AccessDenied must be recorded via ctx.permission_issue"
    svc, _action, _msg = ctx.permissions[0]
    assert svc == "elasticache"
    # And no underutilized rec was fabricated on the failed read.


def test_cloudwatch_transient_error_is_warn_not_permission() -> None:
    clusters = [_cluster("c1", node_type="cache.r5.xlarge", num_nodes=2, engine="redis")]
    cw = _FakeCloudWatch(error=Exception("ThrottlingException: rate exceeded"))
    ctx = _shim_ctx(clusters, cw)

    recs = shim_mod.get_enhanced_elasticache_checks(ctx)["recommendations"]

    assert not ctx.permissions, "a throttle is not a permission gap"
    assert ctx.warnings, "transient CloudWatch failure must surface as a warning"
    assert not [r for r in recs if r["CheckCategory"] == "Underutilized Cluster"]


# --------------------------------------------------------------------------- #
# bnc live audit (2026-07-10): a downsize must FIT, not merely be idle.
# One size down leaves only ~36-48% of the current maxmemory, so low CPU alone
# cannot justify the move. A rec that would have to be reverted is not a saving.
# --------------------------------------------------------------------------- #
def _underutilized(**over):
    rec = {
        "ClusterId": "c1",
        "Engine": "redis",
        "NodeType": "cache.t4g.medium",
        "NumNodes": 1,
        "AvgCPU": 3.0,
        "MemoryHeadroomOk": True,
        "PeakMemoryUsagePct": 5.0,
        "Evictions": 0.0,
        "Recommendation": "Downsize node type or consider smaller instance family",
        "EstimatedSavings": "30-50%",
        "CheckCategory": "Underutilized Cluster",
    }
    rec.update(over)
    return rec


def _scan_with(rec, monkeypatch):
    import services.adapters.elasticache as ec_adapter

    monkeypatch.setattr(
        ec_adapter, "get_enhanced_elasticache_checks",
        lambda _ctx: {"recommendations": [rec]},
    )
    pe = SimpleNamespace(
        get_instance_monthly_price=lambda _svc, node_type, engine=None: {
            "cache.t4g.medium": 69.35, "cache.t4g.small": 35.04,
        }.get(node_type, 0.0)
    )
    ctx = SimpleNamespace(
        region="ap-southeast-1", pricing_engine=pe, pricing_multiplier=1.0, fast_mode=False,
        cost_hub_splits={"elasticache": []}, commitment_coverage=None,
        client=lambda _n: None, warn=lambda *a, **k: None, permission_issue=lambda *a, **k: None,
    )
    return ec_adapter.ElasticacheModule().scan(ctx)


def test_downsize_counted_when_working_set_fits(monkeypatch):
    findings = _scan_with(_underutilized(), monkeypatch)
    assert findings.total_monthly_savings == pytest.approx(69.35 - 35.04, abs=0.02)
    rec = list(findings.sources["enhanced_checks"].recommendations)[0]
    assert rec["Counted"] is True
    assert "working set fits cache.t4g.small" in rec["AuditBasis"]["memory_headroom"]


def test_downsize_withheld_when_memory_would_not_fit(monkeypatch):
    findings = _scan_with(_underutilized(MemoryHeadroomOk=False, PeakMemoryUsagePct=72.0), monkeypatch)
    assert findings.total_monthly_savings == 0.0
    rec = list(findings.sources["enhanced_checks"].recommendations)[0]
    assert rec["Counted"] is False
    assert rec["PotentialMonthlySavings"] == pytest.approx(69.35 - 35.04, abs=0.02)
    assert "exceeds the 35.0% headroom" in rec["AuditBasis"]["withheld"]


def test_downsize_withheld_when_memory_metrics_unreadable(monkeypatch):
    # Absence of evidence is not evidence of headroom (C8).
    findings = _scan_with(
        _underutilized(MemoryHeadroomOk=False, PeakMemoryUsagePct=None, Evictions=None), monkeypatch
    )
    assert findings.total_monthly_savings == 0.0
    rec = list(findings.sources["enhanced_checks"].recommendations)[0]
    assert rec["Counted"] is False
    assert "cannot prove the working set fits" in rec["AuditBasis"]["withheld"]


def test_evictions_block_the_downsize(monkeypatch):
    # Low memory % but the cache is already evicting -> it is not oversized.
    findings = _scan_with(_underutilized(MemoryHeadroomOk=False, PeakMemoryUsagePct=10.0, Evictions=41.0), monkeypatch)
    assert findings.total_monthly_savings == 0.0
    assert list(findings.sources["enhanced_checks"].recommendations)[0]["Counted"] is False
