"""HIGH cost-correctness fixes for the AMI adapter (ami H3 — fail safe).

ami H3: unused-AMI detection previously missed reference paths (EC2 Fleet /
Spot Fleet launch-spec & override ImageIds, and cross-account/public sharing),
so an AMI in active use elsewhere could be emitted as a *deregister-and-delete*
candidate — a destructive false positive. The fix:

  * extends the in-use set with ``describe_fleets`` (LaunchTemplateConfigs
    Overrides ImageId) and ``describe_spot_fleet_requests``
    (LaunchSpecifications ImageId + LaunchTemplateConfigs Overrides ImageId);
  * treats any AMI shared via ``describe_image_attribute(launchPermission)`` as
    in-use (a consumer account we cannot enumerate may depend on it);
  * abstains on this AMI (no counted delete) when the launchPermission read
    fails — ambiguous evidence on a deletion rec (global rule 5);
  * fails safe on a fleet/spot-fleet enumeration error (suppress all
    candidates), consistent with the existing instance/template/ASG fail-safe.

Rate basis (validated live via AWS Pricing MCP, 2026-06-27): EBS snapshot
storage = $0.05/GB-Month us-east-1 (AmazonEC2, usagetype EBS:SnapshotUsage,
SKU 7U7TWP44UP36AT3R). The H3 fix changes *which* AMIs are emitted, not the
dollar formula; the counted dollar still equals snapshot-GB x rate.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services._savings import parse_dollar_savings
from services.adapters.ami import AmiModule
from services.ami import compute_ami_checks

SNAPSHOT_RATE = 0.05


# --------------------------------------------------------------------------- #
# Fakes — precise, dict-shaped boto3 stand-ins (no MagicMock truthiness traps)
# --------------------------------------------------------------------------- #
class _ClientError(Exception):
    """botocore-ClientError stand-in carrying an AWS error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Pager:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **_kwargs):
        return list(self._pages)


class _RaisingPager:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def paginate(self, **_kwargs):
        raise self._exc


class _FakeEc2:
    def __init__(
        self,
        *,
        images: list[dict],
        fleet_pages: list[dict] | None = None,
        spot_pages: list[dict] | None = None,
        fleet_exc: Exception | None = None,
        spot_exc: Exception | None = None,
        shared_ids: set[str] | None = None,
        image_attr_exc: Exception | None = None,
        snap_bytes: dict[str, int] | None = None,
    ) -> None:
        self._paginators = {
            "describe_images": _Pager([{"Images": images}]),
            "describe_instances": _Pager([{"Reservations": []}]),
            "describe_launch_templates": _Pager([{"LaunchTemplates": []}]),
            "describe_fleets": (
                _RaisingPager(fleet_exc) if fleet_exc else _Pager(fleet_pages or [{"Fleets": []}])
            ),
            "describe_spot_fleet_requests": (
                _RaisingPager(spot_exc)
                if spot_exc
                else _Pager(spot_pages or [{"SpotFleetRequestConfigs": []}])
            ),
        }
        self._shared_ids = shared_ids or set()
        self._image_attr_exc = image_attr_exc
        self._snap_bytes = snap_bytes or {}

    def get_paginator(self, name: str):
        return self._paginators[name]

    def describe_launch_template_versions(self, **_kwargs):
        return {"LaunchTemplateVersions": []}

    def describe_image_attribute(self, *, ImageId: str, Attribute: str):  # noqa: N803
        assert Attribute == "launchPermission"
        if self._image_attr_exc is not None:
            raise self._image_attr_exc
        if ImageId in self._shared_ids:
            return {"ImageId": ImageId, "LaunchPermissions": [{"UserId": "210000000000"}]}
        return {"ImageId": ImageId, "LaunchPermissions": []}

    def describe_snapshots(self, *, SnapshotIds: list[str]):  # noqa: N803
        snap_id = SnapshotIds[0]
        full_bytes = self._snap_bytes.get(snap_id, 10 * 1024**3)
        return {"Snapshots": [{"FullSnapshotSizeInBytes": full_bytes, "VolumeSize": 100}]}


class _FakeAutoscaling:
    def get_paginator(self, _name: str):
        return _Pager([{"AutoScalingGroups": []}])

    def describe_launch_configurations(self, **_kwargs):
        return {"LaunchConfigurations": []}


def _ami(image_id: str, age_days: int, with_snapshot: bool = True) -> dict:
    created = (datetime.now(UTC) - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    bdm = []
    if with_snapshot:
        bdm = [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": f"snap-{image_id}", "VolumeSize": 100}}]
    return {"ImageId": image_id, "Name": image_id, "CreationDate": created, "BlockDeviceMappings": bdm}


def _make_ctx(ec2: _FakeEc2) -> SimpleNamespace:
    autoscaling = _FakeAutoscaling()
    pe = SimpleNamespace(get_ebs_snapshot_price_per_gb=lambda: SNAPSHOT_RATE)
    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0)
    ctx.warnings = []
    ctx.permission_issues = []
    ctx.warn = lambda msg, **kw: ctx.warnings.append((msg, kw))
    ctx.permission_issue = lambda msg, **kw: ctx.permission_issues.append((msg, kw))
    ctx.client = lambda n: ec2 if n == "ec2" else autoscaling
    return ctx


# --------------------------------------------------------------------------- #
# Baseline: a genuinely unused, non-shared AMI is still a counted candidate.
# --------------------------------------------------------------------------- #
def test_unused_ami_counted_with_audit_basis():
    ec2 = _FakeEc2(images=[_ami("ami-free", 200)], snap_bytes={"snap-ami-free": 100 * 1024**3})
    out = compute_ami_checks(_make_ctx(ec2))

    assert len(out["old_amis"]) == 1
    rec = out["old_amis"][0]
    # 100 GB stored x $0.05/GB-Mo = $5.00.
    assert rec["EstimatedMonthlySavings"] == 100.0 * SNAPSHOT_RATE
    # counted == rendered: the string parses back to the counted float.
    assert parse_dollar_savings(rec["EstimatedSavings"]) == rec["EstimatedMonthlySavings"]
    # AuditBasis defends the dollar (rate + metric + formula).
    assert "EBS:SnapshotUsage" in rec["AuditBasis"]
    assert "0.0500" in rec["AuditBasis"]


# --------------------------------------------------------------------------- #
# H3: EC2 Fleet override ImageId keeps the AMI in-use (not a delete candidate).
# --------------------------------------------------------------------------- #
def test_ec2_fleet_override_marks_ami_in_use():
    fleet_pages = [
        {"Fleets": [{"LaunchTemplateConfigs": [{"Overrides": [{"ImageId": "ami-fleet"}]}]}]}
    ]
    ec2 = _FakeEc2(images=[_ami("ami-fleet", 200)], fleet_pages=fleet_pages)
    out = compute_ami_checks(_make_ctx(ec2))
    assert out["recommendations"] == []
    assert out["old_amis"] == [] and out["unused_amis"] == []


# --------------------------------------------------------------------------- #
# H3: Spot Fleet inline LaunchSpecifications ImageId keeps the AMI in-use.
# --------------------------------------------------------------------------- #
def test_spot_fleet_launch_spec_marks_ami_in_use():
    spot_pages = [
        {
            "SpotFleetRequestConfigs": [
                {"SpotFleetRequestConfig": {"LaunchSpecifications": [{"ImageId": "ami-spot"}]}}
            ]
        }
    ]
    ec2 = _FakeEc2(images=[_ami("ami-spot", 200)], spot_pages=spot_pages)
    out = compute_ami_checks(_make_ctx(ec2))
    assert out["recommendations"] == []


# --------------------------------------------------------------------------- #
# H3: Spot Fleet launch-template Overrides ImageId keeps the AMI in-use.
# --------------------------------------------------------------------------- #
def test_spot_fleet_launch_template_override_marks_ami_in_use():
    spot_pages = [
        {
            "SpotFleetRequestConfigs": [
                {
                    "SpotFleetRequestConfig": {
                        "LaunchTemplateConfigs": [{"Overrides": [{"ImageId": "ami-spot-lt"}]}]
                    }
                }
            ]
        }
    ]
    ec2 = _FakeEc2(images=[_ami("ami-spot-lt", 200)], spot_pages=spot_pages)
    out = compute_ami_checks(_make_ctx(ec2))
    assert out["recommendations"] == []


# --------------------------------------------------------------------------- #
# H3: a shared (cross-account/public) AMI is treated as in-use, not deleted.
# --------------------------------------------------------------------------- #
def test_shared_ami_not_flagged():
    ec2 = _FakeEc2(
        images=[_ami("ami-shared", 200)],
        shared_ids={"ami-shared"},
        snap_bytes={"snap-ami-shared": 100 * 1024**3},
    )
    out = compute_ami_checks(_make_ctx(ec2))
    assert out["recommendations"] == []


# --------------------------------------------------------------------------- #
# H3 / fail-safe: an ambiguous launchPermission read abstains on THAT AMI and
# classifies the error (AccessDenied -> permission_issue; other -> warn).
# --------------------------------------------------------------------------- #
def test_image_attribute_access_denied_abstains_and_classifies():
    ec2 = _FakeEc2(
        images=[_ami("ami-x", 200)],
        image_attr_exc=_ClientError("AccessDenied"),
        snap_bytes={"snap-ami-x": 100 * 1024**3},
    )
    ctx = _make_ctx(ec2)
    out = compute_ami_checks(ctx)
    # No counted delete on ambiguous evidence.
    assert out["recommendations"] == []
    # AccessDenied is a permission gap, not a transient warn.
    assert len(ctx.permission_issues) == 1
    assert ctx.warnings == []


def test_image_attribute_generic_error_abstains_and_warns():
    ec2 = _FakeEc2(
        images=[_ami("ami-y", 200)],
        image_attr_exc=_ClientError("ThrottlingException"),
        snap_bytes={"snap-ami-y": 100 * 1024**3},
    )
    ctx = _make_ctx(ec2)
    out = compute_ami_checks(ctx)
    assert out["recommendations"] == []
    # Non-permission error -> warn (still surfaced, never swallowed).
    assert len(ctx.warnings) == 1
    assert ctx.permission_issues == []


# --------------------------------------------------------------------------- #
# H3 / fail-safe: a fleet enumeration failure suppresses ALL candidates so an
# AMI referenced only by the unreadable fleet is never deleted.
# --------------------------------------------------------------------------- #
def test_describe_fleets_failure_suppresses_all_candidates():
    ec2 = _FakeEc2(images=[_ami("ami-free", 200)], fleet_exc=_ClientError("AccessDenied"))
    ctx = _make_ctx(ec2)
    out = compute_ami_checks(ctx)
    assert out["recommendations"] == []
    assert out["total_count"] == 0
    # permission gap recorded for the fleet read + the suppression warn.
    assert len(ctx.permission_issues) == 1
    assert any("suppressing" in msg for msg, _ in ctx.warnings)


def test_describe_spot_fleet_failure_suppresses_all_candidates():
    ec2 = _FakeEc2(images=[_ami("ami-free", 200)], spot_exc=_ClientError("RequestLimitExceeded"))
    ctx = _make_ctx(ec2)
    out = compute_ami_checks(ctx)
    assert out["recommendations"] == []
    assert any("suppressing" in msg for msg, _ in ctx.warnings)


# --------------------------------------------------------------------------- #
# scan() path: only the genuinely-unused, non-shared AMI feeds the headline.
# counted == rendered: total_monthly_savings == the single rec's dollar.
# --------------------------------------------------------------------------- #
def test_scan_counts_only_genuine_unused_ami():
    fleet_pages = [{"Fleets": [{"LaunchTemplateConfigs": [{"Overrides": [{"ImageId": "ami-fleet"}]}]}]}]
    images = [_ami("ami-fleet", 200), _ami("ami-free", 200)]
    ec2 = _FakeEc2(
        images=images,
        fleet_pages=fleet_pages,
        snap_bytes={"snap-ami-fleet": 60 * 1024**3, "snap-ami-free": 40 * 1024**3},
    )
    findings = AmiModule().scan(_make_ctx(ec2))

    # ami-fleet is in-use via the fleet override -> excluded; only ami-free.
    assert findings.total_recommendations == 1
    expected = 40.0 * SNAPSHOT_RATE  # $2.00
    assert findings.total_monthly_savings == expected

    rec = findings.sources["old_amis"].recommendations[0]
    assert rec["ImageId"] == "ami-free"
    # counted dollar == rendered dollar on the card.
    assert parse_dollar_savings(rec["EstimatedSavings"]) == findings.total_monthly_savings
