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

    def test_s3_standard_selects_base_tier_not_first_dimension(self):
        """Audit S3-D — pick the beginRange==0 tier ($0.023), not whichever
        priceDimension the API serializes first ($0.022)."""
        price_item = {
            "product": {"attributes": {"usagetype": "TimedStorage-ByteHrs"}},
            "terms": {
                "OnDemand": {
                    "SKU.TERM": {
                        "priceDimensions": {
                            # Deliberately list the $0.022 "next 450 TB" tier first.
                            "d1": {"beginRange": "51200", "pricePerUnit": {"USD": "0.0220000000"}},
                            "d2": {"beginRange": "512000", "pricePerUnit": {"USD": "0.0210000000"}},
                            "d3": {"beginRange": "0", "pricePerUnit": {"USD": "0.0230000000"}},
                        }
                    }
                }
            },
        }
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": [json.dumps(price_item)]}
        engine = PricingEngine("us-east-1", mock_client)
        assert engine.get_s3_monthly_price_per_gb("STANDARD") == pytest.approx(0.023)

    def test_eu_region_display_names_match_price_list_api(self):
        """Audit S3-J — older EU regions use 'EU (X)' in the Price List API; the
        two newest (Zurich, Spain) use 'Europe (X)'. Verified across
        AmazonS3/EC2/RDS. A wrong form makes every lookup for that region
        silently fall back to us-east-1 constants."""
        from core.pricing_engine import REGION_DISPLAY_NAMES

        assert REGION_DISPLAY_NAMES["eu-west-1"] == "EU (Ireland)"
        assert REGION_DISPLAY_NAMES["eu-west-2"] == "EU (London)"
        assert REGION_DISPLAY_NAMES["eu-west-3"] == "EU (Paris)"
        assert REGION_DISPLAY_NAMES["eu-central-1"] == "EU (Frankfurt)"
        assert REGION_DISPLAY_NAMES["eu-north-1"] == "EU (Stockholm)"
        assert REGION_DISPLAY_NAMES["eu-south-1"] == "EU (Milan)"
        # Newest two regions genuinely use the "Europe (X)" form.
        assert REGION_DISPLAY_NAMES["eu-central-2"] == "Europe (Zurich)"
        assert REGION_DISPLAY_NAMES["eu-south-2"] == "Europe (Spain)"

    def test_for_region_same_region_returns_self(self):
        engine = _make_engine(api_return=None)
        assert engine.for_region("us-east-1") is engine

    def test_for_region_builds_and_caches_sibling(self):
        """Audit S3-I — a sibling engine prices at the requested region and is cached."""
        engine = _make_engine(api_return=None)
        sib = engine.for_region("eu-central-1")
        assert sib is not engine
        assert sib._region == "eu-central-1"
        assert sib._display_name == "EU (Frankfurt)"  # Price List API form (audit S3-J)
        # Same client reused; sibling cached (identity stable across calls).
        assert sib._pricing is engine._pricing
        assert engine.for_region("eu-central-1") is sib

    def test_for_region_uses_target_region_location_filter(self):
        """The sibling must query the target region's location, not the scan region."""
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": []}
        engine = PricingEngine("ap-south-1", mock_client)
        engine.for_region("us-east-1").get_s3_monthly_price_per_gb("STANDARD")
        # The most recent get_products call must filter on US East (N. Virginia).
        _, kwargs = mock_client.get_products.call_args
        locations = [f["Value"] for f in kwargs["Filters"] if f["Field"] == "location"]
        assert locations == ["US East (N. Virginia)"]

    def test_s3_skips_staging_and_overhead_rows(self):
        """Audit S3-E/S3-D — select the timed *storage* SKU, not Staging/Overhead."""
        staging = {
            "product": {"attributes": {"usagetype": "TimedStorage-GDA-Staging"}},
            "terms": {
                "OnDemand": {
                    "S.T": {"priceDimensions": {"d": {"beginRange": "0", "pricePerUnit": {"USD": "0.0210000000"}}}}
                }
            },
        }
        storage = {
            "product": {"attributes": {"usagetype": "TimedStorage-GDA-ByteHrs"}},
            "terms": {
                "OnDemand": {
                    "S.T": {"priceDimensions": {"d": {"beginRange": "0", "pricePerUnit": {"USD": "0.0009900000"}}}}
                }
            },
        }
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": [json.dumps(staging), json.dumps(storage)]}
        engine = PricingEngine("us-east-1", mock_client)
        assert engine.get_s3_monthly_price_per_gb("DEEP_ARCHIVE") == pytest.approx(0.00099)

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
        # OpenSearch is not SR-1-specialized, so it uses the unchanged
        # MaxResults=1 path where a bare $/hr price item is a valid mock.
        price = engine.get_instance_monthly_price("AmazonES", "m5.large.search")
        assert price == pytest.approx(0.20 * 730)

    def test_amazones_legacy_elasticsearch_suffix_normalized(self):
        # opensearch L1: a legacy ".elasticsearch" instance type must be looked
        # up under the ".search" SKU the Pricing API actually publishes,
        # otherwise it silently prices to $0.
        mock_client = MagicMock()
        mock_client.get_products.return_value = {
            "PriceList": [
                json.dumps(
                    {"terms": {"OnDemand": {"T": {"priceDimensions": {"d": {"pricePerUnit": {"USD": "0.20"}}}}}}}
                )
            ]
        }
        engine = PricingEngine("us-east-1", mock_client)
        price = engine.get_instance_monthly_price("AmazonES", "m5.large.elasticsearch")
        assert price == pytest.approx(0.20 * 730)
        # The instanceType filter sent to the API was normalized to ".search".
        filters = mock_client.get_products.call_args.kwargs["Filters"]
        itype = next(f["Value"] for f in filters if f["Field"] == "instanceType")
        assert itype == "m5.large.search"

    def test_redshift_selects_compute_instance_not_concurrency_scaling(self):
        """SR-1 / Redshift C2 — a bare instanceType+location filter matches four
        ra3.4xlarge SKUs. The deterministic selector must pick the Compute
        Instance node-hour SKU ($3.26/hr), never the per-second Concurrency
        Scaling ($0.0009/sec), per-GB Managed Storage ($0.024), or $0 Free row.
        Feed the SKUs in adversarial order (correct SKU last)."""

        def sku(product_family, usagetype, unit, usd):
            return {
                "product": {
                    "attributes": {
                        "instanceType": "ra3.4xlarge",
                        "usagetype": usagetype,
                        "productFamily": product_family,
                        "location": "US East (N. Virginia)",
                    }
                },
                "terms": {"OnDemand": {"T": {"priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": usd}}}}}},
            }

        price_list = [
            json.dumps(sku("Redshift Concurrency Scaling", "CS:ra3.4xlarge", "seconds", "0.0009")),
            json.dumps(sku("Redshift Managed Storage", "RMS:ra3.4xlarge", "GB-Mo", "0.024")),
            json.dumps(sku("Redshift Concurrency Scaling", "CSFreeUsage:ra3.4xlarge", "seconds", "0.0")),
            json.dumps(sku("Compute Instance", "Node:ra3.4xlarge", "Hrs", "3.26")),
        ]
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": price_list}
        engine = PricingEngine("us-east-1", mock_client)
        # $3.26/hr × 730 = $2,379.80/mo (the real Compute Instance rate).
        assert engine.get_instance_monthly_price("AmazonRedshift", "ra3.4xlarge") == pytest.approx(3.26 * 730)
        # Determinism: a second call returns the identical rate (cache hit,
        # same SKU chosen every time).
        assert engine.get_instance_monthly_price("AmazonRedshift", "ra3.4xlarge") == pytest.approx(3.26 * 730)

    def test_redshift_rejects_zero_compute_sku(self):
        """If the Compute Instance row were a $0 placeholder it is rejected and
        the function falls back rather than reporting a fabricated $0 node."""
        sku_zero = {
            "product": {
                "attributes": {
                    "instanceType": "ra3.xlplus",
                    "usagetype": "Node:ra3.xlplus",
                    "productFamily": "Compute Instance",
                    "location": "US East (N. Virginia)",
                }
            },
            "terms": {
                "OnDemand": {"T": {"priceDimensions": {"d": {"unit": "Hrs", "pricePerUnit": {"USD": "0.0000000000"}}}}}
            },
        }
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": [json.dumps(sku_zero)]}
        engine = PricingEngine("us-east-1", mock_client)
        assert engine.get_instance_monthly_price("AmazonRedshift", "ra3.xlplus") == 0.0

    def test_elasticache_selects_redis_nodeusage_not_extended_support(self):
        """SR-1 / ElastiCache C2 — all six cache.r6g.large SKUs share unit=Hrs.
        With engine=Redis the selector must pick the exact NodeUsage row
        ($0.206/hr), never the USE1-ExtendedSupportYr3 surcharge ($0.33/hr) or
        the USE1-SyncDurability row. Adversarial order: $0.33 SKU first."""

        def sku(usagetype, cache_engine, usd):
            return {
                "product": {
                    "attributes": {
                        "instanceType": "cache.r6g.large",
                        "usagetype": usagetype,
                        "cacheEngine": cache_engine,
                        "productFamily": "Cache Instance",
                        "location": "US East (N. Virginia)",
                    }
                },
                "terms": {"OnDemand": {"T": {"priceDimensions": {"d": {"unit": "Hrs", "pricePerUnit": {"USD": usd}}}}}},
            }

        price_list = [
            json.dumps(sku("USE1-ExtendedSupportYr3-NodeUsage:cache.r6g.large", "Redis", "0.33")),
            json.dumps(sku("USE1-ExtendedSupportYr1_Yr2-NodeUsage:cache.r6g.large", "Redis", "0.165")),
            json.dumps(sku("USE1-SyncDurability-NodeUsage:cache.r6g.large", "Valkey", "0.0297")),
            json.dumps(sku("NodeUsage:cache.r6g.large", "Valkey", "0.1648")),
            json.dumps(sku("NodeUsage:cache.r6g.large", "Memcached", "0.206")),
            json.dumps(sku("NodeUsage:cache.r6g.large", "Redis", "0.206")),
        ]
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": price_list}
        engine = PricingEngine("us-east-1", mock_client)
        # Redis NodeUsage = $0.206/hr × 730 = $150.38/mo.
        assert engine.get_instance_monthly_price(
            "AmazonElastiCache", "cache.r6g.large", engine="Redis"
        ) == pytest.approx(0.206 * 730)
        # Valkey NodeUsage = $0.1648/hr × 730 = $120.30/mo (engine-scoped cache).
        assert engine.get_instance_monthly_price(
            "AmazonElastiCache", "cache.r6g.large", engine="Valkey"
        ) == pytest.approx(0.1648 * 730)

    def test_node_pricing_matches_region_prefixed_usagetype(self):
        """Outside us-east-1 the Pricing API region-prefixes the usagetype
        (e.g. ``EUW1-NodeUsage:...`` / ``EU-Node:...``). The node selector must
        strip the ``<REGION>-`` prefix and still match, otherwise it returns
        None and the caller silently falls back to $0 in every non-default
        region (a real price mis-read as zero)."""

        def cache_sku(usagetype, usd):
            return {
                "product": {
                    "attributes": {
                        "instanceType": "cache.r6g.large",
                        "usagetype": usagetype,
                        "cacheEngine": "Redis",
                        "productFamily": "Cache Instance",
                        "location": "EU (Ireland)",
                    }
                },
                "terms": {"OnDemand": {"T": {"priceDimensions": {"d": {"unit": "Hrs", "pricePerUnit": {"USD": usd}}}}}},
            }

        def redshift_sku(usagetype, usd):
            return {
                "product": {
                    "attributes": {
                        "instanceType": "ra3.4xlarge",
                        "usagetype": usagetype,
                        "productFamily": "Compute Instance",
                        "location": "EU (Ireland)",
                    }
                },
                "terms": {"OnDemand": {"T": {"priceDimensions": {"d": {"unit": "Hrs", "pricePerUnit": {"USD": usd}}}}}},
            }

        # ElastiCache: EUW1-prefixed NodeUsage must match; the EUW1-prefixed
        # ExtendedSupport surcharge must still be rejected.
        ec_client = MagicMock()
        ec_client.get_products.return_value = {
            "PriceList": [
                json.dumps(cache_sku("EUW1-ExtendedSupportYr3-NodeUsage:cache.r6g.large", "0.36")),
                json.dumps(cache_sku("EUW1-NodeUsage:cache.r6g.large", "0.227")),
            ]
        }
        ec_engine = PricingEngine("eu-west-1", ec_client)
        assert ec_engine.get_instance_monthly_price(
            "AmazonElastiCache", "cache.r6g.large", engine="Redis"
        ) == pytest.approx(0.227 * 730)

        # Redshift: EU-prefixed Node: must match (was silently $0 before the fix).
        rs_client = MagicMock()
        rs_client.get_products.return_value = {"PriceList": [json.dumps(redshift_sku("EU-Node:ra3.4xlarge", "3.48"))]}
        rs_engine = PricingEngine("eu-west-1", rs_client)
        assert rs_engine.get_instance_monthly_price("AmazonRedshift", "ra3.4xlarge") == pytest.approx(3.48 * 730)

    def test_elasticache_without_engine_returns_none(self):
        """Without the engine discriminator the NodeUsage SKU is ambiguous
        (three engines share it) — return None → $0 fallback rather than a
        non-deterministic pick."""
        sku = {
            "product": {
                "attributes": {
                    "instanceType": "cache.r6g.large",
                    "usagetype": "NodeUsage:cache.r6g.large",
                    "cacheEngine": "Redis",
                    "productFamily": "Cache Instance",
                    "location": "US East (N. Virginia)",
                }
            },
            "terms": {"OnDemand": {"T": {"priceDimensions": {"d": {"unit": "Hrs", "pricePerUnit": {"USD": "0.206"}}}}}},
        }
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": [json.dumps(sku)]}
        engine = PricingEngine("us-east-1", mock_client)
        assert engine.get_instance_monthly_price("AmazonElastiCache", "cache.r6g.large") == 0.0

    def test_elasticache_normalizes_lowercase_engine(self):
        """elasticache C2 production bug — the shim reads ``Engine`` from
        DescribeCacheClusters which AWS returns lowercase ("redis"), while the
        Pricing API ``cacheEngine`` attribute is capitalized ("Redis"). A
        verbatim match returns no NodeUsage row → $0. The pricing boundary
        normalizes casing so lowercase "redis" still selects the $0.206/hr
        Redis NodeUsage SKU."""

        def sku(cache_engine, usd):
            return {
                "product": {
                    "attributes": {
                        "instanceType": "cache.r6g.large",
                        "usagetype": "NodeUsage:cache.r6g.large",
                        "cacheEngine": cache_engine,
                        "productFamily": "Cache Instance",
                        "location": "US East (N. Virginia)",
                    }
                },
                "terms": {"OnDemand": {"T": {"priceDimensions": {"d": {"unit": "Hrs", "pricePerUnit": {"USD": usd}}}}}},
            }

        price_list = [
            json.dumps(sku("Redis", "0.206")),
            json.dumps(sku("Valkey", "0.1648")),
        ]
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": price_list}
        engine = PricingEngine("us-east-1", mock_client)
        # Lowercase "redis" (what the real shim produces) must still resolve to
        # the Redis NodeUsage rate, not collapse to $0.
        assert engine.get_instance_monthly_price(
            "AmazonElastiCache", "cache.r6g.large", engine="redis"
        ) == pytest.approx(0.206 * 730)
        # Mixed-case and uppercase also normalize.
        assert engine.get_instance_monthly_price(
            "AmazonElastiCache", "cache.r6g.large", engine="VALKEY"
        ) == pytest.approx(0.1648 * 730)

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
        expected = 32000 * FALLBACK_EBS_IOPS_MONTH["io2"] + 16000 * FALLBACK_IO2_IOPS_TIER2_MONTH
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
