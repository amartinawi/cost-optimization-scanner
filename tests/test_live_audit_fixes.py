"""Regression tests for the live-report audit fixes (Jarir-M2 / eu-central-1).

Covers the CRITICAL + HIGH findings remediated against the live scan output:

* C1 - OpenSearch Graviton detection by token pattern (no static allowlist that
  omitted 8th-gen families and flagged already-Graviton nodes for a fabricated
  x86->Graviton migration).
* C2 - Cost Optimization Hub ``Ec2AutoScalingGroup`` recs route to the ``ec2``
  bucket instead of being silently dropped as unbucketed.
* C3 - ``_render_cost_hub_source`` excludes ``Counted=False`` advisory recs from
  the per-card dollar sum.
* H1 - Aurora cluster discovery filters on real engine tokens only
  (``aurora-mysql`` / ``aurora-postgresql``); the bare ``aurora`` token that
  ``DescribeDBClusters`` rejects is gone.
* H5 - Athena ``$0`` metric-gap recs are ``Counted=False`` and excluded from the
  rec-count headline.
* H6 - ``_humanize_key`` splits camelCase / PascalCase keys on case boundaries
  with acronyms preserved instead of collapsing them via ``.title()``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.client_registry import ClientRegistry
from core.scan_context import ScanContext


# --------------------------------------------------------------------------- #
# C1 - OpenSearch Graviton detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "instance_type",
    [
        "m6g.large.search",
        "r7g.xlarge.search",
        "c8g.2xlarge.search",
        "t4g.small.search",
        "m8g.large.search",  # 8th-gen — the old allowlist missed these
        "r8g.large.search",
        "c8g.large.search",
    ],
)
def test_c1_graviton_families_detected(instance_type: str) -> None:
    """Graviton families (generation digit + 'g') are recognised as ARM."""
    from services.opensearch import _is_graviton_search_type

    assert _is_graviton_search_type(instance_type) is True


@pytest.mark.parametrize(
    "instance_type",
    [
        "m5.large.search",
        "r6i.xlarge.search",
        "c5.large.search",
        "m4.large.search",
        "i3.large.search",
        "r5.large.search",
    ],
)
def test_c1_x86_families_not_graviton(instance_type: str) -> None:
    """x86 families are NOT flagged for a Graviton migration they already aren't."""
    from services.opensearch import _is_graviton_search_type

    assert _is_graviton_search_type(instance_type) is False


# --------------------------------------------------------------------------- #
# C2 - Ec2AutoScalingGroup CoH routing
# --------------------------------------------------------------------------- #
def _make_ctx() -> ScanContext:
    """Build a minimal ScanContext with a mocked client registry."""
    registry = MagicMock(spec=ClientRegistry)
    return ScanContext(
        region="us-east-1",
        account_id="123456789012",
        profile=None,
        fast_mode=False,
        clients=registry,
    )


def test_c2_asg_recs_routed_to_ec2_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cost Optimization Hub ``Ec2AutoScalingGroup`` recs land in the ec2 bucket."""
    import core.scan_orchestrator as orch

    asg_rec = {
        "currentResourceType": "Ec2AutoScalingGroup",
        "resourceId": "my-asg",
        "estimatedMonthlySavings": 42.0,
    }
    monkeypatch.setattr(orch, "get_detailed_cost_hub_recommendations", lambda ctx: [asg_rec])

    ctx = _make_ctx()
    modules = [SimpleNamespace(key="ec2")]
    orchestrator = orch.ScanOrchestrator(ctx, modules)
    orchestrator._prefetch_advisor_data({"ec2"})

    assert ctx.cost_hub_splits["ec2"] == [asg_rec]
    # And it is NOT reported as a dropped/unbucketed type.
    assert not any("dropped" in w.message for w in ctx._warnings)


# --------------------------------------------------------------------------- #
# C3 - advisory CoH recs excluded from the card sum
# --------------------------------------------------------------------------- #
# The per-card aggregate-savings header. A counted card renders this with the
# summed dollar; an all-advisory card must suppress it (the per-rec table rows
# and RI scenario matrix still show each rec's own dollar — that is not the sum).
_AGGREGATE_HEADER = '<p class="savings"><strong>Estimated Monthly Savings:</strong> $'


def test_c3_advisory_coh_rec_not_summed_in_card() -> None:
    """A ``Counted=False`` CoH rec renders the advisory line, not a card sum."""
    from reporter_phase_b import _render_cost_hub_source

    advisory = {
        "actionType": "PurchaseReservedInstances",
        "resourceId": "arn:aws:rds:::ri/abc",
        "currentResourceType": "RdsReservedInstances",
        "estimatedMonthlySavings": 100.0,
        "Counted": False,
    }
    html = _render_cost_hub_source([advisory], "cost_optimization_hub", {})

    # The aggregate (counted) savings header is suppressed for an advisory card...
    assert _AGGREGATE_HEADER not in html
    # ...replaced by the explicit advisory note.
    assert "advisory (not added to the tab total)" in html


def test_c3_counted_coh_rec_shows_positive_dollar() -> None:
    """A counted CoH rec still renders its positive aggregate monthly-savings dollar."""
    from reporter_phase_b import _render_cost_hub_source

    counted = {
        "actionType": "Rightsize",
        "resourceId": "my-instance",
        "currentResourceType": "Ec2Instance",
        "estimatedMonthlySavings": 100.0,
    }
    html = _render_cost_hub_source([counted], "cost_optimization_hub", {})

    assert f"{_AGGREGATE_HEADER}100.00" in html


# --------------------------------------------------------------------------- #
# H1 - Aurora engine filter tokens
# --------------------------------------------------------------------------- #
def test_h1_aurora_engines_are_valid_describe_tokens() -> None:
    """AURORA_ENGINES holds only tokens DescribeDBClusters accepts.

    The bare ``aurora`` token is rejected by the engine filter with
    "Unrecognized engine name", so it must not appear.
    """
    from services.adapters.aurora import AURORA_ENGINES

    assert AURORA_ENGINES == ("aurora-mysql", "aurora-postgresql")
    assert "aurora" not in AURORA_ENGINES


# --------------------------------------------------------------------------- #
# H5 - Athena $0 metric-gap recs are advisory + excluded from the count
# --------------------------------------------------------------------------- #
def test_h5_athena_fast_mode_recs_are_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast-mode Athena recs are ``$0``/``Counted=False`` and excluded from the count."""
    import services.adapters.athena as athena_mod

    recs = [{"WorkGroup": "primary"}, {"WorkGroup": "analytics"}]
    monkeypatch.setattr(
        athena_mod, "get_enhanced_athena_checks", lambda ctx: {"recommendations": recs}
    )

    ctx = SimpleNamespace(fast_mode=True, pricing_multiplier=1.0)
    findings = athena_mod.AthenaModule().scan(ctx)

    assert findings.total_monthly_savings == 0.0
    # $0 advisory recs render but are excluded from the headline count.
    assert findings.total_recommendations == 0
    for rec in recs:
        assert rec["EstimatedMonthlySavings"] == 0.0
        assert rec["Counted"] is False
        assert rec["PricingWarning"]


# --------------------------------------------------------------------------- #
# H6 - _humanize_key splits camel/Pascal case with acronyms preserved
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("DBInstanceIdentifier", "DB Instance Identifier"),
        ("CurrentSize", "Current Size"),
        ("cluster_id", "Cluster Id"),
        ("InstanceType", "Instance Type"),
        ("AvgCPU", "Avg CPU"),
        ("instance_count", "Instance Count"),
    ],
)
def test_h6_humanize_key(key: str, expected: str) -> None:
    """camelCase / PascalCase keys split on case boundaries; acronyms preserved."""
    from reporter_phase_b import _humanize_key

    assert _humanize_key(key) == expected


# --------------------------------------------------------------------------- #
# H2 - grouped card savings reconcile to the counted group sum
# --------------------------------------------------------------------------- #
def test_h2_card_sums_free_text_savings_across_group() -> None:
    """A multi-resource card sums every rec's parsed dollar, not just the first."""
    from reporter_phase_b import _grouped_text_savings_line

    group = [
        {"EstimatedSavings": "$3.65/month per EIP"},
        {"EstimatedSavings": "$3.65/month per EIP"},
        {"EstimatedSavings": "$3.65/month per EIP"},
    ]
    line = _grouped_text_savings_line(group)

    assert "$10.95/month" in line  # 3 x 3.65, not the first rec's 3.65


def test_h2_card_prefers_numeric_savings_when_present() -> None:
    """Numeric EstimatedMonthlySavings is summed in preference to the free text."""
    from reporter_phase_b import _grouped_text_savings_line

    group = [
        {"EstimatedSavings": "$32.85/month if consolidated", "EstimatedMonthlySavings": 32.85},
        {"EstimatedSavings": "$32.85/month if consolidated", "EstimatedMonthlySavings": 32.85},
    ]
    line = _grouped_text_savings_line(group)

    assert "$65.70/month" in line


def test_h2_card_excludes_advisory_recs_from_sum() -> None:
    """Counted=False advisory recs never contribute to the card dollar."""
    from reporter_phase_b import _grouped_text_savings_line

    group = [
        {"EstimatedSavings": "$16.43/month if deleted", "EstimatedMonthlySavings": 16.43},
        {
            "EstimatedSavings": "$50.00/month - advisory",
            "EstimatedMonthlySavings": 50.0,
            "Counted": False,
        },
    ]
    line = _grouped_text_savings_line(group)

    assert "$16.43/month" in line
    assert "66" not in line  # 16.43 + 50.0 must NOT be summed


def test_h2_advisory_only_group_renders_zero_advisory() -> None:
    """An all-advisory group shows an honest $0.00 advisory line, not a rate."""
    from reporter_phase_b import _grouped_text_savings_line

    group = [
        {"EstimatedSavings": "rightsizing", "EstimatedMonthlySavings": 12.0, "Counted": False},
        {"EstimatedSavings": "rightsizing", "EstimatedMonthlySavings": 8.0, "Counted": False},
    ]
    line = _grouped_text_savings_line(group)

    assert "$0.00/month — advisory" in line


def test_h2_no_savings_signal_renders_nothing() -> None:
    """A group with no savings signal at all renders no savings line."""
    from reporter_phase_b import _grouped_text_savings_line

    assert _grouped_text_savings_line([{"ResourceName": "foo"}]) == ""
