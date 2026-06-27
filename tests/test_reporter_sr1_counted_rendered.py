"""SR-1 (HIGH) — Phase-A card dollar is single-sourced to the counted total.

The descriptor-driven ``render_grouped_by_category`` used to print each rec's
free-text ``EstimatedSavings`` string (a percentage like "10-30%" or a hardcoded
"$316/month") while the tab headline summed the computed ``EstimatedMonthlySavings``
float — three potentially inconsistent numbers per card. SR-1 renders the group's
*counted* ``EstimatedMonthlySavings`` sum so the card equals the headline, with an
advisory-only group rendering an honest "$0.00/month — advisory".

The golden snapshot fixtures carry no recommendations for the Phase-A services
(api_gateway/glue/step_functions/lambda/cloudfront/lightsail/dms), so this path
has no snapshot coverage — these direct tests prove the behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reporter_phase_a import PHASE_A_DESCRIPTORS, render_grouped_by_category


def _render(service_key: str, recs: list[dict]) -> str:
    sources = {"enhanced_checks": {"count": len(recs), "recommendations": recs}}
    return render_grouped_by_category(service_key, sources, PHASE_A_DESCRIPTORS[service_key])


def test_api_gateway_card_shows_counted_dollar_not_percentage() -> None:
    recs = [
        {
            "CheckCategory": "API Gateway Type Optimization",
            "ApiId": "a1",
            "ApiName": "x",
            "EstimatedMonthlySavings": 12.50,
            "EstimatedSavings": "10-30% cost reduction for simple APIs",
        }
    ]
    html = _render("api_gateway", recs)
    assert "$12.50/month" in html
    assert "10-30%" not in html  # the percentage string no longer shown as the dollar


def test_group_sums_counted_savings() -> None:
    recs = [
        {"CheckCategory": "Standard vs Express", "EstimatedMonthlySavings": 10.0},
        {"CheckCategory": "Standard vs Express", "EstimatedMonthlySavings": 5.0},
    ]
    html = _render("step_functions", recs)
    assert "$15.00/month" in html


def test_advisory_only_group_renders_zero_advisory() -> None:
    recs = [
        {
            "CheckCategory": "Standard vs Express",
            "EstimatedMonthlySavings": 0.0,
            "Counted": False,
            "EstimatedSavings": "$0.00/month — advisory: no execution metric",
        }
    ]
    html = _render("step_functions", recs)
    assert "$0.00/month — advisory" in html


def test_advisory_rec_excluded_from_group_total() -> None:
    recs = [
        {"CheckCategory": "Standard vs Express", "EstimatedMonthlySavings": 20.0},
        {"CheckCategory": "Standard vs Express", "EstimatedMonthlySavings": 99.0, "Counted": False},
    ]
    html = _render("step_functions", recs)
    assert "$20.00/month" in html  # only the counted 20.0, not 20+99
    assert "119" not in html
    assert "99.00" not in html


def test_no_numeric_savings_keeps_legacy_string() -> None:
    # A rec with no EstimatedMonthlySavings key falls back to the legacy string,
    # so services that do not compute a numeric saving are unchanged.
    recs = [{"CheckCategory": "Standard vs Express", "EstimatedSavings": "20-40% cost reduction"}]
    html = _render("step_functions", recs)
    assert "20-40% cost reduction" in html


def test_glue_hardcoded_dollar_string_replaced_by_counted() -> None:
    recs = [
        {
            "CheckCategory": "Glue Dev Endpoint",
            "JobName": "ep-1",
            "EstimatedMonthlySavings": 219.0,
            "EstimatedSavings": "$316/month per endpoint",
        }
    ]
    html = _render("glue", recs)
    assert "$219.00/month" in html
    assert "$316" not in html
