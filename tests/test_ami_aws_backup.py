"""AFS-2 — an AWS Backup recovery point is not an "unused AMI".

Live evidence, afs-prod / af-south-1 / 2026-08-11 (account 370525687312):
**51 of 56 AMI recs — $281.26 of $302.24 counted, and 31,834 of 34,209 snapshot
GB** — were AWS Backup-created images named ``AwsBackup_<instance-id>_<uuid>``,
spanning 17 source instances and 40–131 days old.

Two structural problems, not one bad card:

1. **The gate can never be false for this resource class.** The test is "not
   referenced by any running instance", which is true of *every* backup image by
   construction — a backup exists precisely so that nothing references it until
   it is needed. So this fires on 100% of AWS Backup AMIs, on every account that
   uses AWS Backup, forever.
2. **The action is wrong and unsafe.** These are recovery points owned by an AWS
   Backup plan with its own retention lifecycle. Deregistering the AMI directly
   circumvents the plan and destroys recovery capability, and the scanner cannot
   see the plan's retention — a 40-day-old image under a 90-day policy is doing
   exactly its job. The real cost lever is **shortening the plan's retention**,
   which is a different action against a different resource.

So the rec is retargeted rather than deleted: it stays visible as a `$0`
advisory carrying the measured storage figure (the amount genuinely at stake if
retention were shortened) and points at the backup plan. That matches how the
repo already treats "real lever, cannot act on it here" — FSx SSD→HDD, the
CloudWatch log-class migration.

Detection is by **tag**, not by name: AWS Backup stamps ``aws:backup:*`` tags on
what it creates. The ``AwsBackup_`` name prefix is kept only as a fallback for
images whose tags did not survive a copy.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services._savings import parse_dollar_savings
from services.ami import _is_aws_backup_managed, compute_ami_checks


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ami(image_id: str, age_days: int, snapshot_id: str | None = "snap-1",
         name: str | None = None, tags: list[dict] | None = None) -> dict:
    created = (datetime.now(UTC) - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    bdm = []
    if snapshot_id is not None:
        bdm = [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": snapshot_id, "VolumeSize": 100}}]
    out = {"ImageId": image_id, "Name": name or image_id,
           "CreationDate": created, "BlockDeviceMappings": bdm}
    if tags is not None:
        out["Tags"] = tags
    return out


def _backup_ami(image_id: str, age_days: int, snapshot_id: str = "snap-1",
                source: str = "i-0b2e25be910fac823") -> dict:
    """An AMI shaped exactly like the af-south-1 ones."""
    return _ami(
        image_id, age_days, snapshot_id,
        name=f"AwsBackup_{source}_41D1B139-7AA2-A6DF-D80C-66DDEC89AF4A",
        tags=[{"Key": "aws:backup:source-resource",
               "Value": f"arn:aws:ec2:af-south-1:370525687312:instance/{source}"}],
    )


def _make_ctx(images: list[dict], snapshot_full_bytes: int = 50 * 1024**3):
    ec2 = MagicMock()
    img_pager = MagicMock()
    img_pager.paginate.return_value = [{"Images": images}]
    inst_pager = MagicMock()
    inst_pager.paginate.return_value = [{"Reservations": [{"Instances": []}]}]
    ec2.get_paginator.side_effect = lambda n: img_pager if n == "describe_images" else inst_pager
    ec2.describe_launch_templates.return_value = {"LaunchTemplates": []}
    ec2.describe_snapshots.return_value = {
        "Snapshots": [{"FullSnapshotSizeInBytes": snapshot_full_bytes, "VolumeSize": 100}]
    }
    ec2.describe_image_attribute.return_value = {"LaunchPermissions": []}
    autoscaling = MagicMock()
    autoscaling.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": []}
    pe = MagicMock()
    pe.get_ebs_snapshot_price_per_gb.return_value = 0.05
    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0)
    ctx.client = lambda n: ec2 if n == "ec2" else autoscaling
    return ctx, ec2


def _all(out: dict) -> list[dict]:
    return out["old_amis"] + out["unused_amis"]


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_detected_by_tag() -> None:
    assert _is_aws_backup_managed(_backup_ami("ami-1", 50)) is True


def test_any_aws_backup_tag_key_counts() -> None:
    """Keyed on the `aws:backup:` namespace rather than one exact key, so a new
    AWS Backup tag does not silently re-open this."""
    for key in ("aws:backup:source-resource", "aws:backup:source-resource-arn"):
        assert _is_aws_backup_managed(_ami("ami-x", 50, tags=[{"Key": key, "Value": "v"}])) is True


def test_name_prefix_is_the_fallback() -> None:
    """Tags can be lost when an image is copied; the name convention survives."""
    ami = _ami("ami-2", 50, name="AwsBackup_i-0abc_41D1B139")
    assert "Tags" not in ami
    assert _is_aws_backup_managed(ami) is True


def test_ordinary_amis_are_not_flagged() -> None:
    assert _is_aws_backup_managed(_ami("ami-3", 50)) is False
    assert _is_aws_backup_managed(_ami("ami-4", 50, name="my-golden-image")) is False
    assert _is_aws_backup_managed(
        _ami("ami-5", 50, tags=[{"Key": "Name", "Value": "AwsBackup_lookalike"}])
    ) is False
    assert _is_aws_backup_managed({}) is False


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #
def test_backup_ami_renders_but_is_never_counted() -> None:
    ctx, _ = _make_ctx([_backup_ami("ami-bk", age_days=40)])
    recs = _all(compute_ami_checks(ctx))
    assert len(recs) == 1
    rec = recs[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert parse_dollar_savings(rec["EstimatedSavings"]) == 0.0
    # The measured storage is preserved — it IS what is at stake if the plan's
    # retention were shortened.
    assert rec["AdvisoryEstimate"] == pytest.approx(50.0 * 0.05, abs=0.01)


def test_the_recommendation_points_at_the_backup_plan() -> None:
    ctx, _ = _make_ctx([_backup_ami("ami-bk", age_days=40)])
    rec = _all(compute_ami_checks(ctx))[0]
    text = (rec["Recommendation"] + rec["EstimatedSavings"]).lower()
    assert "aws backup" in text
    assert "retention" in text
    # It must NOT tell the operator to deregister the image.
    assert "deregister" not in rec["Recommendation"].lower()


def test_the_source_resource_is_named_when_known() -> None:
    ctx, _ = _make_ctx([_backup_ami("ami-bk", 40, source="i-0b2e25be910fac823")])
    rec = _all(compute_ami_checks(ctx))[0]
    assert "i-0b2e25be910fac823" in rec["Recommendation"]


def test_an_ordinary_unused_ami_still_counts() -> None:
    """Regression guard: this fix must not blunt the real lever."""
    ctx, _ = _make_ctx([_ami("ami-plain", age_days=50)])
    recs = _all(compute_ami_checks(ctx))
    assert len(recs) == 1
    assert recs[0].get("Counted", True) is not False
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(2.50, abs=0.01)


# --------------------------------------------------------------------------- #
# It must not consume the dedup budget
# --------------------------------------------------------------------------- #
def test_a_backup_ami_does_not_claim_its_snapshots() -> None:
    """A demoted AMI that claimed the snapshot id would make a genuinely
    deletable AMI sharing it report $0 — silently converting one bad counted
    dollar into a lost real one (the A3 dedup working against us)."""
    ctx, _ = _make_ctx([
        _backup_ami("ami-bk", age_days=60, snapshot_id="snap-shared"),
        _ami("ami-plain", age_days=50, snapshot_id="snap-shared"),
    ])
    recs = {r["ImageId"]: r for r in _all(compute_ami_checks(ctx))}
    assert recs["ami-bk"]["Counted"] is False
    plain = recs["ami-plain"]
    assert plain.get("Counted", True) is not False
    assert plain["EstimatedMonthlySavings"] == pytest.approx(2.50, abs=0.01)
    assert not plain.get("SharedSnapshotIds")


def test_the_launch_permission_call_is_skipped_for_backup_amis() -> None:
    """51 backup AMIs on the audited account = 51 describe_image_attribute calls
    bought nothing. Detection runs before that read."""
    ctx, ec2 = _make_ctx([_backup_ami("ami-bk", age_days=40)])
    compute_ami_checks(ctx)
    assert ec2.describe_image_attribute.call_count == 0


def test_the_af_south_1_shape_counts_nothing() -> None:
    """51 backup AMIs + 5 ordinary ones: only the ordinary ones count."""
    images = [_backup_ami(f"ami-bk{i}", age_days=40 + i, snapshot_id=f"snap-bk{i}")
              for i in range(51)]
    images += [_ami(f"ami-real{i}", age_days=50, snapshot_id=f"snap-real{i}") for i in range(5)]
    recs = _all(compute_ami_checks(_make_ctx(images)[0]))
    assert len(recs) == 56
    counted = [r for r in recs if r.get("Counted", True) is not False]
    assert len(counted) == 5
    assert sum(r["EstimatedMonthlySavings"] for r in counted) == pytest.approx(12.50, abs=0.01)
    assert sum(r["EstimatedMonthlySavings"] for r in recs if r.get("Counted") is False) == 0.0


# --------------------------------------------------------------------------- #
# The demoted GB must not buy ceiling headroom for the survivors
# --------------------------------------------------------------------------- #
def test_demoted_amis_do_not_inflate_the_reconciliation_ceiling() -> None:
    """The share must represent storage the COUNTED recs would actually free.

    Left unfixed, demoting the backup AMIs collapsed the upper bound while the
    ceiling — computed from every rec's SnapshotSizeGB, demoted ones included —
    stayed put, so the factor went to 1.0 and the surviving AMIs jumped to their
    full uncapped bound. On the audited account that was $20.98 -> $141.28: a
    phantom created by the fix itself.
    """
    from services.adapters.ami import AmiModule

    big_backup = {"ImageId": "ami-bk", "SnapshotSizeGB": 31834.0, "Counted": False,
                  "EstimatedMonthlySavings": 0.0}
    real = {"ImageId": "ami-real", "SnapshotSizeGB": 2375.0,
            "EstimatedMonthlySavings": 141.28}

    seen: dict[str, float | None] = {}

    def _fake_reconcile(recs, billed, *, pool_share, **kw):
        seen["pool_share"] = pool_share
        return list(recs), 0.0

    mod = AmiModule()
    mp = pytest.MonkeyPatch()
    ctx = SimpleNamespace(pricing_engine=MagicMock(), pricing_multiplier=1.0,
                          warn=lambda *a, **k: None)
    ctx.client = lambda n: MagicMock()
    import services.adapters.ami as ami_adapter

    mp.setattr(ami_adapter, "compute_ami_checks",
               lambda c, *a, **k: {"old_amis": [], "unused_amis": [big_backup, real], "total_count": 2})
    mp.setattr(ami_adapter, "get_ebs_snapshot_actuals", lambda c: 4420.51)
    mp.setattr(ami_adapter, "region_snapshot_footprint_gib", lambda c: 500131.0)
    mp.setattr(ami_adapter, "reconcile_against_billed", _fake_reconcile)
    try:
        mod.scan(ctx)
    finally:
        mp.undo()

    # 2375 / 500131 — the demoted 31,834 GB must be excluded.
    assert seen["pool_share"] == pytest.approx(2375.0 / 500131.0, rel=1e-6)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
