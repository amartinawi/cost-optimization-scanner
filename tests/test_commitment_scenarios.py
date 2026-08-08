"""Tests for services/commitment_scenarios.py — pure-logic card math."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.commitment_scenarios import (
    RI_SERVICES,
    SP_TYPES,
    build_ri_type_cards,
    build_sp_cards,
    ri_cells_from_response,
    sp_cell_from_response,
)


def _ri_resp(details):
    return {"Recommendations": [{"RecommendationDetails": details}]}


def _rds_detail(**over):
    base = {
        "InstanceDetails": {"RDSInstanceDetails": {
            "InstanceType": "db.r7i.4xlarge", "Region": "eu-west-1",
            "DatabaseEngine": "aurora-postgresql"}},
        "RecommendedNumberOfInstancesToPurchase": "7",
        "EstimatedMonthlySavingsAmount": "1210.40",
        "UpfrontCost": "0",
        "RecurringStandardMonthlyCost": "2891.71",
        "EstimatedMonthlyOnDemandCost": "4102.11",
    }
    base.update(over)
    return base


def test_ri_cells_parse_nested_instance_details():
    cells = ri_cells_from_response("RDS", "1yr", "No Upfront", _ri_resp([_rds_detail()]))
    assert len(cells) == 1
    c = cells[0]
    assert c["instance_type"] == "db.r7i.4xlarge"
    assert c["region"] == "eu-west-1"
    assert c["platform"] == "aurora-postgresql"
    assert c["count"] == 7
    assert c["monthly_savings"] == pytest.approx(1210.40)
    assert c["recurring_monthly"] == pytest.approx(2891.71)
    assert c["ondemand_monthly"] == pytest.approx(4102.11)
    assert (c["term"], c["payment"]) == ("1yr", "No Upfront")


def test_ri_cells_read_legacy_savings_field():
    d = _rds_detail()
    d["EstimatedMonthlySavings"] = d.pop("EstimatedMonthlySavingsAmount")
    cells = ri_cells_from_response("RDS", "1yr", "No Upfront", _ri_resp([d]))
    assert cells[0]["monthly_savings"] == pytest.approx(1210.40)


def test_ri_cells_es_type_joins_class_and_size():
    d = {
        "InstanceDetails": {"ESInstanceDetails": {
            "InstanceClass": "r6g", "InstanceSize": "large.search", "Region": "eu-west-1"}},
        "RecommendedNumberOfInstancesToPurchase": "2",
        "EstimatedMonthlySavingsAmount": "80.00",
        "UpfrontCost": "100",
        "RecurringStandardMonthlyCost": "50",
        "EstimatedMonthlyOnDemandCost": "200",
    }
    cells = ri_cells_from_response("OpenSearch", "3yr", "All Upfront", _ri_resp([d]))
    assert cells[0]["instance_type"] == "r6g.large.search"


def test_ri_cells_zero_savings_detail_dropped():
    cells = ri_cells_from_response(
        "RDS", "1yr", "No Upfront",
        _ri_resp([_rds_detail(EstimatedMonthlySavingsAmount="0")]))
    assert cells == []


def test_sp_cell_parses_summary():
    resp = {"SavingsPlansPurchaseRecommendation": {
        "SavingsPlansPurchaseRecommendationSummary": {
            "EstimatedMonthlySavingsAmount": "512.30",
            "HourlyCommitmentToPurchase": "1.2345",
            "EstimatedSavingsPercentage": "22.1",
            "EstimatedOnDemandCostWithCurrentCommitment": "3000.00",
        },
        "SavingsPlansPurchaseRecommendationDetails": [{"UpfrontCost": "900"}],
    }}
    cell = sp_cell_from_response("COMPUTE_SP", "3yr", "Partial Upfront", resp)
    assert cell["hourly_commitment"] == pytest.approx(1.2345)
    assert cell["monthly_savings"] == pytest.approx(512.30)
    assert cell["upfront"] == pytest.approx(900.0)


def test_sp_cell_empty_response_is_none():
    assert sp_cell_from_response("COMPUTE_SP", "1yr", "No Upfront", {}) is None


# --- Doc-verification-driven cases (Task 1 Step 0) -------------------------
#
# These cover the two points where the confirmed AWS docs diverged from the
# brief's initial guess: the OpenSearch `Service` string ("Amazon OpenSearch
# Service", not the legacy "Amazon Elasticsearch Service"), and DynamoDB's
# distinct `ReservedCapacityDetails.DynamoDBCapacityDetails` shape (no
# `InstanceDetails`, no instance type, capacity units instead).


def test_ri_services_match_doc_confirmed_strings():
    by_label = dict((label, service) for service, label in RI_SERVICES)
    assert by_label["EC2"] == "Amazon Elastic Compute Cloud - Compute"
    assert by_label["RDS"] == "Amazon Relational Database Service"
    assert by_label["ElastiCache"] == "Amazon ElastiCache"
    assert by_label["Redshift"] == "Amazon Redshift"
    assert by_label["OpenSearch"] == "Amazon OpenSearch Service"
    assert by_label["DynamoDB"] == "Amazon DynamoDB"


def test_sp_types_match_doc_confirmed_enum():
    assert SP_TYPES == ("COMPUTE_SP", "EC2_INSTANCE_SP", "SAGEMAKER_SP")


def test_ri_cells_dynamodb_uses_reserved_capacity_details():
    d = {
        "ReservedCapacityDetails": {"DynamoDBCapacityDetails": {
            "CapacityUnits": "100", "Region": "us-east-1"}},
        "RecommendedNumberOfCapacityUnitsToPurchase": "5",
        "EstimatedMonthlySavingsAmount": "300.00",
        "UpfrontCost": "0",
        "RecurringStandardMonthlyCost": "600.00",
        "EstimatedMonthlyOnDemandCost": "900.00",
    }
    cells = ri_cells_from_response("DynamoDB", "1yr", "No Upfront", _ri_resp([d]))
    assert len(cells) == 1
    c = cells[0]
    assert c["region"] == "us-east-1"
    assert c["platform"] == ""
    assert c["count"] == 5
    assert "100" in c["instance_type"]
    assert c["monthly_savings"] == pytest.approx(300.00)


# --- Task 2: Card builders ---------------------------------------------------


def _cell(**over):
    base = {"service": "RDS", "instance_type": "db.r7i.4xlarge", "region": "eu-west-1",
            "platform": "aurora-postgresql", "count": 7, "term": "1yr",
            "payment": "No Upfront", "monthly_savings": 1210.40, "upfront": 0.0,
            "recurring_monthly": 2891.71, "ondemand_monthly": 4102.11}
    base.update(over)
    return base


def test_cards_group_six_cells_into_one():
    cells = [_cell(term=t, payment=p, monthly_savings=s)
             for (t, p, s) in [("1yr", "No Upfront", 1210.40), ("1yr", "Partial Upfront", 1300.0),
                               ("1yr", "All Upfront", 1350.0), ("3yr", "No Upfront", 1500.0),
                               ("3yr", "Partial Upfront", 1600.0), ("3yr", "All Upfront", 1700.0)]]
    cards = build_ri_type_cards(cells, uncovered={}, scan_region="eu-west-1")
    assert len(cards) == 1
    card = cards[0]
    assert card["card_kind"] == "ri_type"
    assert len(card["scenarios"]) == 6
    assert card["monthly_savings"] == pytest.approx(1700.0)          # best cell
    assert card["scenarios"][card["recommended_scenario"]]["monthly_savings"] == pytest.approx(1700.0)
    assert card["Counted"] is False


def test_break_even_months():
    cells = [_cell(upfront=1200.0, monthly_savings=100.0)]
    card = build_ri_type_cards(cells, {}, "eu-west-1")[0]
    assert card["scenarios"][0]["break_even_months"] == pytest.approx(12.0)


def test_break_even_zero_upfront_is_zero():
    card = build_ri_type_cards([_cell(upfront=0.0)], {}, "eu-west-1")[0]
    assert card["scenarios"][0]["break_even_months"] == 0.0


def test_risk_pct_from_best_cell():
    # risk = (recurring + upfront/term_months) / ondemand for the BEST cell.
    cells = [_cell(term="3yr", upfront=3600.0, recurring_monthly=2000.0,
                   monthly_savings=2002.11, ondemand_monthly=4102.11)]
    card = build_ri_type_cards(cells, {}, "eu-west-1")[0]
    # (2000 + 3600/36) / 4102.11 = 2100/4102.11 = 51.2%
    assert card["risk_pct"] == pytest.approx(51.2, abs=0.1)


def test_coverage_join_and_missing_key():
    cells = [_cell()]
    cards = build_ri_type_cards(cells, {"rds:r7i.4xlarge": 3199.65}, "eu-west-1")
    assert cards[0]["uncovered_monthly"] == pytest.approx(3199.65)
    # coverage_pct = 1 - uncovered/ondemand, floored at 0
    assert cards[0]["coverage_pct"] == pytest.approx(22.0, abs=0.1)
    # Missing key -> fields absent entirely (fail closed, never fabricated 0)
    bare = build_ri_type_cards(cells, {}, "eu-west-1")[0]
    assert "uncovered_monthly" not in bare and "coverage_pct" not in bare


def test_scan_region_sorts_first():
    cells = [_cell(region="us-east-1", monthly_savings=9999.0),
             _cell(instance_type="db.t3.medium", region="eu-west-1", monthly_savings=5.0)]
    cards = build_ri_type_cards(cells, {}, "eu-west-1")
    assert cards[0]["region"] == "eu-west-1"


def test_sp_cards_one_per_type():
    cells = [
        {"sp_type": "COMPUTE_SP", "term": "1yr", "payment": "No Upfront",
         "hourly_commitment": 1.2, "monthly_savings": 500.0, "savings_pct": 20.0,
         "upfront": 0.0, "estimated_ondemand_monthly": 2500.0},
        {"sp_type": "COMPUTE_SP", "term": "3yr", "payment": "All Upfront",
         "hourly_commitment": 1.1, "monthly_savings": 800.0, "savings_pct": 32.0,
         "upfront": 9000.0, "estimated_ondemand_monthly": 2500.0},
    ]
    cards = build_sp_cards(cells)
    assert len(cards) == 1
    assert cards[0]["card_kind"] == "sp_commitment"
    assert cards[0]["monthly_savings"] == pytest.approx(800.0)
    assert cards[0]["Counted"] is False


# --- Fix Round 1: F1-F6 (2026-08-08) ----------------------------------------


def test_sp_cards_risk_pct_from_best_cell():
    """F1: SP cards must compute risk_pct = 100 * (1 - savings/ondemand)."""
    cells = [
        {"sp_type": "COMPUTE_SP", "term": "1yr", "payment": "No Upfront",
         "hourly_commitment": 1.2, "monthly_savings": 500.0, "savings_pct": 20.0,
         "upfront": 0.0, "estimated_ondemand_monthly": 2500.0},
        {"sp_type": "COMPUTE_SP", "term": "3yr", "payment": "All Upfront",
         "hourly_commitment": 1.1, "monthly_savings": 800.0, "savings_pct": 32.0,
         "upfront": 9000.0, "estimated_ondemand_monthly": 2500.0},
    ]
    cards = build_sp_cards(cells)
    # Best is 800 savings / 2500 ondemand -> risk = 100 * (1 - 800/2500) = 68%
    assert cards[0]["risk_pct"] == pytest.approx(68.0)


def test_sp_cards_two_types_sorted_by_savings():
    """F4: Multiple SP types should produce separate cards, sorted by monthly_savings desc."""
    cells = [
        {"sp_type": "COMPUTE_SP", "term": "1yr", "payment": "No Upfront",
         "hourly_commitment": 1.0, "monthly_savings": 500.0, "savings_pct": 20.0,
         "upfront": 0.0, "estimated_ondemand_monthly": 2500.0},
        {"sp_type": "EC2_INSTANCE_SP", "term": "1yr", "payment": "No Upfront",
         "hourly_commitment": 0.5, "monthly_savings": 1200.0, "savings_pct": 25.0,
         "upfront": 0.0, "estimated_ondemand_monthly": 4800.0},
    ]
    cards = build_sp_cards(cells)
    assert len(cards) == 2
    assert cards[0]["sp_type"] == "EC2_INSTANCE_SP"
    assert cards[0]["monthly_savings"] == pytest.approx(1200.0)
    assert cards[1]["sp_type"] == "COMPUTE_SP"
    assert cards[1]["monthly_savings"] == pytest.approx(500.0)


def test_break_even_none_for_unprofitable():
    """F2: break_even_months should be None when upfront > 0 but monthly_savings <= 0."""
    cells = [_cell(upfront=100.0, monthly_savings=0.0)]
    card = build_ri_type_cards(cells, {}, "eu-west-1")[0]
    assert card["scenarios"][0]["break_even_months"] is None


def test_mutations_do_not_affect_originals():
    """F3: _finish_scenarios must not mutate the original cell dicts passed in."""
    import copy
    original_cells = [_cell()]
    cells_copy = copy.deepcopy(original_cells)
    build_ri_type_cards(original_cells, {}, "eu-west-1")
    assert original_cells == cells_copy


def test_platform_separates_ri_groups():
    """F5: Different platforms should create separate cards even for same type/region."""
    cells = [
        _cell(platform="Linux/UNIX", monthly_savings=100.0),
        _cell(platform="Windows", monthly_savings=80.0),
    ]
    cards = build_ri_type_cards(cells, {}, "eu-west-1")
    assert len(cards) == 2
    assert {c["platform"] for c in cards} == {"Linux/UNIX", "Windows"}


def test_coverage_normalized_key_and_region_filter():
    """F6: Coverage join must use normalized keys and only for scan_region."""
    cells = [_cell(region="eu-west-1"), _cell(region="us-east-1")]
    # Normalized key for "db.r7i.4xlarge" is "r7i.4xlarge"
    uncovered = {"rds:r7i.4xlarge": 1000.0}
    cards = build_ri_type_cards(cells, uncovered, "eu-west-1")
    # eu-west-1 card should have coverage (scan_region match)
    eu_card = [c for c in cards if c["region"] == "eu-west-1"][0]
    assert "uncovered_monthly" in eu_card
    assert eu_card["uncovered_monthly"] == pytest.approx(1000.0)
    # us-east-1 card should not have coverage (region mismatch)
    us_card = [c for c in cards if c["region"] == "us-east-1"][0]
    assert "uncovered_monthly" not in us_card
    assert "coverage_pct" not in us_card


def test_coverage_normalized_key_with_db_prefix():
    """F6: normalize_type strips db. prefix; key should be rds:r7i.4xlarge, not rds:db.r7i.4xlarge."""
    cells = [_cell(region="eu-west-1")]
    uncovered = {"rds:r7i.4xlarge": 500.0}
    cards = build_ri_type_cards(cells, uncovered, "eu-west-1")
    assert cards[0]["uncovered_monthly"] == pytest.approx(500.0)


# --- Fix Round 2: F7 + additional test gaps -----------------------------------


def test_coverage_ambiguous_multi_platform_omits_fields():
    """F7: Multiple platforms for same type/region make coverage unarbitrable; omit fields."""
    cells = [
        _cell(platform="Linux/UNIX", region="eu-west-1"),
        _cell(platform="Windows", region="eu-west-1"),
    ]
    uncovered = {"rds:r7i.4xlarge": 500.0}
    cards = build_ri_type_cards(cells, uncovered, "eu-west-1")
    # Both cards should lack coverage (ambiguous allocation)
    for card in cards:
        assert "uncovered_monthly" not in card
        assert "coverage_pct" not in card


def test_sp_risk_pct_omitted_when_ondemand_zero():
    """SP risk_pct omitted when estimated_ondemand_monthly <= 0 (fail-closed)."""
    cells = [
        {"sp_type": "COMPUTE_SP", "term": "1yr", "payment": "No Upfront",
         "hourly_commitment": 1.0, "monthly_savings": 100.0, "savings_pct": 50.0,
         "upfront": 0.0, "estimated_ondemand_monthly": 0.0},
    ]
    cards = build_sp_cards(cells)
    assert "risk_pct" not in cards[0]


def test_sp_cards_mutations_do_not_affect_originals():
    """SP cards must not mutate the original cell dicts passed in."""
    import copy
    original_cells = [
        {"sp_type": "COMPUTE_SP", "term": "1yr", "payment": "No Upfront",
         "hourly_commitment": 1.0, "monthly_savings": 500.0, "savings_pct": 20.0,
         "upfront": 0.0, "estimated_ondemand_monthly": 2500.0},
    ]
    cells_copy = copy.deepcopy(original_cells)
    build_sp_cards(original_cells)
    assert original_cells == cells_copy
