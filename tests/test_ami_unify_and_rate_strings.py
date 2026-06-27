"""Tests for the AMI deletion-candidate unification and rate-string parsing fixes.

Covers the audit findings against the tadweer-prod scan:

  F1 — unused AMIs must quantify snapshot storage (no '$0 varies by AMI size').
  F2 — an AMI appears in exactly one source (no old/unused double-listing).
  F3 — in-use AMIs are never flagged for deletion.
  F4 — per-unit rate strings ('$0.01/GB') are not counted as dollar totals.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services._savings import parse_dollar_savings
from services.ami import compute_ami_checks


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ami(image_id: str, age_days: int, snapshot_id: str | None = "snap-1") -> dict:
    created = (datetime.now(UTC) - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    bdm = []
    if snapshot_id is not None:
        bdm = [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": snapshot_id, "VolumeSize": 100}}]
    return {"ImageId": image_id, "Name": image_id, "CreationDate": created, "BlockDeviceMappings": bdm}


def _make_ctx(images: list[dict], running_instances: list[dict], snapshot_full_bytes: int):
    """Build a ScanContext stand-in whose ec2 client serves the given images."""
    ec2 = MagicMock()

    img_pager = MagicMock()
    img_pager.paginate.return_value = [{"Images": images}]
    inst_pager = MagicMock()
    inst_pager.paginate.return_value = [{"Reservations": [{"Instances": running_instances}]}]

    def get_paginator(name):
        return img_pager if name == "describe_images" else inst_pager

    ec2.get_paginator.side_effect = get_paginator
    ec2.describe_launch_templates.return_value = {"LaunchTemplates": []}
    ec2.describe_snapshots.return_value = {
        "Snapshots": [{"FullSnapshotSizeInBytes": snapshot_full_bytes, "VolumeSize": 100}]
    }

    autoscaling = MagicMock()
    autoscaling.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": []}

    pe = MagicMock()
    pe.get_ebs_snapshot_price_per_gb.return_value = 0.05

    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0)
    ctx.client = lambda n: ec2 if n == "ec2" else autoscaling
    return ctx


# --------------------------------------------------------------------------- #
# F1 — unused AMIs are quantified from snapshot storage
# --------------------------------------------------------------------------- #
def test_unused_ami_is_quantified_not_zero():
    # 50-day-old unused AMI (between the 30d floor and 90d "old" line).
    ctx = _make_ctx([_ami("ami-unused", age_days=50)], running_instances=[], snapshot_full_bytes=50 * 1024**3)
    out = compute_ami_checks(ctx)

    unused = out["unused_amis"]
    assert len(unused) == 1
    rec = unused[0]
    # 50 GB stored * $0.05/GB-month = $2.50, never $0.
    assert rec["EstimatedMonthlySavings"] == 50.0 * 0.05
    assert rec["EstimatedMonthlySavings"] > 0
    assert "varies by AMI size" not in rec["EstimatedSavings"]
    assert parse_dollar_savings(rec["EstimatedSavings"]) > 0


def test_old_unused_ami_lands_in_old_source():
    ctx = _make_ctx([_ami("ami-old", age_days=120)], running_instances=[], snapshot_full_bytes=100 * 1024**3)
    out = compute_ami_checks(ctx)
    assert len(out["old_amis"]) == 1
    assert out["old_amis"][0]["EstimatedMonthlySavings"] == 100.0 * 0.05
    assert out["unused_amis"] == []


# --------------------------------------------------------------------------- #
# F2 — no double-listing: each AMI in exactly one source
# --------------------------------------------------------------------------- #
def test_no_double_listing_across_sources():
    images = [_ami("ami-a", age_days=120), _ami("ami-b", age_days=50)]
    ctx = _make_ctx(images, running_instances=[], snapshot_full_bytes=10 * 1024**3)
    out = compute_ami_checks(ctx)
    old_ids = {r["ImageId"] for r in out["old_amis"]}
    unused_ids = {r["ImageId"] for r in out["unused_amis"]}
    assert old_ids == {"ami-a"}
    assert unused_ids == {"ami-b"}
    assert old_ids.isdisjoint(unused_ids)


# --------------------------------------------------------------------------- #
# F3 — in-use AMIs are never deletion candidates
# --------------------------------------------------------------------------- #
def test_in_use_ami_not_flagged():
    images = [_ami("ami-inuse", age_days=200)]
    running = [{"State": {"Name": "running"}, "ImageId": "ami-inuse"}]
    ctx = _make_ctx(images, running_instances=running, snapshot_full_bytes=100 * 1024**3)
    out = compute_ami_checks(ctx)
    assert out["old_amis"] == []
    assert out["unused_amis"] == []


# --------------------------------------------------------------------------- #
# F1 (corollary) — instance-store AMIs (no snapshots) skipped, not emitted at $0
# --------------------------------------------------------------------------- #
def test_instance_store_ami_skipped():
    images = [_ami("ami-nostore", age_days=200, snapshot_id=None)]
    ctx = _make_ctx(images, running_instances=[], snapshot_full_bytes=0)
    out = compute_ami_checks(ctx)
    assert out["old_amis"] == []
    assert out["unused_amis"] == []


# --------------------------------------------------------------------------- #
# F4 — per-unit rate strings are not counted as dollar totals
# --------------------------------------------------------------------------- #
def test_rate_strings_parse_to_zero():
    assert parse_dollar_savings("$0.01/GB data processing + NAT costs") == 0.0
    assert parse_dollar_savings("$0.05/hour") == 0.0
    assert parse_dollar_savings("$3.65/month per EIP") == 3.65
    assert parse_dollar_savings("$12.50/month") == 12.5
    assert parse_dollar_savings("$25") == 25.0


def test_network_rate_string_recs_marked_advisory(monkeypatch):
    """vpc_endpoint / NAT data-processing rate nudges are shown but not counted."""
    import services.adapters.network as net

    vpc = [{"VpcId": "v1", "Recommendation": "Create S3 Gateway endpoint",
            "EstimatedSavings": "$0.01/GB data processing + NAT costs"}]
    eip = [{"AllocationId": "e1", "Recommendation": "Release EIP",
            "EstimatedSavings": "$3.65/month per EIP"}]
    # monkeypatch (not raw assignment) so these stubs auto-revert and do not leak
    # empty network sub-shims into the rest of the session (broke the later
    # NetworkModule scan tests in tests/test_network_lb_high_fixes.py).
    monkeypatch.setattr(net, "get_elastic_ip_checks", lambda c: {"recommendations": eip})
    monkeypatch.setattr(net, "get_nat_gateway_checks", lambda c: {"recommendations": []})
    monkeypatch.setattr(net, "get_vpc_endpoints_checks", lambda c: {"recommendations": vpc})
    monkeypatch.setattr(net, "get_load_balancer_checks", lambda c: {"recommendations": []})
    monkeypatch.setattr(net, "get_auto_scaling_checks", lambda c: {"recommendations": []})

    ctx = SimpleNamespace()
    findings = net.NetworkModule().scan(ctx)

    vpc_rec = findings.sources["vpc_endpoints"].recommendations[0]
    eip_rec = findings.sources["elastic_ips"].recommendations[0]
    assert vpc_rec["Counted"] is False
    assert eip_rec.get("Counted", True) is True
    # Only the EIP's $3.65 is counted; the $0.01/GB rate contributes nothing.
    assert findings.total_monthly_savings == 3.65
