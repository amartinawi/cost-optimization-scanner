"""Pure helpers for the commitment_analysis adapter's Fargate Savings Plan view.

Dependency-free (no boto3) so the per-leg Savings Plan math can be unit-tested
directly. The model isolates Fargate's share of a Compute Savings Plan — which
AWS's aggregate purchase recommendation does not break out — and reconciles the
commitment against the rightsized Fargate baseline.
"""

from __future__ import annotations

from typing import Any

HOURS_PER_MONTH: int = 730

# Term/payment cells surfaced in the full 2x3 matrix.
SP_TERMS: tuple[str, ...] = ("1yr", "3yr")
SP_PAYMENTS: tuple[str, ...] = ("No Upfront", "Partial Upfront", "All Upfront")

DEFAULT_COVERAGE_RATIO: float = 0.70


def fargate_sp_cell(
    legs: dict[str, dict[str, float]],
    cell_rates: dict[str, float],
    *,
    rightsizing_monthly: float = 0.0,
    coverage_ratio: float = DEFAULT_COVERAGE_RATIO,
) -> dict[str, float]:
    """Model one Savings Plan (term, payment) cell for the Fargate baseline.

    Args:
        legs: ``{usage_type: {"od": monthly_on_demand_$, "qty": monthly_hours}}``
            for each SP-eligible Fargate usage type (vCPU/GB, x86/ARM/Windows).
        cell_rates: ``{usage_type: sp_rate_$_per_hour}`` for this cell.
        rightsizing_monthly: $/mo of Fargate rightsizing already identified in the
            Containers tab — applied BEFORE the commitment so the SP is sized
            against the reduced baseline (no double counting).
        coverage_ratio: fraction of the steady baseline worth committing (you
            commit to the always-on floor, not 100% of usage).

    Returns:
        Dict with discount_pct, eligible_od, rightsized_od, sp_monthly_cost,
        ceiling_saving (100% coverage of the rightsized baseline), and
        recommended_saving (ceiling x coverage_ratio).
    """
    eligible_od = sum(v.get("od", 0.0) for v in legs.values())
    sp_cost_full = sum(
        legs[ut].get("qty", 0.0) * rate for ut, rate in cell_rates.items() if ut in legs
    )
    discount = (eligible_od - sp_cost_full) / eligible_od if eligible_od > 0 else 0.0

    rightsized_od = max(0.0, eligible_od - max(0.0, rightsizing_monthly))
    scale = rightsized_od / eligible_od if eligible_od > 0 else 0.0
    ceiling_saving = (eligible_od - sp_cost_full) * scale  # = rightsized_od * discount
    recommended_saving = ceiling_saving * max(0.0, min(1.0, coverage_ratio))

    return {
        "discount_pct": round(discount * 100, 1),
        "eligible_od": round(eligible_od, 2),
        "rightsized_od": round(rightsized_od, 2),
        "sp_monthly_cost": round(sp_cost_full * scale, 2),
        "ceiling_saving": round(ceiling_saving, 2),
        "recommended_saving": round(recommended_saving, 2),
    }


def fargate_sp_analysis(
    legs: dict[str, dict[str, float]],
    rate_matrix: dict[tuple[str, str], dict[str, float]],
    *,
    rightsizing_monthly: float = 0.0,
    coverage_ratio: float = DEFAULT_COVERAGE_RATIO,
) -> dict[str, Any]:
    """Build the full Fargate Savings Plan analysis across every (term, payment) cell.

    Returns a dict with the baseline figures plus a ``cells`` list (one per
    term/payment), sorted by recommended_saving descending. Returns an empty
    ``cells`` list when there is no eligible Fargate spend.
    """
    eligible_od = round(sum(v.get("od", 0.0) for v in legs.values()), 2)
    rightsized_od = round(max(0.0, eligible_od - max(0.0, rightsizing_monthly)), 2)

    cells: list[dict[str, Any]] = []
    for (term, payment), cell_rates in rate_matrix.items():
        cell = fargate_sp_cell(
            legs, cell_rates, rightsizing_monthly=rightsizing_monthly, coverage_ratio=coverage_ratio
        )
        cells.append({"term": term, "payment": payment, **cell})
    cells.sort(key=lambda c: c["recommended_saving"], reverse=True)

    return {
        "eligible_od": eligible_od,
        "rightsized_od": rightsized_od,
        "rightsizing_monthly": round(max(0.0, rightsizing_monthly), 2),
        "coverage_ratio": round(coverage_ratio, 4),
        "cells": cells,
    }
