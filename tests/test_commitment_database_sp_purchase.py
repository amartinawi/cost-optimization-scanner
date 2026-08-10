"""LS-7 — the Database Savings Plan must reach the purchase matrix.

`SP_TYPES` was ``("COMPUTE_SP", "EC2_INSTANCE_SP", "SAGEMAKER_SP")``, so the
plan type never reached ``ce:GetSavingsPlansPurchaseRecommendation``. With the
fabricated coverage-gap card deleted (LS-2), an account with large uncovered
RDS / Aurora / ElastiCache / OpenSearch / DynamoDB spend got **complete
silence**: no coverage card and no purchase card either.

Everything here is pinned against a LIVE probe, not documentation (lesson C14):

* ``ce:GetSavingsPlansPurchaseRecommendation``'s ``SavingsPlansType`` enum is
  ``[COMPUTE_SP, EC2_INSTANCE_SP, SAGEMAKER_SP, DATABASE_SP]`` (botocore
  1.43.47 service model, 2026-08-10) — CE does accept the new type.
* ``savingsplans:DescribeSavingsPlansOfferings`` (2026-08-10) returns, per plan
  type, the combos actually purchasable::

      Compute / EC2Instance / SageMaker : 1yr+3yr x No/Partial/All Upfront (6)
      Database                          : 1yr No Upfront ONLY            (1)

  CE does **not** reject the 5 impossible Database combos — it accepts them and
  returns an empty recommendation. So an unrestricted fan-out would not merely
  waste 5 x $0.01 per scan; if CE ever did answer for a term AWS does not sell,
  the report would render a purchase card for an unbuyable plan. The matrix is
  therefore derived live and fail-open (a failed probe falls back to the full
  matrix, which yields a card rather than silently dropping the plan type).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services.commitment_scenarios import (
    PAYMENTS,
    SP_MATRIX_PROBE_TYPES,
    SP_PLAN_TYPES,
    SP_TYPES,
    TERMS,
    projected_savings,
    sp_fanout_cells,
)

_DB_ONLY = {"DATABASE_SP": frozenset({("ONE_YEAR", "NO_UPFRONT")})}


# --------------------------------------------------------------------------- #
# The type reaches the matrix at all
# --------------------------------------------------------------------------- #
def test_sp_types_carries_the_database_plan() -> None:
    assert SP_TYPES == ("COMPUTE_SP", "EC2_INSTANCE_SP", "SAGEMAKER_SP", "DATABASE_SP")


def test_plan_type_map_covers_every_sp_type() -> None:
    """The offerings probe keys on savingsplans' own planType spelling
    (Compute/EC2Instance/SageMaker/Database), not CE's."""
    assert set(SP_PLAN_TYPES) == set(SP_TYPES)
    assert SP_PLAN_TYPES["DATABASE_SP"] == "Database"
    assert SP_PLAN_TYPES["EC2_INSTANCE_SP"] == "EC2Instance"


# --------------------------------------------------------------------------- #
# Fan-out restriction
# --------------------------------------------------------------------------- #
def test_unknown_matrix_fans_out_every_combo() -> None:
    """Fail-open: with no probe result, every type gets the full 2x3 matrix, so
    a probe failure costs money but never loses a recommendation."""
    cells = sp_fanout_cells({})
    assert len(cells) == len(SP_TYPES) * len(TERMS) * len(PAYMENTS)
    assert sum(1 for c in cells if c[0] == "DATABASE_SP") == 6


def test_probed_matrix_restricts_only_the_probed_type() -> None:
    cells = sp_fanout_cells(_DB_ONLY)
    db = [c for c in cells if c[0] == "DATABASE_SP"]
    assert len(db) == 1
    assert db[0] == ("DATABASE_SP", "ONE_YEAR", "1yr", "NO_UPFRONT", "No Upfront")
    # The three unrestricted types keep all six cells each.
    for sp_type in ("COMPUTE_SP", "EC2_INSTANCE_SP", "SAGEMAKER_SP"):
        assert sum(1 for c in cells if c[0] == sp_type) == 6


def test_an_empty_offered_set_is_not_read_as_unknown() -> None:
    """A type AWS currently sells nothing for must fan out to zero cells, not
    fall back to six — otherwise the fail-open path hides a real withdrawal."""
    cells = sp_fanout_cells({"DATABASE_SP": frozenset()})
    assert [c for c in cells if c[0] == "DATABASE_SP"] == []


def test_only_the_database_plan_is_probed() -> None:
    """The other three are live-verified full-matrix and already fan out fully
    today; probing them would page through ~19k EC2Instance offerings for an
    answer that cannot change their behavior."""
    assert set(SP_MATRIX_PROBE_TYPES) == {"DATABASE_SP"}


# --------------------------------------------------------------------------- #
# Non-overlap: a Database SP discounts the same spend as the database RIs
# --------------------------------------------------------------------------- #
def _sp(sp_type: str, savings: float) -> dict[str, Any]:
    return {"card_kind": "sp_commitment", "sp_type": sp_type, "monthly_savings": savings}


def _ri(service: str, savings: float) -> dict[str, Any]:
    return {"card_kind": "ri_type", "service": service, "monthly_savings": savings}


def test_database_sp_and_database_ris_are_maxed_not_summed() -> None:
    """Both instruments discount the SAME on-demand spend. Summing them would
    double-count — the cardinal sin."""
    total, basis = projected_savings(
        [_ri("RDS", 300.0), _ri("ElastiCache", 100.0)], [_sp("DATABASE_SP", 500.0)]
    )
    assert total == 500.0          # not 900.0
    assert "Database SP" in basis


def test_database_ris_win_when_they_beat_the_plan() -> None:
    total, basis = projected_savings(
        [_ri("RDS", 700.0), _ri("DynamoDB", 100.0)], [_sp("DATABASE_SP", 500.0)]
    )
    assert total == 800.0
    assert "Database SP" not in basis
    assert "RDS" in basis


def test_redshift_ri_adds_on_top_of_a_database_sp() -> None:
    """Redshift is absent from the plan's productTypes, so its RI overlaps
    nothing the plan covers and must still sum. Folding it into the max() would
    under-count a real saving."""
    total, _ = projected_savings(
        [_ri("Redshift", 250.0), _ri("RDS", 100.0)], [_sp("DATABASE_SP", 500.0)]
    )
    assert total == 750.0          # max(500, 100) + 250


def test_database_group_is_independent_of_the_compute_group() -> None:
    total, basis = projected_savings(
        [_ri("EC2", 100.0)], [_sp("COMPUTE_SP", 400.0), _sp("DATABASE_SP", 200.0)]
    )
    assert total == 600.0          # compute group 400 + database group 200
    assert "Compute SP path" in basis and "Database SP" in basis


def test_no_database_instruments_leaves_the_old_arithmetic_intact() -> None:
    """Regression guard: accounts with no Database SP must project exactly as
    they did before this change."""
    total, basis = projected_savings(
        [_ri("RDS", 300.0), _ri("Redshift", 50.0), _ri("OpenSearch", 20.0)],
        [_sp("COMPUTE_SP", 900.0), _sp("SAGEMAKER_SP", 10.0)],
    )
    assert total == 1280.0
    assert basis.startswith("Compute SP path")


# --------------------------------------------------------------------------- #
# Fetch layer
# --------------------------------------------------------------------------- #
class _Ce:
    def __init__(self) -> None:
        self.sp_calls: list[tuple[str, str, str]] = []

    def get_reservation_purchase_recommendation(self, **kw: Any) -> dict[str, Any]:
        return {}

    def get_savings_plans_purchase_recommendation(self, **kw: Any) -> dict[str, Any]:
        self.sp_calls.append((kw["SavingsPlansType"], kw["TermInYears"], kw["PaymentOption"]))
        if kw["SavingsPlansType"] != "DATABASE_SP":
            return {}
        return {"SavingsPlansPurchaseRecommendation": {
            "SavingsPlansPurchaseRecommendationSummary": {
                "EstimatedMonthlySavingsAmount": "412.90",
                "HourlyCommitmentToPurchase": "1.25",
                "EstimatedSavingsPercentage": "18.4",
                "EstimatedOnDemandCostWithCurrentCommitment": "2244.00",
            },
            "SavingsPlansPurchaseRecommendationDetails": [{"UpfrontCost": "0"}],
        }}


class _Offerings:
    """savingsplans stub speaking the live DescribeSavingsPlansOfferings shape."""

    def __init__(self, results: list[dict[str, Any]] | None = None, boom: bool = False) -> None:
        self._results = results if results is not None else [
            {"planType": "Database", "durationSeconds": 31536000, "paymentOption": "No Upfront"}
        ]
        self._boom = boom
        self.calls: list[Any] = []

    def describe_savings_plans_offerings(self, **kw: Any) -> dict[str, Any]:
        if self._boom:
            raise Exception("AccessDeniedException")
        self.calls.append(kw.get("planTypes"))
        return {"searchResults": self._results}


def _ctx(offerings: _Offerings) -> SimpleNamespace:
    ctx = SimpleNamespace(region="us-east-1", cost_hub_splits={}, commitment_coverage=None)
    ctx.client = lambda name, region=None: offerings
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None
    return ctx


def _noop_route(ctx: Any, action: str, exc: Exception) -> None:
    return None


def test_fetch_issues_one_ce_call_for_the_restricted_plan() -> None:
    """Each CE call costs $0.01. The live matrix says only 1yr/No-Upfront is
    purchasable, so 5 of the 6 cells must never be requested."""
    from services.commitment_purchase_fetch import fetch_purchase_cards

    ce, offerings = _Ce(), _Offerings()
    cards, _projected, _basis, _unmatched = fetch_purchase_cards(_ctx(offerings), ce, _noop_route)

    db_calls = [c for c in ce.sp_calls if c[0] == "DATABASE_SP"]
    assert db_calls == [("DATABASE_SP", "ONE_YEAR", "NO_UPFRONT")]
    assert offerings.calls == [["Database"]]          # probed once, by plan type

    db_cards = [c for c in cards if c.get("sp_type") == "DATABASE_SP"]
    assert len(db_cards) == 1
    assert db_cards[0]["monthly_savings"] == 412.90
    assert db_cards[0]["Counted"] is False            # a purchase is a projection


def test_fetch_falls_open_to_the_full_matrix_when_the_probe_fails() -> None:
    """A denied probe must not silently drop the plan type — that would re-open
    the LS-7 silence this fix exists to close."""
    from services.commitment_purchase_fetch import fetch_purchase_cards

    ce, offerings = _Ce(), _Offerings(boom=True)
    fetch_purchase_cards(_ctx(offerings), ce, _noop_route)
    assert len([c for c in ce.sp_calls if c[0] == "DATABASE_SP"]) == 6


def test_offering_probe_reads_every_page() -> None:
    """A partial answer would SHRINK the matrix and skip a CE cell, dropping a
    real card silently. Under-counting is the safe direction, but not silently."""
    from services.commitment_purchase_fetch import fetch_sp_offering_matrix

    pages = [
        {"searchResults": [{"planType": "Database", "durationSeconds": 31536000,
                            "paymentOption": "No Upfront"}], "nextToken": "p2"},
        {"searchResults": [{"planType": "Database", "durationSeconds": 94608000,
                            "paymentOption": "All Upfront"}]},
    ]

    class _Paged:
        def __init__(self) -> None:
            self.n = 0

        def describe_savings_plans_offerings(self, **kw: Any) -> dict[str, Any]:
            page = pages[self.n]
            self.n += 1
            return page

    offered = fetch_sp_offering_matrix(_ctx(_Paged()))  # type: ignore[arg-type]
    assert offered["DATABASE_SP"] == frozenset(
        {("ONE_YEAR", "NO_UPFRONT"), ("THREE_YEARS", "ALL_UPFRONT")}
    )


def test_offering_probe_pager_terminates_on_a_repeated_token() -> None:
    from services.commitment_purchase_fetch import fetch_sp_offering_matrix

    class _Stuck:
        def __init__(self) -> None:
            self.calls = 0

        def describe_savings_plans_offerings(self, **kw: Any) -> dict[str, Any]:
            self.calls += 1
            assert self.calls < 40, "pager did not terminate"
            return {"searchResults": [{"planType": "Database", "durationSeconds": 31536000,
                                       "paymentOption": "No Upfront"}], "nextToken": "same"}

    stuck = _Stuck()
    offered = fetch_sp_offering_matrix(_ctx(stuck))  # type: ignore[arg-type]
    assert offered["DATABASE_SP"] == frozenset({("ONE_YEAR", "NO_UPFRONT")})
    assert stuck.calls <= 2


def test_restricted_card_says_why_the_grid_is_mostly_dashes() -> None:
    """The scenario grid always draws 2 terms x 3 payments. Without a note, the
    five empty Database cells read as missing data rather than 'AWS does not
    sell this'."""
    from services.commitment_purchase_fetch import fetch_purchase_cards

    ce, offerings = _Ce(), _Offerings()
    cards, _p, _b, _u = fetch_purchase_cards(_ctx(offerings), ce, _noop_route)
    note = [c for c in cards if c.get("sp_type") == "DATABASE_SP"][0].get("scenario_grid_note", "")
    assert "1yr" in note and "No Upfront" in note


# --------------------------------------------------------------------------- #
# Render check (D5) — a counted/rendered lever with no render path is invisible
# --------------------------------------------------------------------------- #
def test_renderer_knows_the_new_plan_type() -> None:
    from reporter_phase_b import _INSTRUMENT_ORDER, _SP_LABELS

    assert "DATABASE_SP" in _SP_LABELS
    label, services = _SP_LABELS["DATABASE_SP"]
    assert label == "Database Savings Plan"
    for svc in ("RDS", "ElastiCache", "OpenSearch", "DynamoDB"):
        assert svc in services
    assert "Redshift" not in services      # absent from the offering's productTypes
    assert "DATABASE_SP" in _INSTRUMENT_ORDER


def test_database_sp_card_renders_its_label_and_note() -> None:
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {
        "card_kind": "sp_commitment", "sp_type": "DATABASE_SP",
        "monthly_savings": 412.90, "Counted": False, "severity": "LOW",
        "scenario_grid_note": "AWS offers this plan as 1yr / No Upfront only.",
        "recommended_scenario": 0,
        "scenarios": [{"term": "1yr", "payment": "No Upfront", "monthly_savings": 412.90,
                       "upfront": 0.0, "hourly_commitment": 1.25, "savings_pct": 18.4,
                       "break_even_months": 0.0}],
    }
    out = _render_commitment_purchase_cards([card], "ce:GetSavingsPlansPurchaseRecommendation", {})
    assert "Database Savings Plan" in out
    assert "1yr / No Upfront only" in out


def test_database_sp_is_not_an_ec2_eligible_plan() -> None:
    """It must never win the EC2 SP-vs-RI comparison — it covers no EC2 usage."""
    from reporter_phase_b import _EC2_ELIGIBLE_SP_TYPES

    assert "DATABASE_SP" not in _EC2_ELIGIBLE_SP_TYPES


def test_no_orphaned_cost_hub_bucket_for_the_new_plan() -> None:
    """The CoH ResourceType enum has no DatabaseSavingsPlans member (botocore
    1.43.47), so a match key would be a bucket that can never receive a rec."""
    from services.commitment_scenarios import _COH_SP_MATCH

    assert "DATABASE_SP" not in _COH_SP_MATCH


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
