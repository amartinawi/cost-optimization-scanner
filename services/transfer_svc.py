"""Transfer Family cost optimization checks.

Extracted from CostOptimizer.get_enhanced_transfer_checks() as a free function.
This module will later become TransferModule (T-XXX) implementing ServiceModule.
"""

from __future__ import annotations

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
    print("🔍 [services/transfer_svc.py] Transfer module active")
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

                if state == "ONLINE" and len(protocols) > 1:
                    estimated_savings = "Variable – check AWS Pricing Calculator"

                    checks["protocol_optimization"].append(
                        {
                            "ServerId": server_id,
                            "Protocols": protocols,
                            "Region": ctx.region,
                            "Recommendation": (
                                f"Review if all {len(protocols)} protocols are needed"
                                " - each protocol has hourly charges"
                            ),
                            "EstimatedSavings": estimated_savings,
                            "CheckCategory": "Protocol Optimization",
                            "Note": (
                                f"Protocol costs vary by region ({ctx.region}) and type."
                                " Verify actual pricing in AWS Pricing Calculator before making changes."
                            ),
                        }
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
