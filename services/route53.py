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


def _zones_share_a_vpc(ctx: ScanContext, zone_ids: list[str]) -> bool | None:
    """True when two of these private zones are attached to a common VPC.

    ``True``  — at least one VPC is shared, so the zones really are redundant.
    ``False`` — every zone serves a disjoint VPC set (split-horizon DNS).
    ``None``  — the associations could not be read; the caller must abstain
                rather than assume redundancy (MON-8).
    """
    seen: set[tuple[str, str]] = set()
    try:
        route53 = ctx.client("route53")
        for zone_id in zone_ids:
            resp = route53.get_hosted_zone(Id=_normalize_zone_id(zone_id))
            vpcs = resp.get("VPCs") or []
            if not vpcs:
                return None  # private zone with no reported VPCs — unknown
            keys = {(str(v.get("VPCRegion", "")), str(v.get("VPCId", ""))) for v in vpcs}
            if keys & seen:
                return True
            seen |= keys
        return False
    except Exception as exc:
        ctx.warn(f"Could not read VPC associations for duplicate private zones: {exc}", "route53")
        return None


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
        pricing_multiplier: Accepted for signature parity with sibling adapters,
            but intentionally NOT applied — Route 53 hosted zones are billed at a
            single global rate ($0.50/$0.10 per zone-month) with no regional
            premium. Callers previously passed the regional multiplier (e.g.
            1.12), overstating every zone saving ~12% (route53 global-rate fix).
    """
    # Route 53 is globally flat-priced — collapse any regional multiplier to 1.0.
    pricing_multiplier = 1.0
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

        # MON-7 — each removable zone must be priced against the account size
        # AFTER the ones already claimed. Pricing every zone against the same
        # starting count makes the ladder stand still: with 26 zones and 3
        # removable, all three price at the $0.10 tier ($0.30) when only the
        # first one actually sits there — the real saving is $1.10, because the
        # next two fall back into the $0.50 tier as the count descends.
        zones_claimed = 0

        for zone in hosted_zones:
            zone_id = _normalize_zone_id(zone.get("Id") or "")
            zone_name = zone.get("Name")
            is_private = zone.get("Config", {}).get("PrivateZone", False)
            record_count = zone.get("ResourceRecordSetCount", 0)

            if record_count <= 2:
                # Tier-1 zone removal (most accounts have <25 zones — apply tier-1 rate).
                remaining_zones = len(hosted_zones) - zones_claimed
                zone_savings = _route53_zone_monthly_cost(
                    1, base_zones_in_account=remaining_zones
                ) * pricing_multiplier
                zones_claimed += 1
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
                        "Counted": True,
                        "CheckCategory": "Unused Hosted Zones",
                        "AuditBasis": {
                            "removable_zones": 1,
                            "tier_rates_per_zone_month": [
                                ROUTE53_HOSTED_ZONE_TIER_1,
                                ROUTE53_HOSTED_ZONE_TIER_2,
                            ],
                            "base_zones_in_account": remaining_zones,
                            "zones_already_claimed": zones_claimed - 1,
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
                # MON-8 — the rec's own text says "check VPC associations", and
                # that is exactly the evidence that decides whether these are
                # duplicates at all. Two same-named private zones attached to
                # DIFFERENT VPCs are split-horizon DNS, a correct and common
                # design: consolidating them is impossible, so counting a dollar
                # against them asserts something the check never established.
                # Only zones sharing at least one VPC are genuinely redundant.
                shares_vpc = _zones_share_a_vpc(ctx, zone_ids)
                # Consolidating N same-name zones removes (N-1) of them.
                removable = len(zone_ids) - 1
                # H4 — any of these zones already counted by the unused check
                # (≤2 records) must NOT be counted again here. Subtract the
                # overlap so each zone's monthly $ is summed exactly once.
                normalized_group = {_normalize_zone_id(z) for z in zone_ids}
                overlap = len(normalized_group & counted_unused_ids)
                dedup_removable = max(0, removable - overlap)
                consolidate_savings = _route53_zone_monthly_cost(
                    dedup_removable, base_zones_in_account=len(hosted_zones) - zones_claimed
                ) * pricing_multiplier
                if shares_vpc is not True:
                    # Disjoint VPCs (False) or unreadable associations (None):
                    # the figure is shown, never counted.
                    consolidate_savings = 0.0
                rec: dict[str, Any] = {
                    "ZoneName": zone_name,
                    "ZoneCount": len(zone_ids),
                    "ZoneIds": zone_ids,
                    "Recommendation": "Multiple private zones with same name - check VPC associations",
                    "EstimatedMonthlySavings": round(consolidate_savings, 2),
                    "Counted": consolidate_savings > 0,
                    "CheckCategory": "Duplicate Private Zones",
                    "AuditBasis": {
                        "duplicate_zone_count": len(zone_ids),
                        "removable_zones": dedup_removable,
                        "already_counted_as_unused": overlap,
                        "zones_share_a_vpc": shares_vpc,
                        "base_zones_in_account": len(hosted_zones) - zones_claimed,
                        "region_multiplier": round(pricing_multiplier, 4),
                        "formula": (
                            "route53_tiered_cost((zone_count - 1) - unused_overlap) "
                            "x region_multiplier"
                        ),
                    },
                }
                if consolidate_savings > 0:
                    rec["EstimatedSavings"] = f"${consolidate_savings:.2f}/month if consolidated"
                elif shares_vpc is False:
                    rec["EstimatedSavings"] = (
                        "$0.00/month — advisory: these zones are attached to different "
                        "VPCs (split-horizon DNS), so they are not consolidatable"
                    )
                    rec["Counted"] = False
                elif shares_vpc is None:
                    rec["EstimatedSavings"] = (
                        "$0.00/month — advisory: VPC associations could not be read, so "
                        "whether these zones are genuinely redundant is unproven"
                    )
                    rec["Counted"] = False
                else:
                    # Every removable duplicate is already counted under Unused
                    # Hosted Zones → advisory $0 here (no double-count).
                    rec["EstimatedSavings"] = (
                        "$0.00/month — advisory: removable zones already counted "
                        "under Unused Hosted Zones"
                    )
                    rec["Counted"] = False
                if not rec.get("Counted"):
                    rec["PotentialMonthlySavings"] = round(
                        _route53_zone_monthly_cost(
                            dedup_removable,
                            base_zones_in_account=len(hosted_zones) - zones_claimed,
                        )
                        * pricing_multiplier,
                        2,
                    )
                checks["duplicate_private_zones"].append(rec)

    except Exception as e:
        ctx.warn(f"Could not perform Route 53 checks: {e}", "route53")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
