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
# gp3 provisioned throughput above the free 125 MiB/s baseline (us-east-1:
# $40.96/GiBps-mo = $0.04/MiBps-mo).
FALLBACK_EBS_THROUGHPUT_MIBPS_MONTH: float = 0.04
FALLBACK_EBS_SNAPSHOT_GB_MONTH: float = 0.05
FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH: float = 0.0125
FALLBACK_RDS_STORAGE_GB_MONTH: dict[str, float] = {
    "gp2": 0.115,
    "gp3": 0.115,
    "io1": 0.200,
}
FALLBACK_RDS_BACKUP_GB_MONTH: float = 0.095
# Aurora backup/snapshot storage is billed at a different (lower) rate than
# standard RDS: us-east-1/eu-west-1 Aurora = $0.021/GB-Mo vs standard $0.095/GB-Mo
# (verified via the Pricing API: usagetype EU-Aurora:BackupUsage). Pricing an
# Aurora snapshot at the standard rate overstates by ~4.5x.
FALLBACK_AURORA_BACKUP_GB_MONTH: float = 0.021
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

# RDS engines whose Multi-AZ deployment is published under the "SQL Server
# Mirror" deploymentOption rather than plain "Multi-AZ" (verified via the
# Pricing API: deploymentOption values include "Multi-AZ (SQL Server Mirror)").
_RDS_SQLSERVER_ENGINES: frozenset[str] = frozenset({
    "sqlserver-ee", "sqlserver-se", "sqlserver-ex", "sqlserver-web",
})

# RDS describe-API storage type -> AWS Pricing API 'volumeType' attribute value.
# The Price List uses human-readable labels ("General Purpose", "General
# Purpose-GP3", "Provisioned IOPS", "Provisioned IOPS-IO2"), NOT the gp2/gp3/io1
# wire names — filtering with the raw upper-cased name never matches and silently
# falls back. Verified via get_pricing_attribute_values('AmazonRDS','volumeType').
_RDS_STORAGE_VOLUME_TYPES: dict[str, str] = {
    "gp2": "General Purpose",
    "gp3": "General Purpose-GP3",
    "io1": "Provisioned IOPS",
    "io2": "Provisioned IOPS-IO2",
}


def _rds_multi_az_deployment_option(engine: str, *, multi_az: bool) -> str:
    """Return the Pricing API 'deploymentOption' value for an RDS engine.

    SQL Server publishes Multi-AZ under "Multi-AZ (SQL Server Mirror)"; all other
    engines use plain "Multi-AZ". Single-AZ is uniform across engines.
    """
    if not multi_az:
        return "Single-AZ"
    return "Multi-AZ (SQL Server Mirror)" if engine in _RDS_SQLSERVER_ENGINES else "Multi-AZ"


# RDS engine name -> AWS Pricing API 'databaseEdition' filter value. SQL Server
# and Oracle price *per edition* (verified via the Pricing API: db.m5.large
# SQL Server Web $0.311/hr vs Standard $0.977/hr; Oracle SE2 vs EE differ).
# The describe-API engine string encodes the edition, so without this filter the
# lookup matches several editions and MaxResults picks one non-deterministically.
# Engines without an edition dimension (MySQL/PostgreSQL/MariaDB/Aurora/Db2) are
# absent and get no databaseEdition filter.
_RDS_ENGINE_EDITIONS: dict[str, str] = {
    "sqlserver-ee": "Enterprise",
    "sqlserver-se": "Standard",
    "sqlserver-ex": "Express",
    "sqlserver-web": "Web",
    "oracle-ee": "Enterprise",
    "oracle-ee-cdb": "Enterprise",
    "oracle-se2": "Standard Two",
    "oracle-se2-cdb": "Standard Two",
    "oracle-se": "Standard One",
    "oracle-se1": "Standard One",
}

# RDS describe-API LicenseModel -> AWS Pricing API 'licenseModel' filter value.
_RDS_LICENSE_MODEL_LABELS: dict[str, str] = {
    "license-included": "License included",
    "bring-your-own-license": "Bring your own license",
    "general-public-license": "No license required",
    "postgresql-license": "No license required",
}


def _normalize_rds_license_model(rds_license_model: str | None, engine: str) -> str:
    """Resolve the Pricing API 'licenseModel' value for an RDS instance.

    Prefers the instance's actual ``LicenseModel`` (from ``describe_db_instances``)
    because it is the only reliable signal — Oracle SE2 License-Included
    ($0.438/hr) vs BYOL ($0.171/hr) is a 2.6x swing on the same class, and Oracle
    has NO "No license required" row at all (so the old engine-static default
    silently missed and fell back). When the instance value is absent, fall back
    to an engine-appropriate default: SQL Server and Oracle SE2 are License
    Included on RDS, other Oracle editions are BYOL, everything else carries no
    license charge.
    """
    if rds_license_model:
        mapped = _RDS_LICENSE_MODEL_LABELS.get(rds_license_model.strip().lower())
        if mapped:
            return mapped
    if engine in _RDS_SQLSERVER_ENGINES or engine in ("oracle-se2", "oracle-se2-cdb"):
        return "License included"
    if engine.startswith("oracle"):
        return "Bring your own license"
    return "No license required"
FALLBACK_S3_GB_MONTH: dict[str, float] = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "ONEZONE_IA": 0.01,
    "GLACIER_IR": 0.004,
    "GLACIER": 0.0036,
    "DEEP_ARCHIVE": 0.00099,
    "INTELLIGENT_TIERING": 0.023,
    "EXPRESS_ONE_ZONE": 0.11,
}

# Caller storage-class key → AWS Pricing API `volumeType` value. The Pricing
# API `storageClass` attribute is ambiguous (e.g. "Archive" covers both
# Glacier Flexible Retrieval and Deep Archive), so we pin `volumeType` instead
# and then select the timed-storage row (audit S3-E). INTELLIGENT_TIERING maps
# to its Frequent Access tier — the rate an active object is billed at.
_S3_VOLUME_TYPE_BY_CLASS: dict[str, str] = {
    "STANDARD": "Standard",
    "STANDARD_IA": "Standard - Infrequent Access",
    "ONEZONE_IA": "One Zone - Infrequent Access",
    "GLACIER_IR": "Glacier Instant Retrieval",
    "GLACIER": "Amazon Glacier",
    "DEEP_ARCHIVE": "Glacier Deep Archive",
    "INTELLIGENT_TIERING": "Intelligent-Tiering Frequent Access",
    "EXPRESS_ONE_ZONE": "Express One Zone",
}
FALLBACK_EFS_GB_MONTH: float = 0.30
# EFS $/GB-month by storage class (us-east-1 On-Demand, verified via Pricing API
# 2026-06). Keys are the AWS Pricing API `storageClass` attribute values.
FALLBACK_EFS_GB_MONTH_BY_CLASS: dict[str, float] = {
    "General Purpose": 0.30,
    "Infrequent Access": 0.025,
    "One Zone-General Purpose": 0.16,
    "One Zone-Infrequent Access": 0.0133,
    "Archive": 0.005,
}
# Caller-facing storage-class aliases → AWS Pricing API `storageClass` value.
_EFS_STORAGE_CLASS_LABELS: dict[str, str] = {
    "standard": "General Purpose",
    "general purpose": "General Purpose",
    "ia": "Infrequent Access",
    "infrequent access": "Infrequent Access",
    "one zone": "One Zone-General Purpose",
    "one zone-general purpose": "One Zone-General Purpose",
    "one zone-ia": "One Zone-Infrequent Access",
    "one zone-infrequent access": "One Zone-Infrequent Access",
    "archive": "Archive",
}
# FSx $/GB-month by (fileSystemType, storageType) (us-east-1 Single-AZ On-Demand,
# verified via Pricing API 2026-06; HDD only exists for Windows/Lustre/ONTAP).
FALLBACK_FSX_GB_MONTH: dict[tuple[str, str], float] = {
    ("WINDOWS", "SSD"): 0.130,
    ("WINDOWS", "HDD"): 0.013,
    ("LUSTRE", "SSD"): 0.145,
    ("LUSTRE", "HDD"): 0.025,
    ("ONTAP", "SSD"): 0.144,
    ("OPENZFS", "SSD"): 0.20,
}
# Network fallback constants reconciled to us-east-1 AWS list prices
# (verified via Pricing API 2026-05). Previous values reflected a
# higher-priced region (eu-west-1) which contradicted the per-shim
# fallback ternaries (`else 32.0` for NAT, `else 7.30` for VPC EP,
# `else 16.20` for ALB) — section 4.4 violation of two-different-
# fallback-prices-for-same-SKU.
FALLBACK_EIP_MONTH: float = 3.65       # $0.005/hr × 730 = $3.65/mo
FALLBACK_NAT_MONTH: float = 32.85      # $0.045/hr × 730 = $32.85/mo
FALLBACK_VPC_ENDPOINT_MONTH: float = 7.30   # $0.01/hr × 730 = $7.30/mo
FALLBACK_ALB_MONTH: float = 16.43      # $0.0225/hr × 730 = $16.43/mo
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
        # Lazily-built sibling engines for OTHER regions, sharing the (global)
        # pricing client but each with its own region-scoped cache. Lets adapters
        # price cross-region resources (e.g. S3 buckets, which are global) at the
        # resource's home-region rate instead of the scan region's (audit S3-I).
        self._siblings: dict[str, PricingEngine] = {}
        self._stats: dict[str, int] = {
            "api_calls": 0,
            "cache_hits": 0,
            "fallbacks": 0,
            "api_errors": 0,
        }

    def for_region(self, region_code: str) -> "PricingEngine":
        """Return a ``PricingEngine`` scoped to ``region_code``.

        Returns ``self`` when ``region_code`` matches this engine's region;
        otherwise returns a cached sibling that reuses the same global pricing
        client but keeps a separate region-correct cache. Sibling engines use a
        neutral ``fallback_multiplier`` of 1.0 (the scan-region multiplier does
        not apply to other regions); the live Pricing API path is region-correct
        regardless.
        """
        if region_code == self._region:
            return self
        sibling = self._siblings.get(region_code)
        if sibling is None:
            sibling = PricingEngine(region_code, self._pricing, fallback_multiplier=1.0)
            self._siblings[region_code] = sibling
        return sibling

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

    def _io2_tier_rate(self, group: str, fallback: float) -> float:
        """Region-correct $/IOPS-month for one io2 IOPS tier.

        AWS publishes the three io2 tiers as distinct SKUs distinguished by the
        ``group`` attribute (``EBS IOPS`` / ``EBS IOPS Tier 2`` / ``EBS IOPS
        Tier 3``), so each tier's rate is fetched directly for the region rather
        than approximated by scaling the base rate.
        """
        key = ("ebs_io2_tier", group)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_io2_tier_price(group)
        if price is None:
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for io2 '{group}' in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_ebs_io2_iops_cost(self, iops: int) -> float:
        """Total $/month for `iops` provisioned IOPS on an io2 volume.

        AWS io2 tiers (rates fetched per region from the tier-specific SKUs):
          0–32,000        IOPS @ base rate (≈ $0.065)
          32,001–64,000   IOPS @ tier 2 rate (≈ $0.0455, 30 % discount)
          > 64,000        IOPS @ tier 3 rate (≈ $0.032, 51 % discount)

        Flat-multiply by the base rate overcounts savings on big io2 volumes.
        """
        if iops <= 0:
            return 0.0
        base_rate = self._io2_tier_rate("EBS IOPS", FALLBACK_EBS_IOPS_MONTH["io2"])
        tier2_rate = self._io2_tier_rate("EBS IOPS Tier 2", FALLBACK_IO2_IOPS_TIER2_MONTH)
        tier3_rate = self._io2_tier_rate("EBS IOPS Tier 3", FALLBACK_IO2_IOPS_TIER3_MONTH)
        cost = min(iops, 32000) * base_rate
        if iops > 32000:
            cost += min(iops - 32000, 32000) * tier2_rate
        if iops > 64000:
            cost += (iops - 64000) * tier3_rate
        return cost

    def get_ebs_throughput_monthly_price(self, volume_type: str = "gp3") -> float:
        """$/MiBps-month for provisioned EBS throughput above the free baseline.

        gp3 bills provisioned throughput above 125 MiB/s. The AWS SKU is priced
        per GiBps-month (≈ $40.96), so the live value is converted to per-MiBps
        (÷1024) to match how callers meter throughput in MiB/s.
        """
        key = ("ebs_throughput", volume_type)
        if (cached := self._get_cached(key)) is not None:
            return cached
        raw = self._fetch_ebs_throughput_price(volume_type)
        if raw is None:
            price = self._use_fallback(
                FALLBACK_EBS_THROUGHPUT_MIBPS_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for EBS {volume_type} throughput in {self._region}; using fallback",
            )
        else:
            # SKU is priced per GiBps-month; convert to per-MiBps-month.
            price = raw / 1024.0
        self._cache.set(key, price)
        return price

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
        license_model: str | None = None,
        aurora_io_optimized: bool = False,
    ) -> float:
        """$/month for an RDS DB instance, engine/edition/license/deployment-aware.

        Args:
            engine: RDS engine name as returned by ``describe_db_instances`` (e.g.
                ``"mysql"``, ``"postgres"``, ``"sqlserver-ex"``, ``"aurora-postgresql"``).
                Case-insensitive. The engine string also encodes the edition for
                SQL Server / Oracle (``sqlserver-ee`` -> Enterprise, etc.), which is
                pinned as a ``databaseEdition`` filter.
            instance_class: RDS instance class string (e.g. ``"db.t3.medium"``).
            multi_az: When True, fetches the Multi-AZ deployment-option SKU.
            license_model: The instance's ``LicenseModel`` from describe-API
                (``license-included`` / ``bring-your-own-license`` / …). Required
                for accurate Oracle pricing (LI vs BYOL differ ~2.6x). When omitted
                an engine-appropriate default is used.

        Returns:
            Monthly on-demand price in USD (730 hours). Returns 0.0 when both the
            Pricing API and the fallback constant are unavailable for the given key.

        Notes:
            RDS pricing requires ``databaseEngine``, ``deploymentOption``,
            ``licenseModel`` and (for SQL Server/Oracle) ``databaseEdition`` filters
            in addition to ``instanceType`` + ``location`` to disambiguate. The
            generic ``get_instance_monthly_price`` is not sufficient for RDS.
        """
        normalized = engine.lower().strip()
        resolved_license = _normalize_rds_license_model(license_model, normalized)
        key = (
            "rds_instance", normalized, instance_class,
            "Multi-AZ" if multi_az else "Single-AZ", resolved_license,
            "io-opt" if aurora_io_optimized else "std",
        )
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_rds_instance_price(
            normalized, instance_class, multi_az=multi_az, license_model=license_model,
            aurora_io_optimized=aurora_io_optimized,
        )
        if price is None:
            fallback = FALLBACK_RDS_INSTANCE_MONTHLY * (FALLBACK_RDS_MULTI_AZ_FACTOR if multi_az else 1.0)
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for RDS {instance_class} {normalized} "
                f"({'Multi-AZ' if multi_az else 'Single-AZ'}) in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_rds_backup_storage_price_per_gb(self, engine: str | None = None) -> float:
        """$/GB/month for RDS backup/snapshot storage in self._region.

        Args:
            engine: RDS engine (from describe-API). Aurora engines are billed at a
                distinct, lower backup rate ($0.021/GB-Mo) than standard RDS
                ($0.095/GB-Mo); pricing an Aurora snapshot at the standard rate
                overstates ~4.5x (audit C-A1). Omit / non-Aurora -> standard rate.
        """
        normalized = (engine or "").lower().strip()
        is_aurora = normalized.startswith("aurora")
        # Aurora flavours share one backup rate; "Aurora MySQL" is a safe
        # representative label for the lookup. Standard engines also share one
        # rate; pin "MySQL" for determinism (as before).
        engine_label = "Aurora MySQL" if is_aurora else "MySQL"
        key = ("rds_backup", "aurora" if is_aurora else "standard")
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_rds_backup_price(engine_label)
        if price is None:
            fallback = FALLBACK_AURORA_BACKUP_GB_MONTH if is_aurora else FALLBACK_RDS_BACKUP_GB_MONTH
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for {'Aurora' if is_aurora else 'RDS'} backup storage "
                f"in {self._region}; using fallback",
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

    def get_ec2_hourly_price(
        self,
        instance_type: str,
        os: str = "Linux",
        license_model: str = "No License required",
        quiet: bool = False,
    ) -> float:
        """On-Demand hourly price for EC2 instance_type in self._region.

        ``license_model`` pins the AWS Pricing ``licenseModel`` attribute so the
        lookup is deterministic. "No License required" is the published
        on-demand list price (license included for Windows); pass
        "Bring your own license" for BYOL instances, which are billed at the
        lower base-compute rate.

        ``quiet=True`` suppresses the fallback warning and returns 0.0 on a miss.
        Use it for *speculative* lookups (e.g. probing a candidate rightsizing
        target that may not exist for the family) where a miss is expected and
        not a data-quality concern.
        """
        key = ("ec2_hourly", instance_type, os, license_model)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_ec2_price(instance_type, os, license_model)
        if price is None:
            if quiet:
                return 0.0  # not cached: a transient miss for a speculative type
            price = self._use_fallback(
                0.0,
                f"Pricing API returned no result for EC2 {instance_type} ({os}, {license_model}) in {self._region}",
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

    def get_efs_monthly_price_per_gb(self, storage_class: str = "Standard") -> float:
        """$/GB/month for an EFS storage class in self._region.

        Args:
            storage_class: ``Standard`` (default), ``IA``, ``One Zone``,
                ``One Zone-IA``, or ``Archive`` (case-insensitive; the AWS
                Pricing API ``storageClass`` label is also accepted).
        """
        api_class = _EFS_STORAGE_CLASS_LABELS.get(storage_class.strip().lower(), "General Purpose")
        key = ("efs_gb", api_class)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_efs_price(api_class)
        if price is None:
            fallback = FALLBACK_EFS_GB_MONTH_BY_CLASS.get(api_class, FALLBACK_EFS_GB_MONTH)
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for EFS {api_class} in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_fsx_storage_price_per_gb(
        self, file_system_type: str, storage_type: str, deployment_option: str = "Single-AZ"
    ) -> float:
        """$/GB/month for FSx provisioned storage in self._region.

        Args:
            file_system_type: ``Windows`` | ``Lustre`` | ``ONTAP`` | ``OpenZFS`` (case-insensitive).
            storage_type: ``SSD`` | ``HDD`` (case-insensitive).
            deployment_option: AWS Pricing ``deploymentOption`` (default ``Single-AZ``);
                only used to disambiguate the live lookup, not the fallback.
        """
        fs_type = file_system_type.strip().upper()
        st = storage_type.strip().upper()
        key = ("fsx_gb", fs_type, st, deployment_option)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_fsx_storage_price(fs_type, st, deployment_option)
        if price is None:
            fallback = FALLBACK_FSX_GB_MONTH.get((fs_type, st))
            if fallback is None:
                fallback = FALLBACK_FSX_GB_MONTH.get((fs_type, "SSD"), 0.15)
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for FSx {fs_type} {st} in {self._region}; using fallback",
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

    def _fetch_ec2_price(
        self, instance_type: str, os: str, license_model: str = "No License required"
    ) -> float | None:
        # licenseModel is pinned so the lookup is deterministic. Without it,
        # Windows matches three SKUs (license-included $0.233, license-infra
        # $0.141, BYOL $0.141) and MaxResults=1 would pick one
        # non-deterministically. "No License required" is AWS's published
        # on-demand rate (license included); "Bring your own license" is the
        # lower BYOL rate used when DescribeInstances reports a BYOL platform.
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "capacityStatus", "Value": "Used"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
            {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": license_model},
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
        # io2 has three IOPS SKUs (base + tier 2 + tier 3); pin the base group so
        # MaxResults=1 cannot non-deterministically return a discounted tier.
        if volume_type == "io2":
            filters.append({"Type": "TERM_MATCH", "Field": "group", "Value": "EBS IOPS"})
        return self._call_pricing_api("AmazonEC2", filters)

    def _fetch_io2_tier_price(self, group: str) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": "io2"},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "System Operation"},
            {"Type": "TERM_MATCH", "Field": "group", "Value": group},
        ]
        return self._call_pricing_api("AmazonEC2", filters)

    def _fetch_ebs_throughput_price(self, volume_type: str) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": volume_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Provisioned Throughput"},
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
        license_model: str | None = None,
        aurora_io_optimized: bool = False,
    ) -> float | None:
        """Fetch on-demand monthly RDS instance price; None on miss.

        Pins ``databaseEdition`` (for SQL Server / Oracle), ``licenseModel`` (from
        the instance's actual ``LicenseModel``) and — for Aurora — the ``storage``
        mode (Standard "EBS Only" vs "Aurora IO Optimization Mode") so the lookup
        is deterministic; otherwise multiple editions / license / storage-mode rows
        match and MaxResults picks one arbitrarily (Aurora I/O-Optimized is ~30%
        dearer than Standard for the same class).
        """
        engine_label = _RDS_ENGINE_LABELS.get(engine)
        if engine_label is None:
            # Don't silently price an unmapped engine as MySQL — record it so the
            # mismatch is visible, then fall back to MySQL as a best effort.
            self.warnings.append(
                f"Unknown RDS engine '{engine}' — pricing as MySQL; verify before relying on the figure"
            )
            engine_label = "MySQL"
        resolved_license = _normalize_rds_license_model(license_model, engine)
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": engine_label},
            {"Type": "TERM_MATCH", "Field": "deploymentOption",
             "Value": _rds_multi_az_deployment_option(engine, multi_az=multi_az)},
            {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": resolved_license},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Instance"},
        ]
        edition = _RDS_ENGINE_EDITIONS.get(engine)
        if edition:
            filters.append({"Type": "TERM_MATCH", "Field": "databaseEdition", "Value": edition})
        if engine.startswith("aurora"):
            # Aurora rows are split by storage mode; pin it so Standard and
            # I/O-Optimized don't collide (EBS Only $5.12 vs IO mode $6.656).
            storage_value = "Aurora IO Optimization Mode" if aurora_io_optimized else "EBS Only"
            filters.append({"Type": "TERM_MATCH", "Field": "storage", "Value": storage_value})
        hourly = self._call_pricing_api_hourly("AmazonRDS", filters)
        return hourly * 730 if hourly is not None else None

    def _fetch_rds_storage_price(self, storage_type: str, *, multi_az: bool = False) -> float | None:
        deployment_option = "Multi-AZ" if multi_az else "Single-AZ"
        # Map the gp2/gp3/io1/io2 wire name to the Price List 'volumeType' label;
        # the raw upper-cased name ("GP2") never matches and silently falls back.
        volume_type_label = _RDS_STORAGE_VOLUME_TYPES.get(storage_type.lower(), storage_type)
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Storage"},
            {"Type": "TERM_MATCH", "Field": "volumeType", "Value": volume_type_label},
            {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deployment_option},
        ]
        return self._call_pricing_api("AmazonRDS", filters)

    def _fetch_rds_backup_price(self, engine_label: str = "MySQL") -> float | None:
        # Pin databaseEngine so the MaxResults=1 lookup is deterministic: without it
        # the loose filter could return the wrong engine family (Aurora $0.021 vs
        # standard $0.095, or an RDS Custom row). All standard engines share one
        # rate (MySQL is a safe representative); Aurora flavours share another
        # ("Aurora MySQL" represents them). See get_rds_backup_storage_price_per_gb.
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": engine_label},
        ]
        return self._call_pricing_api("AmazonRDS", filters)

    def _fetch_s3_price(self, storage_class: str) -> float | None:
        """$/GB-month for an S3 storage class via the Pricing API.

        Pins ``volumeType`` + ``productFamily=Storage`` and then selects the
        timed-storage row (excluding Staging/Overhead SKUs) and its base usage
        tier (``beginRange == "0"``). The previous implementation filtered on
        ``storageClass`` alone with ``MaxResults=1``, which (a) returned an
        arbitrary tiered dimension — $0.022 instead of the $0.023 marginal
        Standard rate (audit S3-D) — and (b) used ``storageClass`` labels that
        don't exist as attribute values for Glacier/One-Zone classes, silently
        falling back to constants (audit S3-E).
        """
        volume_type = _S3_VOLUME_TYPE_BY_CLASS.get(storage_class, "Standard")
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
            {"Type": "TERM_MATCH", "Field": "volumeType", "Value": volume_type},
        ]
        return self._select_s3_storage_rate(filters)

    def _select_s3_storage_rate(self, filters: list[dict]) -> float | None:
        """Pick the canonical timed-storage $/GB-month from S3 Pricing results.

        S3 returns several SKUs per ``volumeType`` (timed storage, staging,
        per-object overhead, retrieval). We keep only the ``TimedStorage*``
        storage SKU (skipping Staging/Overhead) and read its base usage tier.
        """
        key_fields = {f["Field"]: f["Value"] for f in filters if f["Field"] not in _LOG_SKIP_FIELDS}
        try:
            resp = self._pricing.get_products(
                ServiceCode="AmazonS3",
                Filters=filters,
                MaxResults=100,
            )
            self._stats["api_calls"] += 1
            price_list = resp.get("PriceList", [])
            if not price_list:
                logger.debug("pricing:GetProducts  AmazonS3  %s  → no results", key_fields)
                return None
            for raw in price_list:
                item = json.loads(raw)
                usagetype = item.get("product", {}).get("attributes", {}).get("usagetype", "")
                if "TimedStorage" not in usagetype:
                    continue
                if "Staging" in usagetype or "Overhead" in usagetype:
                    continue
                price = _extract_s3_base_rate(item)
                if price is not None:
                    logger.debug("pricing:GetProducts  AmazonS3  %s  → $%.6f", key_fields, price)
                    return price
            logger.debug("pricing:GetProducts  AmazonS3  %s  → no timed-storage row", key_fields)
            return None
        except Exception as exc:
            self._stats["api_errors"] += 1
            logger.debug("pricing:GetProducts  AmazonS3  %s  → FAILED: %s", key_fields, exc)
            return None

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

    def _fetch_efs_price(self, storage_class: str = "General Purpose") -> float | None:
        # EFS prices by the `storageClass` attribute (there is NO `volumeType`
        # attribute on AmazonEFS — the previous filter silently never matched).
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "storageClass", "Value": storage_class},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
        ]
        return self._call_pricing_api("AmazonEFS", filters)

    def _fetch_fsx_storage_price(
        self, file_system_type: str, storage_type: str, deployment_option: str
    ) -> float | None:
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
            {"Type": "TERM_MATCH", "Field": "fileSystemType", "Value": file_system_type.capitalize()},
            {"Type": "TERM_MATCH", "Field": "storageType", "Value": storage_type},
            {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deployment_option},
        ]
        return self._call_pricing_api("AmazonFSx", filters)

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


def _extract_s3_base_rate(price_item: dict) -> float | None:
    """Return the base-tier ($/GB-month) OnDemand rate for an S3 storage SKU.

    S3 Standard (and the Intelligent-Tiering Frequent Access tier) encode
    volume-tiered pricing as multiple ``priceDimensions`` within one OnDemand
    term (first 50 TB / next 450 TB / over 500 TB). We deliberately select the
    base tier (``beginRange == "0"``, e.g. $0.023) rather than whichever
    dimension the API happens to serialize first (audit S3-D).
    """
    try:
        on_demand = price_item["terms"]["OnDemand"]
        term = next(iter(on_demand.values()))
        dimensions = term["priceDimensions"]
        chosen = None
        for dim in dimensions.values():
            if dim.get("beginRange") == "0":
                chosen = dim
                break
        if chosen is None:
            chosen = next(iter(dimensions.values()))
        value = float(chosen["pricePerUnit"].get("USD", "0"))
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
