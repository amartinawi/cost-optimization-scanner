"""Transfer Family cost optimization checks.

Extracted from CostOptimizer.get_enhanced_transfer_checks() as a free function.
This module will later become TransferModule (T-XXX) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

TRANSFER_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "idle_servers": {
        "title": "Stop Idle Transfer Family Servers",
        "description": (
            "An ONLINE server bills every enabled protocol at $0.30/hour ($219/month each)"
            " whether or not any client connects."
        ),
        "action": "Stop servers with no client connections (reversible - the server can be started again)",
    },
    "protocol_optimization": {
        "title": "Optimize Transfer Family Protocols",
        "description": "Protocol costs vary by region and endpoint type. Review if all protocols are needed.",
        "action": "Remove unused protocols and check AWS Pricing Calculator for region-specific costs",
    }
}


# A server nobody has connected to in this many days is idle. The window doubles
# as the data-transfer note's window so only one CloudWatch read is made.
_IDLE_WINDOW_DAYS = 30


def _server_protocols(ctx: ScanContext, server_id: str | None) -> list[str]:
    """Protocols enabled on a server, or ``[]`` when unreadable.

    ``ListServers`` returns ``ListedServer``, which has no ``Protocols`` member;
    only ``DescribeServer`` carries it. Returning ``[]`` on failure means the
    caller emits no priced rec, which is the safe direction: the per-protocol
    hourly charge cannot be computed without the protocol count.
    """
    if not server_id:
        return []
    try:
        described = ctx.client("transfer").describe_server(ServerId=server_id)
        protocols = (described.get("Server") or {}).get("Protocols") or []
        return [str(p) for p in protocols]
    except Exception as exc:
        record_aws_error(
            ctx,
            exc,
            service="transfer",
            context=f"transfer:DescribeServer failed for {server_id}",
        )
        return []


def get_enhanced_transfer_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Transfer Family cost optimization checks."""
    checks: dict[str, list[dict[str, Any]]] = {
        "unused_servers": [],
        "protocol_optimization": [],
        "idle_servers": [],
    }

    try:
        paginator = ctx.client("transfer").get_paginator("list_servers")

        for page in paginator.paginate():
            servers = page.get("Servers", [])

            for server in servers:
                server_id = server.get("ServerId")
                state = server.get("State")
                # TR-3 — ListedServer has NO Protocols member (verified against
                # the botocore transfer model): every rec here read `[]`, so the
                # `len(protocols) > 1` protocol lever below could never fire on
                # a real payload, and the data-transfer note it carried never
                # rendered either. Protocols live on DescribedServer. Only an
                # ONLINE server is billing protocol hours, so the extra describe
                # is spent only where a dollar can follow from it.
                protocols = _server_protocols(ctx, server_id) if state == "ONLINE" else []

                rec: dict[str, Any] = {
                    "ServerId": server_id,
                    "Protocols": protocols,
                    "Region": ctx.region,
                    "CheckCategory": "Protocol Optimization",
                }

                if state == "ONLINE" and len(protocols) > 1:
                    # Surface the consolidation candidate, but do NOT bake a
                    # fabricated `(len(protocols) - 1) × $0.30 × 730` dollar into
                    # the rec: removing a protocol only saves money when that
                    # protocol is actually unused, and the shim has no
                    # per-protocol usage signal to prove it. The adapter demotes
                    # this to a $0 advisory unless per-protocol usage evidence is
                    # supplied (transfer H2).
                    rec["Recommendation"] = (
                        f"Review if all {len(protocols)} protocols are needed - each protocol has hourly charges"
                    )
                    rec["EstimatedSavings"] = (
                        "$0.00/month — advisory: confirm per-protocol usage before removing any protocol"
                    )
                    rec["Note"] = (
                        f"Protocol costs vary by region ({ctx.region}) and type."
                        " Verify actual pricing in AWS Pricing Calculator before making changes."
                    )
                    checks["protocol_optimization"].append(rec)

                if not ctx.fast_mode:
                    try:
                        cw = ctx.client("cloudwatch")
                        end = datetime.now(UTC)
                        start = end - timedelta(days=_IDLE_WINDOW_DAYS)
                        uploaded = downloaded = 0.0
                        # TR-1 — the SUM is not the idle signal; the PRESENCE of
                        # datapoints is. AWS documents these metrics as "emitted
                        # every 5 minutes WHILE A CONNECTION IS ESTABLISHED... if
                        # no files or bytes are transferred in the period, '0' is
                        # emitted". So a series of zeros means somebody connected
                        # and moved nothing, while an EMPTY series means nobody
                        # connected at all. A naive `sum == 0` conflates the two
                        # and would flag an actively-used server as idle.
                        datapoints = 0
                        # TR-2 — AWS/Transfer publishes BytesIn / BytesOut
                        # (dimension ServerId), NOT BytesUploaded /
                        # BytesDownloaded: those names match no metric, so
                        # GetMetricStatistics returned empty datapoints with no
                        # error and this note never populated. Worse, any idle
                        # gate built on this helper would have read "no traffic"
                        # for every server — a fail-OPEN. Per the AWS docs the
                        # metrics emit every 5 min *while a connection is
                        # established* (0 when idle within a connection), so an
                        # empty series means no connections at all in the window.
                        for metric_name in ("BytesIn", "BytesOut"):
                            pts = cw.get_metric_statistics(
                                Namespace="AWS/Transfer",
                                MetricName=metric_name,
                                Dimensions=[{"Name": "ServerId", "Value": server_id}],
                                StartTime=start,
                                EndTime=end,
                                Period=86400 * _IDLE_WINDOW_DAYS,
                                Statistics=["Sum"],
                            )
                            for dp in pts.get("Datapoints", []):
                                datapoints += 1
                                if metric_name == "BytesIn":
                                    uploaded += dp.get("Sum", 0)
                                else:
                                    downloaded += dp.get("Sum", 0)
                        if state == "ONLINE" and datapoints == 0 and protocols:
                            checks["idle_servers"].append(
                                {
                                    "ServerId": server_id,
                                    "State": state,
                                    "Protocols": list(protocols),
                                    "ProtocolCount": len(protocols),
                                    "MetricWindowDays": _IDLE_WINDOW_DAYS,
                                    "ConnectionDatapoints": 0,
                                    "IdleEvidence": True,
                                    "Recommendation": (
                                        "No client connected in the last "
                                        f"{_IDLE_WINDOW_DAYS} days - stop the server "
                                        "(reversible) to end its per-protocol hourly charge"
                                    ),
                                    "CheckCategory": "Idle Transfer Servers",
                                }
                            )

                        upload_gb = uploaded / (1024**3)
                        download_gb = downloaded / (1024**3)
                        total_gb = upload_gb + download_gb
                        if total_gb > 0:
                            rec["DataTransferCostGB"] = round(total_gb, 2)
                            rec["DataTransferCostNote"] = (
                                f"~${upload_gb * 0.04:.2f} upload + ${download_gb * 0.04:.2f} download"
                                f" ({_IDLE_WINDOW_DAYS}-day; Transfer Family $0.04/GB each way)"
                            )
                    except Exception as cw_err:
                        # Classify: an AccessDenied/throttle on the CW read is a
                        # permission gap, not "no traffic" (E1). The per-rec note
                        # stays for the empty-datapoints case, which is normal.
                        record_aws_error(
                            ctx,
                            cw_err,
                            service="transfer",
                            context=f"cloudwatch:GetMetricStatistics BytesIn/BytesOut for {server_id}",
                        )
                        rec["DataTransferCostNote"] = (
                            "CloudWatch unavailable — consider monitoring"
                            " BytesIn/BytesOut for S3 transfer cost"
                            " ($0.04/GB upload + $0.04/GB download — Transfer Family fee) awareness"
                        )

                if state in ["STOPPED", "OFFLINE"]:
                    # A stopped/offline server is not billing endpoint hours, so
                    # the saving from terminating it is $0 until/unless billing is
                    # independently evidenced. The adapter marks this advisory and
                    # never layers a protocol-removal figure onto it (transfer H1).
                    checks["unused_servers"].append(
                        {
                            "ServerId": server_id,
                            "State": state,
                            "Protocols": protocols,
                            "Recommendation": f"Server is {state.lower()} - terminate if no longer needed",
                            "EstimatedSavings": (
                                "$0.00/month — advisory: stopped/offline server is not "
                                "billing endpoint hours; termination saving not evidenced"
                            ),
                            "CheckCategory": "Unused Transfer Servers",
                        }
                    )

    except Exception as e:
        ctx.warn(f"Could not analyze Transfer Family resources: {e}", "transfer")

    all_recommendations: list[dict[str, Any]] = []
    for category_recs in checks.values():
        all_recommendations.extend(category_recs)

    return {"recommendations": all_recommendations, "checks": checks}
