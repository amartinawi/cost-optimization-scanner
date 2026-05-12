"""Session-scoped pricing engine: live AWS Pricing API with fallback to hardcoded constants."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ── Region display name mapping ──────────────────────────────────────────────
# AWS Pricing API filters by display name, not region code.
REGION_DISPLAY_NAMES: dict[str, str] = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "us-gov-east-1": "AWS GovCloud (US-East)",
    "us-gov-west-1": "AWS GovCloud (US-West)",
    "ca-central-1": "Canada (Central)",
    "ca-west-1": "Canada West (Calgary)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
    "eu-central-1": "Europe (Frankfurt)",
    "eu-central-2": "Europe (Zurich)",
    "eu-north-1": "Europe (Stockholm)",
    "eu-south-1": "Europe (Milan)",
    "eu-south-2": "Europe (Spain)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "il-central-1": "Israel (Tel Aviv)",
    "af-south-1": "Africa (Cape Town)",
    "sa-east-1": "South America (Sao Paulo)",
    "mx-central-1": "Mexico (Central)",
}

# ── Fallback constants (used only when Pricing API fails) ────────────────────
# Values are us-east-1 On-Demand prices as of 2024-Q4.
FALLBACK_EBS_GB_MONTH: dict[str, float] = {
    "gp2": 0.10,
    "gp3": 0.08,
    "io1": 0.125,
    "io2": 0.125,
    "st1": 0.045,
    "sc1": 0.025,
}
FALLBACK_EBS_IOPS_MONTH: dict[str, float] = {
    "gp3": 0.005,
    "io1": 0.065,
    "io2": 0.065,
}
# AWS io2 IOPS pricing is tiered (us-east-1 rates; other regions scale ~linearly):
#   0–32,000 IOPS:        $0.065/IOPS-month
#   32,001–64,000 IOPS:   $0.0455/IOPS-month
#   >64,000 IOPS:         $0.032/IOPS-month
FALLBACK_IO2_IOPS_TIER2_MONTH: float = 0.0455
FALLBACK_IO2_IOPS_TIER3_MONTH: float = 0.032
FALLBACK_EBS_SNAPSHOT_GB_MONTH: float = 0.05
FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH: float = 0.0125
FALLBACK_RDS_STORAGE_GB_MONTH: dict[str, float] = {
    "gp2": 0.115,
    "gp3": 0.115,
    "io1": 0.200,
}
FALLBACK_RDS_BACKUP_GB_MONTH: float = 0.095
# Single-AZ db.t3.medium MySQL us-east-1 on-demand monthly cost (730h × $0.068/h).
# Used only when AWS Pricing API is unavailable; multiplied by the regional fallback multiplier.
FALLBACK_RDS_INSTANCE_MONTHLY: float = 49.64
# Multiplier applied to FALLBACK_RDS_INSTANCE_MONTHLY for Multi-AZ deployments
# (Multi-AZ is roughly 2× Single-AZ for all engines per AWS Pricing API).
FALLBACK_RDS_MULTI_AZ_FACTOR: float = 2.0

# RDS engine name → AWS Pricing API 'databaseEngine' filter value.
# RDS describe_db_instances returns engine strings like "mysql", "postgres",
# "aurora-postgresql", "oracle-se2", "sqlserver-ex". Map to the human-friendly
# label the Price List service uses on its 'databaseEngine' attribute.
_RDS_ENGINE_LABELS: dict[str, str] = {
    "mysql": "MySQL",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mariadb": "MariaDB",
    "aurora": "Aurora MySQL",
    "aurora-mysql": "Aurora MySQL",
    "aurora-postgresql": "Aurora PostgreSQL",
    "oracle-ee": "Oracle",
    "oracle-ee-cdb": "Oracle",
    "oracle-se": "Oracle",
    "oracle-se1": "Oracle",
    "oracle-se2": "Oracle",
    "oracle-se2-cdb": "Oracle",
    "sqlserver-ee": "SQL Server",
    "sqlserver-se": "SQL Server",
    "sqlserver-ex": "SQL Server",
    "sqlserver-web": "SQL Server",
    "db2-ae": "Db2",
    "db2-se": "Db2",
}

# RDS engines whose Pricing API rows live under licenseModel="License included"
# rather than the default "No license required". Oracle and SQL Server BYOL paths
# (which RDS does not bill for license) would need different handling, but RDS
# only offers LI for SQL Server today and an LI/BYOL split for Oracle.
_RDS_LICENSE_INCLUDED_ENGINES: frozenset[str] = frozenset({
    "sqlserver-ee", "sqlserver-se", "sqlserver-ex", "sqlserver-web",
    "oracle-se2",
})
FALLBACK_S3_GB_MONTH: dict[str, float] = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "ONEZONE_IA": 0.01,
    "GLACIER_IR": 0.004,
    "GLACIER": 0.0036,
    "DEEP_ARCHIVE": 0.00099,
    "INTELLIGENT_TIERING": 0.023,
}
FALLBACK_EFS_GB_MONTH: float = 0.33
FALLBACK_EIP_MONTH: float = 3.65
FALLBACK_NAT_MONTH: float = 35.04
FALLBACK_VPC_ENDPOINT_MONTH: float = 8.03
FALLBACK_ALB_MONTH: float = 20.44
FALLBACK_AURORA_ACU_HOURLY: float = 0.06
SAGEMAKER_OVER_EC2: float = 1.15

# Fields excluded from the debug log key summary (too noisy / always the same).
_LOG_SKIP_FIELDS = frozenset({"location", "productFamily", "tenancy", "capacityStatus", "preInstalledSw"})


class PricingCache:
    """Simple in-memory cache: (service_code, *params) → float price."""

    def __init__(self) -> None:
        self._data: dict[tuple, float] = {}

    def get(self, key: tuple) -> float | None:
        return self._data.get(key)

    def set(self, key: tuple, value: float) -> None:
        self._data[key] = value


class PricingEngine:
    """
    Session-scoped engine for live AWS pricing lookups.

    Always pass region_code (e.g. "eu-west-1").
    pricing_client must be boto3.client("pricing", region_name="us-east-1").
    fallback_multiplier is applied to fallback constants (= ScanContext.pricing_multiplier).
    """

    def __init__(
        self,
        region_code: str,
        pricing_client: Any,
        fallback_multiplier: float = 1.0,
    ) -> None:
        self._region = region_code
        self._display_name = REGION_DISPLAY_NAMES.get(region_code, "US East (N. Virginia)")
        self._pricing = pricing_client
        self._fallback_multiplier = fallback_multiplier
        self._cache = PricingCache()
        self.warnings: list[str] = []
        self._stats: dict[str, int] = {
            "api_calls": 0,
            "cache_hits": 0,
            "fallbacks": 0,
            "api_errors": 0,
        }

    # ── Observability ─────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, int]:
        """Snapshot of Pricing API activity for this scan session."""
        return dict(self._stats)

    def log_summary(self) -> None:
        """Emit an INFO-level summary of all Pricing API activity this session."""
        s = self._stats
        logger.info(
            "PricingEngine [%s] — api_calls=%d  cache_hits=%d  fallbacks=%d  api_errors=%d  warnings=%d",
            self._region,
            s["api_calls"],
            s["cache_hits"],
            s["fallbacks"],
            s["api_errors"],
            len(self.warnings),
        )
        for w in self.warnings:
            logger.warning("  pricing warning: %s", w)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_cached(self, key: tuple) -> float | None:
        """Return a cached price and log the hit; None on miss."""
        cached = self._cache.get(key)
        if cached is not None:
            self._stats["cache_hits"] += 1
            logger.debug("pricing cache-hit  %s → $%.6f", key, cached)
        return cached

    def _use_fallback(self, price: float, message: str) -> float:
        """Record a fallback event, append to warnings, and log at WARNING level."""
        self._stats["fallbacks"] += 1
        self.warnings.append(message)
        logger.warning("pricing fallback  [%s] %s → $%.6f", self._region, message, price)
        return price

    # ── Public API ────────────────────────────────────────────────────────────

    def get_ebs_monthly_price_per_gb(self, volume_type: str) -> float:
        """$/GB/month for EBS volume_type in self._region."""
        key = ("ebs_gb", volume_type)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_ebs_gb_price(volume_type)
        if price is None:
            price = self._use_fallback(
                FALLBACK_EBS_GB_MONTH.get(volume_type, 0.10) * self._fallback_multiplier,
                f"Pricing API unavailable for EBS {volume_type} in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_ebs_iops_monthly_price(self, volume_type: str) -> float:
        """$/IOPS-month for provisioned IOPS volume types (gp3, io1, io2).

        Note: io2 uses tiered pricing above 32,000 IOPS — this method returns
        only the base tier rate. Use :meth:`get_ebs_io2_iops_cost` for volumes
        with > 32,000 provisioned IOPS to capture the tiered discount.
        """
        key = ("ebs_iops", volume_type)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_ebs_iops_price(volume_type)
        if price is None:
            price = self._use_fallback(
                FALLBACK_EBS_IOPS_MONTH.get(volume_type, 0.065) * self._fallback_multiplier,
                f"Pricing API unavailable for EBS IOPS {volume_type} in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_ebs_io2_iops_cost(self, iops: int) -> float:
        """Total $/month for `iops` provisioned IOPS on an io2 volume.

        AWS io2 tiers (us-east-1, regional multiplier applied uniformly):
          0–32,000        IOPS @ base rate (≈ $0.065)
          32,001–64,000   IOPS @ tier 2 rate (≈ $0.0455, 30 % discount)
          > 64,000        IOPS @ tier 3 rate (≈ $0.032, 51 % discount)

        Flat-multiply by the base rate overcounts savings on big io2 volumes.
        """
        if iops <= 0:
            return 0.0
        base_rate = self.get_ebs_iops_monthly_price("io2")
        # Apply the same scaling factor that base_rate has relative to the
        # us-east-1 reference so tier 2/3 rates stay region-consistent.
        ratio = base_rate / FALLBACK_EBS_IOPS_MONTH["io2"] if FALLBACK_EBS_IOPS_MONTH["io2"] else 1.0
        tier2_rate = FALLBACK_IO2_IOPS_TIER2_MONTH * ratio
        tier3_rate = FALLBACK_IO2_IOPS_TIER3_MONTH * ratio
        cost = min(iops, 32000) * base_rate
        if iops > 32000:
            cost += min(iops - 32000, 32000) * tier2_rate
        if iops > 64000:
            cost += (iops - 64000) * tier3_rate
        return cost

    def get_ebs_snapshot_price_per_gb(self, *, archive_tier: bool = False) -> float:
        """$/GB/month for EBS Snapshots in self._region.

        Args:
            archive_tier: When True, returns the Snapshot Archive tier
                ($0.0125/GB-Mo us-east-1, 90-day minimum retention) instead
                of Standard ($0.05/GB-Mo us-east-1).
        """
        key = ("ebs_snapshot", "archive" if archive_tier else "standard")
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_ebs_snapshot_price(archive_tier=archive_tier)
        if price is None:
            fallback = (
                FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH if archive_tier else FALLBACK_EBS_SNAPSHOT_GB_MONTH
            )
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for EBS Snapshot {'Archive' if archive_tier else 'Standard'}"
                f" in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_rds_monthly_storage_price_per_gb(self, storage_type: str, *, multi_az: bool = False) -> float:
        """$/GB/month for RDS storage type (gp2, gp3, io1) in self._region.

        Args:
            storage_type: RDS storage volume type (gp2, gp3, io1, io2).
            multi_az: Whether the DB instance uses Multi-AZ deployment.
                Multi-AZ storage is typically ~2× the Single-AZ price.
        """
        key = ("rds_storage", storage_type, "Multi-AZ" if multi_az else "Single-AZ")
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_rds_storage_price(storage_type, multi_az=multi_az)
        if price is None:
            price = self._use_fallback(
                FALLBACK_RDS_STORAGE_GB_MONTH.get(storage_type, 0.115) * self._fallback_multiplier,
                f"Pricing API unavailable for RDS {storage_type} ({'Multi-AZ' if multi_az else 'Single-AZ'}) in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_rds_instance_monthly_price(
        self,
        engine: str,
        instance_class: str,
        *,
        multi_az: bool = False,
    ) -> float:
        """$/month for an RDS DB instance, engine- and deployment-aware.

        Args:
            engine: RDS engine name as returned by ``describe_db_instances`` (e.g.
                ``"mysql"``, ``"postgres"``, ``"sqlserver-ex"``, ``"aurora-postgresql"``).
                Case-insensitive.
            instance_class: RDS instance class string (e.g. ``"db.t3.medium"``).
            multi_az: When True, fetches the Multi-AZ deployment price (~2× Single-AZ).

        Returns:
            Monthly on-demand price in USD (730 hours). Returns 0.0 when both the
            Pricing API and the fallback constant are unavailable for the given key.

        Notes:
            RDS pricing requires ``databaseEngine``, ``deploymentOption``, and
            ``licenseModel`` filters in addition to ``instanceType`` + ``location``
            to disambiguate. The generic ``get_instance_monthly_price`` is not
            sufficient for RDS — use this method instead.
        """
        normalized = engine.lower().strip()
        key = ("rds_instance", normalized, instance_class, "Multi-AZ" if multi_az else "Single-AZ")
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_rds_instance_price(normalized, instance_class, multi_az=multi_az)
        if price is None:
            fallback = FALLBACK_RDS_INSTANCE_MONTHLY * (FALLBACK_RDS_MULTI_AZ_FACTOR if multi_az else 1.0)
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for RDS {instance_class} {normalized} "
                f"({'Multi-AZ' if multi_az else 'Single-AZ'}) in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_rds_backup_storage_price_per_gb(self) -> float:
        """$/GB/month for RDS backup storage in self._region."""
        key = ("rds_backup",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_rds_backup_price()
        if price is None:
            price = self._use_fallback(
                FALLBACK_RDS_BACKUP_GB_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for RDS backup storage in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_s3_monthly_price_per_gb(self, storage_class: str) -> float:
        """$/GB/month for S3 storage_class in self._region."""
        key = ("s3_gb", storage_class)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_s3_price(storage_class)
        if price is None:
            price = self._use_fallback(
                FALLBACK_S3_GB_MONTH.get(storage_class, 0.023) * self._fallback_multiplier,
                f"Pricing API unavailable for S3 {storage_class} in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_ec2_hourly_price(self, instance_type: str, os: str = "Linux") -> float:
        """On-Demand hourly price for EC2 instance_type in self._region."""
        key = ("ec2_hourly", instance_type, os)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_ec2_price(instance_type, os)
        if price is None:
            price = self._use_fallback(
                0.0,
                f"Pricing API returned no result for EC2 {instance_type} ({os}) in {self._region}",
            )
        self._cache.set(key, price)
        return price

    def get_msk_broker_hourly_price(self, instance_type: str) -> float:
        """On-Demand hourly price for an MSK broker instance in self._region.

        Uses the AmazonMSK service code.  Strips 'kafka.' prefix if present.
        Falls back to EC2 pricing * 1.4 (MSK markup factor) on failure.
        """
        clean_type = instance_type.replace("kafka.", "")
        key = ("msk_hourly", clean_type)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_msk_broker_price(clean_type)
        if price is None:
            try:
                ec2_price = self.get_ec2_hourly_price(clean_type)
                fallback_price = ec2_price * 1.4
            except Exception:
                fallback_price = 0.15
            price = self._use_fallback(
                fallback_price,
                f"Pricing API unavailable for MSK {clean_type} in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_instance_monthly_price(self, service_code: str, instance_type: str) -> float:
        """On-Demand monthly price for non-EC2 instances (ElastiCache, OpenSearch, etc.)."""
        key = (service_code, "monthly", instance_type)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_generic_instance_price(service_code, instance_type)
        if price is None:
            price = self._use_fallback(
                0.0,
                f"Pricing API returned no result for {service_code} {instance_type} in {self._region}",
            )
        self._cache.set(key, price)
        return price

    def get_efs_monthly_price_per_gb(self) -> float:
        key = ("efs_gb",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_efs_price()
        if price is None:
            price = self._use_fallback(
                FALLBACK_EFS_GB_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for EFS Standard in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_eip_monthly_price(self) -> float:
        key = ("eip_month",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_eip_price()
        if price is None:
            price = self._use_fallback(
                FALLBACK_EIP_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for EIP in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_nat_gateway_monthly_price(self) -> float:
        key = ("nat_month",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_nat_gateway_price()
        if price is None:
            price = self._use_fallback(
                FALLBACK_NAT_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for NAT Gateway in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_vpc_endpoint_monthly_price(self) -> float:
        key = ("vpc_ep_month",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_vpc_endpoint_price()
        if price is None:
            price = self._use_fallback(
                FALLBACK_VPC_ENDPOINT_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for VPC Endpoint in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_alb_monthly_price(self) -> float:
        key = ("alb_month",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_alb_price()
        if price is None:
            price = self._use_fallback(
                FALLBACK_ALB_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for ALB in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_aurora_acu_hourly(self) -> float:
        """Hourly price per Aurora Serverless v2 ACU in self._region."""
        key = ("aurora_acu_hourly",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_aurora_acu_price()
        if price is None:
            price = self._use_fallback(
                FALLBACK_AURORA_ACU_HOURLY * self._fallback_multiplier,
                f"Pricing API unavailable for Aurora Serverless v2 ACU in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_sagemaker_instance_monthly(self, instance_type: str) -> float:
        """On-Demand monthly price for SageMaker instance. Fallback: EC2 × 1.15."""
        key = ("sagemaker_monthly", instance_type)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_sagemaker_instance_price(instance_type)
        if price is None:
            try:
                ec2_price = self.get_ec2_hourly_price(instance_type)
                fallback_price = ec2_price * SAGEMAKER_OVER_EC2 * 730
            except Exception:
                fallback_price = 0.0
            price = self._use_fallback(
                fallback_price,
                f"Pricing API unavailable for SageMaker {instance_type} in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    # ── Private fetch methods ─────────────────────────────────────────────────

    def _fetch_ec2_price(self, instance_type: str, os: str) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "capacityStatus", "Value": "Used"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        ]
        return self._call_pricing_api("AmazonEC2", filters)

    def _fetch_ebs_gb_price(self, volume_type: str) -> float | None:
        # AWS Pricing API uses different volumeApiName values per type
        volume_api_names = {
            "gp2": "gp2",
            "gp3": "gp3",
            "io1": "io1",
            "io2": "io2",
            "st1": "st1",
            "sc1": "sc1",
        }
        api_name = volume_api_names.get(volume_type, volume_type)
        filters = [
            {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": api_name},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
        ]
        return self._call_pricing_api("AmazonEC2", filters)

    def _fetch_ebs_iops_price(self, volume_type: str) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": volume_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "System Operation"},
        ]
        return self._call_pricing_api("AmazonEC2", filters)

    def _fetch_ebs_snapshot_price(self, *, archive_tier: bool = False) -> float | None:
        usagetype_value = "EBS:SnapshotArchiveStorage" if archive_tier else "EBS:SnapshotUsage"
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
            {"Type": "TERM_MATCH", "Field": "usagetype", "Value": usagetype_value},
        ]
        return self._call_pricing_api("AmazonEC2", filters)

    def _fetch_rds_instance_price(
        self,
        engine: str,
        instance_class: str,
        *,
        multi_az: bool = False,
    ) -> float | None:
        """Fetch on-demand monthly RDS instance price; None on miss."""
        engine_label = _RDS_ENGINE_LABELS.get(engine, "MySQL")
        license_model = "License included" if engine in _RDS_LICENSE_INCLUDED_ENGINES else "No license required"
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": engine_label},
            {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": "Multi-AZ" if multi_az else "Single-AZ"},
            {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": license_model},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Instance"},
        ]
        hourly = self._call_pricing_api_hourly("AmazonRDS", filters)
        return hourly * 730 if hourly is not None else None

    def _fetch_rds_storage_price(self, storage_type: str, *, multi_az: bool = False) -> float | None:
        deployment_option = "Multi-AZ" if multi_az else "Single-AZ"
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Storage"},
            {"Type": "TERM_MATCH", "Field": "volumeType", "Value": storage_type.upper()},
            {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deployment_option},
        ]
        return self._call_pricing_api("AmazonRDS", filters)

    def _fetch_rds_backup_price(self) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
        ]
        return self._call_pricing_api("AmazonRDS", filters)

    def _fetch_s3_price(self, storage_class: str) -> float | None:
        storage_class_map = {
            "STANDARD": "General Purpose",
            "STANDARD_IA": "Infrequent Access",
            "ONEZONE_IA": "One Zone - Infrequent Access",
            "GLACIER_IR": "Amazon Glacier Instant Retrieval",
            "GLACIER": "Amazon Glacier Flexible Retrieval",
            "DEEP_ARCHIVE": "Amazon Glacier Deep Archive",
            "INTELLIGENT_TIERING": "Intelligent-Tiering",
        }
        storage_class_label = storage_class_map.get(storage_class, "General Purpose")
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "storageClass", "Value": storage_class_label},
        ]
        return self._call_pricing_api("AmazonS3", filters)

    def _fetch_msk_broker_price(self, instance_type: str) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        ]
        return self._call_pricing_api("AmazonMSK", filters)

    def _fetch_sagemaker_instance_price(self, instance_type: str) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        ]
        hourly = self._call_pricing_api("AmazonSageMaker", filters)
        return hourly * 730 if hourly is not None else None

    def _fetch_generic_instance_price(self, service_code: str, instance_type: str) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        ]
        price_hourly = self._call_pricing_api(service_code, filters)
        if price_hourly is not None:
            return price_hourly * 730  # hours/month
        return None

    def _fetch_efs_price(self) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "volumeType", "Value": "Standard"},
        ]
        return self._call_pricing_api("AmazonEFS", filters)

    def _fetch_eip_price(self) -> float | None:
        # EIP pricing lives in AmazonVPC (not AmazonEC2) since AWS rebilled all
        # public IPv4 addresses in 2024. Both idle and in-use are $0.005/hr.
        # group=VPCPublicIPv4Address excludes the ContiguousBlock tier ($0.008/hr).
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "group", "Value": "VPCPublicIPv4Address"},
        ]
        hourly = self._call_pricing_api("AmazonVPC", filters)
        return hourly * 730 if hourly is not None else None

    def _fetch_nat_gateway_price(self) -> float | None:
        # productFamily=NAT Gateway returns 5 rows (hourly, per-GB, provisioned-Gbps,
        # provisioned-Bytes at $0.00, regional-hourly). Use _call_pricing_api_hourly
        # to skip per-GB and $0.00 rows by selecting only unit=Hrs results.
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
            {"Type": "TERM_MATCH", "Field": "operation", "Value": "NatGateway"},
        ]
        hourly = self._call_pricing_api_hourly("AmazonEC2", filters)
        return hourly * 730 if hourly is not None else None

    def _fetch_vpc_endpoint_price(self) -> float | None:
        # productFamily is "VpcEndpoint" (no space — not "VPC Endpoint").
        # endpointType=PrivateLink targets Interface Endpoints billed per-hour.
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "VpcEndpoint"},
            {"Type": "TERM_MATCH", "Field": "endpointType", "Value": "PrivateLink"},
        ]
        hourly = self._call_pricing_api("AmazonVPC", filters)
        return hourly * 730 if hourly is not None else None

    def _fetch_alb_price(self) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Load Balancer"},
        ]
        hourly = self._call_pricing_api("AWSELB", filters)
        return hourly * 730 if hourly is not None else None

    def _fetch_aurora_acu_price(self) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "usagetype", "Value": "ACU-Hour"},
        ]
        return self._call_pricing_api_hourly("AmazonRDS", filters)

    def _call_pricing_api_hourly(self, service_code: str, filters: list[dict]) -> float | None:
        """Like _call_pricing_api but returns the price from the first result whose unit is 'Hrs'.

        Used when a productFamily has multiple pricing dimensions (e.g. NAT Gateway has
        both hourly and per-GB rows). Fetches up to 10 results and skips non-hourly ones.
        """
        key_fields = {f["Field"]: f["Value"] for f in filters if f["Field"] not in _LOG_SKIP_FIELDS}
        logger.debug("pricing:GetProducts[hourly]  %s  %s  region=%s", service_code, key_fields, self._region)
        try:
            resp = self._pricing.get_products(
                ServiceCode=service_code,
                Filters=filters,
                MaxResults=10,
            )
            self._stats["api_calls"] += 1
            for raw in resp.get("PriceList", []):
                price, unit = _extract_usd_with_unit(json.loads(raw))
                if unit == "Hrs" and price is not None:
                    logger.debug("pricing:GetProducts[hourly]  %s  %s  → $%.6f/hr", service_code, key_fields, price)
                    return price
            logger.debug(
                "pricing:GetProducts[hourly]  %s  %s  → no hourly-unit result",
                service_code,
                key_fields,
            )
            return None
        except Exception as exc:
            self._stats["api_errors"] += 1
            logger.debug("pricing:GetProducts[hourly]  %s  %s  → FAILED: %s", service_code, key_fields, exc)
            return None

    def _call_pricing_api(self, service_code: str, filters: list[dict]) -> float | None:
        """
        Call pricing:GetProducts, parse the first result, return USD/unit as float.
        Returns None on any error (API unavailable, no results, parse failure).
        """
        key_fields = {f["Field"]: f["Value"] for f in filters if f["Field"] not in _LOG_SKIP_FIELDS}
        logger.debug("pricing:GetProducts  %s  %s  region=%s", service_code, key_fields, self._region)
        try:
            resp = self._pricing.get_products(
                ServiceCode=service_code,
                Filters=filters,
                MaxResults=1,
            )
            self._stats["api_calls"] += 1
            if not resp.get("PriceList"):
                logger.debug(
                    "pricing:GetProducts  %s  %s  → no results (filter mismatch or unsupported region)",
                    service_code,
                    key_fields,
                )
                return None
            price = _extract_usd(json.loads(resp["PriceList"][0]))
            logger.debug("pricing:GetProducts  %s  %s  → $%.6f", service_code, key_fields, price or 0)
            return price
        except Exception as exc:
            self._stats["api_errors"] += 1
            logger.debug(
                "pricing:GetProducts  %s  %s  → FAILED: %s",
                service_code,
                key_fields,
                exc,
            )
            return None


def _extract_usd(price_item: dict) -> float | None:
    """Navigate the nested Pricing API JSON to extract the USD On-Demand unit price."""
    try:
        on_demand = price_item["terms"]["OnDemand"]
        term = next(iter(on_demand.values()))
        dimension = next(iter(term["priceDimensions"].values()))
        usd_str = dimension["pricePerUnit"].get("USD", "0")
        value = float(usd_str)
        return value if value > 0 else None
    except (KeyError, StopIteration, ValueError):
        return None


def _extract_usd_with_unit(price_item: dict) -> tuple[float | None, str | None]:
    """Like _extract_usd but also returns the billing unit (e.g. 'Hrs', 'GB')."""
    try:
        on_demand = price_item["terms"]["OnDemand"]
        term = next(iter(on_demand.values()))
        dimension = next(iter(term["priceDimensions"].values()))
        usd_str = dimension["pricePerUnit"].get("USD", "0")
        value = float(usd_str)
        unit = dimension.get("unit")
        return (value if value > 0 else None), unit
    except (KeyError, StopIteration, ValueError):
        return None, None
