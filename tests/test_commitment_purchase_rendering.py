"""Tests for the commitment purchase-recommendation renderer and exec-summary
wiring (reporter_phase_b.py, html_report_generator.py).

Split out of tests/test_commitment_scenarios.py (which stayed pure-logic
math) to keep both files under the repo's 800-line file cap.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# --- Task 6: Card renderer -----------------------------------------------


def test_render_ri_card_has_matrix_and_marked_recommendation():
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "RDS", "instance_type": "db.r7i.4xlarge",
            "region": "eu-west-1", "platform": "aurora-postgresql",
            "recommended_count": 7, "current_ondemand_monthly": 4102.11,
            "coverage_pct": 22.0, "uncovered_monthly": 3199.65,
            "scenarios": [
                {"term": "1yr", "payment": "No Upfront", "monthly_savings": 1210.40,
                 "upfront": 0.0, "recurring_monthly": 2891.71, "break_even_months": 0.0},
                {"term": "3yr", "payment": "All Upfront", "monthly_savings": 1700.0,
                 "upfront": 12000.0, "recurring_monthly": 1800.0, "break_even_months": 7.1},
            ],
            "recommended_scenario": 1, "risk_pct": 51.2,
            "Counted": False, "monthly_savings": 1700.0,
            "coh_concurs_monthly": 1650.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "db.r7i.4xlarge" in html and "x7" in html
    assert "$1,210.40" in html and "$1,700.00" in html      # matrix cells
    assert "break-even" in html.lower()
    assert "51.2" in html                                    # risk line
    assert "22.0%" in html                                   # coverage context
    assert "CoH concurs" in html
    assert "projection" in html.lower()                      # advisory chip
    assert html.count("recommended") >= 1                    # AWS pick marked


def test_render_sp_card_states_no_instance_type():
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "sp_commitment", "sp_type": "COMPUTE_SP",
            "scenarios": [{"term": "3yr", "payment": "All Upfront",
                           "monthly_savings": 800.0, "upfront": 9000.0,
                           "hourly_commitment": 1.1, "savings_pct": 32.0,
                           "break_even_months": 11.3}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 800.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "$1.1000/hr" in html
    assert "EC2 + Lambda + Fargate" in html                  # services spanned
    assert "account-level" in html.lower()                   # no fake type detail


def test_render_groups_by_instrument_and_orders_by_savings():
    from reporter_phase_b import _render_commitment_purchase_cards

    small = {"card_kind": "ri_type", "service": "ElastiCache", "instance_type": "cache.t3.micro",
             "region": "eu-west-1", "platform": "redis", "recommended_count": 1,
             "current_ondemand_monthly": 20.0, "scenarios": [], "recommended_scenario": 0,
             "Counted": False, "monthly_savings": 5.0}
    big = {"card_kind": "sp_commitment", "sp_type": "COMPUTE_SP",
           "scenarios": [], "recommended_scenario": 0, "Counted": False,
           "monthly_savings": 900.0}
    html = _render_commitment_purchase_cards([small, big], "purchase_recommendations", {})
    assert html.index("Compute Savings Plan") < html.index("ElastiCache")


def test_render_dynamodb_card_skips_empty_platform_cleanly():
    """DynamoDB RI cards have no platform and a capacity-units instance_type
    string; the generic card layout must not crash or print empty parens."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "DynamoDB", "instance_type": "100 capacity units",
            "region": "us-east-1", "platform": "", "recommended_count": 5,
            "current_ondemand_monthly": 900.0,
            "scenarios": [{"term": "1yr", "payment": "No Upfront", "monthly_savings": 300.0,
                           "upfront": 0.0, "recurring_monthly": 600.0, "break_even_months": 0.0}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 300.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "100 capacity units" in html
    assert "()" not in html
    assert " — —" not in html


def test_render_zero_savings_cell_is_greyed_not_recommended():
    """Ruling: SP $0-savings cells are kept but greyed, never marked recommended.

    F1 regression: Task 2's max() still yields an index even when every
    scenario nets $0, so recommended_scenario can point AT a $0 cell. That
    cell must render muted with NEITHER the recommended class NOR the
    recommended label.
    """
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "sp_commitment", "sp_type": "SAGEMAKER_SP",
            "scenarios": [
                {"term": "1yr", "payment": "No Upfront", "monthly_savings": 0.0,
                 "upfront": 0.0, "hourly_commitment": 0.2, "savings_pct": 0.0,
                 "break_even_months": 0.0},
                {"term": "3yr", "payment": "All Upfront", "monthly_savings": 150.0,
                 "upfront": 500.0, "hourly_commitment": 0.2, "savings_pct": 18.0,
                 "break_even_months": 3.3},
            ],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 150.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "muted" in html
    assert "scenario-cell--recommended" not in html
    assert "recommended</span>" not in html


def test_render_all_zero_sp_card_has_no_recommended_marker_anywhere():
    """F1 sibling: an all-zero SP card renders with no recommended marker at all."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "sp_commitment", "sp_type": "SAGEMAKER_SP",
            "scenarios": [
                {"term": "1yr", "payment": "No Upfront", "monthly_savings": 0.0,
                 "upfront": 0.0, "hourly_commitment": 0.2, "savings_pct": 0.0,
                 "break_even_months": 0.0},
                {"term": "3yr", "payment": "All Upfront", "monthly_savings": 0.0,
                 "upfront": 0.0, "hourly_commitment": 0.2, "savings_pct": 0.0,
                 "break_even_months": 0.0},
            ],
            "recommended_scenario": 1, "Counted": False, "monthly_savings": 0.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "scenario-cell--recommended" not in html
    assert "recommended</span>" not in html


def test_render_break_even_none_renders_phrase_not_zero():
    """break_even_months == None means 'never breaks even'; must not print 0.0."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "Redshift", "instance_type": "ra3.xlplus",
            "region": "eu-west-1", "platform": "", "recommended_count": 2,
            "current_ondemand_monthly": 400.0,
            "scenarios": [{"term": "1yr", "payment": "No Upfront", "monthly_savings": 0.0,
                           "upfront": 100.0, "recurring_monthly": 400.0, "break_even_months": None}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 0.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "never breaks even" in html.lower()
    assert "0.0 mo" not in html


def test_render_coverage_omitted_when_fields_absent():
    """RI cards may carry neither coverage_pct nor uncovered_monthly — fail closed."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "OpenSearch", "instance_type": "r6g.large.search",
            "region": "us-east-1", "platform": "", "recommended_count": 3,
            "current_ondemand_monthly": 600.0,
            "scenarios": [{"term": "1yr", "payment": "No Upfront", "monthly_savings": 100.0,
                           "upfront": 0.0, "recurring_monthly": 500.0, "break_even_months": 0.0}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 100.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "0%" not in html
    assert "0.0%" not in html


def test_render_empty_recs_returns_empty_string():
    from reporter_phase_b import _render_commitment_purchase_cards

    assert _render_commitment_purchase_cards([], "purchase_recommendations", {}) == ""


def test_render_registered_in_phase_b_handlers():
    from reporter_phase_b import PHASE_B_HANDLERS, _render_commitment_purchase_cards

    assert PHASE_B_HANDLERS[("commitment_analysis", "purchase_recommendations")] is (
        _render_commitment_purchase_cards
    )


# --- Task 7: Exec fact + stat cards + SP-vs-RI strip -------------------------
#
# `generate_html_report_from_json` reads a JSON *file path* and its return
# value is the *output file path* it wrote, not the HTML text — neither
# matches the brief's `html = generate_html_report_from_json(data)` snippet.
# We drive `HTMLReportGenerator` directly (the same pattern
# tests/test_reporter_snapshots.py already uses) and read the written file.


def test_exec_summary_shows_projected_fact(tmp_path):
    from html_report_generator import HTMLReportGenerator
    from tests.test_output_audit import make_report

    data = make_report()
    data["scan_time"] = "2026-08-08T00:00:00"  # _get_header requires it; make_report() omits it
    data["summary"]["projected_commitment_monthly_savings"] = 2345.67
    data["summary"]["projected_commitment_basis"] = "Compute SP path"
    out_file = tmp_path / "projected_fact.html"
    HTMLReportGenerator(data).generate_html_report(str(out_file))
    html_out = out_file.read_text()
    assert "Projected commitment" in html_out
    assert "$2,345.67" in html_out
    assert "Compute SP path" in html_out


def test_exec_summary_omits_projected_fact_when_zero(tmp_path):
    from html_report_generator import HTMLReportGenerator
    from tests.test_output_audit import make_report

    data = make_report()
    data["scan_time"] = "2026-08-08T00:00:00"  # _get_header requires it; make_report() omits it
    out_file = tmp_path / "no_projected_fact.html"
    HTMLReportGenerator(data).generate_html_report(str(out_file))
    html_out = out_file.read_text()
    assert "Projected commitment" not in html_out


def test_sp_vs_ri_strip_renders_on_ec2_section():
    from reporter_phase_b import _render_commitment_purchase_cards

    ec2_ri = {"card_kind": "ri_type", "service": "EC2", "instance_type": "r6i.4xlarge",
              "region": "eu-west-1", "platform": "Windows", "recommended_count": 3,
              "current_ondemand_monthly": 4000.0, "scenarios": [], "recommended_scenario": 0,
              "Counted": False, "monthly_savings": 1000.0}
    sp = {"card_kind": "sp_commitment", "sp_type": "COMPUTE_SP", "scenarios": [],
          "recommended_scenario": 0, "Counted": False, "monthly_savings": 1500.0}
    html = _render_commitment_purchase_cards([ec2_ri, sp], "purchase_recommendations", {})
    assert "SP vs RI" in html
    assert "$1,500.00" in html and "$1,000.00" in html
    assert "Lambda" in html          # flexibility trade-off stated


def test_sp_vs_ri_strip_absent_without_ec2_ri():
    """No EC2 RI cards -> no strip, even with a strong SP card present."""
    from reporter_phase_b import _render_commitment_purchase_cards

    sp = {"card_kind": "sp_commitment", "sp_type": "COMPUTE_SP", "scenarios": [],
          "recommended_scenario": 0, "Counted": False, "monthly_savings": 1500.0}
    html = _render_commitment_purchase_cards([sp], "purchase_recommendations", {})
    assert "SP vs RI" not in html


def test_sp_vs_ri_strip_absent_when_best_sp_is_zero():
    """Ruling: a $0 best_sp must not render a strip claiming a '$0.00 leads' comparison."""
    from reporter_phase_b import _render_commitment_purchase_cards

    ec2_ri = {"card_kind": "ri_type", "service": "EC2", "instance_type": "r6i.4xlarge",
              "region": "eu-west-1", "platform": "Windows", "recommended_count": 3,
              "current_ondemand_monthly": 4000.0, "scenarios": [], "recommended_scenario": 0,
              "Counted": False, "monthly_savings": 1000.0}
    zero_sp = {"card_kind": "sp_commitment", "sp_type": "COMPUTE_SP", "scenarios": [],
               "recommended_scenario": 0, "Counted": False, "monthly_savings": 0.0}
    html = _render_commitment_purchase_cards([ec2_ri, zero_sp], "purchase_recommendations", {})
    assert "SP vs RI" not in html


def test_sp_vs_ri_strip_ignores_sagemaker_sp():
    """SAGEMAKER_SP never covers EC2, so it must never be picked as best_sp."""
    from reporter_phase_b import _render_commitment_purchase_cards

    ec2_ri = {"card_kind": "ri_type", "service": "EC2", "instance_type": "r6i.4xlarge",
              "region": "eu-west-1", "platform": "Windows", "recommended_count": 3,
              "current_ondemand_monthly": 4000.0, "scenarios": [], "recommended_scenario": 0,
              "Counted": False, "monthly_savings": 1000.0}
    sagemaker_sp = {"card_kind": "sp_commitment", "sp_type": "SAGEMAKER_SP", "scenarios": [],
                    "recommended_scenario": 0, "Counted": False, "monthly_savings": 5000.0}
    html = _render_commitment_purchase_cards([ec2_ri, sagemaker_sp], "purchase_recommendations", {})
    # No strip at all: the only SP present (SAGEMAKER_SP) is ineligible as best_sp.
    assert "SP vs RI" not in html
    assert "sp-vs-ri" not in html


# --- Final-review fix wave (2026-08-08): B3, M1-M3, M7, L1, L3-L6 -----------


def test_sp_vs_ri_strip_ec2_instance_sp_states_opposite_tradeoff():
    """B3: EC2_INSTANCE_SP is the opposite trade-off from Compute SP — deeper
    discount, family/region-locked, no Lambda/Fargate coverage."""
    from reporter_phase_b import _render_commitment_purchase_cards

    ec2_ri = {"card_kind": "ri_type", "service": "EC2", "instance_type": "r6i.4xlarge",
              "region": "eu-west-1", "platform": "Windows", "recommended_count": 3,
              "current_ondemand_monthly": 4000.0, "scenarios": [], "recommended_scenario": 0,
              "Counted": False, "monthly_savings": 1000.0}
    sp = {"card_kind": "sp_commitment", "sp_type": "EC2_INSTANCE_SP", "scenarios": [],
          "recommended_scenario": 0, "Counted": False, "monthly_savings": 1500.0}
    html = _render_commitment_purchase_cards([ec2_ri, sp], "purchase_recommendations", {})
    assert "SP vs RI" in html
    assert "family- and region-scoped" in html
    assert "does not cover Lambda/Fargate" in html


def test_render_ec2_instance_sp_card_states_family_scoped_with_families():
    """M7: EC2_INSTANCE_SP card says 'family-scoped', not 'account-level', and
    lists the instance_families the best cell carried (from B1)."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "sp_commitment", "sp_type": "EC2_INSTANCE_SP",
            "instance_families": ["m5", "r5"],
            "scenarios": [{"term": "1yr", "payment": "No Upfront", "monthly_savings": 400.0,
                           "upfront": 0.0, "hourly_commitment": 0.5, "savings_pct": 20.0,
                           "break_even_months": 0.0}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 400.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "family-scoped" in html.lower()
    assert "account-level" not in html.lower()
    assert "Families: m5, r5" in html


def test_render_ec2_instance_sp_card_without_families_omits_line():
    """M7: no instance_families on the card -> no 'Families:' line at all."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "sp_commitment", "sp_type": "EC2_INSTANCE_SP",
            "scenarios": [{"term": "1yr", "payment": "No Upfront", "monthly_savings": 400.0,
                           "upfront": 0.0, "hourly_commitment": 0.5, "savings_pct": 20.0,
                           "break_even_months": 0.0}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 400.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "Families:" not in html


def test_render_break_even_zero_renders_immediate():
    """L1: break_even_months == 0.0 renders 'immediate', not '0.0 mo'."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "RDS", "instance_type": "db.t3.micro",
            "region": "eu-west-1", "platform": "", "recommended_count": 1,
            "current_ondemand_monthly": 50.0,
            "scenarios": [{"term": "1yr", "payment": "No Upfront", "monthly_savings": 10.0,
                           "upfront": 0.0, "recurring_monthly": 40.0, "break_even_months": 0.0}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 10.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "break-even immediate" in html.lower()
    assert "0.0 mo" not in html


def test_render_section_header_says_projection():
    """L3: section header reads '$X/mo best-path (projection)'."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "RDS", "instance_type": "db.r7i.4xlarge",
            "region": "eu-west-1", "platform": "", "recommended_count": 1,
            "current_ondemand_monthly": 100.0, "scenarios": [], "recommended_scenario": 0,
            "Counted": False, "monthly_savings": 10.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "best-path (projection)" in html


def test_instrument_order_includes_dynamodb_after_redshift():
    """L4: _INSTRUMENT_ORDER carries DynamoDB, positioned after Redshift."""
    from reporter_phase_b import _INSTRUMENT_ORDER

    assert "DynamoDB" in _INSTRUMENT_ORDER
    assert _INSTRUMENT_ORDER.index("DynamoDB") == _INSTRUMENT_ORDER.index("Redshift") + 1


def test_render_commitment_cards_get_low_priority_class():
    """L5: cards carry severity=LOW so _priority_class doesn't dim the whole
    purchase-recommendations section under an active priority filter."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "RDS", "instance_type": "db.r7i.4xlarge",
            "region": "eu-west-1", "platform": "", "recommended_count": 1,
            "current_ondemand_monthly": 100.0, "scenarios": [], "recommended_scenario": 0,
            "Counted": False, "monthly_savings": 10.0, "severity": "LOW"}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "low-priority" in html


def test_render_uncovered_monthly_without_coverage_pct():
    """L6/M4: a card can carry uncovered_monthly without coverage_pct (M4's
    fail-closed omission) — that dollar figure must still render."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "RDS", "instance_type": "db.r7i.4xlarge",
            "region": "eu-west-1", "platform": "aurora-postgresql", "recommended_count": 2,
            "current_ondemand_monthly": 600.0, "uncovered_monthly": 2000.0,
            "scenarios": [{"term": "1yr", "payment": "No Upfront", "monthly_savings": 100.0,
                           "upfront": 0.0, "recurring_monthly": 500.0, "break_even_months": 0.0}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 100.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "$2,000.00" in html
    assert "coverage % unavailable" in html


def test_stat_card_renders_na_for_none_uncovered_ondemand():
    """M2: uncovered_ondemand_monthly_total == None renders 'n/a', not the
    literal string 'None' or a fabricated 0."""
    from html_report_generator import HTMLReportGenerator
    from tests.test_output_audit import make_report

    data = make_report()
    data["services"]["commitment_analysis"] = {
        "service_name": "Commitment Analysis",
        "total_recommendations": 0,
        "total_monthly_savings": 0.0,
        "sources": {},
        "extras": {
            "sp_utilization_rate": 0.0, "sp_coverage_rate": 0.0,
            "ri_utilization_rate": 0.0, "ri_coverage_rate": 0.0,
            "uncovered_ondemand_monthly_total": None,
            "projected_commitment_monthly_savings": 0.0,
        },
    }
    reporter = HTMLReportGenerator(data)
    stats_html = reporter._get_service_stats("commitment_analysis", data["services"]["commitment_analysis"])
    assert "n/a" in stats_html
    assert "None" not in stats_html


def test_stat_card_labels_carry_dollar_units():
    """M3: the new stat-card labels state units so a bare number doesn't read
    as a percentage/count next to the rate cards."""
    from html_report_generator import _SERVICE_STATS_CONFIG

    labels = [label for label, *_ in _SERVICE_STATS_CONFIG["commitment_analysis"]["multi_source_cards"]]
    assert "Uncovered On-Demand ($/mo)" in labels
    assert "Projected Savings ($/mo)" in labels


def test_render_ri_title_shows_nondefault_tenancy_and_offering_class():
    """M5: tenancy/offering_class earn a title segment only when they carry
    information beyond AWS's own defaults (Shared tenancy, Standard RIs)."""
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "EC2", "instance_type": "r6i.4xlarge",
            "region": "eu-west-1", "platform": "Linux/UNIX", "tenancy": "Dedicated",
            "offering_class": "CONVERTIBLE", "recommended_count": 2,
            "current_ondemand_monthly": 500.0, "scenarios": [], "recommended_scenario": 0,
            "Counted": False, "monthly_savings": 50.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "Dedicated" in html
    assert "CONVERTIBLE" in html


def test_render_ri_title_hides_default_tenancy_and_offering_class():
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "EC2", "instance_type": "r6i.4xlarge",
            "region": "eu-west-1", "platform": "Linux/UNIX", "tenancy": "Shared",
            "offering_class": "STANDARD", "recommended_count": 2,
            "current_ondemand_monthly": 500.0, "scenarios": [], "recommended_scenario": 0,
            "Counted": False, "monthly_savings": 50.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "Shared" not in html
    assert "STANDARD" not in html


def test_css_defines_muted_sp_vs_ri_and_fact_note():
    """M1: the shared .muted / .sp-vs-ri / .fact-note rules exist and are
    theme-aware (built from CSS custom properties, not literal colors)."""
    from html_report_generator import HTMLReportGenerator
    from tests.test_output_audit import make_report

    css = HTMLReportGenerator(make_report())._get_css()
    assert ".muted {" in css
    assert ".sp-vs-ri {" in css
    assert ".fact-note {" in css
    assert "var(--text-secondary)" in css
