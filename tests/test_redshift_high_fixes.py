"""Unit tests for the Redshift adapter HIGH cost-audit fixes (H1 + H2).

Redshift HIGH findings, done together:

  - H1: the Reserved-Instance heuristic was the only ``Counted=True`` Redshift
        lever, so it counted a *commitment buy* as account savings — overlapping
        the Commitment Analysis tab (double count). RI / Serverless-Reservation
        levers are now ``Counted=False`` $0 advisories; commitment_analysis owns
        the RI dollars.
  - H2: the RI rec carried three disagreeing numbers (the shim string
        ``NumberOfNodes x 150``, the "24%" text, and the counted ``0.52`` factor —
        ~1.7x over the real 1-yr No-Upfront RA3 RI discount of ~30%). The rec now
        single-sources an honest ``$0.00/month — advisory`` string so the card
        dollar equals the $0 the headline counts.

Drives the pure adapter logic and the ``scan()`` -> shim path with a
SimpleNamespace ctx + monkeypatched enhanced checks + a fake boto3 paginator,
mirroring tests/test_coh_bucket_audit_fixes.py and
tests/test_audit_fixes_counted_dollars.py. Each counted dollar (or advisory $0)
is proven with an explicit assertion.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.redshift as redshift_adapter
import services.redshift as redshift_shim
from services._savings import parse_dollar_savings
from services.adapters.redshift import (
    ADVISORY_CATEGORIES,
    RI_CATEGORY,
    SERVERLESS_RESERVATION_CATEGORY,
    RedshiftModule,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePricing:
    """Returns a fixed monthly node price for AmazonRedshift lookups."""

    def __init__(self, monthly: float = 100.0) -> None:
        self._monthly = monthly

    def get_instance_monthly_price(self, service_code, instance_type, *, engine=None):  # noqa: ANN001
        assert service_code == "AmazonRedshift"
        return self._monthly


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kw: Any):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeRedshiftClient:
    def __init__(self, clusters: list[dict[str, Any]]) -> None:
        self._clusters = clusters

    def get_paginator(self, op: str) -> _FakePaginator:
        assert op == "describe_clusters"
        return _FakePaginator([{"Clusters": self._clusters}])


class _FakeServerlessClient:
    def __init__(self, workgroups: list[dict[str, Any]]) -> None:
        self._workgroups = workgroups

    def get_paginator(self, op: str) -> _FakePaginator:
        assert op == "list_workgroups"
        return _FakePaginator([{"workgroups": self._workgroups}])


def _ctx(
    *,
    coh_recs: list[dict[str, Any]] | None = None,
    pricing_monthly: float = 100.0,
    pricing_engine: Any = "default",
    clusters: list[dict[str, Any]] | None = None,
    workgroups: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    """SimpleNamespace ctx that records warn / permission_issue and serves fake clients."""
    redshift_client = _FakeRedshiftClient(clusters or [])
    serverless_client = _FakeServerlessClient(workgroups or [])

    def _client(name: str, region: str | None = None) -> Any:
        if name == "redshift":
            return redshift_client
        if name == "redshift-serverless":
            return serverless_client
        raise AssertionError(f"unexpected client {name}")

    ctx = SimpleNamespace(
        pricing_engine=(_FakePricing(pricing_monthly) if pricing_engine == "default" else pricing_engine),
        pricing_multiplier=1.0,
        region="us-east-1",
        account_id="123456789012",
        fast_mode=False,
        cost_hub_splits={"redshift": coh_recs or []},
        warnings=[],
        permissions=[],
        client=_client,
    )
    ctx.warn = lambda message, service=None, **k: ctx.warnings.append((service, message))
    ctx.permission_issue = lambda message, service=None, action=None, **k: ctx.permissions.append(
        (service, action, message)
    )
    return ctx


def _coh_rec(resource_id: str, savings: float, *, action: str = "Rightsize") -> dict[str, Any]:
    return {
        "resourceId": resource_id,
        "resourceArn": f"arn:aws:redshift:us-east-1:1:cluster:{resource_id}",
        "actionType": action,
        "estimatedMonthlySavings": savings,
        "recommendationId": f"coh-{resource_id}",
    }


def _patch_checks(monkeypatch: pytest.MonkeyPatch, recs: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(
        redshift_adapter,
        "get_enhanced_redshift_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )


# --------------------------------------------------------------------------- #
# H1 — RI lever is advisory, not counted (no overlap with commitment_analysis)
# --------------------------------------------------------------------------- #
def test_advisory_categories_contains_commitment_levers() -> None:
    """Mirror of dynamodb's DYNAMODB_ADVISORY_CATEGORIES assertion."""
    assert RI_CATEGORY in ADVISORY_CATEGORIES
    assert SERVERLESS_RESERVATION_CATEGORY in ADVISORY_CATEGORIES


def test_ri_rec_is_advisory_zero_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """H1: an RI heuristic rec with a priceable NodeType is NOT counted; it is a
    $0 advisory. The old code would have counted 100 x 4 x 0.52 = 208.0."""
    _patch_checks(
        monkeypatch,
        [
            {
                "ClusterIdentifier": "warehouse",
                "NodeType": "ra3.xlplus",
                "NumberOfNodes": 4,
                "CheckCategory": RI_CATEGORY,
            }
        ],
    )
    findings = RedshiftModule().scan(_ctx(pricing_monthly=100.0))

    ri = findings.sources["enhanced_checks"].recommendations[0]
    assert ri["Counted"] is False
    assert ri["EstimatedMonthlySavings"] == 0.0
    assert ri["EstimatedSavings"].startswith("$0.00/month — advisory")
    # The commitment dollar is NOT summed into the Redshift headline.
    assert findings.total_monthly_savings == pytest.approx(0.0)
    # The advisory rec carries an AuditBasis pointing the dollar at commitment_analysis.
    assert ri["AuditBasis"]["owner"] == "commitment_analysis"


def test_ri_advisory_leaves_only_coh_dollars(monkeypatch: pytest.MonkeyPatch) -> None:
    """H1: with a CoH rec present, the headline is CoH only — the RI lever adds $0,
    so the commitment is not double-counted across redshift + commitment_analysis."""
    _patch_checks(
        monkeypatch,
        [
            {
                "ClusterIdentifier": "other-cluster",
                "NodeType": "ra3.4xlarge",
                "NumberOfNodes": 2,
                "CheckCategory": RI_CATEGORY,
            }
        ],
    )
    coh = [_coh_rec("warehouse", 321.0)]
    findings = RedshiftModule().scan(_ctx(coh_recs=coh, pricing_monthly=500.0))

    assert findings.total_monthly_savings == pytest.approx(321.0)
    ri = findings.sources["enhanced_checks"].recommendations[0]
    assert ri["Counted"] is False


def test_serverless_reservation_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    """H1/H2: the Serverless-Reservation lever is also a commitment buy -> $0 advisory."""
    _patch_checks(
        monkeypatch,
        [
            {
                "WorkgroupName": "wg-prod",
                "CheckCategory": SERVERLESS_RESERVATION_CATEGORY,
                "EstimatedSavings": "$150/month with reservations",
            }
        ],
    )
    findings = RedshiftModule().scan(_ctx())
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    # The fabricated "$150/month" string is replaced by the honest advisory line.
    assert parse_dollar_savings(rec["EstimatedSavings"]) == pytest.approx(0.0)
    assert findings.total_monthly_savings == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# L2 — the dead per-node heuristic pricing path is removed; no non-advisory
#      heuristic lever is ever counted (CoH is the only counted source)
# --------------------------------------------------------------------------- #
def test_pause_lever_is_zero_advisory_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """L2: a pause-named lever that carries a priceable NodeType is NOT counted —
    the dead REDSHIFT_SAVINGS_FACTORS 'pause' path (which would have counted
    100 x 2 x 1.00 = 200) was removed; the rec is now a $0 advisory."""
    _patch_checks(
        monkeypatch,
        [
            {
                "ClusterIdentifier": "idle-wh",
                "NodeType": "ra3.4xlarge",
                "NumberOfNodes": 2,
                "CheckCategory": "Pause/Resume Scheduling",
            }
        ],
    )
    findings = RedshiftModule().scan(_ctx(pricing_monthly=100.0))
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["EstimatedSavings"].startswith("$0.00/month — advisory")
    assert parse_dollar_savings(rec["EstimatedSavings"]) == pytest.approx(0.0)
    assert findings.total_monthly_savings == pytest.approx(0.0)
    # No fabricated AuditBasis / counted factor survives the L2 removal.
    assert "AuditBasis" not in rec


def test_rightsizing_lever_with_node_type_is_zero_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    """L2: a non-advisory category carrying a NodeType is no longer priced by the
    removed 0.24 'default' factor (which would have counted 100 x 2 x 0.24 = 48) —
    it renders as a $0 advisory."""
    _patch_checks(
        monkeypatch,
        [
            {
                "ClusterIdentifier": "wh",
                "NodeType": "ra3.xlplus",
                "NumberOfNodes": 2,
                "CheckCategory": "Cluster Rightsizing",  # not advisory, has NodeType
            }
        ],
    )
    findings = RedshiftModule().scan(_ctx(pricing_monthly=100.0))
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert parse_dollar_savings(rec["EstimatedSavings"]) == pytest.approx(0.0)
    assert findings.total_monthly_savings == pytest.approx(0.0)


def test_unpriceable_lever_is_zero_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rec without a NodeType cannot be priced -> $0 advisory, no fabricated
    'potential' dollar leaks into the headline (counted == rendered == $0)."""
    _patch_checks(
        monkeypatch,
        [
            {
                "ClusterIdentifier": "wh",
                "CurrentNodes": 6,
                "CheckCategory": "Cluster Rightsizing",
                "EstimatedSavings": "$400.00/month potential",  # fabricated (nodes-2)*100
            }
        ],
    )
    findings = RedshiftModule().scan(_ctx())
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert parse_dollar_savings(rec["EstimatedSavings"]) == pytest.approx(0.0)
    assert findings.total_monthly_savings == pytest.approx(0.0)


def test_pricing_engine_none_emits_no_dollar(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pricing engine -> priceable levers fall back to $0 advisory, never fabricate."""
    _patch_checks(
        monkeypatch,
        [
            {
                "ClusterIdentifier": "wh",
                "NodeType": "ra3.xlplus",
                "NumberOfNodes": 4,
                "CheckCategory": "Pause/Resume Scheduling",
            }
        ],
    )
    findings = RedshiftModule().scan(_ctx(pricing_engine=None))
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert findings.total_monthly_savings == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# CoH authority preserved (regression of the SR-3 behavior in the new code path)
# --------------------------------------------------------------------------- #
def test_coh_suppresses_heuristic_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CoH-covered cluster demotes its heuristic lever; CoH dollars counted once."""
    _patch_checks(
        monkeypatch,
        [
            {
                "ClusterIdentifier": "warehouse",
                "NodeType": "ra3.xlplus",
                "NumberOfNodes": 4,
                "CheckCategory": "Pause/Resume Scheduling",  # would otherwise count
            }
        ],
    )
    coh = [_coh_rec("warehouse", 200.0)]
    findings = RedshiftModule().scan(_ctx(coh_recs=coh, pricing_monthly=100.0))
    heur = findings.sources["enhanced_checks"].recommendations[0]
    assert heur["Counted"] is False
    assert findings.total_monthly_savings == pytest.approx(200.0)


# --------------------------------------------------------------------------- #
# H2 at the shim — the RI rec built by the real shim carries NO fabricated $
# --------------------------------------------------------------------------- #
def test_shim_ri_rec_has_no_fabricated_dollar() -> None:
    """The shim's RI rec must not carry the old NumberOfNodes x 150 string (which,
    for a 4-node cluster, fabricated $600 that disagreed with the counted factor)."""
    create = datetime.now(UTC) - timedelta(days=120)
    ctx = _ctx(
        clusters=[
            {
                "ClusterIdentifier": "warehouse",
                "NodeType": "ra3.4xlarge",
                "ClusterStatus": "available",
                "ClusterCreateTime": create,
                "NumberOfNodes": 4,
            }
        ]
    )
    result = redshift_shim.get_enhanced_redshift_checks(ctx)
    ri = next(r for r in result["recommendations"] if r["CheckCategory"] == RI_CATEGORY)
    # No "$600.00" (4 x 150) and no monthly dollar at all in the shim string.
    assert "600" not in ri["EstimatedSavings"]
    assert parse_dollar_savings(ri["EstimatedSavings"]) == pytest.approx(0.0)
    # And the "24%" disagreement is gone from the recommendation text.
    assert "24%" not in ri["Recommendation"]


def test_scan_drives_shim_and_makes_ri_advisory() -> None:
    """End-to-end scan() over the real shim path: the shim-built RI rec is demoted
    to a $0 advisory by the adapter; the headline counts $0 (no CoH present)."""
    create = datetime.now(UTC) - timedelta(days=90)
    ctx = _ctx(
        clusters=[
            {
                "ClusterIdentifier": "warehouse",
                "NodeType": "ra3.xlplus",
                "ClusterStatus": "available",
                "ClusterCreateTime": create,
                "NumberOfNodes": 4,
            }
        ],
        workgroups=[{"workgroupName": "wg1", "status": "AVAILABLE"}],
        pricing_monthly=250.0,
    )
    findings = RedshiftModule().scan(ctx)

    recs = findings.sources["enhanced_checks"].recommendations
    ri = next(r for r in recs if r["CheckCategory"] == RI_CATEGORY)
    serverless = next(r for r in recs if r["CheckCategory"] == SERVERLESS_RESERVATION_CATEGORY)
    assert ri["Counted"] is False
    assert serverless["Counted"] is False
    # Every rendered rec contributes $0; total headline is $0 (commitment owned elsewhere).
    assert findings.total_monthly_savings == pytest.approx(0.0)
    # No rec carries a counted-looking dollar while contributing $0.
    for r in recs:
        if r.get("Counted") is False:
            assert parse_dollar_savings(r.get("EstimatedSavings", "")) == pytest.approx(0.0)
