"""Unit tests for the RDS adapter's cross-source savings resolution.

The per-resource de-duplication logic moved from the adapter's
``_aggregate_rds_savings`` into the pure ``services.rds_logic.resolve_rds_findings``
(audit H3). These tests assert the savings arithmetic and that the kept
recommendations equal the counted total (counted == rendered).
"""

from __future__ import annotations

import pytest

from services.rds_logic import resolve_rds_findings


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


def _enhanced_rec(arn: str, savings_str: str, category: str = "Multi-AZ Optimization") -> dict:
    """Build an enhanced-check-shaped rec."""
    return {"resourceArn": arn, "EstimatedSavings": savings_str, "CheckCategory": category}


def _savings(co, enhanced) -> float:
    """Convenience: return just the total savings from the resolver."""
    _coh, _co, _enh, total_savings, _count = resolve_rds_findings(co, enhanced)
    return total_savings


class TestResolveRdsSavings:
    def test_single_resource_keeps_max_from_one_source(self):
        co = [_co_rec("arn:aws:rds:us-east-1:1:db:mydb", 30.0)]
        enhanced = [_enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$10.00/month")]
        # Same DB instance: Compute Optimizer ($30) wins over enhanced ($10).
        assert _savings(co, enhanced) == pytest.approx(30.0)

    def test_single_resource_keeps_max_from_other_source(self):
        co = [_co_rec("arn:aws:rds:us-east-1:1:db:mydb", 10.0)]
        enhanced = [_enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$53.00/month")]
        # Same DB instance: enhanced ($53) wins over CO ($10).
        assert _savings(co, enhanced) == pytest.approx(53.0)

    def test_three_recs_one_resource_picks_max_not_sum(self):
        # Multi-AZ + RI + Schedule all firing for the same DB; resolver picks max.
        co = [_co_rec("arn:aws:rds:us-east-1:1:db:mydb", 0.0)]
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$53.00/month with single-AZ"),
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$21.00/month", "Non-Production Scheduling"),
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$34.00/month", "Backup Retention Optimization"),
        ]
        # max(0, 53, 21, 34) = 53, NOT 108.
        assert _savings(co, enhanced) == pytest.approx(53.0)

    def test_separate_resources_sum_independently(self):
        co = [
            _co_rec("arn:aws:rds:us-east-1:1:db:db1", 30.0),
            _co_rec("arn:aws:rds:us-east-1:1:db:db2", 20.0),
        ]
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:db1", "$5.00/month"),
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:db2", "$25.00/month"),
        ]
        # db1: max(30, 5) = 30; db2: max(20, 25) = 25; total = 55.
        assert _savings(co, enhanced) == pytest.approx(55.0)

    def test_snapshot_arn_does_not_dedup_with_db_arn(self):
        co: list[dict] = []
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$10.00/month"),
            _enhanced_rec("arn:aws:rds:us-east-1:1:snapshot:mysnap", "$15.00/month", "Old RDS Snapshots"),
        ]
        assert _savings(co, enhanced) == pytest.approx(25.0)

    def test_percentage_only_string_contributes_zero(self):
        co: list[dict] = []
        enhanced = [_enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "~50% of instance cost")]
        assert _savings(co, enhanced) == 0.0

    def test_rec_without_arn_kept_independently(self):
        # Recs without resourceArn cannot collide on a shared key; each is kept.
        co: list[dict] = []
        enhanced = [
            {"EstimatedSavings": "$10.00/month", "CheckCategory": "Multi-AZ Optimization"},
            {"EstimatedSavings": "$20.00/month", "CheckCategory": "Multi-AZ Optimization"},
        ]
        assert _savings(co, enhanced) == pytest.approx(30.0)

    def test_reserved_instances_excluded_from_savings(self):
        # RI is advisory: rendered but not summed (commitment_analysis is authoritative).
        co: list[dict] = []
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:mydb", "$40.00/month",
                          "Reserved Instance Opportunities"),
        ]
        _coh, _co, kept_enh, savings, count = resolve_rds_findings(co, enhanced)
        assert savings == 0.0          # excluded from the headline
        assert len(kept_enh) == 1      # but still rendered
        assert count == 0              # advisory: excluded from the opportunity count

    def test_counted_equals_rendered(self):
        # The recommendation count equals the number of emitted (rendered) recs.
        co = [_co_rec("arn:aws:rds:us-east-1:1:db:a", 30.0)]
        enhanced = [
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:a", "$10.00/month"),  # loses to CO -> dropped
            _enhanced_rec("arn:aws:rds:us-east-1:1:db:b", "$25.00/month"),  # kept
            _enhanced_rec("arn:aws:rds:us-east-1:1:snapshot:s", "$5.00/month", "Old RDS Snapshots"),
        ]
        _coh, kept_co, kept_enh, savings, count = resolve_rds_findings(co, enhanced)
        assert count == len(kept_co) + len(kept_enh)
        assert savings == pytest.approx(30.0 + 25.0 + 5.0)
