"""Pure decision logic for the EBS adapter — no AWS, no ScanContext.

Extracted so the cross-source de-duplication, Compute Optimizer
finding-actionability, enhanced-check partitioning, and usage-based IOPS
rightsizing rules can be unit-tested without boto3 or live pricing.
"""

from __future__ import annotations

import math
from typing import Any

# Enhanced-check categories that have their own dedicated source / renderer in
# the EBS tab (so they must not be re-counted as generic "other" checks).
EBS_DEDICATED_CATEGORIES: frozenset[str] = frozenset({"Volume Type Optimization", "Unattached Volumes"})

# Compute Optimizer findings that carry no cost-reduction opportunity:
#   "Optimized"          → already right-sized, no savings.
#   "UnderProvisioned"   → recommends a LARGER volume, i.e. a cost increase.
# Both are dropped at the source so the counted total matches the rendered table.
_NON_ACTIONABLE_FINDINGS: frozenset[str] = frozenset({"optimized", "under_provisioned", "underprovisioned"})


def is_actionable_co_finding(finding: str) -> bool:
    """Return True when an EBS Compute Optimizer ``finding`` represents a saving."""
    normalized = str(finding or "").strip().lower().replace(" ", "_")
    return normalized not in _NON_ACTIONABLE_FINDINGS


def normalize_volume_id(raw: str) -> str:
    """Reduce a volume ARN / resourceId to its bare ``vol-...`` identifier.

    Accepts plain ids, ``arn:aws:ec2:...:volume/vol-abc`` ARNs, and
    ``.../vol-abc`` suffixes. Returns ``""`` for falsy input.
    """
    if not raw:
        return ""
    return str(raw).rstrip("/").split("/")[-1].split(":")[-1]


def coh_volume_id(rec: dict[str, Any]) -> str:
    """Volume id for a Cost Optimization Hub EBS recommendation."""
    return normalize_volume_id(rec.get("resourceId") or rec.get("resourceArn") or "")


def co_volume_id(rec: dict[str, Any]) -> str:
    """Volume id for a Compute Optimizer EBS recommendation."""
    return normalize_volume_id(rec.get("volumeArn") or rec.get("resourceId") or "")


def heuristic_volume_id(rec: dict[str, Any]) -> str:
    """Volume id for an in-house heuristic EBS recommendation."""
    return normalize_volume_id(rec.get("VolumeId") or "")


def is_snapshot_category(category: str) -> bool:
    """True when a check category belongs in the dedicated Snapshots tab."""
    return "snapshot" in str(category or "").lower()


def is_unattached_category(category: str) -> bool:
    """True when a check category is the dedicated Unattached Volumes source."""
    return "unattached" in str(category or "").lower()


def partition_enhanced_recs(
    recs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``compute_ebs_checks`` output by render destination.

    Returns ``(gp2_recs, snapshot_recs, other_recs)``:

    - ``gp2_recs``      — ``CheckCategory == "Volume Type Optimization"`` (own source).
    - ``snapshot_recs`` — snapshot categories; rendered in the dedicated Snapshots
      tab and intentionally **not** counted toward EBS savings/totals.
    - ``other_recs``    — remaining checks (e.g. over-provisioned IOPS), excluding
      the dedicated ``Unattached Volumes`` category (carried by its own source).
    """
    gp2: list[dict[str, Any]] = []
    snaps: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for rec in recs:
        category = rec.get("CheckCategory", "")
        if category == "Volume Type Optimization":
            gp2.append(rec)
        elif is_snapshot_category(category):
            snaps.append(rec)
        elif is_unattached_category(category):
            continue
        else:
            other.append(rec)
    return gp2, snaps, other


def dedupe_by_authority(
    coh_recs: list[dict[str, Any]],
    co_recs: list[dict[str, Any]],
    heuristic_rec_lists: list[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Apply authority order Cost Hub > Compute Optimizer > heuristic by volume id.

    Cost Optimization Hub re-surfaces Compute Optimizer findings, and the
    in-house heuristics re-detect the same volumes. Counting all three would
    inflate one volume's savings 2-3x. Cost Hub recs are always retained by the
    caller; this returns the de-duplicated ``(co_kept, heuristic_kept_lists)``.

    The heuristic lists are also de-duplicated **against each other** in the order
    given: a volume claimed by an earlier list drops from every later list. The
    same unattached/``available`` volume is otherwise surfaced as both a 100 %
    delete (``unattached_volumes``) **and** a gp2→gp3 migration delta (or an
    over-provisioned-IOPS reduction), double-counting one volume's savings. With
    ``unattached`` passed first, the full delete cost wins and the migration/IOPS
    legs — which become moot once the volume is deleted — are dropped.

    Args:
        coh_recs: Cost Optimization Hub recommendations (highest authority).
        co_recs: Compute Optimizer recommendations.
        heuristic_rec_lists: Ordered list of heuristic rec lists (e.g. unattached,
            gp2, over-provisioned). Earlier lists have priority; ids they claim are
            accumulated and removed from later lists. Order is preserved.
    """
    coh_ids = {coh_volume_id(r) for r in coh_recs} - {""}
    co_kept = [r for r in co_recs if co_volume_id(r) not in coh_ids]
    covered = coh_ids | ({co_volume_id(r) for r in co_kept} - {""})
    heuristic_kept: list[list[dict[str, Any]]] = []
    for recs in heuristic_rec_lists:
        kept = [r for r in recs if heuristic_volume_id(r) not in covered]
        heuristic_kept.append(kept)
        # Accumulate this list's surviving ids so later lists drop any overlap
        # (e.g. an unattached volume must not also be counted as a gp2 migration).
        covered = covered | ({heuristic_volume_id(r) for r in kept} - {""})
    return co_kept, heuristic_kept


def gp2_baseline_iops(size_gb: float) -> int:
    """gp2 baseline IOPS for a volume: 3 IOPS/GB, floor 100, ceiling 16,000."""
    return int(min(16000, max(100, 3 * size_gb)))


def gp2_to_gp3_net_savings(size_gb: float, gb_delta_per_gb: float, gp3_iops_rate: float) -> float:
    """Net monthly gp2→gp3 saving, accounting for IOPS parity on large volumes.

    The storage-rate delta overstates the saving for volumes whose gp2 baseline
    IOPS exceed gp3's free 3,000: matching that performance on gp3 requires
    provisioning ``baseline − 3000`` IOPS, which is netted out here. For volumes
    ≤ 1,000 GB (gp2 baseline ≤ 3,000) gp3's free tier already matches or exceeds,
    so the full storage delta applies. (Throughput parity is not modelled — gp2's
    size-derived throughput curve is workload-dependent — and is noted as a caveat.)
    """
    storage_savings = max(size_gb, 0.0) * max(gb_delta_per_gb, 0.0)
    provisioned_iops = max(0, gp2_baseline_iops(size_gb) - 3000)
    iops_cost = provisioned_iops * max(gp3_iops_rate, 0.0)
    return max(storage_savings - iops_cost, 0.0)


def recommend_iops_from_usage(
    provisioned_iops: int,
    observed_peak_iops: float,
    *,
    baseline: int = 0,
    headroom: float = 1.3,
) -> int | None:
    """Usage-based recommended provisioned IOPS, or None when no reduction is justified.

    ``recommended = max(baseline, ceil(observed_peak * headroom))``. Returns
    ``None`` when that target is >= the provisioned IOPS (i.e. the volume is
    not over-provisioned given observed demand), so a saving is only ever
    emitted with real CloudWatch evidence behind it.

    Args:
        provisioned_iops: Currently provisioned IOPS on the volume.
        observed_peak_iops: Peak observed IOPS from CloudWatch over the window.
        baseline: Floor for the recommendation (e.g. gp3 free 3000, io1/io2 min 100).
        headroom: Safety multiplier applied to the observed peak (default 30%).
    """
    if provisioned_iops <= 0 or observed_peak_iops < 0:
        return None
    target = max(baseline, math.ceil(observed_peak_iops * headroom))
    if target >= provisioned_iops:
        return None
    return target
