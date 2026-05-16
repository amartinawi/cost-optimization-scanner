"""S3 storage optimization checks.

Extracted from CostOptimizer S3-related methods as free functions.
This module will later become S3Module (T-3xx) implementing ServiceModule.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from core.scan_context import ScanContext

logger = logging.getLogger(__name__)

# Per-bucket CloudWatch calls run against the bucket's *home* region. If that
# region is suffering an outage (e.g. me-south-1 Bahrain / me-central-1 UAE),
# the default boto retry config (10 attempts, 60s timeouts) can stall the
# entire scan for many minutes per bucket. These shorter timeouts plus a
# session-scoped dead-region set let us fail fast and stop hammering an
# unreachable endpoint after the first failure.
_BUCKET_CW_TIMEOUT_CONFIG: Config = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)

_DEAD_CW_REGIONS: set[str] = set()


def _is_endpoint_unreachable(exc: BaseException) -> bool:
    """Return True for errors indicating the CloudWatch endpoint is unreachable."""
    if isinstance(exc, (ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError)):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("RequestTimeout", "EndpointConnectionError", "ServiceUnavailable"):
            return True
    msg = str(exc).lower()
    return "connect timeout" in msg or "read timeout" in msg or "endpoint" in msg and "unreachable" in msg


def _bucket_cloudwatch_client(ctx: ScanContext, bucket_region: str) -> Any | None:
    """Return a CloudWatch client for bucket_region with tight timeouts, or None if region is known-dead."""
    if bucket_region in _DEAD_CW_REGIONS:
        return None
    # Build the client directly (bypasses ClientRegistry default config) so we
    # can apply short-timeout retries without polluting the shared retry config.
    factory = ctx.clients._factory  # AwsSessionFactory  # noqa: SLF001
    return factory.session().client(
        "cloudwatch",
        region_name=bucket_region,
        config=_BUCKET_CW_TIMEOUT_CONFIG,
    )


def _mark_region_dead(ctx: ScanContext, bucket_region: str, reason: str) -> None:
    """Cache a region as unreachable so subsequent buckets skip CloudWatch fast."""
    if bucket_region in _DEAD_CW_REGIONS:
        return
    _DEAD_CW_REGIONS.add(bucket_region)
    ctx.warn(
        f"CloudWatch endpoint unreachable for region {bucket_region}; "
        f"skipping S3 size metrics for all remaining buckets in this region ({reason})",
        service="s3",
    )

S3_STORAGE_COSTS: dict[str, float] = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "ONEZONE_IA": 0.01,
    "GLACIER_FLEXIBLE_RETRIEVAL": 0.0036,
    "GLACIER_INSTANT_RETRIEVAL": 0.004,
    "DEEP_ARCHIVE": 0.00099,
    "INTELLIGENT_TIERING": 0.023,
    "EXPRESS_ONE_ZONE": 0.11,
}

S3_INTELLIGENT_TIERING_MONITORING_FEE: float = 0.0025

# Per-opportunity savings factors applied to a bucket's current monthly storage
# cost. Replaces the legacy blanket × 0.40 multiplier flagged in audit
# L2-S3-001. Values are conservative midpoints grounded in AWS S3 docs:
#
# - lifecycle_missing: Standard → IA after 30 days saves
#   (0.023 − 0.0125)/0.023 ≈ 45.6% on the transitioned slice. Assuming ~65% of
#   bucket data is IA-eligible (industry-typical for general-purpose buckets)
#   the conservative bucket-level reduction is ~0.30.
# - intelligent_tiering: AWS documents 20-40% savings for variable access
#   patterns; conservative midpoint 0.20.
# - both_missing: combined effect dominated by lifecycle; capped at 0.40 to
#   avoid double-counting overlapping savings.
# - static_website: storage-class change does not apply; CloudFront data
#   transfer savings are usage-dependent — emit as $0.00/month informational.
S3_SAVINGS_FACTORS: dict[str, float] = {
    "lifecycle_missing": 0.30,
    "intelligent_tiering": 0.20,
    "both_missing": 0.40,
    "static_website": 0.0,
    "other": 0.0,
}

# Bucket-error classifier — used by all bucket-level S3 calls to route
# AccessDenied / AllAccessDisabled / 403 through ctx.permission_issue and
# everything else through logger.debug. Replaces 18 bare `print()` sites
# flagged in audit L1-S3-001 / L1-S3-002.
_S3_ACCESS_DENIED_CODES: frozenset[str] = frozenset({
    "AccessDenied",
    "AllAccessDisabled",
    "AuthorizationError",
    "Forbidden",
    "MethodNotAllowed",
})


def _is_access_denied(exc: BaseException) -> bool:
    """Return True if the exception represents an IAM permission denial."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _S3_ACCESS_DENIED_CODES:
            return True
    msg = str(exc)
    return "AccessDenied" in msg or "AllAccessDisabled" in msg or "Forbidden" in msg


def _route_bucket_error(
    ctx: ScanContext,
    bucket_name: str,
    exc: BaseException,
    *,
    action: str,
    expected_codes: tuple[str, ...] = (),
) -> None:
    """Route a bucket-level S3 error to permission_issue, expected-miss skip, or logger.

    Args:
        ctx: Scan context.
        bucket_name: Bucket the error pertains to.
        exc: The caught exception.
        action: The S3 IAM action that failed (e.g. ``"s3:GetBucketLifecycleConfiguration"``).
        expected_codes: Substrings that indicate an expected miss (e.g.
            ``"NoSuchLifecycleConfiguration"``); these are swallowed silently.
    """
    msg = str(exc)
    if any(code in msg for code in expected_codes):
        return
    if _is_access_denied(exc):
        ctx.permission_issue(
            f"{action} denied on bucket {bucket_name}",
            service="s3",
            action=action,
        )
        return
    logger.debug("S3 %s error on bucket %s: %s", action, bucket_name, exc)

S3_REGIONAL_MULTIPLIERS: dict[str, dict[str, float]] = {
    "us-east-1": {
        "STANDARD": 1.0,
        "STANDARD_IA": 1.0,
        "ONEZONE_IA": 1.0,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.0,
        "EXPRESS_ONE_ZONE": 1.0,
    },
    "us-east-2": {
        "STANDARD": 1.0,
        "STANDARD_IA": 1.0,
        "ONEZONE_IA": 1.0,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.0,
        "EXPRESS_ONE_ZONE": 1.0,
    },
    "us-west-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "us-west-2": {
        "STANDARD": 1.0,
        "STANDARD_IA": 1.0,
        "ONEZONE_IA": 1.0,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.0,
        "EXPRESS_ONE_ZONE": 1.0,
    },
    "eu-west-1": {
        "STANDARD": 1.0,
        "STANDARD_IA": 1.0,
        "ONEZONE_IA": 1.0,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.0,
        "EXPRESS_ONE_ZONE": 1.0,
    },
    "eu-west-2": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "eu-west-3": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "eu-central-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "eu-central-2": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "eu-north-1": {
        "STANDARD": 0.956,
        "STANDARD_IA": 0.96,
        "ONEZONE_IA": 0.9,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 0.956,
        "EXPRESS_ONE_ZONE": 0.956,
    },
    "eu-south-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "eu-south-2": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-southeast-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-southeast-2": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-southeast-3": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-southeast-4": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-northeast-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-northeast-2": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-northeast-3": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-south-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-south-2": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ap-east-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ca-central-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "ca-west-1": {
        "STANDARD": 1.087,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.087,
        "EXPRESS_ONE_ZONE": 1.087,
    },
    "sa-east-1": {
        "STANDARD": 1.304,
        "STANDARD_IA": 1.28,
        "ONEZONE_IA": 1.3,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.304,
        "EXPRESS_ONE_ZONE": 1.304,
    },
    "me-south-1": {
        "STANDARD": 1.15,
        "STANDARD_IA": 1.12,
        "ONEZONE_IA": 1.2,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.15,
        "EXPRESS_ONE_ZONE": 1.15,
    },
    "me-central-1": {
        "STANDARD": 1.15,
        "STANDARD_IA": 1.12,
        "ONEZONE_IA": 1.2,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.15,
        "EXPRESS_ONE_ZONE": 1.15,
    },
    "af-south-1": {
        "STANDARD": 1.304,
        "STANDARD_IA": 1.28,
        "ONEZONE_IA": 1.3,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.304,
        "EXPRESS_ONE_ZONE": 1.304,
    },
    "il-central-1": {
        "STANDARD": 1.15,
        "STANDARD_IA": 1.12,
        "ONEZONE_IA": 1.2,
        "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
        "GLACIER_INSTANT_RETRIEVAL": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.15,
        "EXPRESS_ONE_ZONE": 1.15,
    },
    "ap-east-2": {
        "STANDARD": 1.18,
        "STANDARD_IA": 1.15,
        "ONEZONE_IA": 1.2,
        "GLACIER": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.18,
    },
    "ap-southeast-5": {
        "STANDARD": 1.12,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.12,
    },
    "ap-southeast-6": {
        "STANDARD": 1.15,
        "STANDARD_IA": 1.12,
        "ONEZONE_IA": 1.2,
        "GLACIER": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.15,
    },
    "ap-southeast-7": {
        "STANDARD": 1.12,
        "STANDARD_IA": 1.08,
        "ONEZONE_IA": 1.1,
        "GLACIER": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.12,
    },
    "mx-central-1": {
        "STANDARD": 1.15,
        "STANDARD_IA": 1.12,
        "ONEZONE_IA": 1.2,
        "GLACIER": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.15,
    },
    "us-gov-east-1": {
        "STANDARD": 1.05,
        "STANDARD_IA": 1.05,
        "ONEZONE_IA": 1.05,
        "GLACIER": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.05,
    },
    "us-gov-west-1": {
        "STANDARD": 1.05,
        "STANDARD_IA": 1.05,
        "ONEZONE_IA": 1.05,
        "GLACIER": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.05,
    },
    "eusc-de-east-1": {
        "STANDARD": 1.15,
        "STANDARD_IA": 1.12,
        "ONEZONE_IA": 1.2,
        "GLACIER": 1.0,
        "DEEP_ARCHIVE": 1.0,
        "INTELLIGENT_TIERING": 1.15,
    },
}

S3_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "s3_bucket_analysis": {
        "title": "S3 Bucket Cost Analysis",
        "description": (
            "Analysis of S3 bucket configurations for lifecycle policies,"
            " Intelligent-Tiering adoption, storage class optimization, and unused bucket detection."
        ),
        "action": (
            "1. Review buckets without lifecycle policies\n"
            "2. Enable Intelligent-Tiering for variable access patterns\n"
            "3. Transition old data to lower-cost storage classes\n"
            "4. Estimated savings: 40-95% depending on optimization type"
        ),
    },
    "enhanced_checks": {
        "title": "Enhanced S3 Checks",
        "description": (
            "Additional S3 checks including multipart upload cleanup,"
            " versioning review, and cross-region replication analysis."
        ),
        "action": (
            "1. Configure lifecycle rules to abort incomplete uploads\n"
            "2. Review bucket versioning and replication settings\n"
            "3. Estimated savings: variable based on findings"
        ),
    },
    "lifecycle_policies": {
        "title": "Configure S3 Lifecycle Policies",
        "description": (
            "Automatically transition objects to lower-cost storage classes based on age and access patterns."
        ),
        "action": (
            "1. Analyze access patterns using S3 Storage Class Analysis\n"
            "2. Create lifecycle rules for IA transition after 30 days\n"
            "3. Configure Glacier transitions for long-term storage\n"
            "4. Estimated savings: 50-95% for infrequent/archive data"
        ),
    },
    "intelligent_tiering": {
        "title": "Enable S3 Intelligent-Tiering",
        "description": ("Automatically optimize costs by moving data between access tiers based on access patterns."),
        "action": (
            "1. Enable Intelligent-Tiering on buckets with unpredictable access\n"
            "2. Configure Archive and Deep Archive tiers for maximum savings\n"
            "3. Monitor cost optimization through S3 Storage Lens\n"
            "4. Estimated savings: 40-95% for variable access patterns"
        ),
    },
    "storage_class_optimization": {
        "title": "Optimize S3 Storage Classes",
        "description": ("Choose appropriate storage classes based on access frequency and retrieval requirements."),
        "action": (
            "1. Use Standard for frequently accessed data\n"
            "2. Use Standard-IA for monthly access patterns\n"
            "3. Use Glacier for quarterly/yearly access\n"
            "4. Estimated savings: 40-80% vs Standard storage"
        ),
    },
    "unused_buckets": {
        "title": "Delete Unused S3 Buckets",
        "description": "Remove empty or unused S3 buckets to eliminate unnecessary costs.",
        "action": (
            "1. Identify buckets with no objects or minimal usage\n"
            "2. Verify no applications depend on the bucket\n"
            "3. Delete unused buckets via console or CLI\n"
            "4. Estimated savings: 100% of bucket costs"
        ),
    },
    "multipart_cleanup": {
        "title": "Clean Up Incomplete Multipart Uploads",
        "description": ("Remove incomplete multipart uploads that continue to incur storage costs."),
        "action": (
            "1. List incomplete multipart uploads\n"
            "2. Configure lifecycle rules to abort incomplete uploads\n"
            "3. Set automatic cleanup after 7 days\n"
            "4. Estimated savings: Variable based on incomplete uploads"
        ),
    },
}


_SC_MAP = {"GLACIER_FLEXIBLE_RETRIEVAL": "GLACIER", "GLACIER_INSTANT_RETRIEVAL": "GLACIER_IR"}


def _calculate_s3_storage_cost(
    size_gb: float,
    storage_class: str,
    region: str,
    ctx: ScanContext | None = None,
) -> float:
    """Return monthly storage cost for ``size_gb`` in ``storage_class`` and ``region``.

    Uses ``PricingEngine`` (live) when available; falls back to module-const
    rates × regional multiplier. ``_SC_MAP`` normalizes storage-class names so
    that lookups against both ``S3_STORAGE_COSTS`` and
    ``S3_REGIONAL_MULTIPLIERS`` share a single namespace (audit L2-S3-004).
    """
    try:
        engine_key = _SC_MAP.get(storage_class, storage_class)
        if ctx and ctx.pricing_engine:
            price = ctx.pricing_engine.get_s3_monthly_price_per_gb(engine_key)
            return round(size_gb * price, 2)
        base_cost = S3_STORAGE_COSTS.get(storage_class, S3_STORAGE_COSTS["STANDARD"])
        # Regional multipliers index by the same engine_key so the
        # GLACIER_FLEXIBLE_RETRIEVAL vs GLACIER spelling drift no longer hides
        # the multiplier in regions that use the legacy key.
        regional_multiplier = (
            S3_REGIONAL_MULTIPLIERS.get(region, {}).get(engine_key)
            or S3_REGIONAL_MULTIPLIERS.get(region, {}).get(storage_class, 1.0)
        )
        return round(size_gb * base_cost * regional_multiplier, 2)
    except Exception as e:
        logger.debug("S3 storage cost calc failed for %s/%s: %s", region, storage_class, e)
        return round(size_gb * S3_STORAGE_COSTS["STANDARD"], 2)


def _is_static_website_bucket(bucket_name: str, s3_client: Any) -> bool:
    """Return True if ``bucket_name`` is configured for static-website hosting.

    Result is intended to be cached on ``bucket_info`` by the caller so it is
    not re-queried for every code path that needs it (audit L2-S3-007).
    """
    try:
        s3_client.get_bucket_website(Bucket=bucket_name)
        return True
    except Exception as e:
        if "NoSuchWebsiteConfiguration" not in str(e):
            logger.debug("S3 GetBucketWebsite error on %s: %s", bucket_name, e)
        return False


def _classify_opportunities(bucket_info: dict[str, Any]) -> str:
    """Return the ``S3_SAVINGS_FACTORS`` key matching this bucket's gaps.

    Resolves the per-opportunity savings model that replaces the legacy
    blanket × 0.40 factor (audit L2-S3-001).
    """
    has_lifecycle = bucket_info.get("HasLifecyclePolicy", False)
    has_tiering = bucket_info.get("HasIntelligentTiering", False)
    is_static = bucket_info.get("IsStaticWebsite", False)
    if is_static:
        return "static_website"
    if not has_lifecycle and not has_tiering:
        return "both_missing"
    if not has_lifecycle:
        return "lifecycle_missing"
    if not has_tiering:
        return "intelligent_tiering"
    return "other"


def _estimate_s3_bucket_cost(
    ctx: ScanContext,
    bucket_name: str,
    size_gb: float,
    bucket_region: str,
) -> float:
    try:
        cloudwatch = _bucket_cloudwatch_client(ctx, bucket_region)
        if cloudwatch is None:
            # Region marked dead earlier in the scan — fall through to size-only fallback.
            raise ConnectTimeoutError(endpoint_url=f"monitoring.{bucket_region}.amazonaws.com")

        storage_classes = [
            "StandardStorage",
            "StandardIAStorage",
            "OneZoneIAStorage",
            "GlacierStorage",
            "DeepArchiveStorage",
            "IntelligentTieringStorage",
        ]

        total_cost: float = 0
        total_accounted_gb: float = 0

        for storage_class in storage_classes:
            try:
                response = cloudwatch.get_metric_statistics(
                    Namespace="AWS/S3",
                    MetricName="BucketSizeBytes",
                    Dimensions=[
                        {"Name": "BucketName", "Value": bucket_name},
                        {"Name": "StorageType", "Value": storage_class},
                    ],
                    StartTime=datetime.now(UTC) - timedelta(days=2),
                    EndTime=datetime.now(UTC),
                    Period=86400,
                    Statistics=["Average"],
                )

                if response["Datapoints"]:
                    class_size_gb = response["Datapoints"][-1]["Average"] / (1024**3)
                    total_accounted_gb += class_size_gb

                    cost_key = {
                        "StandardStorage": "STANDARD",
                        "StandardIAStorage": "STANDARD_IA",
                        "OneZoneIAStorage": "ONEZONE_IA",
                        "GlacierStorage": "GLACIER",
                        "DeepArchiveStorage": "DEEP_ARCHIVE",
                        "IntelligentTieringStorage": "INTELLIGENT_TIERING",
                    }.get(storage_class, "STANDARD")

                    base_cost = S3_STORAGE_COSTS.get(
                        cost_key, S3_STORAGE_COSTS.get("GLACIER_FLEXIBLE_RETRIEVAL", S3_STORAGE_COSTS["STANDARD"])
                    )
                    if ctx.pricing_engine:
                        regional_cost = ctx.pricing_engine.get_s3_monthly_price_per_gb(cost_key)
                    else:
                        regional_multiplier = S3_REGIONAL_MULTIPLIERS.get(bucket_region, {}).get(cost_key, 1.0)
                        regional_cost = base_cost * regional_multiplier
                    storage_cost = class_size_gb * regional_cost

                    # Intelligent-Tiering monitoring fee depends on real object
                    # count ($0.0025 per 1000 objects/month). The legacy heuristic
                    # of class_size_gb × 1000 (audit L2-S3-005) silently invents
                    # a number; without a CloudWatch NumberOfObjects metric we
                    # omit the fee rather than guess. Buckets with many small
                    # objects will be slightly under-estimated; large-object
                    # buckets will be on the nose.
                    total_cost += storage_cost

            except Exception as e:
                if _is_endpoint_unreachable(e):
                    _mark_region_dead(ctx, bucket_region, f"S3 cost-estimate for {bucket_name}/{storage_class}")
                    break  # Stop iterating storage classes — endpoint is gone.
                logger.debug("Error calculating S3 costs for %s: %s", bucket_name, e)
                continue

        if total_accounted_gb < size_gb * 0.1:
            if ctx.pricing_engine:
                total_cost = size_gb * ctx.pricing_engine.get_s3_monthly_price_per_gb("STANDARD")
            else:
                base_cost = S3_STORAGE_COSTS["STANDARD"]
                regional_multiplier = S3_REGIONAL_MULTIPLIERS.get(bucket_region, {}).get("STANDARD", 1.0)
                regional_cost = base_cost * regional_multiplier
                total_cost = size_gb * regional_cost

        return round(total_cost, 2)

    except Exception as e:
        if _is_endpoint_unreachable(e):
            _mark_region_dead(ctx, bucket_region, f"S3 cost-estimate outer for {bucket_name}")
        else:
            logger.debug("S3 storage cost calc outer error for %s: %s", bucket_name, e)
        if ctx.pricing_engine:
            return round(size_gb * ctx.pricing_engine.get_s3_monthly_price_per_gb("STANDARD"), 2)
        base_cost = S3_STORAGE_COSTS["STANDARD"]
        regional_multiplier = S3_REGIONAL_MULTIPLIERS.get(bucket_region, {}).get("STANDARD", 1.0)
        regional_cost = base_cost * regional_multiplier
        return round(size_gb * regional_cost, 2)


def get_s3_bucket_analysis(
    ctx: ScanContext,
    fast_mode: bool,
    pricing_multiplier: float,
) -> dict[str, Any]:
    """Scan every accessible S3 bucket and emit per-bucket recommendations.

    Each bucket's ``SavingsDelta`` is derived from a per-opportunity factor
    (``S3_SAVINGS_FACTORS``) applied to the bucket's estimated monthly storage
    cost — see ``_classify_opportunities``. Replaces the legacy blanket
    ``× 0.40`` multiplier flagged in audit L2-S3-001.

    Note: ``pricing_multiplier`` is accepted for ABI compatibility with the
    adapter signature. Per-storage-class costs come from ``PricingEngine``
    (region-correct) when available, falling back to module constants × the
    regional multiplier dict.
    """
    del pricing_multiplier  # PricingEngine path is authoritative; constant retained for signature stability.
    logger.debug("S3 bucket analysis starting (fast_mode=%s)", fast_mode)
    s3 = ctx.client("s3")

    try:
        response = s3.list_buckets()
        buckets = response.get("Buckets", [])
        logger.debug("Analyzing %d S3 buckets (%s)", len(buckets), "fast" if fast_mode else "full")

        analysis: dict[str, Any] = {
            "total_buckets": len(buckets),
            "buckets_without_lifecycle": [],
            "buckets_without_intelligent_tiering": [],
            "optimization_opportunities": [],
            "top_cost_buckets": [],
            "top_size_buckets": [],
            "permission_issues": [],
        }

        bucket_metrics: list[dict[str, Any]] = []

        for bucket in buckets:
            bucket_name = bucket["Name"]

            try:
                location_response = s3.get_bucket_location(Bucket=bucket_name)
                bucket_region = location_response.get("LocationConstraint")
                if bucket_region is None:
                    bucket_region = "us-east-1"

                bucket_s3_client = ctx.client("s3", region=bucket_region)

            except Exception as e:
                _route_bucket_error(
                    ctx, bucket_name, e, action="s3:GetBucketLocation"
                )
                bucket_region = ctx.region
                bucket_s3_client = ctx.client("s3")
                analysis.setdefault("permission_issues", []).append(
                    {"bucket": bucket_name, "issue": "location_access", "error": str(e)}
                )

            bucket_info: dict[str, Any] = {
                "Name": bucket_name,
                "CreationDate": bucket["CreationDate"].isoformat(),
                "Region": bucket_region,
                "HasLifecyclePolicy": False,
                "HasIntelligentTiering": False,
                "IsStaticWebsite": False,
                "EstimatedMonthlyCost": 0,
                "SizeBytes": 0,
                "SizeGB": 0,
                "OptimizationOpportunities": [],
            }

            # Resolve IsStaticWebsite exactly once per bucket and cache the
            # result on bucket_info; downstream code paths and the enhanced
            # checks no longer need to re-query (audit L2-S3-007).
            bucket_info["IsStaticWebsite"] = _is_static_website_bucket(
                bucket_name, bucket_s3_client
            )

            if fast_mode:
                try:
                    objects_response = bucket_s3_client.list_objects_v2(
                        Bucket=bucket_name, MaxKeys=100
                    )
                    object_count = objects_response.get("KeyCount", 0)

                    if object_count > 0:
                        total_size = sum(
                            obj.get("Size", 0) for obj in objects_response.get("Contents", [])
                        )
                        bucket_info["SizeGB"] = total_size / (1024**3)
                        bucket_info["FastModeWarning"] = (
                            "Fast mode: Size based on sample only - may be significantly understated"
                        )
                        bucket_info["EstimatedMonthlyCost"] = _calculate_s3_storage_cost(
                            bucket_info["SizeGB"], "STANDARD", bucket_region, ctx=ctx
                        )
                except Exception as e:
                    _route_bucket_error(
                        ctx, bucket_name, e, action="s3:ListBucket"
                    )
            else:
                try:
                    bucket_cloudwatch_client = _bucket_cloudwatch_client(ctx, bucket_region)
                    if bucket_cloudwatch_client is None:
                        # Region already marked dead — skip CloudWatch entirely for this bucket.
                        bucket_info["MetricsSkipped"] = (
                            f"CloudWatch endpoint unreachable in {bucket_region}"
                        )
                        continue

                    total_size_gb = 0
                    region_dead_mid_loop = False
                    storage_classes = [
                        "StandardStorage",
                        "StandardIAStorage",
                        "OneZoneIAStorage",
                        "GlacierStorage",
                        "DeepArchiveStorage",
                        "IntelligentTieringStorage",
                    ]

                    for storage_class in storage_classes:
                        try:
                            size_response = bucket_cloudwatch_client.get_metric_statistics(
                                Namespace="AWS/S3",
                                MetricName="BucketSizeBytes",
                                Dimensions=[
                                    {"Name": "BucketName", "Value": bucket_name},
                                    {"Name": "StorageType", "Value": storage_class},
                                ],
                                StartTime=datetime.now(UTC) - timedelta(days=2),
                                EndTime=datetime.now(UTC),
                                Period=86400,
                                Statistics=["Average"],
                            )
                            if size_response["Datapoints"]:
                                class_size_gb = size_response["Datapoints"][-1]["Average"] / (1024**3)
                                total_size_gb += class_size_gb
                        except Exception as e:
                            if _is_endpoint_unreachable(e):
                                _mark_region_dead(
                                    ctx,
                                    bucket_region,
                                    f"S3 size metric for {bucket_name}/{storage_class}",
                                )
                                region_dead_mid_loop = True
                                break  # Stop iterating storage classes for this bucket.
                            logger.debug(
                                "Error getting S3 metrics for %s/%s: %s",
                                bucket_name,
                                storage_class,
                                e,
                            )
                            continue

                    if region_dead_mid_loop:
                        bucket_info["MetricsSkipped"] = (
                            f"CloudWatch endpoint unreachable in {bucket_region}"
                        )

                    if total_size_gb > 0:
                        bucket_info["SizeBytes"] = int(total_size_gb * (1024**3))
                        bucket_info["SizeGB"] = total_size_gb
                        bucket_info["EstimatedMonthlyCost"] = _calculate_s3_storage_cost(
                            total_size_gb, "STANDARD", bucket_region, ctx=ctx
                        )

                except Exception as e:
                    if _is_endpoint_unreachable(e):
                        _mark_region_dead(ctx, bucket_region, f"S3 cost-calc for {bucket_name}")
                    else:
                        logger.debug("Error calculating S3 costs for bucket %s: %s", bucket_name, e)
                    continue

            try:
                bucket_s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
                bucket_info["HasLifecyclePolicy"] = True
            except Exception as e:
                _route_bucket_error(
                    ctx,
                    bucket_name,
                    e,
                    action="s3:GetLifecycleConfiguration",
                    expected_codes=("NoSuchLifecycleConfiguration",),
                )
                bucket_info["OptimizationOpportunities"].append(
                    "Configure lifecycle policies for automatic storage class transitions"
                )
                analysis["buckets_without_lifecycle"].append(bucket_name)

            try:
                tiering_response = bucket_s3_client.list_bucket_intelligent_tiering_configurations(
                    Bucket=bucket_name
                )
                if tiering_response.get("IntelligentTieringConfigurationList"):
                    bucket_info["HasIntelligentTiering"] = True
                else:
                    if bucket_info["IsStaticWebsite"]:
                        bucket_info["OptimizationOpportunities"].append(
                            "Static website: Consider CloudFront CDN for reduced data transfer costs"
                        )
                    else:
                        bucket_info["OptimizationOpportunities"].append(
                            "Enable S3 Intelligent-Tiering for automatic cost optimization"
                        )
                    analysis["buckets_without_intelligent_tiering"].append(bucket_name)
            except Exception as e:
                _route_bucket_error(
                    ctx, bucket_name, e, action="s3:GetIntelligentTieringConfiguration"
                )
                if bucket_info["IsStaticWebsite"]:
                    bucket_info["OptimizationOpportunities"].append(
                        "Static website: Consider CloudFront CDN for reduced data transfer costs"
                    )
                else:
                    bucket_info["OptimizationOpportunities"].append(
                        "Enable S3 Intelligent-Tiering for automatic cost optimization"
                    )
                analysis["buckets_without_intelligent_tiering"].append(bucket_name)

            if not bucket_info["HasLifecyclePolicy"] and not bucket_info["HasIntelligentTiering"]:
                bucket_info["OptimizationOpportunities"].append(
                    "High priority: No cost optimization configured"
                )

            # Per-opportunity savings: classify the bucket's gap and apply the
            # matching factor from S3_SAVINGS_FACTORS. Replaces blanket × 0.40.
            opportunity_key = _classify_opportunities(bucket_info)
            factor = S3_SAVINGS_FACTORS.get(opportunity_key, 0.0)
            cost = bucket_info.get("EstimatedMonthlyCost", 0) or 0
            if bucket_info["OptimizationOpportunities"] and factor > 0:
                savings = round(cost * factor, 2)
                bucket_info["SavingsDelta"] = savings
                bucket_info["EstimatedSavings"] = f"${savings:.2f}/month"
            else:
                bucket_info["SavingsDelta"] = 0.0
                if opportunity_key == "static_website":
                    bucket_info["EstimatedSavings"] = (
                        "$0.00/month - data transfer dependent (CloudFront CDN)"
                    )
                else:
                    bucket_info["EstimatedSavings"] = "$0.00/month"
            bucket_info["OpportunityClass"] = opportunity_key

            bucket_metrics.append(bucket_info)
            analysis["optimization_opportunities"].append(bucket_info)

            if len(bucket_metrics) % 50 == 0:
                logger.debug("Processed %d/%d S3 buckets", len(bucket_metrics), len(buckets))

        logger.debug("Completed S3 bucket analysis for %d buckets", len(buckets))

        analysis["top_cost_buckets"] = sorted(
            bucket_metrics, key=lambda x: x["EstimatedMonthlyCost"], reverse=True
        )[:10]
        analysis["top_size_buckets"] = sorted(
            bucket_metrics, key=lambda x: x["SizeGB"], reverse=True
        )[:10]

        return analysis

    except Exception as e:
        if _is_access_denied(e):
            ctx.permission_issue(
                f"s3:ListAllMyBuckets denied: {e}",
                service="s3",
                action="s3:ListAllMyBuckets",
            )
        else:
            ctx.warn(f"Could not analyze S3 buckets: {e}", service="s3")
        return {
            "total_buckets": 0,
            "buckets_without_lifecycle": [],
            "buckets_without_intelligent_tiering": [],
            "optimization_opportunities": [],
            "top_cost_buckets": [],
            "top_size_buckets": [],
            "permission_issues": [],
        }


def get_enhanced_s3_checks(
    ctx: ScanContext,
    pricing_multiplier: float,
) -> dict[str, Any]:
    """Config-pattern checks layered on top of ``get_s3_bucket_analysis``.

    Every emitted record carries a parseable ``EstimatedSavings`` string. The
    dedicated source for bucket-level dollar savings is
    ``get_s3_bucket_analysis``; checks here are visibility-only (informational
    ``$0.00/month - <reason>``). The adapter dedups overlapping categories so
    ``total_recommendations`` and ``total_monthly_savings`` stay honest.

    Routes ``AccessDenied`` / ``AllAccessDisabled`` / ``403`` from every
    bucket-level call through ``ctx.permission_issue`` (audit L1-S3-002).
    Replaces 15 ``print()`` sites with logger / ctx routing (audit L1-S3-001).
    """
    del pricing_multiplier  # Reserved for future per-operation cost models.
    s3 = ctx.client("s3")
    checks: dict[str, Any] = {
        "lifecycle_missing": [],
        "multipart_uploads": [],
        "storage_class_optimization": [],
        "intelligent_tiering_missing": [],
        "unused_buckets": [],
        "versioning_growth": [],
        "cross_region_replication": [],
        "server_access_logs": [],
        "request_heavy_buckets": [],
        "static_website_optimization": [],
    }
    # Per-bucket cache of _is_static_website_bucket so it's queried at most
    # once per bucket inside this function (audit L2-S3-007).
    static_cache: dict[str, bool] = {}

    def _static(name: str, client: Any) -> bool:
        if name not in static_cache:
            static_cache[name] = _is_static_website_bucket(name, client)
        return static_cache[name]

    try:
        response = s3.list_buckets()
        buckets = response.get("Buckets", [])

        for bucket in buckets:
            bucket_name = bucket["Name"]

            try:
                location_response = s3.get_bucket_location(Bucket=bucket_name)
                bucket_region = location_response.get("LocationConstraint") or "us-east-1"
                bucket_s3_client = (
                    ctx.client("s3", region=bucket_region)
                    if bucket_region != ctx.region
                    else ctx.client("s3")
                )
            except Exception as e:
                _route_bucket_error(ctx, bucket_name, e, action="s3:GetBucketLocation")
                bucket_s3_client = ctx.client("s3")

            try:
                multipart_response = bucket_s3_client.list_multipart_uploads(Bucket=bucket_name)
                uploads = multipart_response.get("Uploads", [])
                if uploads:
                    checks["multipart_uploads"].append(
                        {
                            "BucketName": bucket_name,
                            "IncompleteUploads": len(uploads),
                            "CheckCategory": "Incomplete Multipart Uploads",
                            "Recommendation": (
                                "Configure lifecycle rule to abort incomplete uploads after 7 days"
                            ),
                            "EstimatedSavings": (
                                "$0.00/month - quantify via S3 Storage Lens (incomplete-upload bytes)"
                            ),
                        }
                    )
            except Exception as e:
                _route_bucket_error(ctx, bucket_name, e, action="s3:ListBucketMultipartUploads")

            try:
                bucket_s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
            except Exception as e:
                if "NoSuchLifecycleConfiguration" in str(e):
                    is_static_site = _static(bucket_name, bucket_s3_client)
                    if is_static_site:
                        recommendation = (
                            "Static website detected: Configure lifecycle policies"
                            " for logs/backups only. Consider CloudFront for"
                            " reduced data transfer costs"
                        )
                        category = "Static Website Optimization"
                    else:
                        recommendation = (
                            "Configure lifecycle policies for automatic tiering to reduce storage costs"
                        )
                        category = "Storage Class Optimization"
                    checks["lifecycle_missing"].append(
                        {
                            "BucketName": bucket_name,
                            "IsStaticWebsite": is_static_site,
                            "CheckCategory": category,
                            "Recommendation": recommendation,
                            "SizeGB": 0,
                            "EstimatedMonthlyCost": 0,
                            # Dollar savings live in s3_bucket_analysis; this
                            # entry is the visibility flag only — adapter dedups
                            # against the dedicated source.
                            "EstimatedSavings": (
                                "$0.00/month - covered by S3 Bucket Analysis source"
                            ),
                        }
                    )
                else:
                    _route_bucket_error(
                        ctx, bucket_name, e, action="s3:GetLifecycleConfiguration"
                    )

            try:
                versioning_response = bucket_s3_client.get_bucket_versioning(Bucket=bucket_name)
                if versioning_response.get("Status") == "Enabled":
                    checks["versioning_growth"].append(
                        {
                            "BucketName": bucket_name,
                            "VersioningStatus": "Enabled",
                            "Recommendation": (
                                "Monitor versioning growth and configure lifecycle for old versions"
                            ),
                            "CheckCategory": "Versioning Optimization",
                            "EstimatedSavings": (
                                "$0.00/month - quantify via S3 Storage Lens (noncurrent-version bytes)"
                            ),
                        }
                    )
            except Exception as e:
                _route_bucket_error(ctx, bucket_name, e, action="s3:GetBucketVersioning")

            try:
                replication_response = bucket_s3_client.get_bucket_replication(Bucket=bucket_name)
                if replication_response.get("ReplicationConfiguration"):
                    checks["cross_region_replication"].append(
                        {
                            "BucketName": bucket_name,
                            "HasReplication": True,
                            "Recommendation": (
                                "Review cross-region replication necessity and destination usage"
                            ),
                            "CheckCategory": "Replication Optimization",
                            "EstimatedSavings": (
                                "$0.00/month - depends on replicated-bytes volume and destination region"
                            ),
                        }
                    )
            except Exception as e:
                _route_bucket_error(
                    ctx,
                    bucket_name,
                    e,
                    action="s3:GetReplicationConfiguration",
                    expected_codes=("ReplicationConfigurationNotFoundError",),
                )

            try:
                logging_response = bucket_s3_client.get_bucket_logging(Bucket=bucket_name)
                if logging_response.get("LoggingEnabled"):
                    checks["server_access_logs"].append(
                        {
                            "BucketName": bucket_name,
                            "LoggingEnabled": True,
                            "Recommendation": "Review if server access logs are still needed",
                            "CheckCategory": "Logging Optimization",
                            "EstimatedSavings": (
                                "$0.00/month - depends on log-volume retention policy"
                            ),
                        }
                    )
            except Exception as e:
                _route_bucket_error(ctx, bucket_name, e, action="s3:GetBucketLogging")

            try:
                objects_response = bucket_s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
                if objects_response.get("KeyCount", 0) == 0:
                    bucket_age = (datetime.now(bucket["CreationDate"].tzinfo) - bucket["CreationDate"]).days
                    if bucket_age > 30:
                        checks["unused_buckets"].append(
                            {
                                "BucketName": bucket_name,
                                "AgeDays": bucket_age,
                                "Recommendation": (
                                    f"Empty bucket older than {bucket_age} days - consider deletion"
                                ),
                                "CheckCategory": "Unused Resources",
                                "EstimatedSavings": (
                                    "$0.00/month - empty bucket incurs no storage cost"
                                ),
                            }
                        )
            except Exception as e:
                _route_bucket_error(ctx, bucket_name, e, action="s3:ListBucket")

            if _static(bucket_name, bucket_s3_client):
                checks["static_website_optimization"].append(
                    {
                        "BucketName": bucket_name,
                        "IsStaticWebsite": True,
                        "Recommendation": (
                            "Static website detected: Enable CloudFront CDN"
                            " for reduced data transfer costs and improved performance"
                        ),
                        "CheckCategory": "Static Website Optimization",
                        "EstimatedSavings": (
                            "$0.00/month - data transfer dependent (CloudFront CDN)"
                        ),
                    }
                )

    except Exception as e:
        if _is_access_denied(e):
            ctx.permission_issue(
                f"s3:ListAllMyBuckets denied: {e}",
                service="s3",
                action="s3:ListAllMyBuckets",
            )
        else:
            ctx.warn(f"Could not perform enhanced S3 checks: {e}", service="s3")

    recommendations: list[dict[str, Any]] = []
    for category, items in checks.items():
        for item in items:
            item["CheckCategory"] = item.get(
                "CheckCategory", category.replace("_", " ").title()
            )
            # Defensive: any path that forgot to set EstimatedSavings gets
            # an honest informational default so the adapter parses $0
            # consistently (audit L2-S3-002).
            item.setdefault("EstimatedSavings", "$0.00/month")
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}
