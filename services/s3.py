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
from services._aws_errors import record_aws_error

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

# Evidence-gated savings model (audit S3-B). The legacy S3_SAVINGS_FACTORS
# (0.30 / 0.20 / 0.40) assumed an access pattern — "~65% IA-eligible" — with no
# evidence the data was actually cold, and applied that fraction to a cost base
# that itself charged every byte at the Standard rate (audit S3-A). Both are
# removed.
#
# Replacement: a bucket only earns a concrete dollar saving when (a) it holds
# bytes in S3 Standard that a lifecycle/Intelligent-Tiering transition could
# move, AND (b) CloudWatch S3 *request metrics* show the bucket received zero
# GET requests over the lookback window (i.e. the data is demonstrably cold).
# The saving is then the real Standard→Standard-IA rate delta on the Standard
# bytes — account-specific, grounded in measured size and live pricing. When no
# access-pattern evidence exists (request metrics not enabled, or fast mode),
# the finding is emitted as a $0.00 advisory pointing at Storage Class Analysis
# rather than inventing a dollar figure.
COLD_LOOKBACK_DAYS: int = 30

# Opportunity classes that represent a transition gap a NEW lifecycle policy
# could close (and therefore may carry real savings). A bucket that already has
# a lifecycle policy (the "intelligent_tiering" class: has_lifecycle=True,
# has_tiering=False) is excluded — its existing rule may already be performing
# the transition, so crediting the full Standard->Standard-IA delta would
# overstate / double-count the saving (network_cost / s3 S3-N4).
_GAP_OPPORTUNITY_CLASSES: frozenset[str] = frozenset(
    {"both_missing", "lifecycle_missing"}
)

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

# Fallback-only relative-price multipliers vs us-east-1, used solely when the
# PricingEngine is unavailable (audit S3-G). The authoritative path is
# ``PricingEngine.get_s3_monthly_price_per_gb`` (region-correct live pricing);
# these estimates are a last resort. Key drift is tolerated: newer-region
# entries use "GLACIER" while older entries use "GLACIER_FLEXIBLE_RETRIEVAL" /
# "GLACIER_INSTANT_RETRIEVAL", but every Glacier multiplier is 1.0, so a missed
# lookup falls through to 1.0 with no effect. Only the STANDARD/IA multipliers
# materially affect the fallback path.
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
            "3. Transition cold data to lower-cost storage classes\n"
            "4. Quantified savings shown are the Standard->Standard-IA delta on"
            " bytes proven cold by request metrics; enable S3 Storage Class"
            " Analysis to quantify advisory buckets"
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

    Thin wrapper over ``_s3_price_per_gb`` (which prices at ``region`` via a
    region-correct ``PricingEngine``, audit S3-I, and falls back to module-const
    rates × regional multiplier). Used by the fast-mode single-class estimate.
    """
    try:
        return round(size_gb * _s3_price_per_gb(ctx, storage_class, region), 2)
    except Exception as e:
        logger.debug("S3 storage cost calc failed for %s/%s: %s", region, storage_class, e)
        return round(size_gb * S3_STORAGE_COSTS["STANDARD"], 2)


def _is_static_website_bucket(bucket_name: str, s3_client: Any, ctx: ScanContext | None = None) -> bool:
    """Return True if ``bucket_name`` is configured for static-website hosting.

    Result is intended to be cached on ``bucket_info`` by the caller so it is
    not re-queried for every code path that needs it (audit L2-S3-007).
    """
    try:
        s3_client.get_bucket_website(Bucket=bucket_name)
        return True
    except Exception as e:
        # NoSuchWebsiteConfiguration is the normal "not a website" answer — not an
        # error. Anything else (AccessDenied / throttle) was previously only
        # debug-logged, so a permission gap silently classified the bucket as
        # non-website; classify it so it surfaces in the report instead.
        if "NoSuchWebsiteConfiguration" not in str(e):
            if ctx is not None:
                record_aws_error(
                    ctx, e, service="s3", context=f"GetBucketWebsite on {bucket_name} failed"
                )
            else:
                logger.debug("S3 GetBucketWebsite error on %s: %s", bucket_name, e)
        return False


def _classify_opportunities(bucket_info: dict[str, Any]) -> str:
    """Return the opportunity-class label matching this bucket's config gaps.

    Used to group buckets in the report and to decide which buckets are
    eligible for an evidence-gated saving (see ``_GAP_OPPORTUNITY_CLASSES``).
    Returns ``static_website``, ``both_missing``, ``lifecycle_missing``,
    ``intelligent_tiering``, or ``other`` (fully optimized).
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


# CloudWatch BucketSizeBytes `StorageType` dimension → our storage-class key.
_CW_STORAGE_TYPE_TO_CLASS: dict[str, str] = {
    "StandardStorage": "STANDARD",
    "StandardIAStorage": "STANDARD_IA",
    "OneZoneIAStorage": "ONEZONE_IA",
    "GlacierStorage": "GLACIER",
    "DeepArchiveStorage": "DEEP_ARCHIVE",
    "IntelligentTieringStorage": "INTELLIGENT_TIERING",
}


def _s3_price_per_gb(ctx: ScanContext | None, storage_class: str, region: str) -> float:
    """Return the unrounded $/GB-month rate for ``storage_class`` in ``region``.

    Uses a ``PricingEngine`` scoped to ``region`` via ``for_region`` so a bucket
    is priced at its OWN home region, not the scan region (audit S3-I) — S3 is
    global and buckets routinely live elsewhere. Falls back to the module
    constant × regional multiplier when no engine is available. Returns the raw
    per-GB rate (no rounding) so cheap classes (e.g. Deep Archive $0.00099)
    survive multiplication.
    """
    engine_key = _SC_MAP.get(storage_class, storage_class)
    try:
        if ctx and ctx.pricing_engine:
            # Price at the BUCKET's home region, not the scan region — S3 is
            # global and buckets routinely live elsewhere (audit S3-I).
            engine = ctx.pricing_engine.for_region(region)
            price = engine.get_s3_monthly_price_per_gb(engine_key)
            if price > 0:
                return price
    except Exception as e:  # noqa: BLE001 — pricing must never crash a scan
        logger.debug("S3 per-GB price lookup failed for %s in %s: %s", engine_key, region, e)
    base_cost = S3_STORAGE_COSTS.get(storage_class, S3_STORAGE_COSTS["STANDARD"])
    regional_multiplier = (
        S3_REGIONAL_MULTIPLIERS.get(region, {}).get(engine_key)
        or S3_REGIONAL_MULTIPLIERS.get(region, {}).get(storage_class, 1.0)
    )
    return base_cost * regional_multiplier


def _cost_from_class_sizes(
    ctx: ScanContext | None,
    region: str,
    class_sizes: dict[str, float],
) -> float:
    """Monthly storage cost summed across each class at its OWN rate.

    Replaces the legacy path that priced every byte at the Standard rate
    regardless of the bucket's actual storage-class mix (audit S3-A) — a bucket
    already in Glacier/Deep-Archive was over-stated by up to ~23×.
    """
    total = 0.0
    for storage_class, size_gb in class_sizes.items():
        if size_gb and size_gb > 0:
            total += size_gb * _s3_price_per_gb(ctx, storage_class, region)
    return round(total, 2)


def _assess_bucket_coldness(
    ctx: ScanContext,
    bucket_name: str,
    s3_client: Any,
    bucket_region: str,
) -> str:
    """Classify a bucket's access pattern from measured evidence.

    Returns one of:

    - ``"cold"``  — CloudWatch S3 request metrics are enabled for the whole
      bucket AND recorded zero GET requests over ``COLD_LOOKBACK_DAYS``. The
      data is demonstrably untouched, so a lifecycle/Intelligent-Tiering
      transition to Infrequent Access saves money without incurring retrieval.
    - ``"warm"`` — request metrics show GET activity; an IA transition could
      add retrieval/per-request cost, so no storage saving is credited.
    - ``"unknown"`` — no request-metrics evidence is available (the feature is
      off, or access was denied). The caller emits a $0 advisory rather than a
      fabricated dollar figure (audit S3-B).

    This is the only access-pattern signal the scanner reads directly; S3
    Storage Class Analysis exports its results to a destination bucket and
    cannot be read inline, so it is surfaced as an advisory pointer instead.
    """
    try:
        metrics_response = s3_client.list_bucket_metrics_configurations(Bucket=bucket_name)
    except Exception as e:  # noqa: BLE001
        _route_bucket_error(ctx, bucket_name, e, action="s3:GetMetricsConfiguration")
        return "unknown"

    configs = metrics_response.get("MetricsConfigurationList", [])
    # An entire-bucket request-metrics filter (no Filter key, or an explicit
    # whole-bucket filter) is required to reason about total access.
    filter_ids = [
        c["Id"]
        for c in configs
        if "Filter" not in c or not c.get("Filter")
    ]
    if not filter_ids:
        return "unknown"

    cloudwatch = _bucket_cloudwatch_client(ctx, bucket_region)
    if cloudwatch is None:
        return "unknown"

    total_get_requests = 0.0
    for filter_id in filter_ids:
        try:
            response = cloudwatch.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="GetRequests",
                Dimensions=[
                    {"Name": "BucketName", "Value": bucket_name},
                    {"Name": "FilterId", "Value": filter_id},
                ],
                StartTime=datetime.now(UTC) - timedelta(days=COLD_LOOKBACK_DAYS),
                EndTime=datetime.now(UTC),
                Period=86400,
                Statistics=["Sum"],
            )
        except Exception as e:  # noqa: BLE001
            if _is_endpoint_unreachable(e):
                _mark_region_dead(ctx, bucket_region, f"S3 request metrics for {bucket_name}")
            elif (
                _is_access_denied(e)
                or "AccessDenied" in str(e)
                or "Unauthorized" in str(e)
            ):
                ctx.permission_issue(
                    f"cloudwatch:GetMetricStatistics denied on {bucket_name}",
                    service="cloudwatch",
                    action="cloudwatch:GetMetricStatistics",
                )
            elif "Throttling" in str(e) or "RequestLimitExceeded" in str(e):
                ctx.warn(
                    f"cloudwatch:GetMetricStatistics throttled on {bucket_name}",
                    service="cloudwatch",
                )
            else:
                logger.debug("S3 GetRequests metric error on %s: %s", bucket_name, e)
            return "unknown"
        total_get_requests += sum(dp.get("Sum", 0.0) for dp in response.get("Datapoints", []))

    return "cold" if total_get_requests == 0 else "warm"


def get_s3_bucket_analysis(
    ctx: ScanContext,
    fast_mode: bool,
    pricing_multiplier: float,
) -> dict[str, Any]:
    """Scan every accessible S3 bucket and emit per-bucket recommendations.

    Cost (``EstimatedMonthlyCost``) is summed per storage class at each class's
    own live rate — a bucket already in Glacier/Deep-Archive is no longer priced
    as Standard (audit S3-A).

    ``SavingsDelta`` is evidence-gated (audit S3-B): a bucket earns a concrete
    dollar only when it holds S3 Standard bytes a lifecycle/Intelligent-Tiering
    transition could move AND CloudWatch request metrics show those bytes are
    cold (zero GET requests over ``COLD_LOOKBACK_DAYS``); the saving is the real
    Standard→Standard-IA rate delta on those bytes. Buckets with a config gap
    but no access-pattern evidence are emitted as ``$0.00`` advisories. The old
    assumed-percentage factors (0.30/0.20/0.40) are gone.

    Note: ``pricing_multiplier`` is accepted for ABI compatibility with the
    adapter signature. Per-storage-class costs come from ``PricingEngine``
    (region-correct) when available, falling back to module constants × the
    regional multiplier dict.
    """
    del pricing_multiplier  # PricingEngine path is authoritative; constant retained for signature stability.
    logger.debug("S3 bucket analysis starting (fast_mode=%s)", fast_mode)
    s3 = ctx.client("s3")

    try:
        # Paginate list_buckets: since the 2024 API change it returns at most
        # 10k buckets per page behind a ContinuationToken, so a single call
        # silently under-enumerates large accounts.
        buckets = []
        response = s3.list_buckets()
        buckets.extend(response.get("Buckets", []))
        while response.get("ContinuationToken"):
            response = s3.list_buckets(ContinuationToken=response["ContinuationToken"])
            buckets.extend(response.get("Buckets", []))
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
                bucket_name, bucket_s3_client, ctx
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
                    # Per-class GB, keyed by our storage-class keys. Drives both
                    # the cost (each class at its own rate, audit S3-A) and the
                    # Standard-bytes-only savings model (audit S3-B).
                    class_sizes: dict[str, float] = {}

                    for cw_storage_type, class_key in _CW_STORAGE_TYPE_TO_CLASS.items():
                        try:
                            size_response = bucket_cloudwatch_client.get_metric_statistics(
                                Namespace="AWS/S3",
                                MetricName="BucketSizeBytes",
                                Dimensions=[
                                    {"Name": "BucketName", "Value": bucket_name},
                                    {"Name": "StorageType", "Value": cw_storage_type},
                                ],
                                StartTime=datetime.now(UTC) - timedelta(days=2),
                                EndTime=datetime.now(UTC),
                                Period=86400,
                                Statistics=["Average"],
                            )
                            if size_response["Datapoints"]:
                                class_size_gb = size_response["Datapoints"][-1]["Average"] / (1024**3)
                                class_sizes[class_key] = class_size_gb
                                total_size_gb += class_size_gb
                        except Exception as e:
                            if _is_endpoint_unreachable(e):
                                _mark_region_dead(
                                    ctx,
                                    bucket_region,
                                    f"S3 size metric for {bucket_name}/{cw_storage_type}",
                                )
                                region_dead_mid_loop = True
                                break  # Stop iterating storage classes for this bucket.
                            if (
                                _is_access_denied(e)
                                or "AccessDenied" in str(e)
                                or "Unauthorized" in str(e)
                            ):
                                ctx.permission_issue(
                                    f"cloudwatch:GetMetricStatistics denied on {bucket_name}",
                                    service="cloudwatch",
                                    action="cloudwatch:GetMetricStatistics",
                                )
                            elif "Throttling" in str(e) or "RequestLimitExceeded" in str(e):
                                ctx.warn(
                                    f"cloudwatch:GetMetricStatistics throttled on {bucket_name}",
                                    service="cloudwatch",
                                )
                            else:
                                logger.debug(
                                    "Error getting S3 metrics for %s/%s: %s",
                                    bucket_name,
                                    cw_storage_type,
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
                        bucket_info["ClassSizes"] = class_sizes
                        bucket_info["StandardGB"] = class_sizes.get("STANDARD", 0.0)
                        bucket_info["EstimatedMonthlyCost"] = _cost_from_class_sizes(
                            ctx, bucket_region, class_sizes
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

            # Evidence-gated savings (audit S3-A + S3-B). A bucket earns a
            # concrete dollar only when it has Standard bytes a transition could
            # move AND request metrics prove those bytes are cold. Otherwise it
            # is a $0 advisory — never a fabricated figure.
            opportunity_key = _classify_opportunities(bucket_info)
            bucket_info["OpportunityClass"] = opportunity_key
            standard_gb = bucket_info.get("StandardGB", 0.0) or 0.0
            has_gap = opportunity_key in _GAP_OPPORTUNITY_CLASSES

            savings = 0.0
            if has_gap and standard_gb > 0 and not fast_mode:
                coldness = _assess_bucket_coldness(
                    ctx, bucket_name, bucket_s3_client, bucket_region
                )
                bucket_info["AccessSignal"] = coldness
                if coldness == "cold":
                    std_rate = _s3_price_per_gb(ctx, "STANDARD", bucket_region)
                    ia_rate = _s3_price_per_gb(ctx, "STANDARD_IA", bucket_region)
                    delta = max(std_rate - ia_rate, 0.0)
                    savings = round(standard_gb * delta, 2)
                    bucket_info["PricingBasis"] = (
                        f"{standard_gb:.1f} GB in S3 Standard x ${delta:.4f}/GB "
                        f"Standard->Standard-IA delta; 0 GET requests over "
                        f"{COLD_LOOKBACK_DAYS}d (request metrics)"
                    )

            if savings > 0:
                bucket_info["SavingsDelta"] = savings
                bucket_info["EstimatedSavings"] = f"${savings:.2f}/month"
                bucket_info["Counted"] = True
            else:
                bucket_info["SavingsDelta"] = 0.0
                # F1/F2 — a $0 bucket has no defensible counted saving. Mark it
                # Counted=False (the standard flag every other adapter uses) so the
                # reporter renders it as advisory and excludes it from the headline
                # count, instead of the bespoke "Advisory" flag the reporter's
                # count logic did not recognise (336 $0 cards were being shown as
                # "counted" on the S3 tab header).
                bucket_info["Counted"] = False
                if opportunity_key == "static_website":
                    bucket_info["EstimatedSavings"] = (
                        "$0.00/month - data transfer dependent (CloudFront CDN)"
                    )
                elif has_gap and (standard_gb > 0 or fast_mode):
                    # Real transition gap, but no cold-access evidence (metrics
                    # off, or fast-mode sample) — advise, don't invent dollars.
                    bucket_info["Advisory"] = True
                    bucket_info["EstimatedSavings"] = (
                        "$0.00/month - enable S3 Storage Class Analysis or request "
                        "metrics to quantify (no access-pattern evidence)"
                    )
                else:
                    bucket_info["EstimatedSavings"] = "$0.00/month"

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
        "request_heavy_buckets": [],
        "static_website_optimization": [],
    }
    # Per-bucket cache of _is_static_website_bucket so it's queried at most
    # once per bucket inside this function (audit L2-S3-007).
    static_cache: dict[str, bool] = {}

    def _static(name: str, client: Any) -> bool:
        if name not in static_cache:
            # Forward ctx so an AccessDenied/throttle on GetBucketWebsite is
            # classified here too, not silently debug-logged (the enhanced-checks
            # path was missed in the first pass of this fix).
            static_cache[name] = _is_static_website_bucket(name, client, ctx)
        return static_cache[name]

    try:
        # Paginate list_buckets: since the 2024 API change it returns at most
        # 10k buckets per page behind a ContinuationToken, so a single call
        # silently under-enumerates large accounts.
        buckets = []
        response = s3.list_buckets()
        buckets.extend(response.get("Buckets", []))
        while response.get("ContinuationToken"):
            response = s3.list_buckets(ContinuationToken=response["ContinuationToken"])
            buckets.extend(response.get("Buckets", []))

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

            # Cross-region-replication and server-access-logging checks removed:
            # both emitted a "review necessity / review if still needed" nudge with
            # an explicit $0.00 saving — a best-practice/housekeeping recommendation
            # with no concrete account-specific dollar, which is outside the scanner's
            # strictly-cost scope. Dropping them also removes a get_bucket_replication
            # + get_bucket_logging call per bucket. (Noncurrent-version growth — a
            # real storage cost — is still surfaced via "Versioning Optimization".)

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
