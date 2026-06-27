"""Tests for the EC2 H2 remediation — tag-heuristic levers gated on CW evidence.

The four advanced EC2 levers (cron / batch / instance-store / non-prod) are
inferred from Name/Environment tags and carry no usage evidence. Their
blanket-factor dollar (``EC2_SAVINGS_FACTORS``) is fabricated unless the same
instance is corroborated by the CloudWatch idle/low-CPU signal the rightsizing
checks already gather. These tests pin:

  * ``_tag_heuristic_savings`` — counted with corroboration, $0 advisory without.
  * ``get_advanced_ec2_checks`` — all four levers demote to ``Counted=False`` when
    the instance is not in ``corroborated_ids``; count when it is.
  * The adapter derives ``corroborated_ids`` from the low-utilization enhanced
    categories only (not config-based prev-gen) and renders advisory advanced
    recs without summing them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.ec2 as ec2_adapter
from services.adapters.ec2 import EC2Module
from services.ec2 import _tag_heuristic_savings, get_advanced_ec2_checks


# --------------------------------------------------------------------------- #
# _tag_heuristic_savings — the counted/advisory switch
# --------------------------------------------------------------------------- #
def test_tag_heuristic_savings_counted_when_corroborated() -> None:
    est, extra = _tag_heuristic_savings(46.72, "with an off-hours schedule", corroborated=True)
    assert est == "$46.72/month with an off-hours schedule"
    assert extra == {}  # counted path carries no advisory flag


def test_tag_heuristic_savings_advisory_when_not_corroborated() -> None:
    est, extra = _tag_heuristic_savings(46.72, "with an off-hours schedule", corroborated=False)
    assert est.startswith("$0.00/month")
    assert extra["Counted"] is False
    assert extra["AdvisoryEstimate"] == pytest.approx(46.72)


# --------------------------------------------------------------------------- #
# get_advanced_ec2_checks — every tag lever obeys the corroboration gate
# --------------------------------------------------------------------------- #
def _ctx(instances: list[dict], hourly: float = 0.10):
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Reservations": [{"Instances": instances}]}]
    ec2_client = MagicMock()
    ec2_client.get_paginator.return_value = paginator
    ec2_client.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
    pricing_engine = MagicMock()
    pricing_engine.get_ec2_hourly_price.side_effect = (
        lambda t, os_name="Linux", license_model="No License required", quiet=False: hourly
    )
    return SimpleNamespace(
        region="us-east-1",
        fast_mode=True,
        pricing_multiplier=1.0,
        pricing_engine=pricing_engine,
        client=lambda name, region=None: ec2_client,
        warn=MagicMock(),
        permission_issue=MagicMock(),
    )


@pytest.mark.parametrize(
    "tags, category",
    [
        ([{"Key": "Name", "Value": "cron-runner"}], "Cron Job Instances"),
        ([{"Key": "Name", "Value": "batch-worker"}], "Batch Job Instances"),
        ([{"Key": "Environment", "Value": "dev"}, {"Key": "Name", "Value": "dev-box"}], "Non-Prod Scheduling"),
    ],
)
def test_tag_levers_are_advisory_without_corroboration(tags, category) -> None:
    instances = [{"InstanceId": "i-1", "InstanceType": "m5.large", "State": {"Name": "running"}, "Tags": tags}]
    recs = get_advanced_ec2_checks(_ctx(instances), 1.0, True)["recommendations"]
    rec = next(r for r in recs if r["CheckCategory"] == category)
    assert rec["Counted"] is False
    assert rec["EstimatedSavings"].startswith("$0.00")
    assert rec["AdvisoryEstimate"] > 0  # speculative figure preserved, not summed


@pytest.mark.parametrize(
    "tags, category",
    [
        ([{"Key": "Name", "Value": "cron-runner"}], "Cron Job Instances"),
        ([{"Key": "Name", "Value": "batch-worker"}], "Batch Job Instances"),
        ([{"Key": "Environment", "Value": "dev"}, {"Key": "Name", "Value": "dev-box"}], "Non-Prod Scheduling"),
    ],
)
def test_tag_levers_count_when_corroborated(tags, category) -> None:
    instances = [{"InstanceId": "i-1", "InstanceType": "m5.large", "State": {"Name": "running"}, "Tags": tags}]
    recs = get_advanced_ec2_checks(_ctx(instances), 1.0, True, corroborated_ids=frozenset({"i-1"}))[
        "recommendations"
    ]
    rec = next(r for r in recs if r["CheckCategory"] == category)
    assert "Counted" not in rec
    assert rec["EstimatedSavings"].startswith("$")
    assert not rec["EstimatedSavings"].startswith("$0.00")


def test_instance_store_lever_advisory_without_corroboration() -> None:
    # i3 is an instance-store family; a non-storage name keeps it eligible.
    instances = [{"InstanceId": "i-store", "InstanceType": "i3.large", "State": {"Name": "running"},
                  "Tags": [{"Key": "Name", "Value": "app-server"}]}]
    recs = get_advanced_ec2_checks(_ctx(instances), 1.0, True)["recommendations"]
    store = [r for r in recs if r["CheckCategory"] == "Underutilized Instance Store"]
    assert len(store) == 1
    assert store[0]["Counted"] is False


def test_spot_migration_is_unaffected_by_gate() -> None:
    # Spot uses the live on-demand−Spot delta (real pricing), never a tag factor,
    # so it stays counted even with no corroboration.
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Reservations": [{"Instances": [
            {"InstanceId": "i-spot", "InstanceType": "m5.large", "State": {"Name": "running"},
             "PlatformDetails": "Linux/UNIX",
             "Tags": [{"Key": "interruptible", "Value": "true"}, {"Key": "Name", "Value": "render"}]}
        ]}]}
    ]
    ec2_client = MagicMock()
    ec2_client.get_paginator.return_value = paginator
    ec2_client.describe_spot_price_history.return_value = {"SpotPriceHistory": [{"SpotPrice": "0.035"}]}
    pricing_engine = MagicMock()
    pricing_engine.get_ec2_hourly_price.side_effect = (
        lambda t, os_name="Linux", license_model="No License required", quiet=False: 0.107
    )
    ctx = SimpleNamespace(
        region="us-east-1", fast_mode=True, pricing_multiplier=1.0, pricing_engine=pricing_engine,
        client=lambda name, region=None: ec2_client, warn=MagicMock(), permission_issue=MagicMock(),
    )
    recs = get_advanced_ec2_checks(ctx, 1.0, True)["recommendations"]
    spot = next(r for r in recs if r["CheckCategory"] == "Spot Migration")
    assert "Counted" not in spot
    assert spot["EstimatedSavings"].startswith("$52.56")  # (0.107 - 0.035) × 730


# --------------------------------------------------------------------------- #
# Adapter — corroborated_ids derivation + advisory rendering
# --------------------------------------------------------------------------- #
def _adapter_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        cost_hub_splits={"ec2": []},
        pricing_multiplier=1.0,
        fast_mode=False,
        client=lambda name, region=None: None,
        warn=lambda *a, **k: None,
        permission_issue=lambda *a, **k: None,
    )


def test_adapter_derives_corroborated_ids_from_low_util_only(monkeypatch) -> None:
    """Only idle/rightsizing/burstable enhanced findings corroborate — not prev-gen."""
    enhanced = [
        {"InstanceId": "i-idle", "EstimatedSavings": "$80.00/month", "CheckCategory": "Idle Instances"},
        {"InstanceId": "i-prevgen", "EstimatedSavings": "$5.00/month", "CheckCategory": "Previous Generation Migration"},
    ]
    captured: dict[str, frozenset] = {}

    def _spy_advanced(ctx, mult, fast, corroborated_ids=frozenset()):
        captured["ids"] = corroborated_ids
        return {"recommendations": []}

    monkeypatch.setattr(ec2_adapter, "get_ec2_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_asg_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_enhanced_ec2_checks", lambda *a, **k: {"recommendations": enhanced})
    monkeypatch.setattr(ec2_adapter, "get_advanced_ec2_checks", _spy_advanced)
    monkeypatch.setattr(ec2_adapter, "get_ec2_instance_count", lambda ctx: 2)

    EC2Module().scan(_adapter_ctx())
    assert captured["ids"] == frozenset({"i-idle"})  # prev-gen is config-based, excluded


def test_adapter_renders_advisory_advanced_recs_without_counting(monkeypatch) -> None:
    """A Counted=False advanced rec renders in the tab but adds $0 to the headline."""
    advanced = [
        {"InstanceId": "i-adv", "EstimatedSavings": "$0.00/month — advisory", "Counted": False,
         "AdvisoryEstimate": 40.0, "CheckCategory": "Cron Job Instances"},
        {"InstanceId": "i-cnt", "EstimatedSavings": "$25.00/month", "CheckCategory": "Batch Job Instances"},
    ]
    monkeypatch.setattr(ec2_adapter, "get_ec2_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_asg_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_enhanced_ec2_checks", lambda *a, **k: {"recommendations": []})
    monkeypatch.setattr(ec2_adapter, "get_advanced_ec2_checks", lambda *a, **k: {"recommendations": advanced})
    monkeypatch.setattr(ec2_adapter, "get_ec2_instance_count", lambda ctx: 2)

    findings = EC2Module().scan(_adapter_ctx())
    # Only the counted $25 enters the headline; the advisory adds $0.
    assert findings.total_monthly_savings == pytest.approx(25.0)
    # Both still render (counted == rendered).
    advanced_recs = findings.sources["advanced_ec2_checks"].recommendations
    cats = {r["CheckCategory"] for r in advanced_recs}
    assert cats == {"Cron Job Instances", "Batch Job Instances"}
    advisory = next(r for r in advanced_recs if r["CheckCategory"] == "Cron Job Instances")
    assert advisory["Counted"] is False
