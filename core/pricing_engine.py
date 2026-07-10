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
    # AWS Price List location names are inconsistent for EU regions: the older
    # regions use "EU (X)" while the two newest (Zurich, Spain) use "Europe (X)".
    # Verified across AmazonS3 / AmazonEC2 / AmazonRDS (2026-06-23). Using the
    # wrong form makes EVERY pricing lookup for that region silently fall back to
    # us-east-1 constants (audit S3-J — found when S3-I began pricing buckets at
    # their home region).
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-central-2": "Europe (Zurich)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
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
# Aurora I/O-Optimized STORAGE premium over Standard storage, $/GB-Mo. Live
# us-east-1 (Pricing API 2026-06): Aurora:IO-OptimizedStorageUsage $0.225 −
# Aurora:StorageUsage $0.10 = $0.125/GB-Mo. The Aurora adapter's old hardcoded
# 0.025 was ~5x too low, inflating every io_tier_optimization saving. Used only
# when the live API is unavailable; multiplied by the regional fallback multiplier.
FALLBACK_AURORA_IO_STORAGE_PREMIUM_GB_MONTH: float = 0.125
# Aurora Standard-tier per-request I/O rate, $/1M requests. Live (Pricing API
# 2026-06, usagetype suffix Aurora:StorageIOUsage, unit "IOs"): $0.20/M us-east-1,
# $0.22/M eu-central-1 / eu-west-1. The adapter previously scaled the $0.20
# us-east-1 constant by the regional pricing_multiplier (1.12 → $0.224 in
# Frankfurt) which over-states vs the live $0.22; used only when the live API
# is unavailable, multiplied by the regional fallback multiplier.
FALLBACK_AURORA_IO_RATE_PER_MILLION: float = 0.20
# Single-AZ db.t3.medium MySQL us-east-1 on-demand monthly cost (730h × $0.068/h).
# Used only when AWS Pricing API is unavailable; multiplied by the regional fallback multiplier.
FALLBACK_RDS_INSTANCE_MONTHLY: float = 49.64
# Multiplier applied to FALLBACK_RDS_INSTANCE_MONTHLY for Multi-AZ deployments
# (Multi-AZ is roughly 2× Single-AZ for all engines per AWS Pricing API).
FALLBACK_RDS_MULTI_AZ_FACTOR: float = 2.0
# Single-AZ dms.t3.medium us-east-1 on-demand monthly cost (730h × $0.0745/hr,
# verified via Pricing API 2026-06). Used only when the AWS Pricing API is
# unavailable; ×FALLBACK_DMS_MULTI_AZ_FACTOR for Multi-AZ (Multi-AZ DMS is
# exactly 2x Single-AZ — dms.t3.medium $0.0745/hr vs $0.149/hr).
FALLBACK_DMS_INSTANCE_MONTHLY: float = 54.39
FALLBACK_DMS_MULTI_AZ_FACTOR: float = 2.0

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
_RDS_SQLSERVER_ENGINES: frozenset[str] = frozenset(
    {
        "sqlserver-ee",
        "sqlserver-se",
        "sqlserver-ex",
        "sqlserver-web",
    }
)

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
    "Archive": 0.008,
}
# EFS Infrequent Access per-GB DATA ACCESS charge (read or write; same rate in a
# region). Billed whenever IA-resident data is read/written — must be netted out
# of any IA-lifecycle saving. us-east-1 On-Demand, verified via Pricing API 2026-06.
FALLBACK_EFS_IA_ACCESS_GB: float = 0.01
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
# verified via Pricing API 2026-06). HDD storage exists only for Windows and
# Lustre (Persistent); ONTAP storage is SSD + capacity-pool, OpenZFS is SSD-only.
FALLBACK_FSX_GB_MONTH: dict[tuple[str, str], float] = {
    ("WINDOWS", "SSD"): 0.130,
    ("WINDOWS", "HDD"): 0.013,
    ("LUSTRE", "SSD"): 0.145,
    ("LUSTRE", "HDD"): 0.025,
    ("ONTAP", "SSD"): 0.125,
    ("OPENZFS", "SSD"): 0.09,
}
# Multi-AZ FSx $/GB-month (us-east-1 On-Demand, verified via Pricing API 2026-06).
# Consulted on the fallback path when the deployment is Multi-AZ; the live lookup
# already resolves the distinct Multi-AZ SKU (Multi-AZ is NOT a flat x2 of
# Single-AZ). Lustre has no Multi-AZ deployment, so it is absent here.
FALLBACK_FSX_MULTI_AZ_GB_MONTH: dict[tuple[str, str], float] = {
    ("WINDOWS", "SSD"): 0.230,
    ("WINDOWS", "HDD"): 0.025,
    ("ONTAP", "SSD"): 0.250,
    ("OPENZFS", "SSD"): 0.18,
}
# AWS Pricing API ``fileSystemType`` attribute values, keyed by the upper-cased
# token the adapter passes. ``str.capitalize()`` mangles ONTAP -> "Ontap" and
# OPENZFS -> "Openzfs", which never match, so the live lookup must use this map.
_FSX_FILE_SYSTEM_TYPE_LABELS: dict[str, str] = {
    "WINDOWS": "Windows",
    "LUSTRE": "Lustre",
    "ONTAP": "ONTAP",
    "OPENZFS": "OpenZFS",
}
# Network fallback constants reconciled to us-east-1 AWS list prices
# (verified via Pricing API 2026-05). Previous values reflected a
# higher-priced region (eu-west-1) which contradicted the per-shim
# fallback ternaries (`else 32.0` for NAT, `else 7.30` for VPC EP,
# `else 16.20` for ALB) — section 4.4 violation of two-different-
# fallback-prices-for-same-SKU.
FALLBACK_EIP_MONTH: float = 3.65  # $0.005/hr × 730 = $3.65/mo
FALLBACK_NAT_MONTH: float = 32.85  # $0.045/hr × 730 = $32.85/mo
FALLBACK_VPC_ENDPOINT_MONTH: float = 7.30  # $0.01/hr × 730 = $7.30/mo
FALLBACK_ALB_MONTH: float = 16.43  # $0.0225/hr × 730 = $16.43/mo (Load Balancer-Application)
FALLBACK_NLB_MONTH: float = 16.43  # $0.0225/hr × 730 = $16.43/mo (Load Balancer-Network, same base as ALB)
FALLBACK_GWLB_MONTH: float = 9.13  # $0.0125/hr × 730 = $9.13/mo (Load Balancer-Gateway)
FALLBACK_CLB_MONTH: float = 18.25  # $0.025/hr × 730 = $18.25/mo (Classic Load Balancer)
FALLBACK_AURORA_ACU_HOURLY: float = 0.12  # us-east-1 Aurora Serverless v2 ACU-Hr list rate
SAGEMAKER_OVER_EC2: float = 1.15
# MSK broker $/hr expressed as a multiple of the equivalent EC2 on-demand rate.
# Used only when the live AmazonMSK Broker-hours SKU is unavailable. Validated
# us-east-1 (Pricing API 2026-06): Kafka.m5.large $0.21/hr vs EC2 m5.large
# $0.096/hr = 2.19x. The previous 1.4x markup understated broker cost ~36%.
MSK_BROKER_OVER_EC2: float = 2.19
# Last-ditch MSK broker $/hr used only when BOTH the live MSK lookup and EC2
# pricing fail; region-scaled via the fallback multiplier at the call site.
FALLBACK_MSK_BROKER_HOURLY: float = 0.15

# AWS Fargate compute rates (us-east-1, verified via Pricing API 2026-06).
# Keyed by (architecture, os). ARM is ~20% cheaper than x86; Windows adds a
# higher vCPU/GB rate plus a separate per-vCPU OS license fee. These are
# fallbacks only — the live API value is region-correct and preferred.
FALLBACK_FARGATE_VCPU_HOURLY: dict[tuple[str, str], float] = {
    ("x86", "linux"): 0.04048,
    ("arm", "linux"): 0.03238,
    ("x86", "windows"): 0.046552,
}
FALLBACK_FARGATE_GB_HOURLY: dict[tuple[str, str], float] = {
    ("x86", "linux"): 0.004445,
    ("arm", "linux"): 0.00356,
    ("x86", "windows"): 0.0051117,
}
# Per-vCPU Windows OS license fee (USE1-Fargate-Windows-OS-Hours:perCPU).
FALLBACK_FARGATE_WINDOWS_OS_HOURLY: float = 0.046
# Ephemeral storage above the 20 GB free allotment.
FALLBACK_FARGATE_EPHEMERAL_GB_HOURLY: float = 0.000111

# AWS EKS control-plane and Extended Support rates (us-east-1, Pricing API
# 2026-06). Extended Support is a SURCHARGE billed *in addition* to the base
# per-cluster fee once a cluster's Kubernetes version exits standard support.
FALLBACK_EKS_CONTROL_PLANE_HOURLY: float = 0.10
FALLBACK_EKS_EXTENDED_SUPPORT_HOURLY: float = 0.50

# AWS ECR standard (non-archive) image storage, $/GB-month (Pricing API 2026-06).
FALLBACK_ECR_GB_MONTH: float = 0.10

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

    def drain_warnings(self) -> dict[str, int]:
        """Aggregate every fallback warning into a ``message -> count`` map.

        Includes this engine plus all lazily-built region siblings (so a
        cross-region fallback is not lost). Each ``_use_fallback`` call appends a
        message, so the same rate can repeat once per priced resource; the count
        captures how many resources were affected without flooding the report
        with duplicates. The orchestrator uses this to surface pricing fallbacks
        in the scan diagnostics — disclosing exactly which rates were estimated
        rather than fetched live (a fallback constant is not an account-specific
        live rate).
        """
        counts: dict[str, int] = {}
        for engine in (self, *self._siblings.values()):
            for message in engine.warnings:
                counts[message] = counts.get(message, 0) + 1
        return counts

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
            fallback = FALLBACK_EBS_SNAPSHOT_ARCHIVE_GB_MONTH if archive_tier else FALLBACK_EBS_SNAPSHOT_GB_MONTH
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
            "rds_instance",
            normalized,
            instance_class,
            "Multi-AZ" if multi_az else "Single-AZ",
            resolved_license,
            "io-opt" if aurora_io_optimized else "std",
        )
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_rds_instance_price(
            normalized,
            instance_class,
            multi_az=multi_az,
            license_model=license_model,
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
        """On-Demand $/broker-hour for an MSK broker instance in self._region.

        Selects the deterministic AmazonMSK Broker-hours SKU
        (``computeFamily=<type>`` + ``productFamily='Managed Streaming for Apache
        Kafka (MSK)'`` + ``operation='RunBroker'``; usagetype
        ``<region>-Kafka.<type>``, unit "hours"). Strips a ``kafka.`` prefix if
        present.

        On a live miss falls back to EC2 on-demand × :data:`MSK_BROKER_OVER_EC2`
        (the broker premium ≈ 2.19×, validated us-east-1 m5.large: $0.21 broker
        vs $0.096 EC2 — the previous 1.4× was ~36% low); when EC2 pricing is also
        unavailable, a region-scaled :data:`FALLBACK_MSK_BROKER_HOURLY` constant.
        """
        clean_type = instance_type.replace("kafka.", "")
        key = ("msk_hourly", clean_type)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_msk_broker_price(clean_type)
        if price is None:
            try:
                # quiet: a missing EC2 proxy here is expected (degraded path),
                # not a data-quality signal — don't emit a spurious EC2 warning.
                ec2_price = self.get_ec2_hourly_price(clean_type, quiet=True)
            except Exception:
                ec2_price = 0.0
            if ec2_price and ec2_price > 0:
                fallback_price = ec2_price * MSK_BROKER_OVER_EC2
            else:
                fallback_price = FALLBACK_MSK_BROKER_HOURLY * self._fallback_multiplier
            price = self._use_fallback(
                fallback_price,
                f"Pricing API unavailable for MSK {clean_type} in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_instance_monthly_price(self, service_code: str, instance_type: str, *, engine: str | None = None) -> float:
        """On-Demand monthly price for non-EC2 instances (ElastiCache, OpenSearch, etc.).

        For ``AmazonElastiCache`` pass the cluster ``engine`` (Redis/Memcached/
        Valkey) so the canonical ``NodeUsage:<type>`` SKU is selected
        deterministically. Without it six SKUs (ExtendedSupport, SyncDurability,
        per-engine NodeUsage) share the instance type and the rate is
        ambiguous — see SR-1 / ElastiCache C2.
        """
        key = (service_code, "monthly", instance_type, engine)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_generic_instance_price(service_code, instance_type, engine=engine)
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

    def get_efs_ia_access_price_per_gb(self) -> float:
        """$/GB EFS Infrequent Access data-access charge (read/write) in self._region.

        Used to net the IA read/write charge out of an IA-lifecycle saving so the
        reported number is a NET, not a gross, saving.
        """
        key = ("efs_ia_access",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_efs_ia_access_price()
        if price is None:
            price = self._use_fallback(
                FALLBACK_EFS_IA_ACCESS_GB * self._fallback_multiplier,
                f"Pricing API unavailable for EFS IA access in {self._region}; using fallback",
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
            is_multi_az = "MULTI" in deployment_option.upper()
            table = FALLBACK_FSX_MULTI_AZ_GB_MONTH if is_multi_az else FALLBACK_FSX_GB_MONTH
            fallback = (
                table.get((fs_type, st))
                or table.get((fs_type, "SSD"))
                or FALLBACK_FSX_GB_MONTH.get((fs_type, st))
                or FALLBACK_FSX_GB_MONTH.get((fs_type, "SSD"), 0.15)
            )
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for FSx {fs_type} {st} ({deployment_option}) in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_eip_monthly_price(self) -> float:
        key = ("eip_month",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_eip_price()
        if price is None:
            # Public IPv4 / EIP is billed at a FLAT $0.005/hr ($3.65/mo) in every
            # commercial region — it does NOT vary by region, so the fallback must
            # NOT be region-scaled by `_fallback_multiplier` (doing so fabricated a
            # region-specific rate for a globally flat charge). Route53-class fix.
            price = self._use_fallback(
                FALLBACK_EIP_MONTH,
                f"Pricing API unavailable for EIP in {self._region}; using flat fallback",
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
        return self._lb_monthly_price(
            key=("alb_month",),
            product_family="Load Balancer-Application",
            operation="LoadBalancing:Application",
            fallback=FALLBACK_ALB_MONTH,
            label="ALB",
        )

    def get_nlb_monthly_price(self) -> float:
        return self._lb_monthly_price(
            key=("nlb_month",),
            product_family="Load Balancer-Network",
            operation="LoadBalancing:Network",
            fallback=FALLBACK_NLB_MONTH,
            label="NLB",
        )

    def get_gwlb_monthly_price(self) -> float:
        return self._lb_monthly_price(
            key=("gwlb_month",),
            product_family="Load Balancer-Gateway",
            operation="LoadBalancing:Gateway",
            fallback=FALLBACK_GWLB_MONTH,
            label="GWLB",
        )

    def get_clb_monthly_price(self) -> float:
        return self._lb_monthly_price(
            key=("clb_month",),
            product_family="Load Balancer",
            operation="LoadBalancing",
            fallback=FALLBACK_CLB_MONTH,
            label="Classic LB",
        )

    def _lb_monthly_price(
        self, *, key: tuple, product_family: str, operation: str, fallback: float, label: str
    ) -> float:
        """Shared monthly-price lookup for the four ELB types.

        Each ELB type has its own ``productFamily`` ("Load Balancer-Application"
        for ALB, "-Network" for NLB, "-Gateway" for GWLB, bare "Load Balancer"
        for Classic). The previous ALB lookup filtered ``productFamily="Load
        Balancer"`` which matches ONLY the Classic LB SKU ($0.025/hr), so it
        silently returned the Classic rate instead of the ALB rate.
        """
        if (cached := self._get_cached(key)) is not None:
            return cached
        hourly = self._fetch_lb_base_hourly(product_family, operation)
        price = hourly * 730 if hourly is not None else None
        if price is None:
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for {label} in {self._region}; using fallback",
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

    def get_aurora_io_storage_premium_per_gb(self) -> float:
        """$/GB-month premium of Aurora I/O-Optimized storage over Standard.

        Returns ``(Aurora:IO-OptimizedStorageUsage − Aurora:StorageUsage)`` per
        GB-month in ``self._region`` — both are ``productFamily="Database
        Storage"`` rows on ``AmazonRDS`` billed in ``GB-Mo``. Live us-east-1
        (Pricing API 2026-06): ``$0.225 − $0.10 = $0.125/GB-Mo``. This is the
        additional storage charge a cluster incurs by switching its consumed
        storage to the I/O-Optimized tier; the Aurora adapter nets it (with the
        per-member instance premium) against the saved per-request I/O charges.
        Region-scaled once via the live API; on an API miss returns the
        documented :data:`FALLBACK_AURORA_IO_STORAGE_PREMIUM_GB_MONTH` ×
        fallback_multiplier.

        The exact suffixes are matched so neither tier collides with the other
        (``Aurora:StorageUsage`` is not a suffix of
        ``Aurora:IO-OptimizedStorageUsage``) nor with the $0.00
        ``Aurora:IO-OptimizedStorageUsage-LimitlessPreview`` decoy.
        """
        key = ("aurora_io_storage_premium",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        standard = self._fetch_aurora_storage_rate("Aurora:StorageUsage")
        optimized = self._fetch_aurora_storage_rate("Aurora:IO-OptimizedStorageUsage")
        if standard is not None and optimized is not None and optimized > standard:
            premium = optimized - standard
        else:
            premium = self._use_fallback(
                FALLBACK_AURORA_IO_STORAGE_PREMIUM_GB_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for Aurora I/O-Optimized storage premium in {self._region}; using fallback",
            )
        self._cache.set(key, premium)
        return premium

    def get_aurora_io_rate_per_million(self) -> float:
        """$/1M Aurora Standard-tier I/O requests in ``self._region``.

        The per-request I/O charge a Standard cluster pays and that switching to
        the I/O-Optimized tier eliminates. Sourced live from the Pricing API
        (``productFamily="System Operation"``, usagetype suffix
        ``Aurora:StorageIOUsage``, unit "IOs"): $0.20/M us-east-1, $0.22/M
        eu-central-1 / eu-west-1. The exact suffix avoids the $0.00
        ``Aurora:StorageIOUsage-LimitlessPreview`` decoy. Region-correct once via
        the live API; on a miss returns
        :data:`FALLBACK_AURORA_IO_RATE_PER_MILLION` × fallback_multiplier.
        """
        key = ("aurora_io_rate_per_million",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        rate = self._fetch_aurora_io_rate_per_million()
        if rate is None:
            rate = self._use_fallback(
                FALLBACK_AURORA_IO_RATE_PER_MILLION * self._fallback_multiplier,
                f"Pricing API unavailable for Aurora I/O rate in {self._region}; using fallback",
            )
        self._cache.set(key, rate)
        return rate

    def get_aurora_io_instance_premium_monthly(
        self,
        engine: str,
        instance_class: str,
        *,
        multi_az: bool = False,
        license_model: str | None = None,
    ) -> float:
        """$/month premium of running ONE Aurora member in I/O-Optimized mode.

        Returns ``io_optimized_price − standard_price`` for a single provisioned
        Aurora instance (``engine`` ``aurora-mysql`` / ``aurora-postgresql``),
        i.e. the extra instance cost the cluster pays under the I/O-Optimized
        configuration. The I/O-Optimized instance rate carries a ~30% premium
        (validated us-east-1 db.r6g.large Aurora MySQL Single-AZ: $0.338/hr
        I/O-Optimized vs $0.260/hr Standard → premium $0.078/hr × 730 =
        $56.94/mo). The Aurora adapter sums this premium over the cluster's
        provisioned members so the I/O-tier saving nets the instance-side cost,
        not just the storage-side cost.

        Both legs reuse :meth:`get_rds_instance_monthly_price` (which pins the
        Aurora ``storage`` mode — "EBS Only" vs "Aurora IO Optimization Mode" —
        so the two rates never collide). Returns ``0.0`` when either price is
        unavailable or the computed delta is non-positive, so no premium is
        fabricated when the rate cannot be resolved.
        """
        standard = self.get_rds_instance_monthly_price(
            engine,
            instance_class,
            multi_az=multi_az,
            license_model=license_model,
            aurora_io_optimized=False,
        )
        optimized = self.get_rds_instance_monthly_price(
            engine,
            instance_class,
            multi_az=multi_az,
            license_model=license_model,
            aurora_io_optimized=True,
        )
        if standard <= 0 or optimized <= 0:
            return 0.0
        premium = optimized - standard
        return premium if premium > 0 else 0.0

    def get_dms_instance_monthly_price(
        self, instance_class: str, *, multi_az: bool = False, allow_fallback: bool = True
    ) -> float:
        """$/month for a DMS replication instance in self._region, AZ-aware.

        Pins the ``AWSDatabaseMigrationSvc`` Replication-Server SKU by exact
        usagetype suffix — ``InstanceUsg:dms.<type>`` (Single-AZ) vs
        ``Multi-AZUsg:dms.<type>`` (Multi-AZ) — so the lookup is deterministic.
        The previous generic ``get_instance_monthly_price`` path filtered on
        ``instanceType`` with ``MaxResults=1`` and could return EITHER AZ SKU,
        making the counted rate up to 2x off (Multi-AZ DMS is exactly 2x
        Single-AZ — validated us-east-1 dms.t3.medium: $0.0745/hr Single-AZ vs
        $0.149/hr Multi-AZ).

        Args:
            instance_class: DMS class, with or without the ``dms.`` prefix (e.g.
                ``"dms.t3.medium"`` or ``"t3.medium"``); normalized to
                ``dms.<type>`` for the usagetype match.
            multi_az: When True, selects the Multi-AZ SKU.
            allow_fallback: When False, a missing SKU returns ``0.0`` instead of
                the documented fallback, and raises no pricing warning. Callers
                probing whether a *hypothetical* class exists (e.g. the
                one-size-down target of a rightsizing rec) must pass ``False``:
                otherwise a class that does not exist — ``dms.r5.medium`` — prices
                to a fallback constant and a fabricated delta is counted against
                it.

        Returns:
            Monthly on-demand price in USD (730 hours). Returns the documented
            fallback (× fallback_multiplier; ×2 for Multi-AZ) when the live SKU is
            unavailable, or ``0.0`` when ``allow_fallback`` is False.
        """
        clean = instance_class.strip()
        clean = f"dms.{clean[len('dms.'):] if clean.startswith('dms.') else clean}"
        az = "Multi-AZ" if multi_az else "Single-AZ"
        # Two cache namespaces: a fallback price must never satisfy a strict
        # (allow_fallback=False) lookup, or a fabricated number leaks into a
        # caller that explicitly asked for a real SKU only.
        key = ("dms_instance", clean, az)
        strict_key = ("dms_instance_real", clean, az)

        if allow_fallback:
            if (cached := self._get_cached(key)) is not None:
                return cached
        elif (cached_strict := self._get_cached(strict_key)) is not None:
            return cached_strict

        price = self._fetch_dms_instance_price(clean, multi_az=multi_az)
        if price is None:
            if not allow_fallback:
                self._cache.set(strict_key, 0.0)  # SKU genuinely absent
                return 0.0
            fallback = FALLBACK_DMS_INSTANCE_MONTHLY * (FALLBACK_DMS_MULTI_AZ_FACTOR if multi_az else 1.0)
            price = self._use_fallback(
                fallback * self._fallback_multiplier,
                f"Pricing API unavailable for DMS {clean} "
                f"({'Multi-AZ' if multi_az else 'Single-AZ'}) in {self._region}; using fallback",
            )
            self._cache.set(key, price)
            return price

        self._cache.set(key, price)
        self._cache.set(strict_key, price)  # a real price satisfies both paths
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

    def get_fargate_vcpu_hourly(self, *, architecture: str = "x86", os: str = "linux") -> float:
        """$/vCPU-hour for Fargate in self._region, by architecture and OS.

        Args:
            architecture: "x86" or "arm" (Graviton). ARM is ~20% cheaper.
            os: "linux" or "windows". Windows is billed at a higher rate and
                additionally incurs a per-vCPU OS license fee (see
                :meth:`get_fargate_windows_os_hourly`).
        """
        arch = "arm" if str(architecture).lower() in ("arm", "arm64", "graviton") else "x86"
        os_key = "windows" if str(os).lower().startswith("win") else "linux"
        key = ("fargate_vcpu", arch, os_key)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_fargate_rate("vcpu", arch, os_key)
        if price is None:
            fb = FALLBACK_FARGATE_VCPU_HOURLY.get((arch, os_key)) or FALLBACK_FARGATE_VCPU_HOURLY[("x86", "linux")]
            price = self._use_fallback(
                fb * self._fallback_multiplier,
                f"Pricing API unavailable for Fargate {arch}/{os_key} vCPU in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_fargate_gb_hourly(self, *, architecture: str = "x86", os: str = "linux") -> float:
        """$/GB-hour of memory for Fargate in self._region, by architecture and OS."""
        arch = "arm" if str(architecture).lower() in ("arm", "arm64", "graviton") else "x86"
        os_key = "windows" if str(os).lower().startswith("win") else "linux"
        key = ("fargate_gb", arch, os_key)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_fargate_rate("gb", arch, os_key)
        if price is None:
            fb = FALLBACK_FARGATE_GB_HOURLY.get((arch, os_key)) or FALLBACK_FARGATE_GB_HOURLY[("x86", "linux")]
            price = self._use_fallback(
                fb * self._fallback_multiplier,
                f"Pricing API unavailable for Fargate {arch}/{os_key} memory in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_fargate_windows_os_hourly(self) -> float:
        """Per-vCPU Windows OS license fee for Fargate in self._region."""
        key = ("fargate_win_os",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_fargate_rate("win_os", "x86", "windows")
        if price is None:
            price = self._use_fallback(
                FALLBACK_FARGATE_WINDOWS_OS_HOURLY * self._fallback_multiplier,
                f"Pricing API unavailable for Fargate Windows OS license in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_eks_control_plane_hourly(self) -> float:
        """$/hour for an EKS cluster control plane in self._region."""
        key = ("eks_control_plane",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_eks_rate("perCluster")
        if price is None:
            price = self._use_fallback(
                FALLBACK_EKS_CONTROL_PLANE_HOURLY * self._fallback_multiplier,
                f"Pricing API unavailable for EKS control plane in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_eks_extended_support_hourly(self) -> float:
        """$/hour EKS Extended Support surcharge in self._region.

        This is billed *in addition* to :meth:`get_eks_control_plane_hourly`
        once a cluster's Kubernetes version exits standard support, so it is
        also the realizable saving from upgrading off an extended-support
        version.
        """
        key = ("eks_extended_support",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._fetch_eks_rate("extendedSupport")
        if price is None:
            price = self._use_fallback(
                FALLBACK_EKS_EXTENDED_SUPPORT_HOURLY * self._fallback_multiplier,
                f"Pricing API unavailable for EKS Extended Support in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    def get_ecr_storage_gb_month(self) -> float:
        """$/GB-month for ECR standard (non-archive) image storage in self._region."""
        key = ("ecr_storage_gb",)
        if (cached := self._get_cached(key)) is not None:
            return cached
        price = self._select_by_usagetype_suffix(
            "AmazonECR",
            [{"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name}],
            "TimedStorage-ByteHrs",
        )
        if price is None:
            price = self._use_fallback(
                FALLBACK_ECR_GB_MONTH * self._fallback_multiplier,
                f"Pricing API unavailable for ECR storage in {self._region}; using fallback",
            )
        self._cache.set(key, price)
        return price

    # ── Private fetch methods ─────────────────────────────────────────────────

    def _fetch_ec2_price(self, instance_type: str, os: str, license_model: str = "No License required") -> float | None:
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
        # The usagetype is region-prefixed outside us-east-1 (e.g.
        # EU-EBS:SnapshotUsage), so an exact match misses every other region.
        # Match the suffix instead, and skip the .outposts variant.
        suffix = "EBS:SnapshotArchiveStorage" if archive_tier else "EBS:SnapshotUsage"
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
        ]
        return self._select_by_usagetype_suffix("AmazonEC2", filters, suffix)

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
            {
                "Type": "TERM_MATCH",
                "Field": "deploymentOption",
                "Value": _rds_multi_az_deployment_option(engine, multi_az=multi_az),
            },
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
        """On-Demand $/broker-hour for an MSK broker instance; None on miss.

        AmazonMSK has NO ``instanceType`` attribute — the previous filter pinned
        it and matched nothing, so the live broker-price path was 100% dead and
        every broker silently fell back to an EC2 proxy. Broker SKUs are keyed by
        the ``computeFamily`` attribute (clean type, e.g. ``m5.large``) under the
        single ``Managed Streaming for Apache Kafka (MSK)`` productFamily; pin
        ``computeFamily`` + ``productFamily`` + ``operation='RunBroker'`` so the
        Broker-hours SKU (usagetype ``<region>-Kafka.<type>``) is selected
        deterministically. The billing unit is the lowercase ``"hours"`` (not
        ``"Hrs"``), so the generic ``_call_pricing_api`` / ``_extract_usd`` path
        is used rather than the ``Hrs``-guarded hourly path.
        """
        filters = [
            {"Type": "TERM_MATCH", "Field": "computeFamily", "Value": instance_type},
            {
                "Type": "TERM_MATCH",
                "Field": "productFamily",
                "Value": "Managed Streaming for Apache Kafka (MSK)",
            },
            {"Type": "TERM_MATCH", "Field": "operation", "Value": "RunBroker"},
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

    def _fetch_generic_instance_price(
        self, service_code: str, instance_type: str, *, engine: str | None = None
    ) -> float | None:
        """On-Demand node-hour price for a non-EC2 compute instance (SR-1).

        A bare ``instanceType + location`` filter matches many OnDemand SKUs
        whose billing dimensions differ wildly (per-second Concurrency Scaling,
        per-GB Managed Storage, ExtendedSupport/SyncDurability surcharges, …)
        and boto3 result ordering is **not** guaranteed. ``MaxResults=1``
        therefore returns a non-deterministic, frequently wrong dimension.
        Pin the canonical node-hour SKU per service:

        * ``AmazonRedshift`` — pin ``productFamily=Compute Instance`` (the
          ``usagetype=Node:<type>`` row, ``unit=Hrs``); rejects Concurrency
          Scaling (``CS:``/``CSFreeUsage:``, seconds) and Managed Storage
          (``RMS:``, GB-Mo).
        * ``AmazonElastiCache`` — pin ``cacheEngine=<engine>`` and select the
          exact ``NodeUsage:<type>`` row; all six SKUs share ``unit=Hrs`` so
          unit alone cannot discriminate. Rejects ``USE1-ExtendedSupport*`` and
          ``USE1-SyncDurability-*`` surcharges. Requires ``engine``.
        * other services — unchanged ``MaxResults=1`` path (SR-1 scope is
          Redshift/ElastiCache only).
        """
        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        ]
        if service_code == "AmazonRedshift":
            filters.append({"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Compute Instance"})
            hourly = self._select_instance_node_hourly("AmazonRedshift", filters, usagetype_prefix="Node:")
            return hourly * 730 if hourly is not None else None
        if service_code == "AmazonElastiCache":
            if not engine:
                logger.debug(
                    "pricing:GetProducts  AmazonElastiCache  %s  → no engine supplied; "
                    "cannot disambiguate NodeUsage SKU (SR-1)",
                    instance_type,
                )
                return None
            # Normalize engine casing to the Pricing-API canonical form
            # (Redis/Memcached/Valkey). The ElastiCache DescribeCacheClusters
            # ``Engine`` field is lowercase ("redis"); the Pricing API
            # ``cacheEngine`` attribute is capitalized ("Redis"). A verbatim
            # TERM_MATCH on "redis" returns no NodeUsage row → None → $0
            # (elasticache C2 production bug).
            engine_norm = str(engine).strip().capitalize()
            filters.append({"Type": "TERM_MATCH", "Field": "cacheEngine", "Value": engine_norm})
            hourly = self._select_instance_node_hourly(
                "AmazonElastiCache",
                filters,
                usagetype_exact=f"NodeUsage:{instance_type}",
                attributes_exact={"cacheEngine": engine_norm},
            )
            return hourly * 730 if hourly is not None else None
        if (
            service_code == "AmazonES"
            and isinstance(instance_type, str)
            and instance_type.endswith(".elasticsearch")
        ):
            # Legacy Elasticsearch-domain instance types carry the
            # ``.elasticsearch`` suffix, but the Pricing API only publishes the
            # ``.search`` SKU — a verbatim lookup matches nothing and silently
            # prices to $0. Normalize so legacy (un-migrated) ES domains resolve
            # their real instance rate (opensearch L1).
            filters[0]["Value"] = instance_type.removesuffix(".elasticsearch") + ".search"
        price_hourly = self._call_pricing_api(service_code, filters)
        if price_hourly is not None:
            return price_hourly * 730  # hours/month
        return None

    def _select_instance_node_hourly(
        self,
        service_code: str,
        filters: list[dict],
        *,
        usagetype_prefix: str | None = None,
        usagetype_exact: str | None = None,
        attributes_exact: dict[str, str] | None = None,
    ) -> float | None:
        """Pick the canonical node-hour OnDemand SKU from a multi-SKU set.

        Mirrors ``_select_efs_storage_rate``: fetch up to 100 rows and select
        the one whose ``usagetype`` matches the canonical node-usage pattern
        (``Node:<type>`` for Redshift, ``NodeUsage:<type>`` for ElastiCache)
        and whose billing ``unit`` is ``Hrs``. The match is region-prefix aware:
        the AWS Pricing API region-prefixes usagetypes outside us-east-1 (e.g.
        ``EUW1-NodeUsage:cache.r6g.large``, ``EU-Node:ra3.4xlarge``) while
        us-east-1 carries no prefix, so we strip a leading ``<REGION>-`` before
        comparing — otherwise the selector matches only in us-east-1 and silently
        returns ``None`` (a $0 fallback that mis-reads a real price as zero) in
        every other region. ``attributes_exact`` adds
        defense-in-depth exact-match guards (e.g. ``cacheEngine``) so the
        selector stays deterministic even when a caller's mock or a stale API
        page returns rows for multiple engines. Rejects per-second / per-GB /
        ExtendedSupport / SyncDurability / $0 rows that share the same
        ``instanceType``.
        """
        key_fields = {f["Field"]: f["Value"] for f in filters if f["Field"] not in _LOG_SKIP_FIELDS}
        try:
            resp = self._pricing.get_products(
                ServiceCode=service_code,
                Filters=filters,
                MaxResults=100,
            )
            self._stats["api_calls"] += 1
            for raw in resp.get("PriceList", []):
                item = json.loads(raw)
                attrs = item.get("product", {}).get("attributes", {})
                usagetype = attrs.get("usagetype", "")
                # Strip a leading "<REGION>-" prefix so a node SKU matches in every
                # region, not just us-east-1. Node/NodeUsage usagetypes never
                # contain "-" internally (the instance type uses "."), so the first
                # "-" is the region separator; an unprefixed (us-east-1) usagetype
                # is unchanged.
                bare_usagetype = usagetype.split("-", 1)[-1]
                if usagetype_prefix is not None and not bare_usagetype.startswith(usagetype_prefix):
                    continue
                if usagetype_exact is not None and bare_usagetype != usagetype_exact:
                    continue
                if attributes_exact and any(attrs.get(k) != v for k, v in attributes_exact.items()):
                    continue
                price, unit = _extract_usd_with_unit(item)
                if price is None or unit != "Hrs":
                    continue
                logger.debug(
                    "pricing:GetProducts[node]  %s  %s  → $%.6f/hr (%s)",
                    service_code,
                    key_fields,
                    price,
                    usagetype,
                )
                return price
            logger.debug(
                "pricing:GetProducts[node]  %s  %s  → no node-hour SKU matched",
                service_code,
                key_fields,
            )
            return None
        except Exception as exc:
            self._stats["api_errors"] += 1
            logger.debug(
                "pricing:GetProducts[node]  %s  %s  → FAILED: %s",
                service_code,
                key_fields,
                exc,
            )
            return None

    def _fetch_efs_price(self, storage_class: str = "General Purpose") -> float | None:
        # EFS prices by the `storageClass` attribute (there is NO `volumeType`
        # attribute on AmazonEFS — the previous filter silently never matched).
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "storageClass", "Value": storage_class},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
        ]
        return self._select_efs_storage_rate(filters)

    def _fetch_efs_ia_access_price(self) -> float | None:
        # The IA DataAccess read/write SKUs share storageClass="Infrequent Access"
        # with the IA storage SKU, so select the DataAccess row explicitly.
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "storageClass", "Value": "Infrequent Access"},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
        ]
        return self._select_efs_access_rate(filters)

    def _select_efs_access_rate(self, filters: list[dict]) -> float | None:
        """Pick the IA per-GB DataAccess $/GB rate (read == write) from EFS results."""
        key_fields = {f["Field"]: f["Value"] for f in filters if f["Field"] not in _LOG_SKIP_FIELDS}
        try:
            resp = self._pricing.get_products(
                ServiceCode="AmazonEFS",
                Filters=filters,
                MaxResults=100,
            )
            self._stats["api_calls"] += 1
            for raw in resp.get("PriceList", []):
                item = json.loads(raw)
                usagetype = item.get("product", {}).get("attributes", {}).get("usagetype", "")
                if "DataAccess" not in usagetype:
                    continue
                price = _extract_usd(item)
                if price is not None:
                    logger.debug("pricing:GetProducts  AmazonEFS  %s  → IA access $%.6f", key_fields, price)
                    return price
            logger.debug("pricing:GetProducts  AmazonEFS  %s  → no DataAccess row", key_fields)
            return None
        except Exception as exc:
            self._stats["api_errors"] += 1
            logger.debug("pricing:GetProducts  AmazonEFS  %s  → FAILED: %s", key_fields, exc)
            return None

    def _select_efs_storage_rate(self, filters: list[dict]) -> float | None:
        """Pick the timed-storage $/GB-month from EFS Pricing results.

        For the IA and Archive classes the ``storageClass`` filter alone matches
        THREE SKUs that share the class: the ``*TimedStorage*-ByteHrs`` storage
        row AND the per-GB ``*DataAccess-Bytes`` read/write rows ($0.01 / $0.03).
        ``MaxResults=1`` could therefore return the access rate as if it were the
        storage rate. Keep only the ``TimedStorage`` storage SKU (skipping
        DataAccess and the SmallFiles rounding overhead).
        """
        key_fields = {f["Field"]: f["Value"] for f in filters if f["Field"] not in _LOG_SKIP_FIELDS}
        try:
            resp = self._pricing.get_products(
                ServiceCode="AmazonEFS",
                Filters=filters,
                MaxResults=100,
            )
            self._stats["api_calls"] += 1
            for raw in resp.get("PriceList", []):
                item = json.loads(raw)
                usagetype = item.get("product", {}).get("attributes", {}).get("usagetype", "")
                if "TimedStorage" not in usagetype or "SmallFiles" in usagetype:
                    continue
                price = _extract_usd(item)
                if price is not None:
                    logger.debug("pricing:GetProducts  AmazonEFS  %s  → $%.6f", key_fields, price)
                    return price
            logger.debug("pricing:GetProducts  AmazonEFS  %s  → no timed-storage row", key_fields)
            return None
        except Exception as exc:
            self._stats["api_errors"] += 1
            logger.debug("pricing:GetProducts  AmazonEFS  %s  → FAILED: %s", key_fields, exc)
            return None

    def _fetch_fsx_storage_price(
        self, file_system_type: str, storage_type: str, deployment_option: str
    ) -> float | None:
        # ``fileSystemType`` is a CASE-SENSITIVE attribute whose values are
        # Windows / Lustre / ONTAP / OpenZFS. ``str.capitalize()`` produced
        # "Ontap"/"Openzfs", which never matched, so ONTAP/OpenZFS silently fell
        # back to constants. Pin the exact label instead.
        fs_label = _FSX_FILE_SYSTEM_TYPE_LABELS.get(file_system_type.strip().upper(), file_system_type)
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
            {"Type": "TERM_MATCH", "Field": "fileSystemType", "Value": fs_label},
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
        # The PrivateLink filter returns BOTH the per-hour (VpcEndpoint-Hours)
        # and per-GB (VpcEndpoint-Bytes) SKUs; select the hourly row explicitly
        # rather than relying on MaxResults=1 returning the right one (correct
        # only by coincidence today since both rates are $0.01) (network NET-07).
        hourly = self._call_pricing_api_hourly("AmazonVPC", filters)
        return hourly * 730 if hourly is not None else None

    def _fetch_lb_base_hourly(self, product_family: str, operation: str) -> float | None:
        """Return the base hourly $/hr for a load-balancer type.

        Filters AWSELB by ``productFamily`` + ``operation`` (region-independent,
        unlike the region-prefixed ``usagetype``) and selects the standard
        ``LoadBalancerUsage`` Hrs row — excluding the Trust Store (``TS-``) and
        ``Outposts-`` variants that share the same productFamily/operation.
        """
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": product_family},
            {"Type": "TERM_MATCH", "Field": "operation", "Value": operation},
        ]
        key_fields = {f["Field"]: f["Value"] for f in filters if f["Field"] not in _LOG_SKIP_FIELDS}
        try:
            resp = self._pricing.get_products(ServiceCode="AWSELB", Filters=filters, MaxResults=100)
            self._stats["api_calls"] += 1
            for raw in resp.get("PriceList", []):
                item = json.loads(raw)
                usagetype = item.get("product", {}).get("attributes", {}).get("usagetype", "")
                if not usagetype.endswith("LoadBalancerUsage"):
                    continue
                if "TS-" in usagetype or "Outposts" in usagetype:
                    continue
                price, unit = _extract_usd_with_unit(item)
                if unit == "Hrs" and price is not None:
                    logger.debug("pricing:GetProducts  AWSELB  %s  → $%.6f/hr", key_fields, price)
                    return price
            logger.debug("pricing:GetProducts  AWSELB  %s  → no base LoadBalancerUsage row", key_fields)
            return None
        except Exception as exc:
            self._stats["api_errors"] += 1
            logger.debug("pricing:GetProducts  AWSELB  %s  → FAILED: %s", key_fields, exc)
            return None

    def _fetch_aurora_acu_price(self) -> float | None:
        # The usagetype "ACU-Hour" does not exist; AWS publishes the Aurora
        # Serverless v2 ACU rate as <region>-Aurora:ServerlessV2Usage (unit
        # "ACU-Hr", e.g. EU-Aurora:ServerlessV2Usage @ $0.14). Match the suffix.
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": "Aurora MySQL"},
        ]
        return self._select_by_usagetype_suffix("AmazonRDS", filters, "Aurora:ServerlessV2Usage")

    def _fetch_aurora_storage_rate(self, usagetype_suffix: str) -> float | None:
        """$/GB-month for an Aurora storage tier by exact usagetype suffix.

        ``usagetype_suffix`` is ``Aurora:StorageUsage`` (Standard) or
        ``Aurora:IO-OptimizedStorageUsage`` (I/O-Optimized). Both are
        ``productFamily="Database Storage"`` rows (unit "GB-Mo") whose Aurora
        rate is engine-independent ($0.10 / $0.225 in us-east-1 across MySQL,
        PostgreSQL and the engine-neutral "Any" row), so no databaseEngine pin
        is needed. Region prefixes (USE1-, EUW1-, …) vary, so the suffix is
        matched. The exact suffix avoids the $0.00
        ``…StorageUsage-LimitlessPreview`` decoy.
        """
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Storage"},
        ]
        return self._select_by_usagetype_suffix("AmazonRDS", filters, usagetype_suffix)

    def _fetch_aurora_io_rate_per_million(self) -> float | None:
        """$/1M Aurora Standard-tier I/O requests in ``self._region``; None on miss.

        ``productFamily="System Operation"`` row on ``AmazonRDS`` (unit "IOs",
        priced per request) whose usagetype is ``<region>-Aurora:StorageIOUsage``.
        The Aurora I/O rate is engine-independent, so no databaseEngine pin is
        needed. Region prefixes (USE1-, EUC1-, …) vary, so the suffix is matched;
        the exact suffix ``Aurora:StorageIOUsage`` avoids the $0.00
        ``…StorageIOUsage-LimitlessPreview`` decoy. The published rate is
        per-request ($0.0000002/IO); ×1e6 to per-million.
        """
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "System Operation"},
        ]
        per_io = self._select_by_usagetype_suffix("AmazonRDS", filters, "Aurora:StorageIOUsage")
        return per_io * 1_000_000 if per_io is not None else None

    def _fetch_dms_instance_price(self, dms_class: str, *, multi_az: bool = False) -> float | None:
        """Fetch on-demand monthly DMS replication-instance price; None on miss.

        ``dms_class`` is the full ``dms.<type>`` form. Selects the deterministic
        ``AWSDatabaseMigrationSvc`` Replication-Server SKU by exact usagetype
        suffix: ``Multi-AZUsg:<dms_class>`` (Multi-AZ) or
        ``InstanceUsg:<dms_class>`` (Single-AZ). Region prefixes vary, so the
        suffix is matched; the two AZ suffixes are mutually exclusive (neither is
        a suffix of the other). unit is "Hrs"; ×730 to monthly.
        """
        suffix = f"Multi-AZUsg:{dms_class}" if multi_az else f"InstanceUsg:{dms_class}"
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Replication Server"},
        ]
        hourly = self._select_by_usagetype_suffix("AWSDatabaseMigrationSvc", filters, suffix)
        return hourly * 730 if hourly is not None else None

    # Maps a logical Fargate rate to the usagetype suffix AWS publishes for it.
    # Region prefixes (USE1-, EUW1-, …) vary, so we match on the suffix.
    _FARGATE_USAGETYPE_SUFFIX: dict[tuple[str, str, str], str] = {
        ("vcpu", "x86", "linux"): "Fargate-vCPU-Hours:perCPU",
        ("gb", "x86", "linux"): "Fargate-GB-Hours",
        ("vcpu", "arm", "linux"): "Fargate-ARM-vCPU-Hours:perCPU",
        ("gb", "arm", "linux"): "Fargate-ARM-GB-Hours",
        ("vcpu", "x86", "windows"): "Fargate-Windows-vCPU-Hours:perCPU",
        ("gb", "x86", "windows"): "Fargate-Windows-GB-Hours",
        ("win_os", "x86", "windows"): "Fargate-Windows-OS-Hours:perCPU",
    }

    def _fetch_fargate_rate(self, leg: str, arch: str, os_key: str) -> float | None:
        """Fetch a Fargate $/hour rate by selecting the row whose usagetype suffix matches.

        AmazonECS returns many SKUs (Fargate vCPU/GB across arch/OS, ephemeral
        storage, managed instances) for one location, so we cannot rely on
        MaxResults=1. Match the exact usagetype suffix instead.
        """
        suffix = self._FARGATE_USAGETYPE_SUFFIX.get((leg, arch, os_key))
        if suffix is None:
            return None
        # AmazonECS returns hundreds of SKUs at one location (Fargate + ECS
        # Managed Instances), so narrow with a leg-specific attribute before
        # selecting the exact usagetype suffix — otherwise the Fargate rows can
        # fall outside the first page of results.
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        ]
        if leg == "vcpu":
            filters.append({"Type": "TERM_MATCH", "Field": "cputype", "Value": "perCPU"})
        elif leg == "gb":
            filters.append({"Type": "TERM_MATCH", "Field": "memorytype", "Value": "perGB"})
        elif leg == "win_os":
            filters.append({"Type": "TERM_MATCH", "Field": "cputype", "Value": "perCPU OS License Fee"})
        return self._select_by_usagetype_suffix("AmazonECS", filters, suffix)

    def _fetch_eks_rate(self, suffix_tail: str) -> float | None:
        """Fetch an EKS $/hour rate (perCluster or extendedSupport) by usagetype suffix."""
        operation = {"perCluster": "CreateOperation", "extendedSupport": "ExtendedSupport"}.get(suffix_tail)
        filters = [
            {"Type": "TERM_MATCH", "Field": "location", "Value": self._display_name},
        ]
        if operation:
            filters.append({"Type": "TERM_MATCH", "Field": "operation", "Value": operation})
        return self._select_by_usagetype_suffix("AmazonEKS", filters, f"AmazonEKS-Hours:{suffix_tail}")

    def _select_by_usagetype_suffix(
        self, service_code: str, filters: list[dict], suffix: str, *, max_pages: int = 20
    ) -> float | None:
        """Return the USD/unit price of the first result whose usagetype ends with `suffix`.

        Paginates up to ``max_pages`` so the match is found even in large catalogs
        (e.g. AmazonRDS) where the target SKU falls outside the first page.
        """
        key_fields = {f["Field"]: f["Value"] for f in filters if f["Field"] not in _LOG_SKIP_FIELDS}
        next_token: str | None = None
        try:
            for _ in range(max_pages):
                kwargs: dict[str, Any] = {"ServiceCode": service_code, "Filters": filters, "MaxResults": 100}
                if next_token:
                    kwargs["NextToken"] = next_token
                resp = self._pricing.get_products(**kwargs)
                self._stats["api_calls"] += 1
                for raw in resp.get("PriceList", []):
                    item = json.loads(raw)
                    usagetype = item.get("product", {}).get("attributes", {}).get("usagetype", "")
                    if not usagetype.endswith(suffix):
                        continue
                    price = _extract_usd(item)
                    if price is not None:
                        logger.debug("pricing:GetProducts  %s  %s~%s  → $%.6f", service_code, key_fields, suffix, price)
                        return price
                next_token = resp.get("NextToken")
                if not next_token:
                    break
            logger.debug("pricing:GetProducts  %s  %s~%s  → no match", service_code, key_fields, suffix)
            return None
        except Exception as exc:
            self._stats["api_errors"] += 1
            logger.debug("pricing:GetProducts  %s  %s~%s  → FAILED: %s", service_code, key_fields, suffix, exc)
            return None

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
