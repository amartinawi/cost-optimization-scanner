"""Lightsail cost optimization checks.

Extracted from CostOptimizer.get_enhanced_lightsail_checks() as a free function.
This module will later become LightsailModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "idle_instances": {
        "title": "Delete Idle Lightsail Instances",
        "description": "Stopped Lightsail instances still incur charges. Delete unused instances.",
        "action": "Delete stopped instances or restart if needed",
    }
}

# Live-validated Lightsail bundle monthly list prices (us-east-1), fetched from
# the AWS Pricing API (``AmazonLightsail`` productFamily ``Lightsail Instance``,
# usagetype ``USE1-BundleUsage:<mem>`` / ``USE1-BundleUsage:<mem>_win``) on
# 2026-06-27, price-list version 20260612203611. AWS publishes one flat monthly
# price per bundle; it equals the bundle's hourly on-demand rate × 744 (the
# per-month billing cap AWS applies to an always-on Lightsail instance), so
# deleting an always-on instance recovers exactly this monthly figure.
#
# Keyed by (size, OS). The bundle *generation* (``_2_0`` vs ``_3_0``) does NOT
# change the standard-bundle price — both resolve to the same Pricing-API
# usagetype — so generation is parsed out and ignored. Replaces the previous
# synthetic ×2 geometric ``_BUNDLE_COSTS`` (lightsail H1) and the OS-blind
# lookup that mispriced Windows bundles at the $20 default (lightsail H2).
_BUNDLE_MONTHLY_LINUX: dict[str, float] = {
    "nano": 5.00,       # 0.5 GB  USE1-BundleUsage:0.5GB   ($0.00672/hr × 744)
    "micro": 7.00,      # 1 GB    USE1-BundleUsage:1GB     ($0.00940/hr × 744)
    "small": 12.00,     # 2 GB    USE1-BundleUsage:2GB     ($0.01612/hr × 744)
    "medium": 24.00,    # 4 GB    USE1-BundleUsage:4GB     ($0.03225/hr × 744)
    "large": 44.00,     # 8 GB    USE1-BundleUsage:8GB     ($0.05913/hr × 744)
    "xlarge": 84.00,    # 16 GB   USE1-BundleUsage:16GB    ($0.11290/hr × 744)
    "2xlarge": 164.00,  # 32 GB   USE1-BundleUsage:32GB    ($0.22043/hr × 744)
}

_BUNDLE_MONTHLY_WINDOWS: dict[str, float] = {
    "nano": 9.50,       # 0.5 GB  USE1-BundleUsage:0.5GB_win   ($0.01276/hr × 744)
    "micro": 14.00,     # 1 GB    USE1-BundleUsage:1GB_win     ($0.01881/hr × 744)
    "small": 22.00,     # 2 GB    USE1-BundleUsage:2GB_win     ($0.02956/hr × 744)
    "medium": 44.00,    # 4 GB    USE1-BundleUsage:4GB_win     ($0.05913/hr × 744)
    "large": 74.00,     # 8 GB    USE1-BundleUsage:8GB_win     ($0.09946/hr × 744)
    "xlarge": 124.00,   # 16 GB   USE1-BundleUsage:16GB_win    ($0.16666/hr × 744)
    "2xlarge": 244.00,  # 32 GB   USE1-BundleUsage:32GB_win    ($0.32795/hr × 744)
}

# Unattached ("unused") Lightsail static IP: $0.005/hr after the first free
# hour (AWS Pricing API usagetype ``USE1-UnusedStaticIP``, validated
# 2026-06-27). ×730 = $3.65/mo. Matches the AWS public-IPv4 charge.
LIGHTSAIL_UNUSED_STATIC_IP_HOURLY: float = 0.005
HOURS_PER_MONTH: int = 730

_VALID_BUNDLE_SIZES: frozenset = frozenset(_BUNDLE_MONTHLY_LINUX)


def _parse_bundle_id(bundle_id: str) -> tuple[str | None, bool]:
    """Parse a Lightsail bundle id into ``(size_token, is_windows)``.

    Lightsail bundle ids look like ``medium_2_0`` (Linux gen-2),
    ``medium_3_0`` (gen-3), or ``medium_win_2_0`` (Windows). The leading
    underscore-separated token is the size; a ``win`` token marks Windows.

    Returns ``(None, is_windows)`` when the size token is not a recognized
    standard bundle (unknown generation/optimized family or malformed id) so
    callers fail safe to a $0 advisory instead of fabricating a price.
    """
    if not bundle_id:
        return None, False
    parts = bundle_id.lower().split("_")
    is_windows = "win" in parts
    size = parts[0]
    if size in _VALID_BUNDLE_SIZES:
        return size, is_windows
    return None, is_windows


def get_lightsail_bundle_cost(bundle_id: str) -> float | None:
    """Return the live-validated monthly cost for a Lightsail bundle, or None.

    Returns the AWS-published monthly list price for the bundle's size and OS
    (Linux vs Windows), or ``None`` for an unrecognized bundle id (unknown
    generation, Compute/Memory-optimized family, or malformed) so callers fail
    safe to a $0 advisory rather than counting a fabricated default dollar
    (lightsail H3 — the old $20 default leaked into the headline).
    """
    size, is_windows = _parse_bundle_id(bundle_id)
    if size is None:
        return None
    table = _BUNDLE_MONTHLY_WINDOWS if is_windows else _BUNDLE_MONTHLY_LINUX
    return table.get(size)


def get_enhanced_lightsail_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Lightsail cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "idle_instances": [],
        "oversized_instances": [],
        "unused_static_ips": [],
        "load_balancer_optimization": [],
        "database_optimization": [],
    }

    try:
        lightsail = ctx.client("lightsail")
        paginator = lightsail.get_paginator("get_instances")
        instances: list[dict[str, Any]] = []
        for page in paginator.paginate():
            instances.extend(page.get("instances", []))

        for instance in instances:
            instance_name = instance.get("name")
            instance_state = instance.get("state", {}).get("name")
            # H3: no ``or "medium_2_0"`` fallback — a missing/unknown bundle id
            # must NOT be silently priced as a medium instance. The adapter
            # resolves the real per-bundle price (OS-aware) and demotes
            # unrecognized bundle ids to a $0 advisory.
            bundle_id: str = instance.get("bundleId") or ""
            _size, is_windows = _parse_bundle_id(bundle_id)
            operating_system = "Windows" if is_windows else "Linux"

            if instance_state == "stopped":
                # Dollar is resolved in the adapter (single-sourced number +
                # string, region-scaled) so the card never desyncs from the
                # counted total.
                checks["idle_instances"].append(
                    {
                        "InstanceName": instance_name,
                        "State": instance_state,
                        "BundleId": bundle_id,
                        "OperatingSystem": operating_system,
                        "Recommendation": "Delete stopped Lightsail instance to eliminate costs",
                        "EstimatedSavings": "$0.00/month — pending pricing",
                        "CheckCategory": "Idle Resource Cleanup",
                    }
                )

            if instance_state == "running" and ("xlarge" in bundle_id.lower() or "large" in bundle_id.lower()):
                checks["oversized_instances"].append(
                    {
                        "InstanceName": instance_name,
                        "BundleId": bundle_id,
                        "OperatingSystem": operating_system,
                        "State": instance_state,
                        "Recommendation": (
                            "Review instance utilization - consider downsizing if CPU/memory usage is consistently low"
                        ),
                        # Name-based oversized with no utilization metric — the
                        # adapter demotes this to a $0 advisory (lightsail C1).
                        "EstimatedSavings": "$0.00/month — advisory",
                        "CheckCategory": "Instance Rightsizing",
                        "Note": ("Recommendation based on instance size - verify actual utilization before downsizing"),
                    }
                )

        # get_static_ips has no boto3 paginator; follow nextPageToken manually
        # (mirrors the get_instances paginator above) so accounts with more
        # static IPs than one page are fully reported, not silently truncated.
        static_ips: list[dict[str, Any]] = []
        page_token = None
        while True:
            kwargs = {"pageToken": page_token} if page_token else {}
            static_ips_response = lightsail.get_static_ips(**kwargs)
            static_ips.extend(static_ips_response.get("staticIps", []))
            page_token = static_ips_response.get("nextPageToken")
            if not page_token:
                break

        for static_ip in static_ips:
            if not static_ip.get("attachedTo"):
                # Dollar is resolved + region-scaled in the adapter (H4) so the
                # counted static-IP saving matches the rendered card.
                checks["unused_static_ips"].append(
                    {
                        "StaticIpName": static_ip.get("name"),
                        "IpAddress": static_ip.get("ipAddress"),
                        "Recommendation": "Release unused static IP to avoid charges",
                        "EstimatedSavings": "$0.00/month — pending pricing",
                        "CheckCategory": "Unused Resource Cleanup",
                    }
                )

    except Exception as e:
        ctx.warn(f"Could not analyze Lightsail resources: {e}", "lightsail")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
