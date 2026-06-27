"""HIGH cost-correctness regression tests for the EBS adapter (ebs H1).

ebs H1 — an unattached/``available`` volume was double-counted: the full delete
cost (100 %, ``unattached_volumes`` source) PLUS the gp2→gp3 migration delta (and,
for io1/io2/gp3, the over-provisioned-IOPS reduction). One volume's savings were
inflated by ~20 %/volume.

The fix makes :func:`services.ebs_logic.dedupe_by_authority` accumulate claimed
volume ids across the heuristic lists in priority order (``unattached`` first), so
the full-delete leg wins and the migration / IOPS legs — moot once the volume is
deleted — are dropped.

Rates validated live against the AWS Pricing API (us-east-1, publicationDate
2026-06-26):

  - gp2 storage  $0.10/GB-Mo  (SKU HY3BZPP2B6K8MSJF, usagetype EBS:VolumeUsage.gp2)
  - gp3 storage  $0.08/GB-Mo  (SKU JG3KUJMBRGHV3N8G, usagetype EBS:VolumeUsage.gp3)
  - gp2→gp3 delta  $0.02/GB-Mo

So a 1,000 GB unattached gp2 volume = $100.00/mo delete; the (now-suppressed)
migration leg would have added 1,000 × $0.02 = $20.00/mo.

Driven without AWS: the pure dedup logic is exercised directly, and the adapter's
``scan()`` path runs with monkeypatched source helpers + a ``SimpleNamespace`` ctx.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.adapters.ebs as ebs_adapter
from services.adapters.ebs import EbsModule
from services.ebs_logic import dedupe_by_authority, normalize_volume_id


# --------------------------------------------------------------------------- #
# Test doubles (mirrors tests/test_ebs_adapter.py)
# --------------------------------------------------------------------------- #
class _FakeEngine:
    """Minimal PricingEngine stand-in: gp2 $0.10, gp3 $0.08 (live us-east-1)."""

    def __init__(self, gp2: float = 0.10, gp3: float = 0.08) -> None:
        self._gp2 = gp2
        self._gp3 = gp3

    def get_ebs_monthly_price_per_gb(self, volume_type: str) -> float:
        return {"gp2": self._gp2, "gp3": self._gp3}.get(volume_type, 0.0)

    def get_ebs_iops_monthly_price(self, volume_type: str) -> float:
        return 0.005


def _ctx(**overrides):
    warns: list = []
    perms: list = []
    base = dict(
        region="us-east-1",
        pricing_multiplier=1.0,
        pricing_engine=_FakeEngine(),
        fast_mode=False,
        old_snapshot_days=90,
        cost_hub_splits={"ebs": []},
        warn=lambda message, service="": warns.append((service, message)),
        permission_issue=lambda message, service="", action=None: perms.append((service, action, message)),
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    ns._warns = warns  # type: ignore[attr-defined]
    ns._perms = perms  # type: ignore[attr-defined]
    return ns


def _patch_sources(monkeypatch, *, enhanced=None, co=None, unattached=None):
    monkeypatch.setattr(
        ebs_adapter, "compute_ebs_checks", lambda *a, **k: {"recommendations": enhanced or []}
    )
    monkeypatch.setattr(ebs_adapter, "get_ebs_compute_optimizer_recs", lambda *a, **k: co or [])
    monkeypatch.setattr(ebs_adapter, "get_unattached_volumes", lambda *a, **k: unattached or [])
    monkeypatch.setattr(ebs_adapter, "get_ebs_volume_count", lambda ctx: {})


# --------------------------------------------------------------------------- #
# Pure logic: heuristic lists dedup against each other, priority order
# --------------------------------------------------------------------------- #
class TestDedupeAcrossHeuristicLists:
    def test_unattached_claim_drops_same_vol_from_gp2_and_iops(self):
        """An unattached volume claims its id; gp2 + IOPS legs for it are dropped."""
        unattached = [{"VolumeId": "vol-1"}]
        gp2 = [{"VolumeId": "vol-1"}, {"VolumeId": "vol-2"}]  # vol-1 overlaps unattached
        iops = [{"VolumeId": "vol-1"}, {"VolumeId": "vol-3"}]  # vol-1 overlaps unattached

        _co_kept, (un_kept, gp2_kept, iops_kept) = dedupe_by_authority(
            [], [], [unattached, gp2, iops]
        )

        assert [r["VolumeId"] for r in un_kept] == ["vol-1"]  # full-delete leg wins
        assert [r["VolumeId"] for r in gp2_kept] == ["vol-2"]  # vol-1 dropped
        assert [r["VolumeId"] for r in iops_kept] == ["vol-3"]  # vol-1 dropped

    def test_distinct_volumes_all_survive(self):
        """No false dedup: distinct ids across the three lists are all kept."""
        _co_kept, (un_kept, gp2_kept, iops_kept) = dedupe_by_authority(
            [], [], [[{"VolumeId": "vol-a"}], [{"VolumeId": "vol-b"}], [{"VolumeId": "vol-c"}]]
        )
        assert [r["VolumeId"] for r in un_kept] == ["vol-a"]
        assert [r["VolumeId"] for r in gp2_kept] == ["vol-b"]
        assert [r["VolumeId"] for r in iops_kept] == ["vol-c"]

    def test_coh_still_wins_over_all_heuristics(self):
        """Authority order preserved: CoH-covered volume drops from every list."""
        coh = [{"resourceId": "vol-x"}]
        unattached = [{"VolumeId": "vol-x"}, {"VolumeId": "vol-y"}]
        gp2 = [{"VolumeId": "vol-x"}]
        _co_kept, (un_kept, gp2_kept) = dedupe_by_authority(coh, [], [unattached, gp2])
        assert [r["VolumeId"] for r in un_kept] == ["vol-y"]
        assert gp2_kept == []

    def test_empty_volume_ids_never_dedupe(self):
        """Recs with no resolvable id are always kept (not collapsed to one)."""
        _co_kept, (a_kept, b_kept) = dedupe_by_authority(
            [], [], [[{"VolumeId": ""}], [{"VolumeId": ""}]]
        )
        assert len(a_kept) == 1 and len(b_kept) == 1


# --------------------------------------------------------------------------- #
# scan() path: counted dollar proves no double-count
# --------------------------------------------------------------------------- #
class TestScanNoDoubleCount:
    def test_1000gb_unattached_gp2_counts_delete_only(self, monkeypatch):
        """1,000 GB unattached gp2 = $100 delete ONLY, not +$20 gp2→gp3 migration."""
        vol = "vol-unattached-gp2"
        # Same volume surfaces as a full-delete (unattached) AND a gp2 migration.
        unattached = [
            {"VolumeId": vol, "Size": 1000, "VolumeType": "gp2", "EstimatedMonthlyCost": 100.0}
        ]
        enhanced = [
            {"VolumeId": vol, "Size": 1000, "CheckCategory": "Volume Type Optimization"}
        ]
        _patch_sources(monkeypatch, enhanced=enhanced, unattached=unattached)
        ctx = _ctx()

        findings = EbsModule().scan(ctx)

        # $100 delete only — the $20 migration leg is de-duplicated away.
        assert findings.total_monthly_savings == pytest.approx(100.0)
        # The migration source is now empty; the unattached source carries the vol.
        assert findings.sources["gp2_migration"].count == 0
        assert findings.sources["unattached_volumes"].count == 1
        # One counted opportunity, not two.
        assert findings.total_recommendations == 1

    def test_1000gb_unattached_io2_counts_delete_only(self, monkeypatch):
        """An unattached io2 volume also flagged over-provisioned counts delete only."""
        vol = "vol-unattached-io2"
        unattached = [
            {"VolumeId": vol, "Size": 1000, "VolumeType": "io2", "EstimatedMonthlyCost": 250.0}
        ]
        # The over-provisioned-IOPS check is a non-gp2/non-snapshot "other" rec.
        enhanced = [
            {
                "VolumeId": vol,
                "CheckCategory": "Over Provisioned Iops",
                "EstimatedSavings": "$40.00/month",
            }
        ]
        _patch_sources(monkeypatch, enhanced=enhanced, unattached=unattached)
        ctx = _ctx()

        findings = EbsModule().scan(ctx)

        # $250 delete only — the $40 IOPS-reduction leg is suppressed (moot on delete).
        assert findings.total_monthly_savings == pytest.approx(250.0)
        assert findings.sources["enhanced_checks"].count == 0
        assert findings.sources["unattached_volumes"].count == 1
        assert findings.total_recommendations == 1

    def test_distinct_unattached_and_gp2_both_counted(self, monkeypatch):
        """Guard against over-dedup: a DIFFERENT gp2 volume is still counted."""
        unattached = [
            {"VolumeId": "vol-un", "Size": 1000, "VolumeType": "gp2", "EstimatedMonthlyCost": 100.0}
        ]
        enhanced = [
            {"VolumeId": "vol-gp2", "Size": 1000, "CheckCategory": "Volume Type Optimization"}
        ]
        _patch_sources(monkeypatch, enhanced=enhanced, unattached=unattached)
        ctx = _ctx()

        findings = EbsModule().scan(ctx)

        # $100 unattached + $20 gp2→gp3 (1000 × (0.10−0.08)) = $120 — both legitimate.
        assert findings.total_monthly_savings == pytest.approx(120.0)
        assert findings.sources["gp2_migration"].count == 1
        assert findings.sources["unattached_volumes"].count == 1
        assert findings.total_recommendations == 2
        # The kept gp2 rec carries the adapter-computed per-volume dollar + basis.
        gp2_rec = findings.sources["gp2_migration"].recommendations[0]
        assert gp2_rec["EstimatedSavings"] == "$20.00/month"
        assert "AuditBasis" in gp2_rec


# --------------------------------------------------------------------------- #
# Sanity: normalize_volume_id keeps the dedup key canonical across sources
# --------------------------------------------------------------------------- #
def test_dedup_key_is_normalized_volume_id():
    assert normalize_volume_id("arn:aws:ec2:us-east-1:1:volume/vol-abc") == "vol-abc"
    assert normalize_volume_id("vol-abc") == "vol-abc"
