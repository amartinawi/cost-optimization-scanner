"""Corroborate upper-bound savings against actually-billed spend (lesson C8).

Several levers price a deletion candidate from a resource's *provisioned* or
*full* size — RDS snapshot ``AllocatedStorage``, an AMI's backing-snapshot
``FullSnapshotSizeInBytes``. AWS bills less than that: backup bytes sit below
provisioned size, and EBS snapshots bill only the unique changed blocks across a
chain. The figure is therefore an **upper bound**, and counting it unconditionally
overstates the saving.

The rule this module enforces:

    An upper bound is counted only up to what Cost Explorer says is actually
    billed. When the billed figure cannot be read, the bound is **demoted to a
    $0 advisory** — never counted.

Failing open (counting an uncorroborated bound when the evidence read fails) is
the specific defect lesson **C8** exists to prevent: a missing ``ce:GetCostAndUsage``
permission silently *inflated* the RDS snapshot tab by $719.58/mo on a live
account. Removing evidence must never increase counted savings.
"""

from __future__ import annotations

from typing import Any


def reconcile_against_billed(
    recs: list[dict[str, Any]],
    billed: float | None,
    *,
    pool_label: str,
    savings_key: str = "EstimatedMonthlySavings",
    grant_hint: str = "grant ce:GetCostAndUsage to quantify",
) -> tuple[list[dict[str, Any]], float]:
    """Cap ``recs``' upper-bound savings at ``billed``; demote when unsubstantiated.

    Args:
        recs: Recommendations carrying a positive numeric upper-bound saving.
        billed: Actual billed $/month for the pool these recs draw from. ``None``
            means the read failed (unsubstantiated). ``<= 0`` means Cost Explorer
            answered and nothing is billed — either way nothing is realizable.
        pool_label: Human name of the billed pool, for the audit trail.
        savings_key: The numeric savings field to cap.
        grant_hint: What the operator can do to make the bound quantifiable.

    Returns:
        ``(new_recs, counted_total)``. Recs are copies — the input is not mutated.
        Demoted recs carry ``Counted=False``, a zeroed ``savings_key``, the bound
        preserved as ``PotentialMonthlySavings``, and an advisory
        ``EstimatedSavings`` string, so a numeric-summing consumer cannot pick up
        a demoted advisory.
    """
    def _sav(rec: dict[str, Any]) -> float:
        try:
            return float(rec.get(savings_key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    upper = sum(_sav(r) for r in recs)
    pool = float(billed) if billed is not None else 0.0
    substantiated = billed is not None and pool > 0
    factor = 1.0
    if substantiated and upper > 0 and pool < upper:
        factor = pool / upper

    out: list[dict[str, Any]] = []
    counted = 0.0
    for rec in recs:
        value = _sav(rec)
        if value <= 0:  # already advisory / size-unknown — leave untouched
            out.append(rec)
            continue
        new = dict(rec)
        if not substantiated:
            new["Counted"] = False
            new[savings_key] = 0.0
            new["PotentialMonthlySavings"] = round(value, 2)
            new["EstimatedSavings"] = f"up to ${value:.2f}/month — advisory ({grant_hint})"
            new["ReconciliationBasis"] = (
                f"no Cost Explorer actual for {pool_label} — upper bound retained as advisory (not counted)"
                if billed is None
                else f"{pool_label} bills $0.00/mo — nothing realizable"
            )
        else:
            capped = round(value * factor, 2)
            new[savings_key] = capped
            new["ActualBilledPool"] = round(pool, 2)
            if factor < 1.0:
                new["Reconciled"] = True
                new["ReconciliationFactor"] = round(factor, 4)
                new["UpperBoundBeforeReconciliation"] = round(value, 2)
                new["EstimatedSavings"] = (
                    f"${capped:.2f}/month (reconciled to actual billed {pool_label} via Cost Explorer)"
                )
            else:
                new["ReconciliationBasis"] = f"not capped — upper bound <= actual billed {pool_label}"
                new["EstimatedSavings"] = f"${capped:.2f}/month"
            counted += capped
        out.append(new)
    return out, round(counted, 2)
