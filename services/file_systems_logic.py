"""Pure decision logic for the EFS/FSx (file_systems) adapter — no AWS, no ScanContext.

Every COUNTED file-system saving must be a real price delta or a measured-storage
number, not a blanket factor. These helpers compute those numbers and de-duplicate
findings so one file system's savings is never stacked across checks.
"""

from __future__ import annotations

from typing import Any

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


# FS-2 — `efs_lifecycle_net_savings` and its `EfsLifecycleEstimate` are gone.
# They computed
#     cold_gb = standard_gb - monthly_access_gb
# which subtracts a 30-day I/O FLOW from a byte STOCK. That has no valid
# interpretation, and its error is not bounded in the safe direction: a partial
# read of a large file resets that whole file's lifecycle clock while
# contributing only the bytes read to the metric, so the difference can exceed
# the true cold set without limit.
#
# The lever it fed is now an advisory (see services/efs_fsx.py), so what remains
# here is an upper BOUND on that advisory, plus the break-even file size that
# explains why even a correct cold figure would not settle the question.

# EFS IA and Archive bill a MINIMUM of 128 KiB per file. Below that mean file
# size the transition LOSES money, and EFS publishes neither a file count nor a
# size distribution — so the SIGN of this lever is unknown, not just its
# magnitude.
EFS_IA_MIN_BILLED_FILE_KIB: float = 128.0


def efs_lifecycle_ceiling(standard_gb: float, standard_rate: float, ia_rate: float) -> float:
    """Upper bound on an IA-lifecycle saving: every Standard byte cold, every file large.

    Deliberately a CEILING, not an estimate. It is rendered as "up to $X if
    realizable" on a $0 advisory and is never counted.
    """
    return max(standard_gb, 0.0) * max(standard_rate - ia_rate, 0.0)


def efs_ia_breakeven_file_kib(standard_rate: float, ia_rate: float) -> float:
    """Mean file size below which moving to IA COSTS money.

    A file smaller than the 128 KiB minimum still bills 128 KiB in IA, so the
    per-file cost ratio is `128 / size_kib`. Break-even is where the IA bill for
    a padded file equals the Standard bill for the real one.
    """
    if standard_rate <= 0 or ia_rate <= 0:
        return 0.0
    return EFS_IA_MIN_BILLED_FILE_KIB * (ia_rate / standard_rate)


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
