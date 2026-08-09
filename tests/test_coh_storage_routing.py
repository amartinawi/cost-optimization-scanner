"""Tranche 4 — the four real CoH ResourceTypes that had no route.

``RdsDbInstanceStorage`` / ``AuroraDbClusterStorage`` and
``DynamoDbReservedCapacity`` / ``MemoryDbReservedInstances`` are all in AWS's
25-value ResourceType enum, but the orchestrator ``type_map`` had no entry for
any of them, so their AWS-computed dollars fell through to ``unbucketed_types``
and were dropped.

Routing the storage types is not a pure addition: the RDS and Aurora adapters
treat *any* CoH rec on a resource as authority to suppress that resource's
local levers. "Shrink this DB's volume" and "disable this DB's Multi-AZ" are
different remediations, so a storage rec must count WITHOUT suppressing. These
tests pin both halves.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services._coh_dedup import COH_STORAGE_TYPES, is_storage_coh_rec
from services.commitment_scenarios import _COH_RI_MATCH, merge_coh_concurrence
from services.rds_logic import resolve_rds_findings

_ARN = "arn:aws:rds:us-east-1:123456789012:db:prod-mysql"


def _coh(rtype: str, savings: float = 30.0) -> dict:
    return {
        "recommendationId": f"rec-{rtype}",
        "resourceArn": _ARN,
        "resourceId": "prod-mysql",
        "currentResourceType": rtype,
        "actionType": "Rightsize",
        "estimatedMonthlySavings": savings,
    }


def _heuristic(savings: float = 120.0) -> dict:
    return {
        "resourceArn": _ARN,
        "DBInstanceIdentifier": "prod-mysql",
        "CheckCategory": "Multi-AZ Optimization",
        "EstimatedSavings": f"${savings:.2f}/month",
    }


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_storage_types_are_classified_as_storage() -> None:
    assert COH_STORAGE_TYPES == {"RdsDbInstanceStorage", "AuroraDbClusterStorage"}
    for rtype in COH_STORAGE_TYPES:
        assert is_storage_coh_rec(_coh(rtype)) is True


def test_instance_and_missing_types_are_not_storage() -> None:
    assert is_storage_coh_rec(_coh("RdsDbInstance")) is False
    assert is_storage_coh_rec({}) is False
    assert is_storage_coh_rec({"currentResourceType": None}) is False


# --------------------------------------------------------------------------- #
# Suppression semantics
# --------------------------------------------------------------------------- #
def test_instance_coh_rec_still_suppresses_the_local_lever() -> None:
    """The existing CoH > heuristic authority rule is unchanged."""
    _, _, kept_enh, _, _ = resolve_rds_findings(
        [], [_heuristic()], coh_recs=[_coh("RdsDbInstance")]
    )
    assert kept_enh == []


def test_storage_coh_rec_does_not_suppress_the_local_lever() -> None:
    """A $30 volume rec must not silently delete a $120 Multi-AZ rec."""
    for rtype in sorted(COH_STORAGE_TYPES):
        kept_coh, _, kept_enh, savings, _ = resolve_rds_findings(
            [], [_heuristic()], coh_recs=[_coh(rtype)]
        )
        assert len(kept_coh) == 1, rtype
        assert [r["CheckCategory"] for r in kept_enh] == ["Multi-AZ Optimization"], rtype
        # Both remediations are real and additive, so both are in the total.
        assert savings == 150.0, rtype


def test_a_storage_rec_alongside_an_instance_rec_still_suppresses() -> None:
    """The instance rec's authority is not weakened by a sibling storage rec."""
    _, _, kept_enh, _, _ = resolve_rds_findings(
        [],
        [_heuristic()],
        coh_recs=[_coh("RdsDbInstanceStorage"), _coh("RdsDbInstance")],
    )
    assert kept_enh == []


def test_aurora_excludes_storage_recs_from_its_covered_set() -> None:
    """Mirrors the adapter's inline loop: the guard must precede the id read."""
    import services.adapters.aurora as aurora_mod

    src = Path(aurora_mod.__file__).read_text()
    loop = src.split('cost_hub_splits", {}).get("rds", []):', 1)[1][:400]
    assert "is_storage_coh_rec(r)" in loop
    assert loop.index("is_storage_coh_rec(r)") < loop.index("normalize_rds_arn")


# --------------------------------------------------------------------------- #
# Reserved-capacity routing
# --------------------------------------------------------------------------- #
def _card(service: str, savings: float) -> dict:
    return {
        "card_kind": "ri_type",
        "service": service,
        "monthly_savings": savings,
        "region": "us-east-1",
    }


def _ri_rec(rtype: str, savings: float) -> dict:
    return {
        "actionType": "PurchaseReservedInstances",
        "currentResourceType": rtype,
        "estimatedMonthlySavings": savings,
        "region": "us-east-1",
    }


def test_dynamodb_reserved_capacity_merges_into_its_card() -> None:
    """RI_SERVICES builds a DynamoDB card, so without the _COH_RI_MATCH entry
    the same purchase rendered twice."""
    assert "DynamoDB" in _COH_RI_MATCH
    cards, matched = merge_coh_concurrence(
        [_card("DynamoDB", 500.0)], [_ri_rec("DynamoDbReservedCapacity", 90.0)]
    )
    assert matched == [0]
    assert cards[0]["coh_concurs_monthly"] == 90.0


def test_memorydb_reserved_instances_stays_unmatched() -> None:
    """CE builds no MemoryDB card, so the rec has nothing to merge into and
    must survive for the standalone (advisory) CoH source."""
    cards, matched = merge_coh_concurrence(
        [_card("DynamoDB", 500.0)], [_ri_rec("MemoryDbReservedInstances", 40.0)]
    )
    assert matched == []
    assert "coh_concurs_monthly" not in cards[0]


def test_storage_rec_is_never_demoted_by_an_rds_reservation() -> None:
    """An RDS RI covers instance hours, not storage. The demotion path keys off
    coh_resource_type(), which finds no instance class on a storage rec — so a
    heavily-reserved account must not zero out its storage savings."""
    from services.commitment_coverage import CommitmentCoverage, coh_resource_type, demote_coh_by_commitment

    storage_rec = dict(
        _coh("RdsDbInstanceStorage"),
        currentResourceDetails={
            "rdsDbInstanceStorage": {
                "configuration": {
                    "storageType": "gp2",
                    "allocatedStorageInGb": 500,
                    "iops": 3000,
                }
            }
        },
    )
    # "storageType" must not be mistaken for an instance class.
    assert coh_resource_type(storage_rec) == ""

    coverage = CommitmentCoverage(rds_ri_families=frozenset({"db.r5"}))
    assert coverage.has_any_commitment
    counted, demoted = demote_coh_by_commitment(
        [storage_rec], coverage, "rds", lambda r: float(r["estimatedMonthlySavings"])
    )
    assert len(counted) == 1 and demoted == []


def test_rds_does_not_publish_storage_ids_to_aurora() -> None:
    """RdsModule publishes its CoH/CO-covered ids for the Aurora adapter to
    suppress against. Leaking a storage rec's id there defeats aurora's own
    guard through the back door, so BOTH paths must skip it."""
    import services.adapters.rds as rds_mod

    src = Path(rds_mod.__file__).read_text()
    loop = src.split("for r in coh_kept + co_kept:", 1)[1][:500]
    assert "is_storage_coh_rec(r)" in loop
    assert loop.index("is_storage_coh_rec(r)") < loop.index("normalize_rds_arn")
