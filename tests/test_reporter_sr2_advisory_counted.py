"""SR-2 (HIGH) — Phase-B cards render counted dollars, exclude advisory ones.

The Phase-B renderers used to (a) print a shim's free-text ``EstimatedSavings``
percentage instead of the live-priced counted dollar, and (b) sum
``Counted=False`` advisory recs into the per-card category total — so an idle
OpenSearch domain that contributed $0, an advisory RI buy, or two
mutually-exclusive EKS Spot+Graviton alternatives all rendered as realized
savings the headline never counted. SR-2 single-sources the per-card dollar to
the *counted* ``EstimatedMonthlySavings`` and labels advisory recs out of the
total.

The golden fixtures carry no recs for the SR-2 target services
(commitment_analysis/eks_cost/msk/opensearch/redshift), so these direct tests
prove the behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reporter_phase_b import (
    _render_eks_source,
    _render_generic_other_rec,
    _render_opensearch_enhanced_checks,
    render_generic_per_rec,
)


# --------------------------------------------------------------------------- #
# OpenSearch H2 — grouped card shows counted sum, not the first domain's string
# --------------------------------------------------------------------------- #
def test_opensearch_card_sums_counted_not_percentage() -> None:
    recs = [
        {"CheckCategory": "Idle Domain", "DomainName": "d1", "EstimatedMonthlySavings": 100.0,
         "EstimatedSavings": "100% of domain cost"},
        {"CheckCategory": "Idle Domain", "DomainName": "d2", "EstimatedMonthlySavings": 50.0,
         "EstimatedSavings": "100% of domain cost"},
    ]
    html = _render_opensearch_enhanced_checks(recs, "enhanced_checks", {})
    assert "$150.00/month" in html
    assert "100% of domain cost" not in html


def test_opensearch_advisory_domain_excluded_from_total() -> None:
    recs = [
        {"CheckCategory": "Graviton Migration", "DomainName": "d1", "EstimatedMonthlySavings": 80.0},
        {"CheckCategory": "Graviton Migration", "DomainName": "d2", "EstimatedMonthlySavings": 999.0,
         "Counted": False},
    ]
    html = _render_opensearch_enhanced_checks(recs, "enhanced_checks", {})
    assert "$80.00/month" in html  # advisory 999 excluded
    assert "1,079" not in html and "999" not in html


def test_opensearch_advisory_only_group_renders_zero_advisory() -> None:
    recs = [{"CheckCategory": "Idle Domain", "DomainName": "d1", "EstimatedMonthlySavings": 0.0, "Counted": False}]
    html = _render_opensearch_enhanced_checks(recs, "enhanced_checks", {})
    assert "$0.00/month — advisory" in html


# --------------------------------------------------------------------------- #
# EKS H3 — card total excludes advisory; advisory lines labelled
# --------------------------------------------------------------------------- #
def test_eks_card_total_excludes_advisory_alternative() -> None:
    recs = [
        {"check_category": "Node Group", "resource_id": "ng-1", "monthly_savings": 30.0},
        {"check_category": "Node Group", "resource_id": "ng-1", "monthly_savings": 99.0, "Counted": False},
    ]
    html = _render_eks_source(recs, "node_group_optimization", {})
    assert "$30.00" in html  # counted only
    assert "$129.00" not in html
    assert "advisory — not added to the tab total" in html


# --------------------------------------------------------------------------- #
# Generic per-rec (msk H2 / redshift H3 / commitment H1)
# --------------------------------------------------------------------------- #
def test_generic_counted_rec_shows_computed_dollar_not_percentage() -> None:
    rec = {"CheckCategory": "MSK Broker", "ClusterName": "c1", "EstimatedMonthlySavings": 200.0,
           "EstimatedSavings": "20% reduction"}
    html = _render_generic_other_rec("", rec, "enhanced_checks")
    assert "$200.00/month" in html
    assert "20% reduction" not in html
    # The raw EstimatedMonthlySavings property line must not be dumped.
    assert "Estimatedmonthlysavings" not in html


def test_generic_advisory_rec_labelled_and_zeroed() -> None:
    rec = {"CheckCategory": "Reserved Nodes", "ClusterName": "c1", "EstimatedMonthlySavings": 500.0,
           "Counted": False, "EstimatedSavings": "24% RI discount"}
    html = _render_generic_other_rec("", rec, "purchase_recommendations")
    assert "advisory (not added to the tab total)" in html
    assert "$0.00/month" in html
    assert "$500.00/month" not in html
    assert "24% RI discount" not in html


def test_generic_legacy_string_kept_when_no_numeric() -> None:
    rec = {"CheckCategory": "X", "ResourceId": "r1", "EstimatedSavings": "20-40% cost reduction"}
    html = _render_generic_other_rec("", rec, "enhanced_checks")
    assert "20-40% cost reduction" in html


def test_redshift_ri_advisory_via_per_rec_dispatch() -> None:
    # redshift routes through render_generic_per_rec -> _render_generic_other_rec.
    recs = [{"CheckCategory": "Reserved Instance", "ClusterName": "rs1", "EstimatedMonthlySavings": 300.0,
             "Counted": False}]
    html = render_generic_per_rec("redshift", recs, "enhanced_checks")
    assert "advisory (not added to the tab total)" in html
    assert "$300.00/month" not in html
