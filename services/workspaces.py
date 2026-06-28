"""WorkSpaces cost optimization checks.

Extracted from CostOptimizer.get_enhanced_workspaces_checks() as a free function.
This module will later become WorkspacesModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

WORKSPACE_BUNDLE_MAP: dict[str, str] = {
    "VALUE": "1",
    "STANDARD": "2",
    "PERFORMANCE": "3",
    "POWER": "8",
    "POWERPRO": "19",
    "GRAPHICS": "4",
    "GRAPHICSPRO": "5",
}

# AWS WorkSpaces AlwaysOn monthly prices (us-east-1, Windows-licensed bundle),
# validated against the live AWS Pricing API (AmazonWorkSpaces, runningMode
# "AlwaysOn", resourceType "Hardware", license "Included") on 2026-06-27.
# Region-scaled via pricing_multiplier at the per-rec emit site.
# Used as the authoritative price source because AWS WorkSpaces Pricing API
# uses a `bundle` filter (not `instanceType`), which the generic PricingEngine
# instance lookup cannot reach.
#   VALUE            SKU MUPRA7BF29NZWQ8T   $25/mo
#   STANDARD         SKU 8QF9FB4JU4AVYMA6   $35/mo
#   PERFORMANCE      SKU RR5JMWYJX3UNMRUW   $50/mo
#   POWER            SKU 35FRPJ8PTT25VTPY   $78/mo
#   POWERPRO         SKU 9M7TNDXEHK432522   $140/mo
#   GRAPHICS_G4DN    SKU U3PA7649DE3EYP3D   $537/mo
#   GRAPHICSPRO      SKU 4GQGYJZHP5W278AE   $999/mo
#   GRAPHICSPRO_G4DN SKU SAXMZ7UAX736ARQ5   $959/mo
# GRAPHICS (the legacy non-g4dn GPU bundle) is retired and no longer live-priceable;
# its $350 figure is the last-published list price — documented, not verified.
WORKSPACE_BUNDLE_MONTHLY: dict[str, float] = {
    "VALUE": 25.0,
    "STANDARD": 35.0,
    "PERFORMANCE": 50.0,
    "POWER": 78.0,
    "POWERPRO": 140.0,
    "GRAPHICS": 350.0,
    "GRAPHICS_G4DN": 537.0,
    "GRAPHICSPRO": 999.0,
    "GRAPHICSPRO_G4DN": 959.0,
}

# AWS WorkSpaces AutoStop pricing per bundle: (fixed_monthly_fee, hourly_rate),
# us-east-1 Windows-licensed, validated against the live AWS Pricing API
# (runningMode "AutoStop"): the "-AutoStop-User" Month SKU is the fixed monthly
# fee that covers the root+user volumes, the "-AutoStop-Usage" hour SKU is the
# running rate. Validated 2026-06-27. AutoStop monthly cost =
# fee + hourly x running_hours, so the AlwaysOn->AutoStop saving depends on
# measured session hours and can be wrong-signed for heavy users (break-even at
# ~ (always_on - fee) / hourly running hours per month).
#   VALUE       fee $7.25  (JNGF6QJ7S8NGRDG7)  hourly $0.22  (N8H42DT5ZWBZY36X)
#   STANDARD    fee $9.75  (KJ39V6HYK37HHHBQ)  hourly $0.30  (A7CA9Z8E4QB96DZW)
#   PERFORMANCE fee $13.00 (389ZYFSTCG96AWRZ)  hourly $0.47  (ZZ9URENHNP5Z2387)
#   POWER       fee $19.00 (YE55FXMUCR7VAU2K)  hourly $0.68  (XMVTCHAM8XRX95HZ)
#   POWERPRO    fee $19.00 (5VDV3YE9GPCNV2DM)  hourly $1.53  (SY6C2E3MC768M3HA)
#   GRAPHICSPRO fee $66.00 (APF9TQ4CVK3FT3GP)  hourly $11.62 (MAVK7TYFJ3BTK4MV)
# Bundles without an entry (retired GRAPHICS, g4dn graphics) fall back to a $0
# advisory rather than a fabricated AutoStop saving.
WORKSPACE_AUTOSTOP_PRICING: dict[str, tuple[float, float]] = {
    "VALUE": (7.25, 0.22),
    "STANDARD": (9.75, 0.30),
    "PERFORMANCE": (13.0, 0.47),
    "POWER": (19.0, 0.68),
    "POWERPRO": (19.0, 1.53),
    "GRAPHICSPRO": (66.0, 11.62),
}

# Provisioned WorkSpaces session-hours are read from CloudWatch over this window
# so the AlwaysOn->AutoStop projection is gated on measured usage (audit C2).
WORKSPACES_SESSION_LOOKBACK_DAYS: int = 30
WORKSPACES_SESSION_METRIC_PERIOD_1H: int = 3600
WORKSPACES_HOURS_PER_MONTH: int = 730

WORKSPACE_BUNDLE_RANK: dict[str, int] = {
    "VALUE": 0,
    "STANDARD": 1,
    "PERFORMANCE": 2,
    "POWER": 3,
    "POWERPRO": 4,
    "GRAPHICS": 5,
    "GRAPHICSPRO": 6,
}

WORKSPACES_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "billing_mode_optimization": {
        "title": "Optimize WorkSpaces Billing Mode",
        "description": "Use AUTO_STOP mode for occasional users instead of ALWAYS_ON.",
        "action": "Switch to AUTO_STOP billing mode",
    },
    "bundle_rightsizing": {
        "title": "Rightsize WorkSpaces Bundle",
        "description": "Downgrade over-provisioned WorkSpaces bundles based on utilization.",
        "action": "Downgrade to a smaller bundle type",
    },
}


def _get_bundle_price_by_compute_type(ctx: ScanContext, compute_type: str) -> float:
    """Return monthly $ price for a WorkSpaces bundle by compute-type name.

    Resolves directly via WORKSPACE_BUNDLE_MONTHLY (AWS-list prices). The
    generic PricingEngine instance lookup is bypassed because AWS WorkSpaces
    Pricing API uses a `bundle` attribute, not `instanceType`, so the
    previous get_instance_monthly_price("AmazonWorkSpaces", bundle_id)
    call structurally returned 0. Region-scaled by ctx.pricing_multiplier.
    """
    base = WORKSPACE_BUNDLE_MONTHLY.get(compute_type.upper(), 0.0)
    return base * ctx.pricing_multiplier


def _read_monthly_connected_hours(cloudwatch: Any, workspace_id: str) -> float | None:
    """Estimate a WorkSpace's monthly user-connected hours from CloudWatch.

    Reads ``AWS/WorkSpaces`` ``UserConnected`` (1 while a user is connected, 0
    otherwise) over the last ``WORKSPACES_SESSION_LOOKBACK_DAYS`` days at hourly
    granularity and counts the hours with a connection (``Maximum >= 1``), then
    scales the window up to a ``WORKSPACES_HOURS_PER_MONTH`` month. Returns
    ``None`` when CloudWatch returns no datapoints (metric unavailable) so the
    adapter falls back to a $0 advisory instead of fabricating a saving; a real
    zero-usage window returns ``0.0``. Raises on API errors so the caller can
    classify permission vs. transient failures.
    """
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(days=WORKSPACES_SESSION_LOOKBACK_DAYS)
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/WorkSpaces",
        MetricName="UserConnected",
        Dimensions=[{"Name": "WorkspaceId", "Value": workspace_id}],
        StartTime=start_time,
        EndTime=end_time,
        Period=WORKSPACES_SESSION_METRIC_PERIOD_1H,
        Statistics=["Maximum"],
    )
    datapoints = resp.get("Datapoints", [])
    if not datapoints:
        return None
    connected_hours = sum(1 for dp in datapoints if dp.get("Maximum", 0) >= 1)
    window_hours = WORKSPACES_SESSION_LOOKBACK_DAYS * 24
    return connected_hours * (WORKSPACES_HOURS_PER_MONTH / window_hours)


def get_enhanced_workspaces_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced WorkSpaces cost optimization checks.

    The AlwaysOn->AutoStop (billing-mode) lever is gated on measured session
    hours: ``AWS/WorkSpaces`` ``UserConnected`` is read per provisioned WorkSpace
    so the adapter can project the AutoStop cost (fee + hourly x hours) rather
    than apply a blind reduction factor. CloudWatch reads are skipped when
    ``ctx.fast_mode`` is set (the lever degrades to a $0 advisory), and any
    CloudWatch failure is classified on ``ctx`` rather than swallowed.
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "billing_mode_optimization": [],
        "bundle_rightsizing": [],
        "unused_workspaces": [],
    }

    fast_mode = bool(getattr(ctx, "fast_mode", False))
    # Emit each cross-cutting CloudWatch notice at most once.
    notices = {"fast_mode": False, "cw_denied": False, "cw_error": False}

    def _note_cw_failure(exc: Exception) -> None:
        msg = str(exc)
        denied = "AccessDenied" in msg or "UnauthorizedOperation" in msg or "OptInRequired" in msg
        if denied and not notices["cw_denied"]:
            notices["cw_denied"] = True
            ctx.permission_issue(
                "CloudWatch metrics denied for WorkSpaces (AlwaysOn-to-AutoStop "
                "analysis degraded to advisory)",
                service="workspaces",
                action="cloudwatch:GetMetricStatistics",
            )
        elif not denied and not notices["cw_error"]:
            notices["cw_error"] = True
            ctx.warn(
                f"CloudWatch metrics unavailable for WorkSpaces ({type(exc).__name__})",
                service="workspaces",
            )

    try:
        workspaces = ctx.client("workspaces")
        try:
            cloudwatch = ctx.client("cloudwatch")
        except Exception as e:  # CloudWatch optional — degrade billing-mode to advisory.
            _note_cw_failure(e)
            cloudwatch = None

        if fast_mode and not notices["fast_mode"]:
            notices["fast_mode"] = True
            ctx.warn(
                "Fast mode: skipped WorkSpaces CloudWatch reads — AlwaysOn-to-AutoStop "
                "savings reported as advisory.",
                service="workspaces",
            )

        paginator = workspaces.get_paginator("describe_workspaces")

        for page in paginator.paginate():
            ws_list = page.get("Workspaces", [])

            for workspace in ws_list:
                workspace_id = workspace.get("WorkspaceId")
                state = workspace.get("State")
                props = workspace.get("WorkspaceProperties", {})
                running_mode = props.get("RunningMode")
                # C3: carry the real bundle so scan() prices the actual ComputeType
                # rather than defaulting every rec to STANDARD.
                compute_type = props.get("ComputeTypeName", "STANDARD")
                # workspaces L1: the bundle price tables are us-east-1
                # Windows+Included rates. A POSITIVELY non-Windows OS or a
                # BRING_YOUR_OWN_LICENSE WorkSpace makes the bundle figure
                # unreliable; absent fields (older API versions) are treated as the
                # priced default so we never downgrade on missing data — we only
                # flag an advisory caveat, never a counted dollar.
                ws_os = str(props.get("OperatingSystemName", "")).upper()
                bundle_license = str(workspace.get("License", "")).upper()
                non_windows_pricing = (
                    (bool(ws_os) and "WINDOWS" not in ws_os)
                    or bundle_license in ("BRING_YOUR_OWN_LICENSE", "BYOL")
                )

                if state == "AVAILABLE" and running_mode == "ALWAYS_ON":
                    # C2: gate the AutoStop projection on measured session hours.
                    measured_hours: float | None = None
                    if cloudwatch is not None and not fast_mode:
                        try:
                            measured_hours = _read_monthly_connected_hours(cloudwatch, workspace_id)
                        except Exception as e:
                            _note_cw_failure(e)
                            measured_hours = None
                    rec: dict[str, Any] = {
                        "WorkspaceId": workspace_id,
                        "CurrentMode": running_mode,
                        "ComputeType": compute_type,
                        "Recommendation": (
                            "Consider AUTO_STOP mode for occasional users - monitor usage patterns first"
                        ),
                        "CheckCategory": "Billing Mode Optimization",
                        "Note": "Verify user login patterns before switching to AUTO_STOP",
                    }
                    if measured_hours is not None:
                        rec["MeasuredMonthlyHours"] = round(float(measured_hours), 1)
                    checks["billing_mode_optimization"].append(rec)

                if state in ["STOPPED", "ERROR", "SUSPENDED"]:
                    checks["unused_workspaces"].append(
                        {
                            "WorkspaceId": workspace_id,
                            "State": state,
                            "RunningMode": running_mode,
                            "ComputeType": compute_type,
                            "Recommendation": f"Workspace in {state} state - terminate if no longer needed",
                            "CheckCategory": "Unused WorkSpaces",
                        }
                    )

                if state == "AVAILABLE":
                    current_rank = WORKSPACE_BUNDLE_RANK.get(compute_type, -1)
                    if current_rank <= 0:
                        continue

                    target_type = None
                    if current_rank >= 4 and ctx.pricing_engine:
                        target_type = "STANDARD"
                    elif current_rank >= 3:
                        target_type = "PERFORMANCE" if current_rank > 2 else None

                    if target_type and target_type != compute_type:
                        # Resolve prices by compute_type name directly; the
                        # numeric bundle IDs (1/2/3/etc.) are kept in
                        # WORKSPACE_BUNDLE_MAP only for legacy adapter use.
                        current_price = _get_bundle_price_by_compute_type(ctx, compute_type)
                        target_price = _get_bundle_price_by_compute_type(ctx, target_type)
                        savings = max(current_price - target_price, 0.0)

                        if savings > 0:
                            # Advisory only: a high bundle tier is NOT evidence the
                            # WorkSpace is over-provisioned, and WorkSpaces publishes
                            # no default CPU/memory utilization metric to CloudWatch
                            # to gate on — so counting the current->target delta (and
                            # claiming "based on utilization profile") fabricated a
                            # saving. Carry the potential figure; the adapter renders
                            # it Counted=False, never summed.
                            rightsizing_rec: dict[str, Any] = {
                                "WorkspaceId": workspace_id,
                                "CurrentBundle": compute_type,
                                "RecommendedBundle": target_type,
                                "Counted": False,
                                "PotentialMonthlySavings": round(savings, 2),
                                "Recommendation": (
                                    f"If {compute_type} is underutilized, downgrading to {target_type} "
                                    f"would save ~${savings:.2f}/mo — verify CPU/memory utilization first "
                                    f"(not measured: WorkSpaces does not publish utilization to CloudWatch "
                                    f"by default)"
                                ),
                                "CheckCategory": "Bundle Rightsizing",
                            }
                            if non_windows_pricing:
                                # The price delta was computed from Windows+Included
                                # tables; for a Linux or BYOL WorkSpace it is
                                # indicative only (workspaces L1).
                                _why = (
                                    f"runs {ws_os.title()}"
                                    if (ws_os and "WINDOWS" not in ws_os)
                                    else "is BRING_YOUR_OWN_LICENSE"
                                )
                                rightsizing_rec["PricingWarning"] = (
                                    f"figure assumes Windows+Included rates; this WorkSpace {_why} "
                                    f"so the real delta differs"
                                )
                            checks["bundle_rightsizing"].append(rightsizing_rec)

    except Exception as e:
        # C1 — classify: a describe_workspaces AccessDenied is a permission gap,
        # not a generic warning; the WorkSpaces tab must not vanish with no signal.
        record_aws_error(ctx, e, service="workspaces", context="workspaces:DescribeWorkspaces failed")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}
