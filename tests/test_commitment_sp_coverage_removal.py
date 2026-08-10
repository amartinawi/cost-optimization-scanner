"""LS-2 — the SP coverage-gap CARD is gone; the coverage RATE survives.

`_check_sp_coverage` used to emit one "SP Coverage Gap" recommendation per
service whose CoveragePercentage was under 80%, carrying

    potential = od * (1.0 - rate) * AVG_SP_DISCOUNT_RATE   # 0.30 flat

Three independent defects in that one line:

* the flat ``0.30`` was a fabricated fraction with no account-specific input —
  the same defect class as the Athena ``0.75`` removed as a CRITICAL (ATH-1),
  and the rec's own reason string admitted "verify against live offering rates";
* ``(1.0 - rate)`` double-discounts. CE's ``OnDemandCost`` is ALREADY the
  uncovered residue — the live-pinned fixture in ``test_commitment_utilization``
  proves ``TotalCost == OnDemandCost + SpendCoveredBySavingsPlans`` and
  ``CoveragePercentage == covered / TotalCost`` — so on that row the old formula
  reported $22.20 where the real uncovered figure is $200.00, a 9x understatement;
* the result was written to ``monthly_savings``, a key ``_advisory_line`` never
  reads (it looks at ``AdvisoryEstimate`` / ``PotentialMonthlySavings``), so the
  figure was silently dropped and the defect went unnoticed.

Deleting the projection leaves a card with no dollar at all — service name, a
coverage percentage and an "80%+" target. That is a best-practice nudge, which
this project's scope forbids: every emitted recommendation must produce a
concrete account-specific $ saving. So the emission goes, exactly as the
MediaStore storage lever and the OpenSearch version-upgrade nudges did.

``OnDemandCost`` cannot rescue it. CE documents it as "the cost of your usage at
the public On-Demand rate" — a RATE EQUIVALENT, not billed spend, and
un-region-filtered. Live on 597637668689 the EC2 row reads $13.37 while actual
unblended on-demand was ~$0.0000009 (free tier). Rendering it would contradict
the "Uncovered On-Demand ($/mo)" stat card on the same tab, which is built from
``UnblendedCost`` filtered by REGION and PURCHASE_TYPE.

What survives is the honest part: the account-wide coverage RATE feeding the
"SP Coverage" stat card. A stat is context, not a recommendation. The
accumulators must therefore keep seeing EVERY returned service — including the
ones that no longer emit a card — which is what most of this file pins.

NOTE (LS-7, filed): Savings Plans coverage for the DATABASE_SP services
(DynamoDB, RDS/Aurora, ElastiCache, OpenSearch, Neptune, DocumentDB, Keyspaces,
Timestream, DMS) is now unattributed, because ``commitment_scenarios.SP_TYPES``
fetches only COMPUTE_SP / EC2_INSTANCE_SP / SAGEMAKER_SP. Database Savings Plans
went GA 2025-12; ``DATABASE_SP`` is in the live CE enum. Adding it is a separate
change with its own verification (Database SP ships a 1-year No-Upfront term
only, so the existing term/payment matrix cannot be fanned out blindly).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from services.adapters.commitment_analysis import CommitmentAnalysisModule

_TP = {"Start": "2026-07-10", "End": "2026-08-09"}


def _ctx() -> SimpleNamespace:
    ctx = SimpleNamespace(region="us-east-1", warnings=[], permissions=[])
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(msg)
    return ctx


def _ce(rows: list[tuple[str, str, str, str]]) -> SimpleNamespace:
    """rows = [(service, on_demand, covered, coverage_pct), ...]."""
    return SimpleNamespace(
        get_savings_plans_coverage=lambda **kw: {
            "SavingsPlansCoverages": [
                {
                    "Attributes": {"SERVICE": svc},
                    "Coverage": {
                        "OnDemandCost": od,
                        "SpendCoveredBySavingsPlans": cov,
                        "TotalCost": str(float(od) + float(cov)),
                        "CoveragePercentage": pct,
                    },
                }
                for svc, od, cov, pct in rows
            ]
        }
    )


# The exact payload ce:GetSavingsPlansCoverage returned for account
# 597637668689 / us-east-1 on 2026-08-10. All three rows produced a card
# before this change; two of them carried effectively no spend at all.
_LIVE_ROWS = [
    ("AWS Lambda", "0.0", "0", "0.0"),
    ("Amazon DynamoDB", "0.00015950", "0", "0.0"),
    ("Amazon Elastic Compute Cloud - Compute", "13.365118665800001", "0", "0.0"),
]


def _run(rows: list[tuple[str, str, str, str]]) -> tuple[list[dict[str, Any]], float | None]:
    return CommitmentAnalysisModule()._check_sp_coverage(_ctx(), _ce(rows), _TP)


# --------------------------------------------------------------------------- #
# The card is gone
# --------------------------------------------------------------------------- #
def test_live_payload_emits_no_coverage_gap_cards() -> None:
    recs, _ = _run(_LIVE_ROWS)
    assert recs == []


def test_no_card_even_for_a_large_uncovered_balance() -> None:
    """Size is not the objection — a dollarless nudge is out of scope at any
    magnitude, and OnDemandCost is a list-rate equivalent, not billed spend."""
    recs, _ = _run([("Amazon Elastic Compute Cloud - Compute", "500000.00", "0", "0.0")])
    assert recs == []


def test_no_card_for_a_database_sp_service() -> None:
    """DynamoDB IS Savings-Plans-coverable via a Database SP (GA 2025-12), so it
    is suppressed for having no defensible dollar — never for ineligibility."""
    recs, _ = _run([("Amazon DynamoDB", "9578.93", "0", "0.0")])
    assert recs == []


def test_no_card_for_a_well_covered_service() -> None:
    recs, _ = _run([("AWS Lambda", "10.00", "990.00", "99.0")])
    assert recs == []


# --------------------------------------------------------------------------- #
# The rate — and therefore the stat card — is unchanged
# --------------------------------------------------------------------------- #
def test_rate_still_computed_from_every_returned_service() -> None:
    """The accumulators must run for services that no longer emit a card; if a
    suppression were placed above them the stat card would silently become a
    different metric under the same label."""
    _, rate = _run(
        [
            ("AWS Lambda", "354.36", "0", "0.0"),
            ("Amazon Elastic Compute Cloud - Compute", "200.00", "1610.74", "88.9"),
        ]
    )
    assert rate == pytest.approx(1610.74 / (554.36 + 1610.74), abs=0.01)


def test_rate_counts_a_zero_spend_service_in_the_denominator() -> None:
    _, rate = _run(_LIVE_ROWS)
    assert rate == pytest.approx(0.0)


def test_rate_is_zero_when_nothing_is_returned() -> None:
    _, rate = _run([])
    assert rate == 0.0


def test_unknown_service_still_feeds_the_rate() -> None:
    """COMMIT-01 suppressed the unattributable CARD; it never suppressed the
    spend, and that must stay true now that no card is emitted at all."""
    ce = SimpleNamespace(
        get_savings_plans_coverage=lambda **kw: {
            "SavingsPlansCoverages": [
                {
                    "Attributes": {},
                    "Coverage": {
                        "OnDemandCost": "100.00",
                        "SpendCoveredBySavingsPlans": "300.00",
                        "TotalCost": "400.00",
                        "CoveragePercentage": "75.0",
                    },
                }
            ]
        }
    )
    recs, rate = CommitmentAnalysisModule()._check_sp_coverage(_ctx(), ce, _TP)
    assert recs == []
    assert rate == pytest.approx(0.75, abs=0.01)


# --------------------------------------------------------------------------- #
# The fabricated rate cannot come back
# --------------------------------------------------------------------------- #
def test_flat_discount_constant_is_gone() -> None:
    """A flat average-discount factor has no account-specific input. Its
    presence is the whole ATH-1 defect class; pin its absence."""
    assert not hasattr(CommitmentAnalysisModule, "AVG_SP_DISCOUNT_RATE")


def test_percentage_threshold_constant_is_gone() -> None:
    assert not hasattr(CommitmentAnalysisModule, "COVERAGE_GAP_THRESHOLD")


def test_error_is_classified_not_swallowed() -> None:
    def boom(**kw):
        raise Exception("AccessDeniedException")

    ctx = _ctx()
    recs, rate = CommitmentAnalysisModule()._check_sp_coverage(
        ctx, SimpleNamespace(get_savings_plans_coverage=boom), _TP
    )
    assert recs == []
    assert ctx.warnings or ctx.permissions


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
