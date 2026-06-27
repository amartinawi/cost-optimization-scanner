"""Route 53 cost optimization checks.

Extracted from CostOptimizer.get_route53_checks() as a free function.
This module will later become Route53Module (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

# Route 53 hosted zone pricing (us-east-1 / global):
#   First 25 hosted zones:  $0.50/zone/month
#   Each additional zone:   $0.10/zone/month
# Source: https://aws.amazon.com/route53/pricing/
ROUTE53_HOSTED_ZONE_TIER_1: float = 0.50
ROUTE53_HOSTED_ZONE_TIER_2: float = 0.10
ROUTE53_HOSTED_ZONE_TIER_1_LIMIT: int = 25


def _route53_zone_monthly_cost(extra_zones: int, *, base_zones_in_account: int = 0) -> float:
    """Return monthly cost for `extra_zones` removable zones, given that
    `base_zones_in_account` zones already exist (so we know which tier
    each removable zone sits in).

    The first `ROUTE53_HOSTED_ZONE_TIER_1_LIMIT` zones cost $0.50/month,
    the rest cost $0.10/month. Removing zones saves the most-expensive
    tier first.
    """
    if extra_zones <= 0:
        return 0.0
    # Zones currently above the tier-1 limit are the cheapest to remove.
    above_tier_1 = max(0, base_zones_in_account - ROUTE53_HOSTED_ZONE_TIER_1_LIMIT)
    cheap_removable = min(extra_zones, above_tier_1) * ROUTE53_HOSTED_ZONE_TIER_2
    remaining = extra_zones - min(extra_zones, above_tier_1)
    expensive_removable = remaining * ROUTE53_HOSTED_ZONE_TIER_1
    return cheap_removable + expensive_removable


def _normalize_zone_id(raw: str) -> str:
    """Reduce a hosted-zone identifier to its bare id for cross-check dedup.

    Route 53 returns the zone id as ``/hostedzone/Z123ABC`` from some calls
    and as the bare ``Z123ABC`` from others. Strip to the final path segment
    so the ``unused_hosted_zones`` and ``duplicate_private_zones`` checks key
    on the same value and never count one zone's monthly $ twice (H4).
    """
    return (raw or "").split("/")[-1]


ROUTE53_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "unused_hosted_zones": {
        "title": "Remove Unused Hosted Zones",
        "description": "Hosted zones with only NS/SOA records may be unused and incur monthly charges.",
        "action": "Delete hosted zones that are no longer serving traffic",
    },
    "unnecessary_health_checks": {
        "title": "Review Health Check Necessity",
        "description": "Health checks incur monthly costs; verify each is tied to a routing policy.",
        "action": "Remove health checks not associated with routing policies",
    },
}


def get_route53_checks(ctx: ScanContext, pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Route 53 optimization checks.

    Args:
        ctx: Scan context with route53 client.
        pricing_multiplier: Regional pricing multiplier applied to per-rec
            $ values. Route 53 is a global service so multiplier is usually
            1.0 but kept for consistency with sibling adapters.
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "unused_hosted_zones": [],
        "unnecessary_health_checks": [],
        "complex_routing_simple_use": [],
        "old_records_deleted_resources": [],
        "duplicate_private_zones": [],
    }

    try:
        route53 = ctx.client("route53")

        paginator = route53.get_paginator("list_hosted_zones")
        hosted_zones: list[dict[str, Any]] = []
        for page in paginator.paginate():
            hosted_zones.extend(page.get("HostedZones", []))

        # H4 — track which normalized zone ids the unused check has already
        # claimed so the duplicate-private-zone check below cannot count the
        # same zone's monthly $ a second time (count each zone once).
        counted_unused_ids: set[str] = set()

        for zone in hosted_zones:
            zone_id = _normalize_zone_id(zone.get("Id") or "")
            zone_name = zone.get("Name")
            is_private = zone.get("Config", {}).get("PrivateZone", False)
            record_count = zone.get("ResourceRecordSetCount", 0)

            if record_count <= 2:
                # Tier-1 zone removal (most accounts have <25 zones — apply tier-1 rate).
                zone_savings = _route53_zone_monthly_cost(
                    1, base_zones_in_account=len(hosted_zones)
                ) * pricing_multiplier
                counted_unused_ids.add(zone_id)
                checks["unused_hosted_zones"].append(
                    {
                        "HostedZoneId": zone_id,
                        "ZoneName": zone_name,
                        "RecordCount": record_count,
                        "IsPrivate": is_private,
                        "Recommendation": "Hosted zone has minimal records - verify if still needed",
                        "EstimatedSavings": f"${zone_savings:.2f}/month per zone if deleted",
                        "EstimatedMonthlySavings": round(zone_savings, 2),
                        "CheckCategory": "Unused Hosted Zones",
                        "AuditBasis": {
                            "removable_zones": 1,
                            "tier_rates_per_zone_month": [
                                ROUTE53_HOSTED_ZONE_TIER_1,
                                ROUTE53_HOSTED_ZONE_TIER_2,
                            ],
                            "base_zones_in_account": len(hosted_zones),
                            "record_count": record_count,
                            "region_multiplier": round(pricing_multiplier, 4),
                            "formula": "route53_tiered_cost(1) x region_multiplier",
                        },
                    }
                )

            try:
                rec_paginator = route53.get_paginator("list_resource_record_sets")
                records: list[dict[str, Any]] = []
                for page in rec_paginator.paginate(HostedZoneId=zone_id):
                    records.extend(page.get("ResourceRecordSets", []))

                weighted_records = 0
                latency_records = 0
                geolocation_records = 0

                for record in records:
                    if record.get("Weight") is not None:
                        weighted_records += 1
                    if record.get("Region") is not None:
                        latency_records += 1
                    if record.get("GeoLocation") is not None:
                        geolocation_records += 1

                # Complex routing simple use finding removed: emitted vague "simple routing
                # reduces query costs" with no $ tied (Route 53 query cost is very low).
                _ = (weighted_records, latency_records, geolocation_records, record_count)

            except Exception as e:
                ctx.warn(f"Could not analyze records for zone {zone_name}: {e}", "route53")

        try:
            hc_paginator = route53.get_paginator("list_health_checks")
            health_checks: list[dict[str, Any]] = []
            for page in hc_paginator.paginate():
                health_checks.extend(page.get("HealthChecks", []))

            for health_check in health_checks:
                hc_id = health_check.get("Id")
                hc_config = health_check.get("HealthCheckConfig", {})
                hc_type = hc_config.get("Type")

                # Unnecessary Health Checks finding removed: $0.50/check is a generic AWS
                # rate quoted regardless of routing-dependency analysis — not a per-account
                # quantified saving.
                _ = (hc_id, hc_type)

        except Exception as e:
            ctx.warn(f"Could not analyze Route 53 health checks: {e}", "route53")

        private_zones = [z for z in hosted_zones if z.get("Config", {}).get("PrivateZone", False)]
        zone_names: dict[str, list[str]] = {}

        for zone in private_zones:
            zone_name = zone.get("Name") or ""
            zone_id_raw = zone.get("Id") or ""
            if zone_name in zone_names:
                zone_names[zone_name].append(zone_id_raw)
            else:
                zone_names[zone_name] = [zone_id_raw]

        for zone_name, zone_ids in zone_names.items():
            if len(zone_ids) > 1:
                # Consolidating N same-name zones removes (N-1) of them.
                removable = len(zone_ids) - 1
                # H4 — any of these zones already counted by the unused check
                # (≤2 records) must NOT be counted again here. Subtract the
                # overlap so each zone's monthly $ is summed exactly once.
                normalized_group = {_normalize_zone_id(z) for z in zone_ids}
                overlap = len(normalized_group & counted_unused_ids)
                dedup_removable = max(0, removable - overlap)
                consolidate_savings = _route53_zone_monthly_cost(
                    dedup_removable, base_zones_in_account=len(hosted_zones)
                ) * pricing_multiplier
                rec: dict[str, Any] = {
                    "ZoneName": zone_name,
                    "ZoneCount": len(zone_ids),
                    "ZoneIds": zone_ids,
                    "Recommendation": "Multiple private zones with same name - check VPC associations",
                    "EstimatedMonthlySavings": round(consolidate_savings, 2),
                    "CheckCategory": "Duplicate Private Zones",
                    "AuditBasis": {
                        "duplicate_zone_count": len(zone_ids),
                        "removable_zones": dedup_removable,
                        "already_counted_as_unused": overlap,
                        "base_zones_in_account": len(hosted_zones),
                        "region_multiplier": round(pricing_multiplier, 4),
                        "formula": (
                            "route53_tiered_cost((zone_count - 1) - unused_overlap) "
                            "x region_multiplier"
                        ),
                    },
                }
                if consolidate_savings > 0:
                    rec["EstimatedSavings"] = f"${consolidate_savings:.2f}/month if consolidated"
                else:
                    # Every removable duplicate is already counted under Unused
                    # Hosted Zones → advisory $0 here (no double-count).
                    rec["EstimatedSavings"] = (
                        "$0.00/month — advisory: removable zones already counted "
                        "under Unused Hosted Zones"
                    )
                    rec["Counted"] = False
                checks["duplicate_private_zones"].append(rec)

    except Exception as e:
        ctx.warn(f"Could not perform Route 53 checks: {e}", "route53")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
