"""Tests for services/commitment_scenarios.py — pure-logic card math."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.commitment_scenarios import (
    RI_SERVICES,
    SP_TYPES,
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
