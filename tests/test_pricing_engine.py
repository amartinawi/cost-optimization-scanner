"""Unit tests for core/pricing_engine.py"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from core.pricing_engine import (
    FALLBACK_ALB_MONTH,
    FALLBACK_EBS_GB_MONTH,
    FALLBACK_EBS_IOPS_MONTH,
    FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH,
    FALLBACK_EBS_SNAPSHOT_GB_MONTH,
    FALLBACK_EFS_GB_MONTH,
    FALLBACK_EIP_MONTH,
    FALLBACK_IO2_IOPS_TIER2_MONTH,
    FALLBACK_IO2_IOPS_TIER3_MONTH,
    FALLBACK_NAT_MONTH,
    FALLBACK_RDS_BACKUP_GB_MONTH,
    FALLBACK_RDS_INSTANCE_MONTHLY,
    FALLBACK_RDS_MULTI_AZ_FACTOR,
    FALLBACK_RDS_STORAGE_GB_MONTH,
    FALLBACK_S3_GB_MONTH,
    FALLBACK_VPC_ENDPOINT_MONTH,
    PricingCache,
    PricingEngine,
)


def _make_engine(api_return: float | None = None) -> PricingEngine:
    mock_client = MagicMock()
    if api_return is not None:
        price_item = {
            "terms": {
                "OnDemand": {"SKU_TERM": {"priceDimensions": {"SKU_DIM": {"pricePerUnit": {"USD": str(api_return)}}}}}
            }
        }
        mock_client.get_products.return_value = {"PriceList": [json.dumps(price_item)]}
    else:
        mock_client.get_products.return_value = {"PriceList": []}
    return PricingEngine("us-east-1", mock_client)


class TestPricingCache:
    def test_cache_miss_returns_none(self):
        cache = PricingCache()
        assert cache.get(("key",)) is None

    def test_cache_hit_returns_value(self):
        cache = PricingCache()
        cache.set(("key",), 0.10)
        assert cache.get(("key",)) == 0.10

    def test_cache_overwrite(self):
        cache = PricingCache()
        cache.set(("key",), 0.10)
        cache.set(("key",), 0.20)
        assert cache.get(("key",)) == 0.20


class TestPricingEngine:
    def test_ebs_fallback_when_api_empty(self):
        engine = _make_engine(api_return=None)
        assert engine.get_ebs_monthly_price_per_gb("gp3") == FALLBACK_EBS_GB_MONTH["gp3"]

    def test_ebs_live_price(self):
        engine = _make_engine(api_return=0.085)
        assert engine.get_ebs_monthly_price_per_gb("gp3") == 0.085

    def test_ec2_hourly_fallback(self):
        engine = _make_engine(api_return=None)
        price = engine.get_ec2_hourly_price("m5.large")
        assert isinstance(price, float)
        assert price == 0.0

    def test_instance_monthly_price_fallback(self):
        engine = _make_engine(api_return=None)
        price = engine.get_instance_monthly_price("AmazonRedshift", "dc2.large")
        assert isinstance(price, float)
        assert price == 0.0

    def test_efs_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_efs_monthly_price_per_gb() == FALLBACK_EFS_GB_MONTH

    def test_eip_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_eip_monthly_price() == FALLBACK_EIP_MONTH

    def test_nat_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_nat_gateway_monthly_price() == FALLBACK_NAT_MONTH

    def test_vpc_endpoint_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_vpc_endpoint_monthly_price() == FALLBACK_VPC_ENDPOINT_MONTH

    def test_alb_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_alb_monthly_price() == FALLBACK_ALB_MONTH

    def test_fallback_multiplier_applied(self):
        engine = _make_engine(api_return=None)
        engine._fallback_multiplier = 1.5
        assert engine.get_efs_monthly_price_per_gb() == FALLBACK_EFS_GB_MONTH * 1.5

    def test_caching_prevents_duplicate_calls(self):
        engine = _make_engine(api_return=0.10)
        engine.get_ebs_monthly_price_per_gb("gp3")
        engine.get_ebs_monthly_price_per_gb("gp3")
        assert engine._pricing.get_products.call_count == 1

    def test_rds_storage_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_rds_monthly_storage_price_per_gb("gp3") == FALLBACK_RDS_STORAGE_GB_MONTH["gp3"]

    def test_rds_backup_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_rds_backup_storage_price_per_gb() == FALLBACK_RDS_BACKUP_GB_MONTH

    def test_s3_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_s3_monthly_price_per_gb("STANDARD") == FALLBACK_S3_GB_MONTH["STANDARD"]

    def test_ebs_iops_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_ebs_iops_monthly_price("io1") == FALLBACK_EBS_IOPS_MONTH["io1"]

    def test_warning_emitted_on_fallback(self):
        engine = _make_engine(api_return=None)
        engine.get_efs_monthly_price_per_gb()
        assert len(engine.warnings) == 1
        assert "EFS" in engine.warnings[0]

    def test_ec2_live_price(self):
        engine = _make_engine(api_return=0.096)
        assert engine.get_ec2_hourly_price("m5.large") == 0.096

    def test_instance_monthly_live_price(self):
        engine = _make_engine(api_return=0.20)
        price = engine.get_instance_monthly_price("AmazonElastiCache", "cache.m5.large")
        assert price == pytest.approx(0.20 * 730)

    def test_rds_instance_monthly_fallback_single_az(self):
        engine = _make_engine(api_return=None)
        price = engine.get_rds_instance_monthly_price("mysql", "db.t3.medium", multi_az=False)
        assert price == FALLBACK_RDS_INSTANCE_MONTHLY

    def test_rds_instance_monthly_fallback_multi_az(self):
        engine = _make_engine(api_return=None)
        price = engine.get_rds_instance_monthly_price("postgres", "db.t3.medium", multi_az=True)
        assert price == pytest.approx(FALLBACK_RDS_INSTANCE_MONTHLY * FALLBACK_RDS_MULTI_AZ_FACTOR)

    def test_rds_instance_monthly_engine_normalization(self):
        """SQL Server engine maps to License included; helper should not crash."""
        engine = _make_engine(api_return=None)
        price = engine.get_rds_instance_monthly_price("SQLSERVER-EX", "db.m5.large", multi_az=False)
        # SQLServer uses License Included path but fallback is engine-agnostic.
        assert price == FALLBACK_RDS_INSTANCE_MONTHLY

    def test_rds_instance_monthly_cache(self):
        engine = _make_engine(api_return=0.072)
        engine.get_rds_instance_monthly_price("postgres", "db.t3.medium", multi_az=False)
        engine.get_rds_instance_monthly_price("postgres", "db.t3.medium", multi_az=False)
        # Cache hit on the second call: only one API roundtrip.
        assert engine._pricing.get_products.call_count == 1

    def test_rds_instance_monthly_separate_cache_for_multi_az(self):
        """Multi-AZ vs Single-AZ are separate cache keys for the same instance class."""
        engine = _make_engine(api_return=0.072)
        engine.get_rds_instance_monthly_price("postgres", "db.t3.medium", multi_az=False)
        engine.get_rds_instance_monthly_price("postgres", "db.t3.medium", multi_az=True)
        assert engine._pricing.get_products.call_count == 2

    def test_ebs_snapshot_standard_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_ebs_snapshot_price_per_gb() == FALLBACK_EBS_SNAPSHOT_GB_MONTH

    def test_ebs_snapshot_archive_fallback(self):
        engine = _make_engine(api_return=None)
        archive_price = engine.get_ebs_snapshot_price_per_gb(archive_tier=True)
        assert archive_price == FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH
        # Archive should be cheaper than Standard.
        assert archive_price < FALLBACK_EBS_SNAPSHOT_GB_MONTH

    def test_ebs_snapshot_caches_separately_per_tier(self):
        engine = _make_engine(api_return=0.05)
        engine.get_ebs_snapshot_price_per_gb()
        engine.get_ebs_snapshot_price_per_gb(archive_tier=True)
        assert engine._pricing.get_products.call_count == 2

    def test_ebs_gp3_iops_fallback(self):
        engine = _make_engine(api_return=None)
        assert engine.get_ebs_iops_monthly_price("gp3") == FALLBACK_EBS_IOPS_MONTH["gp3"]

    def test_ebs_io2_iops_cost_single_tier(self):
        """Below 32k IOPS, io2 cost = iops × base rate."""
        engine = _make_engine(api_return=None)
        cost = engine.get_ebs_io2_iops_cost(16000)
        assert cost == pytest.approx(16000 * FALLBACK_EBS_IOPS_MONTH["io2"])

    def test_ebs_io2_iops_cost_two_tiers(self):
        """48k IOPS spans tier 1 (0-32k) and tier 2 (32k-64k)."""
        engine = _make_engine(api_return=None)
        cost = engine.get_ebs_io2_iops_cost(48000)
        expected = (
            32000 * FALLBACK_EBS_IOPS_MONTH["io2"]
            + 16000 * FALLBACK_IO2_IOPS_TIER2_MONTH
        )
        assert cost == pytest.approx(expected)

    def test_ebs_io2_iops_cost_three_tiers(self):
        """80k IOPS spans all three tiers."""
        engine = _make_engine(api_return=None)
        cost = engine.get_ebs_io2_iops_cost(80000)
        expected = (
            32000 * FALLBACK_EBS_IOPS_MONTH["io2"]
            + 32000 * FALLBACK_IO2_IOPS_TIER2_MONTH
            + 16000 * FALLBACK_IO2_IOPS_TIER3_MONTH
        )
        assert cost == pytest.approx(expected)

    def test_ebs_io2_iops_cost_zero_iops(self):
        engine = _make_engine(api_return=None)
        assert engine.get_ebs_io2_iops_cost(0) == 0.0


class TestPricingEngineLiveOption:
    def test_live_only_skips_without_flag(self, live_only):
        pytest.skip("This test only runs with --live flag")
