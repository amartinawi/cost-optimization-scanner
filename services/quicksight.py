"""QuickSight BI service cost optimization checks.

Extracted from CostOptimizer.get_enhanced_quicksight_checks() as a free function.
This module will later become QuickSightModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

import logging
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

# Edition-aware SPICE $/GB-month (us-east-1, live-validated via AWS Pricing API
# — quicksight C1). Enterprise SKU R8PKSKFCHES8YSKK ($0.38); Standard SKU
# T4GAEKP5WQQWCUD5 ($0.25). The previous $0.38-for-both assumption overstated
# Standard accounts by 52%.
SPICE_RATE_PER_GB: dict[str, float] = {
    "STANDARD": 0.25,
    "ENTERPRISE": 0.38,
}
SPICE_RATE_DEFAULT: float = 0.38


def quicksight_spice_rate(edition: str) -> float:
    """SPICE $/GB-month for the account edition (quicksight C1)."""
    return SPICE_RATE_PER_GB.get(str(edition).upper(), SPICE_RATE_DEFAULT)


logger = logging.getLogger(__name__)

QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "spice_optimization": {
        "title": "Optimize QuickSight SPICE Usage",
        "description": "Review SPICE capacity and optimize data refresh schedules.",
        "action": "Optimize SPICE capacity and refresh schedules",
    }
}


def get_enhanced_quicksight_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced QuickSight cost optimization checks."""
    checks: dict[str, list[dict[str, Any]]] = {
        "spice_optimization": [],
        "user_optimization": [],
        "capacity_optimization": [],
    }

    try:
        quicksight = ctx.client("quicksight")

        subscription = quicksight.describe_account_subscription(AwsAccountId=ctx.account_id)
        account_info = subscription.get("AccountInfo", {})
        if account_info.get("AccountSubscriptionStatus") != "ACCOUNT_CREATED":
            return {"recommendations": [], "checks": checks}
        # Real account edition (STANDARD / ENTERPRISE) drives the SPICE $/GB rate
        # (quicksight C1): Standard = $0.25/GB-Mo (SKU T4GAEKP5WQQWCUD5),
        # Enterprise = $0.38/GB-Mo (SKU R8PKSKFCHES8YSKK), both live-validated.
        # The previous ``PurchaseMode`` source was the SPICE purchase mode, not
        # the account edition, and the $0.38-for-both assumption overstated
        # Standard accounts by 52%.
        account_edition = account_info.get("Edition", "")

        namespaces_paginator = quicksight.get_paginator("list_namespaces")
        namespaces: list[dict[str, Any]] = []
        for page in namespaces_paginator.paginate(AwsAccountId=ctx.account_id):
            namespaces.extend(page.get("Namespaces", []))

        total_users = 0
        user_enum_failed = False
        for namespace in namespaces:
            namespace_name = namespace.get("Name")
            try:
                paginator = quicksight.get_paginator("list_users")
                for page in paginator.paginate(AwsAccountId=ctx.account_id, Namespace=namespace_name):
                    total_users += len(page.get("UserList", []))
            except Exception as ns_exc:
                # H2 — record the per-namespace failure; an inability to enumerate
                # users must not silently zero the account-level SPICE check below.
                record_aws_error(
                    ctx,
                    ns_exc,
                    service="quicksight",
                    context=f"quicksight:ListUsers failed for namespace '{namespace_name}'",
                )
                user_enum_failed = True
                continue

        # H2 — proceed when enumeration failed so a ListUsers permission gap does
        # not masquerade as "0 users → no SPICE finding".
        spice_supported = hasattr(quicksight, "describe_spice_capacity")
        if (total_users > 0 or user_enum_failed) and not spice_supported:
            # LW-02: AWS exposes no read API for account-level SPICE capacity in
            # boto3 (only the write-side UpdateSPICECapacityConfiguration), so this
            # lever is not implementable against real AWS — the call raised
            # AttributeError on every scan. Skip cleanly instead of warning. Unit
            # tests inject a fake client that DOES expose describe_spice_capacity,
            # so the SPICE accounting below (quicksight L3/H1/H3) stays exercised.
            logger.info(
                "QuickSight SPICE capacity is not exposed by the AWS API "
                "(no read operation in boto3) - skipping SPICE optimization check"
            )
        elif total_users > 0 or user_enum_failed:
            try:
                spice_capacity = quicksight.describe_spice_capacity(AwsAccountId=ctx.account_id)
                capacity_config = spice_capacity.get("SpiceCapacityConfiguration", {})

                used_capacity = capacity_config.get("UsedCapacityInBytes", 0) / (1024**3)
                total_capacity = capacity_config.get("TotalCapacityInBytes", 0) / (1024**3)

                # The 50% threshold splits a COUNTED reclaim opportunity from an
                # ADVISORY one (quicksight L3). Below 50% used (>50% idle), the
                # unused capacity is large enough to be a concrete reclaim — counted.
                # Between 50% and 100% used (1–49% idle), the headroom is too small
                # to safely reclaim (SPICE needs refresh headroom), so it is a $0
                # ``Counted=False`` advisory: surfaced, never summed.
                if total_capacity > 0 and used_capacity < total_capacity:
                    unused_gb = round(total_capacity - used_capacity, 2)
                    util_pct = round((used_capacity / total_capacity) * 100, 1)
                    is_advisory = used_capacity >= total_capacity * 0.5
                    rec = {
                        "UserCount": total_users,
                        "UsedCapacityGB": round(used_capacity, 2),
                        "TotalCapacityGB": round(total_capacity, 2),
                        "UtilizationPercent": util_pct,
                        "UnusedSpiceCapacityGB": unused_gb,
                        "Edition": account_edition,
                        "Recommendation": (
                            f"SPICE capacity underutilized"
                            f" ({round(used_capacity, 1)}/{round(total_capacity, 1)}"
                            " GB) - consider reducing"
                        ),
                        # H3 — the dollar (string + number) is single-sourced in
                        # the adapter from quicksight_spice_rate × pricing_multiplier
                        # so the card string and the counted number always agree and
                        # are region-scaled. The shim deliberately carries no
                        # ``EstimatedSavings`` dollar (it had a non-region-scaled,
                        # whole-dollar string that disagreed with the counted number
                        # in any non-us-east-1 region).
                        "CheckCategory": "SPICE Optimization",
                    }
                    if is_advisory:
                        # quicksight L3: 1–49% idle — advisory only, never counted.
                        rec["Counted"] = False
                        rec["PricingWarning"] = (
                            "SPICE idle < 50% — advisory only; partial headroom is needed for "
                            "dataset refreshes and is not a concrete reclaim opportunity"
                        )
                    checks["spice_optimization"].append(rec)
            except Exception as e:
                # H1 — classify the SPICE-capacity read failure (the only
                # revenue-producing QuickSight check); never swallow it.
                record_aws_error(ctx, e, service="quicksight", context="quicksight:DescribeSpiceCapacity failed")

    except Exception as e:
        error_str = str(e)
        if "ResourceNotFoundException" in error_str and "account does not exist" in error_str:
            logger.info("QuickSight is not enabled in this account - skipping QuickSight analysis")
        else:
            ctx.warn(f"Could not analyze QuickSight resources: {e}", "quicksight")

    all_recommendations: list[dict[str, Any]] = []
    for _, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
