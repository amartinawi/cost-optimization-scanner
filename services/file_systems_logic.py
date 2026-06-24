"""Pure decision logic for the EFS/FSx (file_systems) adapter — no AWS, no ScanContext.

Every COUNTED file-system saving must be a real price delta or a measured-storage
number, not a blanket factor. These helpers compute those numbers and de-duplicate
findings so one file system's savings is never stacked across checks.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# Conservative, LABELED assumption used ONLY for the advisory (no-evidence)
# indicative figure: the share of an un-tiered EFS file system's Standard data
# that is infrequently accessed and would transition to IA once a lifecycle
# policy is enabled. When CloudWatch access metrics ARE available the counted
# path replaces this guess with measured cold bytes (see efs_lifecycle_net_savings).
EFS_IA_TRANSITION_FRACTION: float = 0.5

# Lookback window for the EFS access-metric read (matches the common
# "Transition to IA after 30 days" lifecycle rule).
EFS_METRIC_WINDOW_DAYS: int = 30

# Below these sizes the optimization is not worth surfacing as a dollar finding.
EFS_MIN_LIFECYCLE_GB: float = 1.0
EFS_ONE_ZONE_MIN_GB: float = 1.0
# HDD has a 2,000 GiB minimum and only pays off at scale; gate SSD→HDD on this.
FSX_SSD_TO_HDD_MIN_GB: int = 2000


def fs_id(rec: dict[str, Any]) -> str:
    """File-system / cache id used to de-duplicate findings."""
    return str(rec.get("FileSystemId") or rec.get("FileCacheId") or "")


def efs_lifecycle_savings(
    standard_gb: float,
    standard_rate: float,
    ia_rate: float,
    fraction: float = EFS_IA_TRANSITION_FRACTION,
) -> float:
    """Monthly saving from enabling EFS IA lifecycle on un-tiered Standard data.

    ``standard_gb`` is the MEASURED Standard-class size; the rate delta is real;
    ``fraction`` is the labeled access-pattern assumption.
    """
    delta = max(standard_rate - ia_rate, 0.0)
    return max(standard_gb, 0.0) * delta * max(fraction, 0.0)


class EfsLifecycleEstimate(NamedTuple):
    """Evidence-based EFS IA-lifecycle saving breakdown (all monthly $)."""

    cold_gb: float          # Standard bytes not accessed in the metric window
    gross_savings: float    # cold_gb x (Standard - IA) rate delta
    access_charge: float    # monthly accessed bytes x IA per-GB access rate
    net_savings: float      # gross_savings - access_charge


def efs_lifecycle_net_savings(
    standard_gb: float,
    monthly_access_gb: float,
    standard_rate: float,
    ia_rate: float,
    ia_access_rate: float,
) -> EfsLifecycleEstimate:
    """NET monthly saving from enabling EFS IA lifecycle, from measured access.

    ``cold_gb`` is the measured Standard bytes MINUS the bytes actually read or
    written over the metric window (anything touched in the window is treated as
    hot and excluded — conservative). The IA per-GB access charge on the accessed
    bytes is netted out, so the result is a defensible NET, not a gross figure.
    """
    cold_gb = max(standard_gb - max(monthly_access_gb, 0.0), 0.0)
    gross = cold_gb * max(standard_rate - ia_rate, 0.0)
    access_charge = max(monthly_access_gb, 0.0) * max(ia_access_rate, 0.0)
    return EfsLifecycleEstimate(cold_gb, gross, access_charge, gross - access_charge)


def efs_one_zone_savings(total_gb: float, regional_rate: float, one_zone_rate: float) -> float:
    """Monthly saving from migrating a Regional EFS to One Zone (deterministic delta)."""
    return max(total_gb, 0.0) * max(regional_rate - one_zone_rate, 0.0)


def efs_idle_savings(total_gb: float, storage_rate: float) -> float:
    """Monthly saving from deleting an idle EFS (100% of its storage cost)."""
    return max(total_gb, 0.0) * max(storage_rate, 0.0)


def fsx_ssd_to_hdd_savings(capacity_gb: float, ssd_rate: float, hdd_rate: float) -> float:
    """Monthly saving from switching FSx SSD storage to HDD (deterministic delta)."""
    return max(capacity_gb, 0.0) * max(ssd_rate - hdd_rate, 0.0)


def dedupe_counted(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep at most one counted finding per file-system id — highest saving wins.

    Prevents stacking (idle + lifecycle + one-zone on the same EFS, or two FSx
    checks on the same volume) beyond 100% of the resource's cost. Findings carry
    their numeric saving under the ``_savings`` key.
    """
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for f in findings:
        key = fs_id(f) or f"_anon_{id(f)}"
        existing = best.get(key)
        if existing is None:
            best[key] = f
            order.append(key)
        elif f.get("_savings", 0.0) > existing.get("_savings", 0.0):
            best[key] = f
    return [best[k] for k in order]
