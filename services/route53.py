"""Route 53 cost optimization checks.

Extracted from CostOptimizer.get_route53_checks() as a free function.
This module will later become Route53Module (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/route53.py] Route53 module active")

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


def get_route53_checks(ctx: ScanContext) -> dict[str, Any]:
    """Route 53 optimization checks"""
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

        for zone in hosted_zones:
            zone_id = (zone.get("Id") or "").split("/")[-1]
            zone_name = zone.get("Name")
            is_private = zone.get("Config", {}).get("PrivateZone", False)
            record_count = zone.get("ResourceRecordSetCount", 0)

            if record_count <= 2:
                checks["unused_hosted_zones"].append(
                    {
                        "HostedZoneId": zone_id,
                        "ZoneName": zone_name,
                        "RecordCount": record_count,
                        "IsPrivate": is_private,
                        "Recommendation": "Hosted zone has minimal records - verify if still needed",
                        "EstimatedSavings": "$0.50/month per zone if deleted",
                        "CheckCategory": "Unused Hosted Zones",
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

                total_complex = weighted_records + latency_records + geolocation_records
                if total_complex > 0 and record_count < 10:
                    checks["complex_routing_simple_use"].append(
                        {
                            "HostedZoneId": zone_id,
                            "ZoneName": zone_name,
                            "WeightedRecords": weighted_records,
                            "LatencyRecords": latency_records,
                            "GeolocationRecords": geolocation_records,
                            "Recommendation": "Complex routing policies for simple zone - verify necessity",
                            "EstimatedSavings": "Simple routing reduces query costs",
                            "CheckCategory": "Unnecessary Complex Routing",
                        }
                    )

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

                if hc_type in ["HTTP", "HTTPS", "TCP"]:
                    checks["unnecessary_health_checks"].append(
                        {
                            "HealthCheckId": hc_id,
                            "Type": hc_type,
                            "Recommendation": "Health check without routing dependency - verify necessity",
                            "EstimatedSavings": "$0.50/month per health check if removed",
                            "CheckCategory": "Unnecessary Health Checks",
                        }
                    )

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
                checks["duplicate_private_zones"].append(
                    {
                        "ZoneName": zone_name,
                        "ZoneCount": len(zone_ids),
                        "ZoneIds": zone_ids,
                        "Recommendation": "Multiple private zones with same name - check VPC associations",
                        "EstimatedSavings": f"${(len(zone_ids) - 1) * 0.50:.2f}/month if consolidated",
                        "CheckCategory": "Duplicate Private Zones",
                    }
                )

    except Exception as e:
        ctx.warn(f"Could not perform Route 53 checks: {e}", "route53")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
