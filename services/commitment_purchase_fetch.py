"""CE purchase-recommendation fan-out for the Commitment Analysis adapter.

Fetch layer between the boto3 Cost Explorer client and the pure card math in
``services/commitment_scenarios.py``: fans out the full RI/SP purchase matrix
(one independent call per cell), normalizes CE's region LOCATION NAMES to
region codes, builds the per-type scenario cards, computes the non-overlap
projected figure, and merges Cost-Optimization-Hub concurrence.

Split out of ``services/adapters/commitment_analysis.py`` (2026-08-09) purely
for file-size discipline — behavior is byte-identical to the adapter method it
replaces, plus the J-1 region-normalization fix from the Jarir-M2 live audit.
"""

from __future__ import annotations

from typing import Any, Callable

from core.pricing_engine import REGION_DISPLAY_NAMES
from services.commitment_scenarios import (
    PAYMENTS,
    RI_SERVICES,
    SP_TYPES,
    TERMS,
    build_ri_type_cards,
    build_sp_cards,
    merge_coh_concurrence,
    projected_savings,
    ri_cells_from_response,
    sp_cell_from_response,
)


def fetch_purchase_cards(
    ctx: Any, ce: Any, route_error: Callable[[Any, str, Exception], None]
) -> tuple[list[dict[str, Any]], float, str, list[dict[str, Any]]]:
    """Fan out the full CE purchase matrix and build per-type scenario cards.

    Every cell is one independent CE call; a denied/throttled cell degrades
    only that cell (via ``route_error``). RI cells fan out across
    ``RI_SERVICES`` (6) x ``TERMS`` (2) x ``PAYMENTS`` (3); SP cells across
    ``SP_TYPES`` (3) x the same term/payment matrix.

    A CoH RI/SP rec routed here (``ctx.cost_hub_splits["commitment_analysis"]``)
    that concurs with a built card merges into that card's
    ``coh_concurs_monthly`` figure (``merge_coh_concurrence``) instead of
    rendering twice; the unmatched remainder returns for the caller's
    standalone CoH source.

    Args:
        ctx: Scan context (region, commitment_coverage, cost_hub_splits).
        ce: Cost Explorer boto3 client.
        route_error: Callable(ctx, action, exc) classifying per-cell failures.

    Returns:
        Tuple of ``(cards, projected_monthly, basis, unmatched_coh_recs)``.
    """
    ri_cells: list[dict[str, Any]] = []
    for service_api, service_label in RI_SERVICES:
        for term_api, term_label in TERMS:
            for payment_api, payment_label in PAYMENTS:
                try:
                    resp = ce.get_reservation_purchase_recommendation(
                        Service=service_api,
                        LookbackPeriodInDays="THIRTY_DAYS",
                        TermInYears=term_api,
                        PaymentOption=payment_api,
                    )
                except Exception as e:  # noqa: BLE001 — cell-isolated by design
                    route_error(
                        ctx,
                        f"ce:GetReservationPurchaseRecommendation[{service_label}/({term_label}, {payment_label})]",
                        e,
                    )
                    continue
                ri_cells.extend(ri_cells_from_response(service_label, term_label, payment_label, resp))

    sp_cells: list[dict[str, Any]] = []
    for sp_type in SP_TYPES:
        for term_api, term_label in TERMS:
            for payment_api, payment_label in PAYMENTS:
                try:
                    resp = ce.get_savings_plans_purchase_recommendation(
                        SavingsPlansType=sp_type,
                        TermInYears=term_api,
                        PaymentOption=payment_api,
                        LookbackPeriodInDays="THIRTY_DAYS",
                    )
                except Exception as e:  # noqa: BLE001 — cell-isolated by design
                    route_error(
                        ctx,
                        f"ce:GetSavingsPlansPurchaseRecommendation[{sp_type}/({term_label}, {payment_label})]",
                        e,
                    )
                    continue
                cell = sp_cell_from_response(sp_type, term_label, payment_label, resp)
                if cell:
                    sp_cells.append(cell)

    # CE details carry LOCATION NAMES ("EU (Frankfurt)"), but the coverage
    # join, scan-region sort, and region tags key on region codes — without
    # this the coverage context is dead on every real account (Jarir-M2
    # live audit J-1: $13,343/mo uncovered, zero coverage lines rendered).
    location_to_region = {v: k for k, v in REGION_DISPLAY_NAMES.items()}
    for cell in ri_cells:
        cell["region"] = location_to_region.get(cell["region"], cell["region"])

    coverage = getattr(ctx, "commitment_coverage", None)
    uncovered = dict(coverage.uncovered_on_demand) if coverage is not None else {}
    ri_cards = build_ri_type_cards(ri_cells, uncovered, getattr(ctx, "region", ""))
    sp_cards = build_sp_cards(sp_cells)
    projected, basis = projected_savings(ri_cards, sp_cards)

    coh_recs = [r for r in (getattr(ctx, "cost_hub_splits", {}) or {}).get("commitment_analysis", [])
                if isinstance(r, dict)]
    cards, matched_indices = merge_coh_concurrence(ri_cards + sp_cards, coh_recs)
    matched = set(matched_indices)
    unmatched_coh = [r for i, r in enumerate(coh_recs) if i not in matched]
    return cards, projected, basis, unmatched_coh
