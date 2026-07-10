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

import json

import pytest

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
        snap_exc: Exception | None = None,
        region_snapshots: list[dict] | None = None,
    ) -> None:
        # The region's whole snapshot estate. AMI savings are capped at the flagged
        # snapshots' SHARE of the billed EBS:SnapshotUsage pool, so this is the
        # denominator. Defaults to exactly the flagged snapshots (share = 1.0), which
        # keeps tests written about *capping* testing capping.
        if region_snapshots is None:
            region_snapshots = [
                {"SnapshotId": sid, "FullSnapshotSizeInBytes": b}
                for sid, b in (snap_bytes or {}).items()
            ]
        self._paginators = {
            "describe_images": _Pager([{"Images": images}]),
            "describe_snapshots": _Pager([{"Snapshots": region_snapshots}]),
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
        self._snap_exc = snap_exc

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
        if self._snap_exc is not None:
            raise self._snap_exc
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


class _FakeCe:
    """CE stub reporting an ample billed EBS:SnapshotUsage pool.

    AMI savings are a snapshot-size UPPER BOUND and are only counted up to the
    billed pool (lesson C8). Tests below exercise AMI selection logic, not
    capping, so they supply a pool large enough to leave the bound intact.
    A ``billed`` of None simulates ce:GetCostAndUsage being unavailable.
    """

    def __init__(self, billed: float | None = 10_000.0) -> None:
        self._billed = billed

    def get_cost_and_usage(self, **_kw):
        if self._billed is None:
            raise RuntimeError("AccessDeniedException")
        return {"ResultsByTime": [{"Groups": [
            {"Keys": ["APS1-EBS:SnapshotUsage"],
             "Metrics": {"UnblendedCost": {"Amount": str(self._billed)}}}
        ]}]}


def _make_ctx(ec2: _FakeEc2, billed_snapshot_pool: float | None = 10_000.0) -> SimpleNamespace:
    autoscaling = _FakeAutoscaling()
    ce = _FakeCe(billed_snapshot_pool)
    pe = SimpleNamespace(get_ebs_snapshot_price_per_gb=lambda: SNAPSHOT_RATE)
    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0, region="ap-southeast-1")
    ctx.warnings = []
    ctx.permission_issues = []
    ctx.warn = lambda msg, **kw: ctx.warnings.append((msg, kw))
    ctx.permission_issue = lambda msg, **kw: ctx.permission_issues.append((msg, kw))
    ctx.client = lambda n: {"ec2": ec2, "autoscaling": autoscaling, "ce": ce}.get(n, autoscaling)
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
    # A fully-measured AMI is not flagged as an estimate.
    assert rec["SizeEstimated"] is False
    assert "inferred from" not in rec["AuditBasis"]


# --------------------------------------------------------------------------- #
# counted == rendered — the rendered string and the counted numeric agree to the
# cent even when GB is fractional (the dollar is rounded once at the source).
# --------------------------------------------------------------------------- #
def test_snapshot_cost_string_matches_numeric_when_fractional():
    # 49.975 GB x $0.05 = $2.49875 — without rounding the numeric ($2.49875) and
    # the rendered string ("$2.50") diverge; the fix rounds once so they agree.
    frac_bytes = int(49.975 * 1024**3)
    ec2 = _FakeEc2(images=[_ami("ami-frac", 200)], snap_bytes={"snap-ami-frac": frac_bytes})
    out = compute_ami_checks(_make_ctx(ec2))

    rec = out["old_amis"][0]
    assert rec["EstimatedMonthlySavings"] == round(rec["EstimatedMonthlySavings"], 2)
    assert parse_dollar_savings(rec["EstimatedSavings"]) == rec["EstimatedMonthlySavings"]


# --------------------------------------------------------------------------- #
# LOW: when describe_snapshots fails, the size falls back to the AMI's
# block-device-mapping VolumeSize (an upper bound) and the rec discloses the
# estimate — the dollar is never presented as fully measured.
# --------------------------------------------------------------------------- #
def test_snapshot_describe_failure_falls_back_and_discloses_estimate():
    ec2 = _FakeEc2(images=[_ami("ami-est", 200)], snap_exc=RuntimeError("throttled"))
    out = compute_ami_checks(_make_ctx(ec2))

    assert len(out["old_amis"]) == 1
    rec = out["old_amis"][0]
    # BDM VolumeSize=100 is the upper-bound fallback; 100 GB x $0.05 = $5.00.
    assert rec["EstimatedMonthlySavings"] == 100.0 * SNAPSHOT_RATE
    assert parse_dollar_savings(rec["EstimatedSavings"]) == rec["EstimatedMonthlySavings"]
    # The estimate is disclosed, not silently presented as measured.
    assert rec["SizeEstimated"] is True
    assert "inferred from" in rec["AuditBasis"]


# --------------------------------------------------------------------------- #
# LOW (cardinal sin guard): describe_snapshots fails AND the block-device
# mapping carries no VolumeSize — never fabricate an 8 GB dollar. With no
# defensible size the AMI is skipped entirely (fail safe on missing data).
# --------------------------------------------------------------------------- #
def test_no_fabricated_size_when_describe_fails_and_no_volume_size():
    created = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ami = {
        "ImageId": "ami-nosize",
        "Name": "ami-nosize",
        "CreationDate": created,
        # SnapshotId present but NO VolumeSize on the mapping.
        "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": "snap-x"}}],
    }
    ec2 = _FakeEc2(images=[ami], snap_exc=RuntimeError("throttled"))
    out = compute_ami_checks(_make_ctx(ec2))

    # No fabricated 8 GB dollar — the unsizable AMI produces no recommendation.
    assert out["recommendations"] == []
    assert out["old_amis"] == [] and out["unused_amis"] == []


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


# --------------------------------------------------------------------------- #
# AMI-001 — a snapshot shared across AMIs is counted ONCE (cardinal-sin double-
# count fix). An EBS snapshot is billed once and cannot be freed until every AMI
# referencing it is deregistered, so attributing its storage to each AMI overstates.
# --------------------------------------------------------------------------- #
def _ami_with_snaps(image_id: str, age_days: int, snap_ids: list[str]) -> dict:
    created = (datetime.now(UTC) - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    bdm = [
        {"DeviceName": f"/dev/sd{chr(98 + i)}", "Ebs": {"SnapshotId": s, "VolumeSize": 100}}
        for i, s in enumerate(snap_ids)
    ]
    return {"ImageId": image_id, "Name": image_id, "CreationDate": created, "BlockDeviceMappings": bdm}


def test_shared_snapshot_counted_once_second_ami_is_advisory():
    # ami-a and ami-b both reference ONLY snap-shared (200 GB -> $10). It must be
    # counted once ($10), not twice ($20); the second AMI becomes a $0 advisory.
    ec2 = _FakeEc2(
        images=[_ami_with_snaps("ami-a", 200, ["snap-shared"]), _ami_with_snaps("ami-b", 200, ["snap-shared"])],
        snap_bytes={"snap-shared": 200 * 1024**3},
    )
    out = compute_ami_checks(_make_ctx(ec2))
    recs = out["old_amis"]
    assert len(recs) == 2
    counted = [r for r in recs if r.get("Counted") is not False]
    advisory = [r for r in recs if r.get("Counted") is False]
    assert len(counted) == 1 and len(advisory) == 1
    # The shared snapshot's $10 is counted exactly once.
    assert counted[0]["EstimatedMonthlySavings"] == 200.0 * SNAPSHOT_RATE
    assert advisory[0]["EstimatedMonthlySavings"] == 0.0
    assert "snap-shared" in advisory[0]["SharedSnapshotIds"]
    assert advisory[0]["EstimatedSavings"].startswith("$0.00/month")
    # Headline = $10 (NOT $20) — the double-count is gone.
    assert sum(r["EstimatedMonthlySavings"] for r in recs) == 200.0 * SNAPSHOT_RATE


def test_partial_shared_snapshot_counts_only_unique_gb():
    # ami-a -> snap-shared (200 GB). ami-b -> snap-shared + snap-uniqueB (100 GB).
    # ami-a counts snap-shared ($10); ami-b counts only its unique snap ($5).
    ec2 = _FakeEc2(
        images=[
            _ami_with_snaps("ami-a", 200, ["snap-shared"]),
            _ami_with_snaps("ami-b", 200, ["snap-shared", "snap-uniqueB"]),
        ],
        snap_bytes={"snap-shared": 200 * 1024**3, "snap-uniqueB": 100 * 1024**3},
    )
    out = compute_ami_checks(_make_ctx(ec2))
    recs = out["old_amis"]
    a = next(r for r in recs if r["ImageId"] == "ami-a")
    b = next(r for r in recs if r["ImageId"] == "ami-b")
    assert a["EstimatedMonthlySavings"] == 200.0 * SNAPSHOT_RATE  # $10 (snap-shared)
    assert b["EstimatedMonthlySavings"] == 100.0 * SNAPSHOT_RATE  # $5 (only snap-uniqueB)
    assert b.get("Counted") is not False  # still a counted candidate
    assert "shared" in b["AuditBasis"].lower()  # discloses the shared portion
    # snap-shared counted once -> total $15, not $25.
    assert sum(r["EstimatedMonthlySavings"] for r in recs) == 300.0 * SNAPSHOT_RATE


def test_unsizable_snapshot_is_skipped_not_treated_as_shared():
    # describe_snapshots fails AND the BDM carries no VolumeSize -> the AMI cannot
    # be sized. It must be SKIPPED (no fabricated dollar), not emitted as a "shared"
    # advisory (incremental_gb==0 with empty shared_ids).
    created = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ami = {
        "ImageId": "ami-nosize",
        "Name": "ami-nosize",
        "CreationDate": created,
        "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": "snap-x"}}],
    }
    ec2 = _FakeEc2(images=[ami], snap_exc=RuntimeError("throttled"))
    out = compute_ami_checks(_make_ctx(ec2))
    assert out["recommendations"] == []


def test_unsizable_snapshot_not_claimed_so_later_ami_can_count_it():
    # Claim-order safety (adversarial-review defect): an UNSIZABLE snapshot
    # (describe fails, no VolumeSize) must NOT be claimed into the dedup set, or a
    # later AMI that CAN size the same snapshot would be wrongly zeroed as "shared".
    from services.ami import _snapshot_storage_gb

    counted: set[str] = set()
    # AMI-x: snap-z, describe throws, no VolumeSize -> unsizable -> must NOT claim.
    ami_x = {"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-z"}}]}
    gb_x, _all_x, shared_x, _est_x = _snapshot_storage_gb(
        _FakeEc2(images=[], snap_exc=RuntimeError("throttled")), ami_x, counted
    )
    assert gb_x == 0.0 and shared_x == [] and "snap-z" not in counted  # left unclaimed

    # AMI-y: same snap-z, describe SUCCEEDS (200 GB) -> counted, NOT "shared".
    ami_y = {"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-z", "VolumeSize": 100}}]}
    gb_y, _all_y, shared_y, _est_y = _snapshot_storage_gb(
        _FakeEc2(images=[], snap_bytes={"snap-z": 200 * 1024**3}), ami_y, counted
    )
    assert gb_y == 200.0 and shared_y == []  # genuinely counted
    assert "snap-z" in counted


def test_self_duplicate_snapshot_counted_once_not_reported_shared():
    # A snapshot mapped twice within ONE AMI's block devices is counted once and is
    # NOT mislabeled "shared with another AMI".
    from services.ami import _snapshot_storage_gb

    ami = {
        "BlockDeviceMappings": [
            {"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": "snap-dup", "VolumeSize": 100}},
            {"DeviceName": "/dev/sdb", "Ebs": {"SnapshotId": "snap-dup", "VolumeSize": 100}},
        ]
    }
    gb, all_ids, shared, _est = _snapshot_storage_gb(
        _FakeEc2(images=[], snap_bytes={"snap-dup": 100 * 1024**3}), ami, set()
    )
    assert gb == 100.0  # counted once, not 200
    assert shared == []  # self-duplicate is not "shared"
    assert all_ids == ["snap-dup", "snap-dup"]


# --------------------------------------------------------------------------- #
# C8: AMI upper bounds are corroborated against billed EBS:SnapshotUsage
# --------------------------------------------------------------------------- #
def test_ami_savings_demoted_when_cost_explorer_unavailable():
    """Removing evidence must never raise counted savings (bnc: ce:* SCP-denied)."""
    ec2 = _FakeEc2(images=[_ami("ami-free", 200)], snap_bytes={"snap-ami-free": 100 * 1024**3})
    findings = AmiModule().scan(_make_ctx(ec2, billed_snapshot_pool=None))
    assert findings.total_monthly_savings == 0.0
    rec = list(findings.sources["old_amis"].recommendations)[0]
    assert rec["Counted"] is False
    assert rec["PotentialMonthlySavings"] == 100.0 * SNAPSHOT_RATE
    assert "no Cost Explorer actual" in rec["ReconciliationBasis"]


def test_ami_savings_capped_at_billed_snapshot_pool():
    ec2 = _FakeEc2(images=[_ami("ami-free", 200)], snap_bytes={"snap-ami-free": 100 * 1024**3})
    findings = AmiModule().scan(_make_ctx(ec2, billed_snapshot_pool=1.50))
    # Upper bound is 100GB x rate; billed pool is only $1.50 -> capped to it.
    assert findings.total_monthly_savings == pytest.approx(1.50, abs=0.02)
    rec = list(findings.sources["old_amis"].recommendations)[0]
    assert rec["Reconciled"] is True
    assert rec["UpperBoundBeforeReconciliation"] == pytest.approx(100.0 * SNAPSHOT_RATE)


def test_snapshot_actuals_match_usage_type_not_service():
    """EBS snapshots bill under CE service 'EC2 - Other'.

    Regression: filtering SERVICE == 'Amazon Elastic Compute Cloud - Compute'
    matched nothing, returned $0, and falsely demoted $161.60/mo of real AMI
    savings on bnc. Scope by region; match the usage type.
    """
    from services.advisor import get_ebs_snapshot_actuals

    captured = {}

    class _Ce:
        def get_cost_and_usage(self, **kw):
            captured.update(kw)
            return {"ResultsByTime": [{"Groups": [
                {"Keys": ["APS1-EBS:SnapshotUsage"], "Metrics": {"UnblendedCost": {"Amount": "173.06"}}},
                {"Keys": ["APS1-EBS:SnapshotUsage.Archive"], "Metrics": {"UnblendedCost": {"Amount": "10.00"}}},
                {"Keys": ["APS1-BoxUsage:t3.small"], "Metrics": {"UnblendedCost": {"Amount": "500.00"}}},
            ]}]}

    ctx = SimpleNamespace(region="ap-southeast-1", client=lambda n: _Ce() if n == "ce" else None,
                          warn=lambda *a, **k: None, permission_issue=lambda *a, **k: None)
    assert get_ebs_snapshot_actuals(ctx) == pytest.approx(183.06)   # both snapshot lines, not BoxUsage

    # The filter must NOT constrain SERVICE (snapshots live under "EC2 - Other").
    dumped = json.dumps(captured.get("Filter"))
    assert "SERVICE" not in dumped
    assert "REGION" in dumped


def test_ami_savings_limited_to_its_share_of_the_snapshot_estate():
    """afs-prod: 317 unused AMIs held 23.7% of the region's snapshot footprint.

    Capping at the whole billed pool credited 100% of the $5,124.78/mo bill --
    $3,911.50/mo that survives deleting them, because 2,259 other snapshots keep
    billing. The ceiling is the SHARE of the pool, never the pool.
    """
    # Flagged AMI holds 100 GiB; the region's estate is 400 GiB -> 25% share.
    ec2 = _FakeEc2(
        images=[_ami("ami-free", 200)],
        snap_bytes={"snap-ami-free": 100 * 1024**3},
        region_snapshots=[
            {"SnapshotId": "snap-ami-free", "FullSnapshotSizeInBytes": 100 * 1024**3},
            {"SnapshotId": "snap-other-1", "FullSnapshotSizeInBytes": 300 * 1024**3},
        ],
    )
    findings = AmiModule().scan(_make_ctx(ec2, billed_snapshot_pool=1000.0))
    rec = list(findings.sources["old_amis"].recommendations)[0]
    # Upper bound 100GiB x rate = $5.00; its 25% share of the $1000 pool is $250,
    # which exceeds the bound -> counted in full, NOT scaled up to the pool.
    assert findings.total_monthly_savings == pytest.approx(100.0 * SNAPSHOT_RATE, abs=0.02)
    assert rec["AttributableShare"] == pytest.approx(0.25)

    # Now make the bound exceed its share: a $10 pool -> 25% share = $2.50 ceiling.
    findings2 = AmiModule().scan(_make_ctx(ec2, billed_snapshot_pool=10.0))
    rec2 = list(findings2.sources["old_amis"].recommendations)[0]
    assert findings2.total_monthly_savings == pytest.approx(2.50, abs=0.02)
    assert rec2["Reconciled"] is True
    assert rec2["AttributableCeiling"] == pytest.approx(2.50)
    assert findings2.total_monthly_savings < 10.0   # never the whole pool


def test_ami_demoted_when_snapshot_estate_cannot_be_enumerated():
    """Cannot measure the share -> cannot claim a fraction of the pool (C8)."""
    ec2 = _FakeEc2(images=[_ami("ami-free", 200)], snap_bytes={"snap-ami-free": 100 * 1024**3})
    ec2._paginators["describe_snapshots"] = _RaisingPager(RuntimeError("AccessDenied"))
    findings = AmiModule().scan(_make_ctx(ec2, billed_snapshot_pool=10_000.0))
    assert findings.total_monthly_savings == 0.0
    rec = list(findings.sources["old_amis"].recommendations)[0]
    assert rec["Counted"] is False
    assert "share of EBS snapshot storage could not be measured" in rec["EstimatedSavings"]
