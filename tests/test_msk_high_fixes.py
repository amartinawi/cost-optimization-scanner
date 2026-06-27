"""Unit tests for the MSK adapter HIGH cost-audit fixes (H1, H3).

Same SimpleNamespace-ctx + fake-boto3 style as ``tests/test_lambda_audit_fixes.py``
and ``tests/test_audit_fixes_counted_dollars.py``:

  - H1  The broker leg of the current-spend AuditBasis is priced from the live
        ``PricingEngine.get_msk_broker_hourly_price`` Broker-hours SKU (the real
        $0.21/broker-hr us-east-1 m5.large rate), not an EC2 proxy or a dead path.
  - H3  The storage leg uses the cluster's REAL per-broker ``VolumeSize`` carried
        on the rec by the shim; when the volume size is unknown the storage leg
        is OMITTED entirely — never defaulted to a phantom 100 GB.

  Every MSK rec stays a $0 advisory (``Counted=False``): MSK exposes no
  utilization / target-broker-size signal at scan time, so no realizable saving
  is fabricated (rejects the prior blanket 30% factor).

Validated live (AWS Pricing API, us-east-1, 2026-04):
  - AmazonMSK Broker-hours ``USE1-Kafka.m5.large`` = $0.21/broker-hr.
  - AmazonMSK provisioned storage ``USE1-Kafka.Storage.GP2`` = $0.10/GB-Mo.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import services.adapters.msk as adapter_mod
from services.adapters.msk import (
    HOURS_PER_MONTH,
    MSK_STORAGE_RATE_PER_GB_MONTH,
    MskModule,
)
from services.msk import get_enhanced_msk_checks

BROKER_HOURLY_M5_LARGE = 0.21  # live us-east-1 Broker-hours rate (validated)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePricing:
    """Records broker-price lookups; returns the live-validated rate."""

    def __init__(self, rates: dict[str, float] | None = None) -> None:
        self._rates = rates or {"m5.large": BROKER_HOURLY_M5_LARGE}
        self.calls: list[str] = []

    def get_msk_broker_hourly_price(self, instance_type: str) -> float:
        self.calls.append(instance_type)
        clean = instance_type.replace("kafka.", "")
        return self._rates.get(clean, 0.0)


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeKafkaClient:
    """Minimal boto3 ``kafka`` client driving the enhanced-checks shim."""

    def __init__(self, clusters: list[dict[str, Any]]) -> None:
        self._clusters = clusters

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "list_clusters":
            return _FakePaginator([{"ClusterInfoList": self._clusters}])
        if name == "list_clusters_v2":
            return _FakePaginator([{"ClusterInfoList": []}])
        raise ValueError(name)


def _ctx(
    kafka_client: _FakeKafkaClient | None,
    pricing_engine: Any,
    *,
    pricing_multiplier: float = 1.0,
    region: str = "us-east-1",
) -> SimpleNamespace:
    warnings: list[tuple[Any, str]] = []
    return SimpleNamespace(
        pricing_engine=pricing_engine,
        pricing_multiplier=pricing_multiplier,
        region=region,
        warnings=warnings,
        client=lambda name, region=None: kafka_client if name == "kafka" else None,
        warn=lambda msg, service=None: warnings.append((service, msg)),
    )


def _cluster(
    *,
    name: str = "c1",
    state: str = "ACTIVE",
    instance_type: str | None = "kafka.m5.large",
    brokers: int = 3,
    volume_size: int | None = 1500,
) -> dict[str, Any]:
    bng: dict[str, Any] = {}
    if instance_type is not None:
        bng["InstanceType"] = instance_type
    if volume_size is not None:
        bng["StorageInfo"] = {"EBSStorageInfo": {"VolumeSize": volume_size}}
    return {
        "ClusterName": name,
        "State": state,
        "NumberOfBrokerNodes": brokers,
        "BrokerNodeGroupInfo": bng,
    }


def _recs(findings: Any) -> tuple[dict[str, Any], ...]:
    return findings.sources["enhanced_checks"].recommendations


def _by_category(recs: tuple[dict[str, Any], ...], category: str) -> dict[str, Any]:
    return next(r for r in recs if r["CheckCategory"] == category)


# --------------------------------------------------------------------------- #
# H3 (shim) — BrokerStorageGB is set from the REAL VolumeSize, omitted if unknown
# --------------------------------------------------------------------------- #
def test_shim_sets_broker_storage_gb_from_real_volume() -> None:
    client = _FakeKafkaClient([_cluster(volume_size=1500)])
    ctx = _ctx(client, _FakePricing())

    result = get_enhanced_msk_checks(ctx)
    recs = result["recommendations"]

    rightsizing = _by_category(tuple(recs), "Cluster Rightsizing")
    storage = _by_category(tuple(recs), "MSK Storage Optimization")
    assert rightsizing["BrokerStorageGB"] == 1500
    assert rightsizing["NumberOfBrokerNodes"] == 3
    assert storage["BrokerStorageGB"] == 1500
    assert storage["NumberOfBrokerNodes"] == 3


def test_shim_omits_broker_storage_gb_when_unknown() -> None:
    # Cluster reports no StorageInfo at all → VolumeSize unknown.
    client = _FakeKafkaClient([_cluster(volume_size=None)])
    ctx = _ctx(client, _FakePricing())

    result = get_enhanced_msk_checks(ctx)
    rightsizing = _by_category(tuple(result["recommendations"]), "Cluster Rightsizing")

    # H3: no phantom default — the key is simply absent.
    assert "BrokerStorageGB" not in rightsizing


# --------------------------------------------------------------------------- #
# H1 — broker leg priced from the live Broker-hours SKU
# --------------------------------------------------------------------------- #
def test_scan_broker_leg_uses_live_broker_hours_rate() -> None:
    pricing = _FakePricing()
    client = _FakeKafkaClient([_cluster(brokers=3, volume_size=1500)])
    findings = MskModule().scan(_ctx(client, pricing))

    rightsizing = _by_category(_recs(findings), "Cluster Rightsizing")
    basis = rightsizing["AuditBasis"]

    # The fixed pricing method was actually consumed (msk H1).
    assert pricing.calls == ["kafka.m5.large"]
    assert basis["broker_hourly_rate"] == BROKER_HOURLY_M5_LARGE
    assert basis["broker_count"] == 3
    expected_broker = round(BROKER_HOURLY_M5_LARGE * HOURS_PER_MONTH * 3, 2)
    assert basis["broker_monthly_cost"] == expected_broker  # 0.21*730*3 = 459.9
    assert basis["broker_monthly_cost"] == 459.9


# --------------------------------------------------------------------------- #
# H3 — storage leg uses the real VolumeSize, never a phantom 100 GB
# --------------------------------------------------------------------------- #
def test_scan_storage_leg_uses_real_volume_not_phantom_100() -> None:
    client = _FakeKafkaClient([_cluster(brokers=3, volume_size=1500)])
    findings = MskModule().scan(_ctx(client, _FakePricing()))

    rightsizing = _by_category(_recs(findings), "Cluster Rightsizing")
    basis = rightsizing["AuditBasis"]

    expected_storage = round(1500 * MSK_STORAGE_RATE_PER_GB_MONTH * 3, 2)
    phantom_storage = round(100 * MSK_STORAGE_RATE_PER_GB_MONTH * 3, 2)
    assert basis["storage_gb_per_broker"] == 1500
    assert basis["storage_monthly_cost"] == expected_storage  # 1500*0.10*3 = 450.0
    assert basis["storage_monthly_cost"] == 450.0
    assert basis["storage_monthly_cost"] != phantom_storage  # not the old 30.0
    # current spend = broker + storage, both from evidence.
    assert basis["current_monthly_cost"] == round(459.9 + 450.0, 2)


def test_scan_storage_leg_omitted_when_volume_unknown() -> None:
    client = _FakeKafkaClient([_cluster(brokers=3, volume_size=None)])
    findings = MskModule().scan(_ctx(client, _FakePricing()))

    rightsizing = _by_category(_recs(findings), "Cluster Rightsizing")
    basis = rightsizing["AuditBasis"]

    # H3: unknown size → omit the storage leg; do NOT invent 100 GB ($30/mo).
    assert basis["storage_leg"] == "omitted — per-broker VolumeSize unknown"
    assert "storage_monthly_cost" not in basis
    # current spend is broker-only, with no phantom storage added in.
    assert basis["current_monthly_cost"] == 459.9


def test_scan_storage_only_rec_prices_storage_leg_alone() -> None:
    client = _FakeKafkaClient([_cluster(brokers=3, volume_size=2000)])
    findings = MskModule().scan(_ctx(client, _FakePricing()))

    storage = _by_category(_recs(findings), "MSK Storage Optimization")
    basis = storage["AuditBasis"]

    # No InstanceType on this rec → broker leg absent, storage leg present.
    assert "broker_monthly_cost" not in basis
    assert basis["storage_monthly_cost"] == round(2000 * 0.10 * 3, 2)  # 600.0
    assert basis["current_monthly_cost"] == 600.0


def test_scan_storage_leg_region_scaled_for_module_constant() -> None:
    # The storage rate is a module constant → region-scaled via pricing_multiplier.
    client = _FakeKafkaClient([_cluster(brokers=2, volume_size=1500)])
    ctx = _ctx(client, _FakePricing(), pricing_multiplier=1.10)
    findings = MskModule().scan(ctx)

    rightsizing = _by_category(_recs(findings), "Cluster Rightsizing")
    basis = rightsizing["AuditBasis"]
    assert basis["storage_monthly_cost"] == round(1500 * 0.10 * 2 * 1.10, 2)  # 330.0


# --------------------------------------------------------------------------- #
# Dollar honesty — every MSK rec is a $0 advisory; nothing fed to the headline
# --------------------------------------------------------------------------- #
def test_scan_all_recs_are_zero_advisories() -> None:
    client = _FakeKafkaClient([_cluster(brokers=3, volume_size=1500)])
    findings = MskModule().scan(_ctx(client, _FakePricing()))

    recs = _recs(findings)
    assert recs  # rightsizing + storage
    for rec in recs:
        assert rec["Counted"] is False
        assert rec["EstimatedMonthlySavings"] == 0.0
        assert rec["EstimatedSavings"].startswith("$0.00/month — advisory")
        assert rec["AuditBasis"]["realizable_monthly_savings"] == 0.0
    assert findings.total_monthly_savings == 0.0


def test_scan_does_not_mutate_source_recs(monkeypatch) -> None:
    source = [
        {
            "ClusterName": "c1",
            "InstanceType": "kafka.m5.large",
            "NumberOfBrokerNodes": 3,
            "BrokerStorageGB": 1500,
            "CheckCategory": "Cluster Rightsizing",
            "EstimatedSavings": "$200/month potential",
        }
    ]
    monkeypatch.setattr(
        adapter_mod,
        "get_enhanced_msk_checks",
        lambda ctx: {"recommendations": source},
    )
    findings = MskModule().scan(_ctx(None, _FakePricing()))

    # Immutability: the shim's dict is untouched; the adapter emits a new object.
    assert "Counted" not in source[0]
    assert "AuditBasis" not in source[0]
    assert source[0]["EstimatedSavings"] == "$200/month potential"
    emitted = _recs(findings)[0]
    assert emitted is not source[0]
    assert emitted["Counted"] is False
