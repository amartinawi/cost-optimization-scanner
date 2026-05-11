"""Unit tests for the RDS adapter's per-resource savings aggregation."""

from __future__ import annotations

import pytest

from services.adapters.rds import _aggregate_rds_savings


def _co_rec(arn: str, value: float) -> dict:
    """Build a Compute-Optimizer-shaped rec with one rank-1 savingsOpportunity."""
    return {
        "resourceArn": arn,
        "instanceRecommendationOptions": [
            {
                "rank": 1,
                "savingsOpportunity": {
                    "estimatedMonthlySavings": {"currency": "USD", "value": value},
                },
            },
        ],
    }


def _enhanced_rec(arn: str, savings_str: str) -> dict:
    """Build an enhanced-check-shaped rec."""
    return {"resourceArn": arn, "EstimatedSavings": savings_str}


class TestAggregateRdsSavings:
    def test_single_resource_keeps_max_from_one_source(self):
        co = [_co_rec("arn:aws:rds:us-east-1:1:db:mydb", 30.0)]
        enhanced = [_enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$10.00/month")]
        # Same DB instance: Compute Optimizer ($30) wins over enhanced ($10).
        assert _aggregate_rds_savings(co, enhanced) == pytest.approx(30.0)

    def test_single_resource_keeps_max_from_other_source(self):
        co = [_co_rec("arn:aws:rds:us-east-1:1:db:mydb", 10.0)]
        enhanced = [_enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$53.00/month")]
        # Same DB instance: enhanced ($53) wins over CO ($10).
        assert _aggregate_rds_savings(co, enhanced) == pytest.approx(53.0)

    def test_three_recs_one_resource_picks_max_not_sum(self):
        # The headline-inflation scenario the audit called out: Multi-AZ + RI + Schedule
        # all firing for the same DB; aggregator must pick max, not sum.
        co = [_co_rec("arn:aws:rds:us-east-1:1:db:mydb", 0.0)]
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$53.00/month with single-AZ"),
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$21.00/month with 1-yr RI"),
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$34.00/month with shutdown"),
        ]
        # max(0, 53, 21, 34) = 53, NOT 108.
        assert _aggregate_rds_savings(co, enhanced) == pytest.approx(53.0)

    def test_separate_resources_sum_independently(self):
        # Different DBs do not dedup against each other.
        co = [
            _co_rec("arn:aws:rds:us-east-1:1:db:db1", 30.0),
            _co_rec("arn:aws:rds:us-east-1:1:db:db2", 20.0),
        ]
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:db1", "$5.00/month"),
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:db2", "$25.00/month"),
        ]
        # db1: max(30, 5) = 30; db2: max(20, 25) = 25; total = 55.
        assert _aggregate_rds_savings(co, enhanced) == pytest.approx(55.0)

    def test_snapshot_arn_does_not_dedup_with_db_arn(self):
        # Snapshot arns are in a different namespace (snapshot vs db); the aggregator
        # should treat them as independent resources.
        co: list[dict] = []
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$10.00/month"),
            _enhanced_rec("arn:aws:rds:us-east-1:1:snapshot:mysnap", "$15.00/month"),
        ]
        assert _aggregate_rds_savings(co, enhanced) == pytest.approx(25.0)

    def test_percentage_only_string_contributes_zero(self):
        # Post-L2-004 fix: parse_dollar_savings returns 0 for percentage strings.
        co: list[dict] = []
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "~50% of instance cost"),
        ]
        assert _aggregate_rds_savings(co, enhanced) == 0.0

    def test_rec_without_arn_falls_to_untagged_pool(self):
        # Recs without resourceArn cannot be deduped; sum into the untagged pool.
        co: list[dict] = []
        enhanced = [
            {"EstimatedSavings": "$10.00/month"},
            {"EstimatedSavings": "$20.00/month"},
        ]
        assert _aggregate_rds_savings(co, enhanced) == pytest.approx(30.0)
