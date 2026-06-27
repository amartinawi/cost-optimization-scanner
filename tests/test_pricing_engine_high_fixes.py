"""HIGH cost-correctness fixes for core/pricing_engine.py.

Covers the four pricing-engine HIGH findings (each rate live-validated against
the AWS Pricing API on 2026-06, us-east-1):

* aurora H1 — ``get_aurora_io_storage_premium_per_gb`` returns the
  ``IO-OptimizedStorageUsage − StorageUsage`` delta ($0.225 − $0.10 =
  $0.125/GB-Mo), not the old hardcoded $0.025.
* aurora H2 — ``get_aurora_io_instance_premium_monthly`` exposes the per-member
  I/O-Optimized instance premium (~30%: db.r6g.large $0.338/hr vs $0.260/hr),
  threading the ``aurora_io_optimized`` storage-mode pin into the lookup.
* msk H1 — ``_fetch_msk_broker_price`` now pins ``computeFamily`` /
  ``productFamily`` / ``operation=RunBroker`` (the old ``instanceType`` filter
  was 100% dead); the fallback markup is recalibrated to EC2 × 2.19 and the
  last-ditch constant is region-scaled.
* dms H1/H2 — ``get_dms_instance_monthly_price`` pins
  ``InstanceUsg:dms.<type>`` (Single-AZ) vs ``Multi-AZUsg:dms.<type>``
  (Multi-AZ), so the rate is deterministic (was up to 2x off).

These tests drive the pure pricing logic with a filter-aware fake
``pricing:GetProducts`` client that returns realistic multi-SKU pages, proving
each method selects the correct SKU rather than depending on result ordering.
"""

from __future__ import annotations

import json

import pytest

from core.pricing_engine import (
    FALLBACK_AURORA_IO_STORAGE_PREMIUM_GB_MONTH,
    FALLBACK_DMS_INSTANCE_MONTHLY,
    FALLBACK_DMS_MULTI_AZ_FACTOR,
    FALLBACK_MSK_BROKER_HOURLY,
    MSK_BROKER_OVER_EC2,
    PricingEngine,
)

HOURS_PER_MONTH = 730


def _row(usagetype: str, usd: str, *, unit: str = "Hrs", **attrs: str) -> str:
    """Serialize one AWS Pricing PriceList item (OnDemand, single dimension)."""
    product_attrs = {"usagetype": usagetype}
    product_attrs.update(attrs)
    return json.dumps(
        {
            "product": {"attributes": product_attrs},
            "terms": {
                "OnDemand": {
                    "TERM": {
                        "priceDimensions": {
                            "DIM": {"unit": unit, "pricePerUnit": {"USD": usd}}
                        }
                    }
                }
            },
        }
    )


class _FakePricing:
    """Filter-aware fake ``pricing:GetProducts`` returning realistic SKU pages.

    Branches on ServiceCode + key filter fields so the engine's deterministic
    selectors are genuinely exercised (the right SKU is chosen by usagetype
    suffix / attribute pin, not by ordering). Set ``msk_live=False`` to simulate
    a dead MSK lookup (exercises the EC2-proxy fallback).
    """

    def __init__(self, *, msk_live: bool = True, ec2_live: bool = True) -> None:
        self.msk_live = msk_live
        self.ec2_live = ec2_live
        self.calls: list[tuple[str, dict, int]] = []

    def get_products(self, ServiceCode, Filters, MaxResults=100, NextToken=None):  # noqa: N803
        f = {flt["Field"]: flt["Value"] for flt in Filters}
        self.calls.append((ServiceCode, f, MaxResults))
        return {"PriceList": self._rows_for(ServiceCode, f)}

    def _rows_for(self, service: str, f: dict) -> list[str]:
        if service == "AWSDatabaseMigrationSvc":
            # Both AZ SKUs present — the suffix selector must pick the right one.
            return [
                _row("InstanceUsg:dms.t3.medium", "0.0745", unit="Hrs"),
                _row("Multi-AZUsg:dms.t3.medium", "0.149", unit="Hrs"),
            ]
        if service == "AmazonMSK":
            if not self.msk_live:
                return []
            # Only the broker-hour SKU; note the unit is lowercase "hours".
            return [
                _row(
                    "USE1-Kafka.m5.large",
                    "0.21",
                    unit="hours",
                    computeFamily="m5.large",
                    operation="RunBroker",
                )
            ]
        if service == "AmazonEC2":
            if not self.ec2_live:
                return []
            return [_row("USE1-BoxUsage:m5.large", "0.096", unit="Hrs")]
        if service == "AmazonRDS":
            pf = f.get("productFamily")
            if pf == "Database Storage":
                return [
                    _row("Aurora:StorageUsage", "0.10", unit="GB-Mo"),
                    _row("Aurora:IO-OptimizedStorageUsage", "0.225", unit="GB-Mo"),
                    # $0.00 decoy whose usagetype must NOT match the io-opt suffix.
                    _row("Aurora:IO-OptimizedStorageUsage-LimitlessPreview", "0.0", unit="GB-Mo"),
                ]
            if pf == "Database Instance":
                if f.get("storage") == "Aurora IO Optimization Mode":
                    return [_row("InstanceUsageIOOptimized:db.r6g.large", "0.338", unit="Hrs")]
                return [_row("InstanceUsage:db.r6g.large", "0.26", unit="Hrs")]
        return []


def _engine(client: _FakePricing | None = None, *, multiplier: float = 1.0) -> PricingEngine:
    return PricingEngine("us-east-1", client or _FakePricing(), fallback_multiplier=multiplier)


class _EmptyPricing:
    """Always returns no results — exercises the fallback-constant paths."""

    def get_products(self, ServiceCode, Filters, MaxResults=100, NextToken=None):  # noqa: N803
        return {"PriceList": []}


# ── aurora H1 — I/O-Optimized STORAGE premium ────────────────────────────────


class TestAuroraIoStoragePremium:
    def test_live_premium_is_io_minus_standard(self):
        """$0.225 (IO-Optimized) − $0.10 (Standard) = $0.125/GB-Mo, not 0.025."""
        engine = _engine()
        premium = engine.get_aurora_io_storage_premium_per_gb()
        assert premium == pytest.approx(0.125, abs=1e-9)

    def test_premium_ignores_limitless_zero_decoy(self):
        """The $0.00 ``…-LimitlessPreview`` row must not be selected for io-opt."""
        client = _FakePricing()
        engine = _engine(client)
        engine.get_aurora_io_storage_premium_per_gb()
        # Both legs query Database Storage on AmazonRDS.
        rds_storage_calls = [
            c for c in client.calls if c[0] == "AmazonRDS" and c[1].get("productFamily") == "Database Storage"
        ]
        assert len(rds_storage_calls) == 2

    def test_fallback_constant_region_scaled(self):
        engine = PricingEngine("us-east-1", _EmptyPricing(), fallback_multiplier=1.5)
        premium = engine.get_aurora_io_storage_premium_per_gb()
        assert premium == pytest.approx(FALLBACK_AURORA_IO_STORAGE_PREMIUM_GB_MONTH * 1.5)

    def test_premium_is_cached(self):
        client = _FakePricing()
        engine = _engine(client)
        engine.get_aurora_io_storage_premium_per_gb()
        before = len(client.calls)
        engine.get_aurora_io_storage_premium_per_gb()
        assert len(client.calls) == before  # served from cache, no new API calls


# ── aurora H2 — I/O-Optimized INSTANCE premium ───────────────────────────────


class TestAuroraIoInstancePremium:
    def test_live_per_member_premium(self):
        """db.r6g.large Aurora MySQL: $0.338/hr − $0.260/hr = $56.94/mo (~30%)."""
        engine = _engine()
        premium = engine.get_aurora_io_instance_premium_monthly("aurora-mysql", "db.r6g.large")
        assert premium == pytest.approx((0.338 - 0.26) * HOURS_PER_MONTH, abs=1e-6)
        assert premium == pytest.approx(56.94, abs=1e-2)

    def test_premium_threads_storage_mode_pin(self):
        """Both legs run; one pins 'Aurora IO Optimization Mode', one 'EBS Only'."""
        client = _FakePricing()
        engine = _engine(client)
        engine.get_aurora_io_instance_premium_monthly("aurora-mysql", "db.r6g.large")
        storage_modes = {
            c[1].get("storage")
            for c in client.calls
            if c[0] == "AmazonRDS" and c[1].get("productFamily") == "Database Instance"
        }
        assert storage_modes == {"EBS Only", "Aurora IO Optimization Mode"}

    def test_zero_when_price_unavailable(self):
        """No fabricated premium when either rate is missing."""
        engine = PricingEngine("us-east-1", _EmptyPricing(), fallback_multiplier=1.0)
        # Empty client → both instance lookups fall back to the SAME RDS fallback
        # constant for both storage modes, so the premium nets to exactly 0.0.
        premium = engine.get_aurora_io_instance_premium_monthly("aurora-mysql", "db.r6g.large")
        assert premium == 0.0


# ── msk H1 — dead broker-price lookup ────────────────────────────────────────


class TestMskBrokerPrice:
    def test_live_broker_price_uses_compute_family_not_instance_type(self):
        client = _FakePricing()
        engine = _engine(client)
        price = engine.get_msk_broker_hourly_price("kafka.m5.large")
        assert price == pytest.approx(0.21)
        msk_calls = [c for c in client.calls if c[0] == "AmazonMSK"]
        assert msk_calls, "MSK Pricing API must be queried"
        fields = msk_calls[0][1]
        assert "computeFamily" in fields and fields["computeFamily"] == "m5.large"
        assert "instanceType" not in fields  # the old dead attribute
        assert fields.get("productFamily") == "Managed Streaming for Apache Kafka (MSK)"
        assert fields.get("operation") == "RunBroker"

    def test_fallback_markup_is_ec2_times_2_19(self):
        """Dead MSK lookup → EC2 $0.096 × 2.19 (recalibrated from the wrong 1.4)."""
        client = _FakePricing(msk_live=False, ec2_live=True)
        engine = _engine(client)
        price = engine.get_msk_broker_hourly_price("m5.large")
        assert price == pytest.approx(0.096 * MSK_BROKER_OVER_EC2)
        # The recalibrated markup is materially higher than the old 1.4x.
        assert price > 0.096 * 1.4

    def test_last_ditch_constant_region_scaled(self):
        """Both MSK and EC2 unavailable → region-scaled last-ditch constant."""
        engine = PricingEngine("us-east-1", _EmptyPricing(), fallback_multiplier=2.0)
        price = engine.get_msk_broker_hourly_price("m5.large")
        assert price == pytest.approx(FALLBACK_MSK_BROKER_HOURLY * 2.0)


# ── dms H1/H2 — non-deterministic Single-AZ vs Multi-AZ SKU ───────────────────


class TestDmsInstancePrice:
    def test_single_az_selects_instance_usg_sku(self):
        client = _FakePricing()
        engine = _engine(client)
        price = engine.get_dms_instance_monthly_price("dms.t3.medium", multi_az=False)
        assert price == pytest.approx(0.0745 * HOURS_PER_MONTH, abs=1e-6)

    def test_multi_az_selects_multi_az_usg_sku(self):
        client = _FakePricing()
        engine = _engine(client)
        price = engine.get_dms_instance_monthly_price("dms.t3.medium", multi_az=True)
        assert price == pytest.approx(0.149 * HOURS_PER_MONTH, abs=1e-6)

    def test_multi_az_is_exactly_double_single_az(self):
        """The whole point of the fix: AZ choice changes the rate (here 2x)."""
        engine = _engine()
        single = engine.get_dms_instance_monthly_price("dms.t3.medium", multi_az=False)
        multi = engine.get_dms_instance_monthly_price("dms.t3.medium", multi_az=True)
        assert multi == pytest.approx(2 * single, rel=1e-6)

    def test_class_without_dms_prefix_is_normalized(self):
        engine = _engine()
        with_prefix = engine.get_dms_instance_monthly_price("dms.t3.medium")
        without_prefix = engine.get_dms_instance_monthly_price("t3.medium")
        assert with_prefix == pytest.approx(without_prefix)
        assert without_prefix == pytest.approx(0.0745 * HOURS_PER_MONTH, abs=1e-6)

    def test_fetch_pins_replication_server_product_family(self):
        client = _FakePricing()
        engine = _engine(client)
        engine.get_dms_instance_monthly_price("dms.t3.medium", multi_az=True)
        dms_calls = [c for c in client.calls if c[0] == "AWSDatabaseMigrationSvc"]
        assert dms_calls and dms_calls[0][1].get("productFamily") == "Replication Server"

    def test_single_az_fallback_constant(self):
        engine = PricingEngine("us-east-1", _EmptyPricing(), fallback_multiplier=1.0)
        price = engine.get_dms_instance_monthly_price("dms.t3.medium", multi_az=False)
        assert price == pytest.approx(FALLBACK_DMS_INSTANCE_MONTHLY)

    def test_multi_az_fallback_is_doubled(self):
        engine = PricingEngine("us-east-1", _EmptyPricing(), fallback_multiplier=1.0)
        price = engine.get_dms_instance_monthly_price("dms.t3.medium", multi_az=True)
        assert price == pytest.approx(FALLBACK_DMS_INSTANCE_MONTHLY * FALLBACK_DMS_MULTI_AZ_FACTOR)
