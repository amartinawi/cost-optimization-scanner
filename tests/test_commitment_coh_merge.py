"""Tests for merge_coh_concurrence's RI-branch instance-type matching (M6,
final-review fix wave 2026-08-08).

Split out of tests/test_commitment_scenarios.py to keep that file under the
repo's 800-line file cap; see that file for the base card-builder tests and
the pre-existing (still-passing) merge_coh_concurrence coverage.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.commitment_scenarios import merge_coh_concurrence


def _ri_card(service="RDS", savings=1000.0, instance_type="x"):
    return {"card_kind": "ri_type", "service": service, "instance_type": instance_type,
            "region": "eu-west-1", "Counted": False, "monthly_savings": savings,
            "scenarios": [], "recommended_scenario": 0}


def test_coh_merge_ri_typed_rec_matches_correct_card_not_richest():
    """A CoH RI rec that names an instance type (via recommendedResourceSummary,
    mirroring reporter_phase_b._coh_recommended_scenario's nested-summary
    shape) must land on the matching card, never the richest card of the
    service."""
    richest = _ri_card("RDS", 5000.0, instance_type="db.r7i.4xlarge")
    poorer = _ri_card("RDS", 100.0, instance_type="db.m6i.large")
    coh = [{
        "actionType": "PurchaseReservedInstances",
        "currentResourceType": "RdsReservedInstances",
        "estimatedMonthlySavings": 90.0,
        "recommendedResourceSummary": {"rdsReservedInstances": {"instanceType": "db.m6i.large"}},
    }]
    merged, matched = merge_coh_concurrence([richest, poorer], coh)
    poorer_out = next(c for c in merged if c["instance_type"] == "db.m6i.large")
    richest_out = next(c for c in merged if c["instance_type"] == "db.r7i.4xlarge")
    assert poorer_out["coh_concurs_monthly"] == pytest.approx(90.0)
    assert "coh_concurs_monthly" not in richest_out
    assert matched == [0]


def test_coh_merge_ri_typed_rec_no_matching_card_stays_unmatched():
    """A CoH RI rec naming a type absent from every built card must stay
    unmatched (renders standalone), never fall back to the richest card."""
    card = _ri_card("RDS", 1000.0)  # instance_type == "x"
    coh = [{
        "actionType": "PurchaseReservedInstances",
        "currentResourceType": "RdsReservedInstances",
        "estimatedMonthlySavings": 500.0,
        "recommendedResourceSummary": {"rdsReservedInstances": {"instanceType": "db.r7i.4xlarge"}},
    }]
    merged, matched = merge_coh_concurrence([card], coh)
    assert "coh_concurs_monthly" not in merged[0]
    assert matched == []


def test_coh_merge_ri_untyped_rec_still_uses_service_level_richest():
    """A bare-summary rec (no recognizable type) keeps the pre-existing
    service-level richest-card behavior — this is the shape all the prior
    merge_coh_concurrence tests use, so it must not regress."""
    richest = _ri_card("RDS", 5000.0)
    poorer = _ri_card("RDS", 100.0)
    coh = [{"actionType": "PurchaseReservedInstances", "currentResourceType": "RdsReservedInstances",
            "estimatedMonthlySavings": 90.0}]
    merged, matched = merge_coh_concurrence([poorer, richest], coh)
    richest_out = next(c for c in merged if c["monthly_savings"] == 5000.0)
    poorer_out = next(c for c in merged if c["monthly_savings"] == 100.0)
    assert richest_out["coh_concurs_monthly"] == pytest.approx(90.0)
    assert "coh_concurs_monthly" not in poorer_out
    assert matched == [0]


def test_coh_merge_ri_typed_rec_wrong_service_stays_unmatched():
    """A named type that only matches a DIFFERENT service's card must not
    leak across services (sanity check: service-level filter still applies
    before the type-level narrowing)."""
    rds_card = _ri_card("RDS", 1000.0, instance_type="db.m6i.large")
    coh = [{
        "actionType": "PurchaseReservedInstances",
        "currentResourceType": "EC2ReservedInstances",
        "estimatedMonthlySavings": 500.0,
        "recommendedResourceSummary": {"ec2ReservedInstances": {"instanceType": "db.m6i.large"}},
    }]
    merged, matched = merge_coh_concurrence([rds_card], coh)
    assert "coh_concurs_monthly" not in merged[0]
    assert matched == []


# --- Live CoH payload shapes (pinned 2026-08-09 on afs-prod) -----------------


def test_live_lowercase_spelling_matches_ec2_card():
    cards = [_ri_card("EC2", 1000.0)]
    coh = [{"actionType": "PurchaseReservedInstances",
            "currentResourceType": "Ec2ReservedInstances",   # live spelling
            "estimatedMonthlySavings": 900.0}]
    merged, matched = merge_coh_concurrence(cards, coh)
    assert matched == [0]
    assert merged[0]["coh_concurs_monthly"] == pytest.approx(900.0)


def test_detailed_shape_configuration_type_lands_on_matching_card():
    small = dict(_ri_card("EC2", 100.0), instance_type="c5a.4xlarge")
    rich = dict(_ri_card("EC2", 5000.0), instance_type="m5.large")
    coh = [{"actionType": "PurchaseReservedInstances",
            "currentResourceType": "Ec2ReservedInstances",
            "estimatedMonthlySavings": 400.0,
            "recommendedResourceDetails": {"ec2ReservedInstances": {"configuration": {
                "instanceType": "c5a.4xlarge", "term": "ThreeYears"}}}}]
    merged, matched = merge_coh_concurrence([small, rich], coh)
    assert matched == [0]
    assert merged[0].get("coh_concurs_monthly") == pytest.approx(400.0)   # typed match
    assert "coh_concurs_monthly" not in merged[1]                          # not the richest


def test_string_summary_type_token_lands_on_matching_card():
    small = dict(_ri_card("EC2", 100.0), instance_type="r6i.4xlarge")
    rich = dict(_ri_card("EC2", 5000.0), instance_type="m5.large")
    coh = [{"actionType": "PurchaseReservedInstances",
            "currentResourceType": "Ec2ReservedInstances",
            "estimatedMonthlySavings": 1985.77,
            "recommendedResourceSummary":
                "4 r6i.4xlarge Windows (Amazon VPC) in eu-west-1 with three years term (AllUpfront)"}]
    merged, matched = merge_coh_concurrence([small, rich], coh)
    assert matched == [0]
    assert merged[0].get("coh_concurs_monthly") == pytest.approx(1985.77)
    assert "coh_concurs_monthly" not in merged[1]


def test_string_summary_type_absent_from_cards_stays_unmatched():
    cards = [dict(_ri_card("EC2", 5000.0), instance_type="m5.large")]
    coh = [{"actionType": "PurchaseReservedInstances",
            "currentResourceType": "Ec2ReservedInstances",
            "estimatedMonthlySavings": 2427.28,
            "recommendedResourceSummary":
                "8 c5a.4xlarge Red Hat Enterprise Linux in eu-west-1 with three years term (AllUpfront)"}]
    merged, matched = merge_coh_concurrence(cards, coh)
    assert matched == []                                # renders standalone, no guessing
    assert "coh_concurs_monthly" not in merged[0]


def test_concurrence_narrows_by_rec_region_across_same_type_cards():
    """Live af-south-1 audit: the region-filtered prefetch fetched af-south-1's
    cache.r7g.large rec ($841.83) but type-only narrowing let max() hand it to
    the RICHER eu-west-1 card of the same type ($934.25)."""
    af = dict(_ri_card("ElastiCache", 841.83), instance_type="cache.r7g.large", region="af-south-1")
    eu = dict(_ri_card("ElastiCache", 934.25), instance_type="cache.r7g.large", region="eu-west-1")
    coh = [{"actionType": "PurchaseReservedInstances",
            "currentResourceType": "ElastiCacheReservedInstances",
            "estimatedMonthlySavings": 841.83, "region": "af-south-1",
            "recommendedResourceSummary":
                "9 cache.r7g.large Valkey in af-south-1 with three years term (AllUpfront)"}]
    merged, matched = merge_coh_concurrence([eu, af], coh)
    assert matched == [0]
    assert merged[1].get("coh_concurs_monthly") == pytest.approx(841.83)  # af card
    assert "coh_concurs_monthly" not in merged[0]                          # not richer eu card


def test_concurrence_rec_region_without_matching_card_stays_unmatched():
    cards = [dict(_ri_card("ElastiCache", 934.25), instance_type="cache.r7g.large", region="eu-west-1")]
    coh = [{"actionType": "PurchaseReservedInstances",
            "currentResourceType": "ElastiCacheReservedInstances",
            "estimatedMonthlySavings": 100.0, "region": "ap-south-1"}]
    merged, matched = merge_coh_concurrence(cards, coh)
    assert matched == []
    assert "coh_concurs_monthly" not in merged[0]
