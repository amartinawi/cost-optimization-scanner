"""RS-A / RS-1 — the one defensible counted Redshift lever.

Before this, the tab was structurally incapable of reporting a dollar from
either direction: its only counted source was the Cost Optimization Hub bucket,
and ``RedshiftCluster`` is not a CoH ResourceType (the enum has 25 values and
that is not one of them), so the bucket can never receive a rec — while every
local heuristic was ``$0`` by construction.

An idle provisioned cluster wastes 100% of its compute spend, and PAUSE recovers
exactly that, so no target size has to be guessed. That is the MSK-1 argument.

Four gates keep it honest, each of which an adversarial review insisted on:

* the node count is read from the RAW key, not the defaulted variable — reusing
  the default would make the abstain guard unreachable, which is the
  MSK-5 / GL-4 / WS-3 bug this repo has fixed three times;
* a healthy cluster produces no card at all;
* AWS refuses to pause a cluster with automated snapshots off or an HSM cluster,
  so a saving with no action behind it is not counted;
* only RA3 counts. RA3 separates compute from managed storage so the
  compute-only delta is exact; DC2/DS2 bundle local SSD into the node-hour and
  the free-allocation treatment of a paused DC2/DS2 cluster is unverified, so
  those render their figure without counting it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import services.adapters.redshift as adapter_mod
from services.redshift import get_enhanced_redshift_checks

_IDLE = "Idle Cluster"


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **kw: Any) -> list[dict[str, Any]]:
        return self._pages


class _FakeRedshift:
    def __init__(self, clusters: list[dict[str, Any]]) -> None:
        self._clusters = clusters

    def get_paginator(self, name: str) -> _FakePaginator:
        return _FakePaginator([{"Clusters": self._clusters}])


class _FakeCw:
    """`maxima` is the list of hourly Maximum values, or None for a failed read."""

    def __init__(self, maxima: list[float] | None, error: Exception | None = None) -> None:
        self._maxima = maxima
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def get_metric_statistics(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if self._error is not None:
            raise self._error
        return {"Datapoints": [{"Maximum": m} for m in (self._maxima or [])]}


def _cluster(
    *,
    cid: str = "dw-1",
    node_type: str = "ra3.xlplus",
    nodes: int | None = 2,
    status: str = "available",
    retention: int = 1,
    hsm: bool = False,
) -> dict[str, Any]:
    c: dict[str, Any] = {
        "ClusterIdentifier": cid,
        "NodeType": node_type,
        "ClusterStatus": status,
        "AutomatedSnapshotRetentionPeriod": retention,
    }
    if nodes is not None:
        c["NumberOfNodes"] = nodes
    if hsm:
        c["HsmStatus"] = {"Status": "applying"}
    return c


def _ctx(clusters, *, maxima=None, cw_error=None, fast=False, rate=1.086 * 730, coverage=None):
    cw = _FakeCw(maxima, cw_error)
    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(
            get_instance_monthly_price=lambda code, itype, **kw: rate if "ra3" in itype or "dc2" in itype else 0.0
        ),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=fast,
        cost_hub_splits={},
        commitment_coverage=coverage,
        warnings=[],
        permissions=[],
    )
    clients = {"redshift": _FakeRedshift(clusters), "cloudwatch": cw}
    ctx.client = lambda name, region=None: clients.get(name)
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(msg)
    ctx._cw = cw
    return ctx


_ALL_ZERO = [0.0] * 720


def _idle_recs(ctx) -> list[dict[str, Any]]:
    out = get_enhanced_redshift_checks(ctx)
    return [r for r in out["recommendations"] if r["CheckCategory"] == _IDLE]


def _scan(ctx):
    return adapter_mod.RedshiftModule().scan(ctx)


def _idle_card(findings) -> dict[str, Any] | None:
    for block in findings.sources.values():
        for r in block.recommendations:
            if r.get("CheckCategory") == _IDLE:
                return r
    return None


# --------------------------------------------------------------------------- #
# The evidence gate
# --------------------------------------------------------------------------- #
def test_no_connections_across_the_window_is_idle() -> None:
    assert len(_idle_recs(_ctx([_cluster()], maxima=_ALL_ZERO))) == 1


def test_any_connection_produces_no_card_at_all() -> None:
    """A healthy cluster must not get an "Idle Cluster" advisory either."""
    maxima = [0.0] * 719 + [3.0]
    assert _idle_recs(_ctx([_cluster()], maxima=maxima)) == []


def test_short_series_abstains() -> None:
    """A cluster created mid-window has a partial series; that is not evidence."""
    assert _idle_recs(_ctx([_cluster()], maxima=[0.0] * 100)) == []


def test_empty_series_abstains() -> None:
    assert _idle_recs(_ctx([_cluster()], maxima=[])) == []


def test_denied_metric_read_abstains_and_is_classified() -> None:
    ctx = _ctx([_cluster()], cw_error=Exception("AccessDeniedException"))
    assert _idle_recs(ctx) == []
    assert ctx.permissions or ctx.warnings


def test_fast_mode_abstains_and_makes_no_metric_call() -> None:
    ctx = _ctx([_cluster()], maxima=_ALL_ZERO, fast=True)
    assert _idle_recs(ctx) == []
    assert ctx._cw.calls == []


def test_unavailable_cluster_is_not_a_candidate() -> None:
    assert _idle_recs(_ctx([_cluster(status="paused")], maxima=_ALL_ZERO)) == []


def test_metric_uses_the_documented_dimension() -> None:
    ctx = _ctx([_cluster()], maxima=_ALL_ZERO)
    _idle_recs(ctx)
    call = ctx._cw.calls[0]
    assert call["Namespace"] == "AWS/Redshift"
    assert call["MetricName"] == "DatabaseConnections"
    assert call["Dimensions"] == [{"Name": "ClusterIdentifier", "Value": "dw-1"}]


# --------------------------------------------------------------------------- #
# The fabricated-quantity guard
# --------------------------------------------------------------------------- #
def test_missing_node_count_abstains_rather_than_assuming_one() -> None:
    """The shim keeps a defaulted `NumberOfNodes, 1` for its legacy checks. The
    counted lever must read the RAW key or its abstain guard is unreachable."""
    assert _idle_recs(_ctx([_cluster(nodes=None)], maxima=_ALL_ZERO)) == []


# --------------------------------------------------------------------------- #
# Pricing and the RA3 restriction
# --------------------------------------------------------------------------- #
def test_idle_ra3_cluster_counts_its_whole_compute_spend() -> None:
    findings = _scan(_ctx([_cluster(nodes=2)], maxima=_ALL_ZERO))
    card = _idle_card(findings)
    assert card is not None and card["Counted"] is True
    assert card["EstimatedMonthlySavings"] == pytest.approx(2 * 1.086 * 730, abs=0.01)
    assert findings.total_monthly_savings == pytest.approx(2 * 1.086 * 730, abs=0.01)
    assert "if paused" in card["EstimatedSavings"]


def test_dc2_cluster_renders_its_figure_without_counting_it() -> None:
    """DC2 bundles local SSD into the node-hour; the paused-storage treatment is
    unverified, so the figure is shown but not counted."""
    findings = _scan(_ctx([_cluster(node_type="dc2.large", nodes=4)], maxima=_ALL_ZERO))
    card = _idle_card(findings)
    assert card["Counted"] is False
    assert card["EstimatedMonthlySavings"] == 0.0
    assert card["PotentialMonthlySavings"] == pytest.approx(4 * 1.086 * 730, abs=0.01)
    assert findings.total_monthly_savings == 0.0
    assert "bundles local SSD" in card["EstimatedSavings"]


def test_cluster_aws_cannot_pause_is_not_counted() -> None:
    """AWS refuses to pause a cluster with automated snapshots disabled. A saving
    with no action behind it is not a saving."""
    findings = _scan(_ctx([_cluster(retention=0)], maxima=_ALL_ZERO))
    card = _idle_card(findings)
    assert card["Counted"] is False
    assert card["PotentialMonthlySavings"] > 0
    assert "cannot pause" in card["EstimatedSavings"]


def test_hsm_cluster_is_not_counted() -> None:
    findings = _scan(_ctx([_cluster(hsm=True)], maxima=_ALL_ZERO))
    assert _idle_card(findings)["Counted"] is False


def test_unpriceable_node_type_abstains_with_no_figure() -> None:
    ctx = _ctx([_cluster(node_type="ra3.mystery")], maxima=_ALL_ZERO, rate=0.0)
    card = _idle_card(_scan(ctx))
    assert card["Counted"] is False
    assert "PotentialMonthlySavings" not in card
    assert "no live Redshift SKU" in card["EstimatedSavings"]


def test_reserved_node_coverage_demotes_the_counted_dollar() -> None:
    """A reserved cluster bills the reservation whether paused or not."""
    from services.commitment_coverage import CommitmentCoverage

    coverage = CommitmentCoverage(redshift_ri_types=frozenset({"ra3.xlplus"}))
    findings = _scan(_ctx([_cluster()], maxima=_ALL_ZERO, coverage=coverage))
    assert findings.total_monthly_savings == 0.0
    assert _idle_card(findings)["Counted"] is False


def test_audit_basis_discloses_the_residual_risk() -> None:
    card = _idle_card(_scan(_ctx([_cluster()], maxima=_ALL_ZERO)))
    basis = card["AuditBasis"]
    assert "materialized-view auto-refresh" in basis["residual_risk"]
    assert "PAUSE (reversible)" in basis["residual_risk"]
    assert basis["node_count"] == 2
