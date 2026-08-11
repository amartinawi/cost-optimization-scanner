"""Fixes for output-audit findings RDS-01 / EC2-01 / EBS-01 (afs-prod, 2026-08-08).

Ledger: docs/audits/live/afs-prod_eu-west-1_20260808.md

- RDS-01 (D4): a size-unreported snapshot rec self-describes as advisory in its
  ``EstimatedSavings`` string but never set ``Counted=False`` — it inflated the
  counted recommendation headline (3 phantom recs on afs-prod).
- EC2-01 (B3): enhanced-check idle/rightsizing recs carried their dollar ONLY in
  the string ("$680.36/month if rightsized"); a JSON consumer reading numeric
  fields saw $0. The tab total parses the string, so the numeric must mirror it
  to the cent (B2 lockstep), never change the total.
- EBS-01 (B3): same class for gp2→gp3 migration recs.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_ebs_high_fixes import _ctx as _ebs_ctx  # noqa: E402
from tests.test_ebs_high_fixes import _patch_sources  # noqa: E402
from tests.test_rds_audit_fixes import _EnhancedCtx, _FakeRdsClient, _recs  # noqa: E402
from services.adapters.ebs import EbsModule  # noqa: E402


# --------------------------------------------------------------------------- #
# RDS-01 — advisory-by-string snapshot recs must be Counted=False (D4)
# --------------------------------------------------------------------------- #


def _old_time():
    return datetime.now(timezone.utc) - timedelta(days=400)


def test_zero_size_cluster_snapshot_is_advisory_flagged():
    snaps = [
        {"DBClusterSnapshotIdentifier": "size-unknown", "SnapshotCreateTime": _old_time(),
         "AllocatedStorage": 0, "Engine": "aurora-mysql"},
        {"DBClusterSnapshotIdentifier": "real-1000gb", "SnapshotCreateTime": _old_time(),
         "AllocatedStorage": 1000, "Engine": "aurora-mysql"},
    ]
    ctx = _EnhancedCtx(_FakeRdsClient(cluster_snapshots=snaps))
    recs = {r["SnapshotId"]: r for r in _recs(ctx)
            if r.get("CheckCategory") == "Old Aurora Cluster Snapshots"}
    # Advisory branch: flag AND numeric zero, in lockstep with the string.
    assert recs["size-unknown"]["Counted"] is False
    assert recs["size-unknown"]["EstimatedMonthlySavings"] == 0.0
    # Quantified branch stays counted (no Counted=False).
    assert recs["real-1000gb"].get("Counted") is not False


def test_zero_size_manual_db_snapshot_is_advisory_flagged():
    snaps = [
        {"DBSnapshotIdentifier": "manual-size-unknown", "SnapshotCreateTime": _old_time(),
         "AllocatedStorage": 0, "Engine": "postgres"},
        {"DBSnapshotIdentifier": "manual-real", "SnapshotCreateTime": _old_time(),
         "AllocatedStorage": 100, "Engine": "postgres"},
    ]
    ctx = _EnhancedCtx(_FakeRdsClient(snapshots=snaps))
    recs = {r["SnapshotId"]: r for r in _recs(ctx)
            if r.get("CheckCategory") == "Old RDS Snapshots"}
    assert recs["manual-size-unknown"]["Counted"] is False
    assert recs["manual-size-unknown"]["EstimatedMonthlySavings"] == 0.0
    assert recs["manual-real"].get("Counted") is not False


# --------------------------------------------------------------------------- #
# EC2-01 — heuristic idle/rightsizing recs carry numeric alongside string (B3)
# --------------------------------------------------------------------------- #


class _FakeCw:
    """CPU metric datapoints so the utilization pipeline runs."""

    def get_metric_statistics(self, **kwargs):
        return {"Datapoints": [{"Average": 3.0, "Maximum": 12.0}]}


def _ec2_ctx(instances, prices):
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Reservations": [{"Instances": instances}]}]
    ec2_client = MagicMock()
    ec2_client.get_paginator.return_value = paginator
    ec2_client.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
    cw = _FakeCw()

    def _price(instance_type, os_name="Linux", license_model="No License required", quiet=False):
        return prices[instance_type]

    pricing_engine = MagicMock()
    pricing_engine.get_ec2_hourly_price.side_effect = _price
    return SimpleNamespace(
        region="eu-west-1",
        fast_mode=False,
        pricing_multiplier=1.0,
        pricing_engine=pricing_engine,
        client=lambda name, region=None: cw if name == "cloudwatch" else ec2_client,
        warn=MagicMock(),
        permission_issue=MagicMock(),
    )


def _run_enhanced(monkeypatch, verdict, mem_pct=35.0):
    import services.ec2 as ec2_mod

    monkeypatch.setattr(ec2_mod, "_classify_utilization", lambda *a, **k: verdict)
    monkeypatch.setattr(ec2_mod, "_network_bytes_per_hour", lambda *a, **k: None)
    # r6i is memory-optimized, so AFS-1 requires a memory reading before the
    # dollar may be COUNTED. These tests are about B2/B3 string<->numeric
    # lockstep, so they supply the evidence and keep their original figures; the
    # missing-evidence case is pinned in tests/test_ec2_memory_evidence.py.
    monkeypatch.setattr(ec2_mod, "_memory_used_percent", lambda *a, **k: mem_pct)
    ctx = _ec2_ctx(
        [{"InstanceId": "i-afs1", "InstanceType": "r6i.4xlarge",
          "State": {"Name": "running"}, "PlatformDetails": "Windows", "Tags": []}],
        prices={"r6i.4xlarge": 1.864, "r6i.2xlarge": 0.932},
    )
    return ec2_mod.get_enhanced_ec2_checks(ctx, 1.0, fast_mode=False)["recommendations"]


def test_rightsizing_rec_numeric_mirrors_string(monkeypatch):
    recs = [r for r in _run_enhanced(monkeypatch, "rightsize")
            if r["CheckCategory"] == "Rightsizing Opportunities"]
    assert len(recs) == 1
    # (1.864 - 0.932) x 730 = 680.36 — the afs-prod figure, verified vs live API.
    assert recs[0]["EstimatedSavings"].startswith("$680.36/month")
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(680.36, abs=0.005)


def test_idle_rec_numeric_mirrors_string(monkeypatch):
    recs = [r for r in _run_enhanced(monkeypatch, "idle")
            if r["CheckCategory"] == "Idle Instances"]
    assert len(recs) == 1
    # Idle = full on-demand cost: 1.864 x 730 = 1360.72.
    assert recs[0]["EstimatedSavings"].startswith("$1360.72/month")
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(1360.72, abs=0.005)


def test_lockstep_holds_through_the_afs1_demotion(monkeypatch):
    """B2/B3 must survive the AFS-1 demotion: when the memory evidence is absent
    the string and the numeric both go to $0 together. A demotion that zeroed
    only the numeric would leave the dollar in the tab total, which sums by
    parsing the string."""
    from services._savings import parse_dollar_savings

    recs = [r for r in _run_enhanced(monkeypatch, "rightsize", mem_pct=None)
            if r["CheckCategory"] == "Rightsizing Opportunities"]
    assert len(recs) == 1
    assert recs[0]["Counted"] is False
    assert recs[0]["EstimatedMonthlySavings"] == 0.0
    assert parse_dollar_savings(recs[0]["EstimatedSavings"]) == 0.0
    assert recs[0]["AdvisoryEstimate"] == pytest.approx(680.36, abs=0.005)


# --------------------------------------------------------------------------- #
# EBS-01 — gp2→gp3 recs carry numeric alongside string (B3)
# --------------------------------------------------------------------------- #


def test_gp2_migration_rec_numeric_mirrors_string(monkeypatch):
    enhanced = [{"VolumeId": "vol-gp2", "Size": 1000, "CheckCategory": "Volume Type Optimization"}]
    _patch_sources(monkeypatch, enhanced=enhanced)
    findings = EbsModule().scan(_ebs_ctx())
    rec = findings.sources["gp2_migration"].recommendations[0]
    # 1000 x (0.10 - 0.08) = $20.00 — string and numeric in lockstep (B2).
    assert rec["EstimatedSavings"] == "$20.00/month"
    assert rec["EstimatedMonthlySavings"] == pytest.approx(20.0)
    # The numeric is a mirror, not a second sum: the tab total is unchanged.
    assert findings.total_monthly_savings == pytest.approx(20.0)
