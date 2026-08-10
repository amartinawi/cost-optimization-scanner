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
    SP_MATRIX_PROBE_TYPES,
    SP_PLAN_TYPES,
    TERMS,
    build_ri_type_cards,
    build_sp_cards,
    merge_coh_concurrence,
    projected_savings,
    ri_cells_from_response,
    sp_cell_from_response,
    sp_fanout_cells,
)

# durationSeconds -> the CE TermInYears enum member. AWS reports plan length in
# seconds; anything outside this map is a term CE cannot be asked about.
_DURATION_TERMS: dict[int, str] = {31536000: "ONE_YEAR", 94608000: "THREE_YEARS"}
# The offerings API spells payment options in prose; CE uses the enum.
_PAYMENT_APIS: dict[str, str] = {
    "No Upfront": "NO_UPFRONT",
    "Partial Upfront": "PARTIAL_UPFRONT",
    "All Upfront": "ALL_UPFRONT",
}
_TERM_LABELS: dict[str, str] = dict(TERMS)
_PAYMENT_LABELS: dict[str, str] = dict(PAYMENTS)
# Only Database is probed and it currently has ONE offering; the cap exists to
# bound a misbehaving pager, not to sample.
_OFFERING_PAGE_CAP = 20


def fetch_sp_offering_matrix(ctx: Any) -> dict[str, frozenset[tuple[str, str]]]:
    """Resolve which term/payment combos AWS actually sells, per SP type.

    Only ``SP_MATRIX_PROBE_TYPES`` is probed (see that constant for why), via
    ``savingsplans:DescribeSavingsPlansOfferings`` — a FREE call, unlike the
    $0.01 CE cells it saves.

    Fail-open by omission: a type that could not be resolved is left out of the
    result, and ``sp_fanout_cells`` then requests its full matrix. Losing the
    probe must never lose a recommendation, which is the LS-7 defect itself.
    """
    offered: dict[str, frozenset[tuple[str, str]]] = {}
    for sp_type in sorted(SP_MATRIX_PROBE_TYPES):
        plan_type = SP_PLAN_TYPES.get(sp_type)
        if not plan_type:
            continue
        combos: set[tuple[str, str]] = set()
        try:
            client = ctx.client("savingsplans")
            token: str | None = None
            # Paged, because a partial answer would SHRINK the matrix and drop a
            # CE cell — losing a real card silently. The page cap bounds a
            # misbehaving pager; the token guard matches the one in
            # services/commitment_coverage.py.
            for _page in range(_OFFERING_PAGE_CAP):
                kwargs: dict[str, Any] = {"planTypes": [plan_type], "maxResults": 1000}
                if token:
                    kwargs["nextToken"] = token
                resp = client.describe_savings_plans_offerings(**kwargs)
                for offering in resp.get("searchResults", []) or []:
                    if not isinstance(offering, dict):
                        continue
                    term = _DURATION_TERMS.get(offering.get("durationSeconds") or 0)
                    payment = _PAYMENT_APIS.get(str(offering.get("paymentOption") or ""))
                    if term and payment:
                        combos.add((term, payment))
                next_token = resp.get("nextToken")
                if not isinstance(next_token, str) or not next_token or next_token == token:
                    break
                token = next_token
        except Exception as exc:  # noqa: BLE001 — fail-open to the full matrix
            _warn(ctx, f"Could not read {plan_type} Savings Plan offerings "
                       f"({sp_type} purchase matrix not narrowed): {exc}")
            continue
        offered[sp_type] = frozenset(combos)
    return offered


def _warn(ctx: Any, message: str) -> None:
    warn = getattr(ctx, "warn", None)
    if callable(warn):
        warn(message, service="commitment_analysis")


def _grid_note(combos: frozenset[tuple[str, str]]) -> str:
    """Human note for a plan type whose scenario grid is mostly blank.

    The renderer always draws 2 terms x 3 payments, so without this the five
    empty Database cells read as missing data rather than "AWS does not sell
    this". Built from the probed combos, so it stays true if AWS adds a term.
    """
    if not combos:
        return "AWS currently offers no purchasable term for this plan."
    pairs = sorted(
        (_TERM_LABELS.get(t, t), _PAYMENT_LABELS.get(p, p)) for t, p in combos
    )
    return ("AWS offers this plan as "
            + " and ".join(f"{term} / {payment}" for term, payment in pairs)
            + " only; the remaining cells are not purchasable.")


def fetch_purchase_cards(
    ctx: Any, ce: Any, route_error: Callable[[Any, str, Exception], None]
) -> tuple[list[dict[str, Any]], float, str, list[dict[str, Any]]]:
    """Fan out the full CE purchase matrix and build per-type scenario cards.

    Every cell is one independent CE call; a denied/throttled cell degrades
    only that cell (via ``route_error``). RI cells fan out across
    ``RI_SERVICES`` (6) x ``TERMS`` (2) x ``PAYMENTS`` (3). SP cells fan out
    across ``SP_TYPES`` (4) x the same matrix, MINUS the combos AWS does not
    sell for a given plan type — resolved live by ``fetch_sp_offering_matrix``,
    which is why a Database SP costs 1 CE call rather than 6.

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

    # Which SP term/payment cells are worth a $0.01 CE call (LS-7).
    offered = fetch_sp_offering_matrix(ctx)

    sp_cells: list[dict[str, Any]] = []
    for sp_type, term_api, term_label, payment_api, payment_label in sp_fanout_cells(offered):
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
    for card in sp_cards:
        combos = offered.get(card.get("sp_type", ""))
        if combos is not None and len(combos) < len(TERMS) * len(PAYMENTS):
            card["scenario_grid_note"] = _grid_note(combos)
    projected, basis = projected_savings(ri_cards, sp_cards)

    coh_recs = [r for r in (getattr(ctx, "cost_hub_splits", {}) or {}).get("commitment_analysis", [])
                if isinstance(r, dict)]
    cards, matched_indices = merge_coh_concurrence(ri_cards + sp_cards, coh_recs)
    matched = set(matched_indices)
    unmatched_coh = [r for i, r in enumerate(coh_recs) if i not in matched]
    return cards, projected, basis, unmatched_coh
