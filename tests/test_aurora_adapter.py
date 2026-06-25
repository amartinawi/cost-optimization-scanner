"""Tests for the Aurora provisioned-instance rightsizing + Graviton checks."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.adapters.aurora import _check_provisioned_instances
from services.aurora_logic import (
    graviton_equivalent,
    is_graviton_family,
    parse_instance_class,
    rightsize_target_size,
)


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #
def test_parse_instance_class():
    assert parse_instance_class("db.r5.8xlarge") == ("db.r5", "8xlarge", 32)
    assert parse_instance_class("db.r6g.2xlarge") == ("db.r6g", "2xlarge", 8)
    assert parse_instance_class("garbage") is None


def test_is_graviton_family():
    assert is_graviton_family("db.r6g") and is_graviton_family("db.t4g") and is_graviton_family("db.x2g")
    assert not is_graviton_family("db.r5") and not is_graviton_family("db.r6i")


def test_graviton_equivalent():
    assert graviton_equivalent("db.r5") == "db.r6g"
    assert graviton_equivalent("db.m6i") == "db.m6g"
    assert graviton_equivalent("db.r6g") is None  # already graviton


def test_rightsize_target_peak_aware():
    # 32 vCPU, peak 20% -> needs 32*0.20*1.2 = 7.7 vCPU -> 2xlarge (8).
    assert rightsize_target_size(32, 20.0) == "2xlarge"
    # peak 44% -> needs 17 vCPU -> 8xlarge (=current 32? no, 4xlarge=16<17 -> 8xlarge=current) -> None.
    assert rightsize_target_size(32, 44.0) is None
    # busy instance, no downsize.
    assert rightsize_target_size(32, 95.0) is None


# --------------------------------------------------------------------------- #
# Adapter check
# --------------------------------------------------------------------------- #
def _pricing():
    pe = MagicMock()
    prices = {
        "db.r5.8xlarge": 3737.60, "db.r5.2xlarge": 934.40,
        "db.r6g.8xlarge": 3344.86, "db.r6g.2xlarge": 836.58,
    }
    pe.get_rds_instance_monthly_price.side_effect = lambda engine, cls: prices.get(cls, 0.0)
    return pe


def _cw(avg, peak):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {
        "Datapoints": [{"Average": avg, "Maximum": peak}]
    }
    return cw


def _ctx(rds, cw):
    ns = SimpleNamespace(region="eu-west-1", pricing_multiplier=1.0, fast_mode=False, pricing_engine=_pricing())
    ns.client = lambda n, region=None: {"rds": rds, "cloudwatch": cw}.get(n)
    ns.warn = MagicMock()
    ns.permission_issue = MagicMock()
    return ns


def _rds(instances):
    rds = MagicMock()
    pag = MagicMock()
    pag.paginate.return_value = [{"DBInstances": instances}]
    rds.get_paginator.return_value = pag
    return rds


def test_idle_reader_rightsized_then_graviton():
    rds = _rds([{
        "DBInstanceIdentifier": "reader", "DBInstanceClass": "db.r5.8xlarge",
        "Engine": "aurora-mysql", "DBInstanceStatus": "available",
    }])
    cw = _cw(avg=1.5, peak=20.5)  # idle reader -> rightsize to 2xlarge
    recs = _check_provisioned_instances(_ctx(rds, cw), rds, cw, fast_mode=False)
    by = {r["check_type"]: r for r in recs}
    assert by["instance_rightsizing"]["TargetSize"] == "db.r5.2xlarge"
    assert by["instance_rightsizing"]["monthly_savings"] == round(3737.60 - 934.40, 2)
    # Graviton priced on the RIGHTSIZED class (2xlarge), not the original 8xlarge.
    assert by["instance_graviton"]["CurrentSize"] == "db.r5.2xlarge"
    assert by["instance_graviton"]["monthly_savings"] == round(934.40 - 836.58, 2)


def test_busy_writer_graviton_only():
    rds = _rds([{
        "DBInstanceIdentifier": "writer", "DBInstanceClass": "db.r5.8xlarge",
        "Engine": "aurora-mysql", "DBInstanceStatus": "available",
    }])
    cw = _cw(avg=15.2, peak=44.4)  # peak too high to downsize
    recs = _check_provisioned_instances(_ctx(rds, cw), rds, cw, fast_mode=False)
    types = {r["check_type"] for r in recs}
    assert types == {"instance_graviton"}  # no rightsizing
    grav = recs[0]
    assert grav["CurrentSize"] == "db.r5.8xlarge"
    assert grav["monthly_savings"] == round(3737.60 - 3344.86, 2)


def test_fast_mode_skips_rightsizing_keeps_graviton():
    rds = _rds([{
        "DBInstanceIdentifier": "reader", "DBInstanceClass": "db.r5.8xlarge",
        "Engine": "aurora-mysql", "DBInstanceStatus": "available",
    }])
    cw = _cw(avg=1.5, peak=20.5)
    recs = _check_provisioned_instances(_ctx(rds, cw), rds, cw, fast_mode=True)
    types = {r["check_type"] for r in recs}
    assert types == {"instance_graviton"}  # rightsizing needs metrics -> skipped
    assert recs[0]["CurrentSize"] == "db.r5.8xlarge"  # graviton on full size


def test_non_aurora_and_graviton_instances_skipped():
    rds = _rds([
        {"DBInstanceIdentifier": "mysql", "DBInstanceClass": "db.r5.large", "Engine": "mysql", "DBInstanceStatus": "available"},
        {"DBInstanceIdentifier": "already-arm", "DBInstanceClass": "db.r6g.large", "Engine": "aurora-mysql", "DBInstanceStatus": "available"},
    ])
    cw = _cw(avg=80, peak=95)
    recs = _check_provisioned_instances(_ctx(rds, cw), rds, cw, fast_mode=False)
    # mysql excluded (not aurora); r6g excluded from graviton (already arm) and busy (no rightsize).
    assert recs == []
