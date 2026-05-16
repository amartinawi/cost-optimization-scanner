"""Transfer Family cost optimization checks.

Extracted from CostOptimizer.get_enhanced_transfer_checks() as a free function.
This module will later become TransferModule (T-XXX) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.scan_context import ScanContext

TRANSFER_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "protocol_optimization": {
        "title": "Optimize Transfer Family Protocols",
        "description": "Protocol costs vary by region and endpoint type. Review if all protocols are needed.",
        "action": "Remove unused protocols and check AWS Pricing Calculator for region-specific costs",
    }
}


def get_enhanced_transfer_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Transfer Family cost optimization checks."""
    checks: dict[str, list[dict[str, Any]]] = {
        "unused_servers": [],
        "protocol_optimization": [],
        "endpoint_optimization": [],
    }

    try:
        paginator = ctx.client("transfer").get_paginator("list_servers")

        for page in paginator.paginate():
            servers = page.get("Servers", [])

            for server in servers:
                server_id = server.get("ServerId")
                state = server.get("State")
                protocols = server.get("Protocols", [])

                rec: dict[str, Any] = {
                    "ServerId": server_id,
                    "Protocols": protocols,
                    "Region": ctx.region,
                    "CheckCategory": "Protocol Optimization",
                }

                if state == "ONLINE" and len(protocols) > 1:
                    removable = len(protocols) - 1
                    rec["Recommendation"] = (
                        f"Review if all {len(protocols)} protocols are needed - each protocol has hourly charges"
                    )
                    rec["EstimatedSavings"] = f"${removable * 0.30 * 730:.2f}/mo from removing {removable} protocol(s)"
                    rec["Note"] = (
                        f"Protocol costs vary by region ({ctx.region}) and type."
                        " Verify actual pricing in AWS Pricing Calculator before making changes."
                    )
                    checks["protocol_optimization"].append(rec)

                if not ctx.fast_mode:
                    try:
                        cw = ctx.client("cloudwatch")
                        end = datetime.now(timezone.utc)
                        start = end - timedelta(days=14)
                        uploaded = downloaded = 0.0
                        for metric_name in ("BytesUploaded", "BytesDownloaded"):
                            pts = cw.get_metric_statistics(
                                Namespace="AWS/Transfer",
                                MetricName=metric_name,
                                Dimensions=[{"Name": "ServerId", "Value": server_id}],
                                StartTime=start,
                                EndTime=end,
                                Period=86400 * 14,
                                Statistics=["Sum"],
                            )
                            for dp in pts.get("Datapoints", []):
                                if metric_name == "BytesUploaded":
                                    uploaded += dp.get("Sum", 0)
                                else:
                                    downloaded += dp.get("Sum", 0)
                        total_gb = (uploaded + downloaded) / (1024**3)
                        if total_gb > 0:
                            rec["DataTransferCostGB"] = round(total_gb, 2)
                            rec["DataTransferCostNote"] = f"~${total_gb * 0.09:.2f}/mo S3 data transfer"
                    except Exception:
                        rec["DataTransferCostNote"] = (
                            "CloudWatch unavailable — consider monitoring"
                            " BytesUploaded/BytesDownloaded for S3 transfer cost"
                            " (~$0.09/GB) awareness"
                        )

                if state in ["STOPPED", "OFFLINE"]:
                    checks["unused_servers"].append(
                        {
                            "ServerId": server_id,
                            "State": state,
                            "Protocols": protocols,
                            "Recommendation": f"Server is {state.lower()} - terminate if no longer needed",
                            "EstimatedSavings": "Full server hourly costs",
                            "CheckCategory": "Unused Transfer Servers",
                        }
                    )

    except Exception as e:
        ctx.warn(f"Could not analyze Transfer Family resources: {e}", "transfer")

    all_recommendations: list[dict[str, Any]] = []
    for category_recs in checks.values():
        all_recommendations.extend(category_recs)

    return {"recommendations": all_recommendations, "checks": checks}
