"""Lambda cost optimization checks.

Extracted from CostOptimizer.get_enhanced_lambda_checks() as a free function.
This module will later become LambdaModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

LAMBDA_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "excessive_memory": {
        "title": "Rightsize Lambda Memory",
        "description": "Functions with high memory allocation may be over-provisioned.",
        "action": "Analyze actual memory usage and rightsize for cost savings",
    },
    "low_invocation": {
        "title": "Consolidate or Delete Low-Usage Functions",
        "description": "Functions with very few invocations may be candidates for removal.",
        "action": "Consider consolidating or deleting unused functions",
    },
    "provisioned_concurrency": {
        "title": "Review Provisioned Concurrency",
        "description": "Provisioned concurrency is expensive and should be reviewed.",
        "action": "Remove provisioned concurrency for non-critical functions",
    },
    "vpc_without_need": {
        "title": "Remove Unnecessary VPC Configuration",
        "description": "VPC-configured functions incur ENI costs and cold start latency.",
        "action": "Remove VPC configuration if not accessing VPC resources",
    },
    "high_reserved_concurrency": {
        "title": "Review Reserved Concurrency",
        "description": "Excessively high reserved concurrency limits other functions.",
        "action": "Rightsize reserved concurrency to actual needs",
    },
    "arm_migration": {
        "title": "Migrate to ARM/Graviton",
        "description": "ARM architecture provides better price-performance for supported runtimes.",
        "action": "Migrate active x86_64 functions to arm64 for 20% cost savings",
    },
}

ARM_SUPPORTED_RUNTIMES: tuple[str, ...] = (
    "python3.8",
    "python3.9",
    "python3.10",
    "python3.11",
    "python3.12",
    "nodejs18.x",
    "nodejs20.x",
    "java11",
    "java17",
    "java21",
    "dotnet6",
    "dotnet8",
)

EXCESSIVE_MEMORY_THRESHOLD: int = 3008

LOW_INVOCATION_30DAY_THRESHOLD: float = 100

ARM_MIN_WEEKLY_INVOCATIONS: float = 10

HIGH_RESERVED_CONCURRENCY_THRESHOLD: int = 100

INVOCATION_METRIC_PERIOD_30D: int = 2592000

INVOCATION_METRIC_PERIOD_1D: int = 86400

# Provisioned Concurrency utilization is read over this window so the adapter
# can size the saving to the *unused* fraction of allocated concurrency rather
# than assuming a flat haircut. Daily granularity keeps the GetMetricStatistics
# call cheap while still capturing the peak.
PC_UTIL_LOOKBACK_DAYS: int = 14

PC_UTIL_METRIC_PERIOD_1D: int = 86400


def _read_pc_max_utilization(cloudwatch: Any, function_name: str) -> float | None:
    """Return the peak ProvisionedConcurrencyUtilization (0..1) for a function.

    Reads the ``AWS/Lambda`` ``ProvisionedConcurrencyUtilization`` metric over
    the last ``PC_UTIL_LOOKBACK_DAYS`` days and returns the maximum datapoint.
    Returns ``None`` when CloudWatch has no datapoints (idle window or the
    metric is dimensioned by alias/version only) so the adapter can fall back to
    a $0 advisory instead of fabricating a saving. Raises on API errors so the
    caller can classify permission vs. transient failures.
    """
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(days=PC_UTIL_LOOKBACK_DAYS)
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName="ProvisionedConcurrencyUtilization",
        Dimensions=[{"Name": "FunctionName", "Value": function_name}],
        StartTime=start_time,
        EndTime=end_time,
        Period=PC_UTIL_METRIC_PERIOD_1D,
        Statistics=["Maximum"],
    )
    datapoints = resp.get("Datapoints", [])
    if not datapoints:
        return None
    return max(dp["Maximum"] for dp in datapoints)


def get_enhanced_lambda_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Lambda cost optimization checks.

    CloudWatch reads (ARM-migration invocations, Provisioned Concurrency
    utilization) are skipped when ``ctx.fast_mode`` is set — the ARM nudge is
    $0 advisory either way, and PC savings fall back to a $0 advisory without a
    utilization metric. Permission/transient CloudWatch and config failures are
    recorded on ``ctx`` (permission_issue / warn) rather than swallowed.
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "excessive_memory": [],
        "low_invocation": [],
        "provisioned_concurrency": [],
        "vpc_without_need": [],
        "high_reserved_concurrency": [],
        "arm_migration": [],
    }

    fast_mode = bool(getattr(ctx, "fast_mode", False))
    # Emit each cross-cutting warning at most once so a many-function account
    # does not flood the report with identical per-function messages.
    notices = {"fast_mode": False, "cw_denied": False, "cw_error": False, "config_error": False}

    def _note_cw_failure(exc: Exception) -> None:
        msg = str(exc)
        denied = "AccessDenied" in msg or "UnauthorizedOperation" in msg
        if denied and not notices["cw_denied"]:
            notices["cw_denied"] = True
            ctx.permission_issue(
                "CloudWatch metrics denied for Lambda (ARM / Provisioned Concurrency "
                "analysis degraded to advisory)",
                service="lambda",
                action="cloudwatch:GetMetricStatistics",
            )
        elif not denied and not notices["cw_error"]:
            notices["cw_error"] = True
            ctx.warn(f"CloudWatch metrics unavailable for Lambda ({type(exc).__name__})", service="lambda")

    def _note_config_failure(exc: Exception, what: str) -> None:
        if not notices["config_error"]:
            notices["config_error"] = True
            ctx.warn(
                f"Could not read Lambda {what} ({type(exc).__name__}); some checks degraded.",
                service="lambda",
            )

    try:
        lambda_client = ctx.client("lambda")
        cloudwatch = ctx.client("cloudwatch")

        if fast_mode and not notices["fast_mode"]:
            notices["fast_mode"] = True
            ctx.warn(
                "Fast mode: skipped Lambda CloudWatch reads — ARM-migration and "
                "Provisioned Concurrency savings reported as advisory.",
                service="lambda",
            )

        paginator = lambda_client.get_paginator("list_functions")

        for page in paginator.paginate():
            for function in page["Functions"]:
                function_name = function["FunctionName"]
                _function_arn = function["FunctionArn"]
                memory_size = function["MemorySize"]
                _timeout = function["Timeout"]
                runtime = function.get("Runtime", "Unknown")
                architectures = function.get("Architectures", ["x86_64"])
                architecture = "arm64" if "arm64" in architectures else "x86_64"

                try:
                    config = lambda_client.get_function_configuration(FunctionName=function_name)
                    vpc_config = config.get("VpcConfig", {})
                    reserved_concurrency = config.get("ReservedConcurrentExecutions")
                except Exception as e:
                    _note_config_failure(e, "function configuration")
                    vpc_config = {}
                    reserved_concurrency = None

                if memory_size >= EXCESSIVE_MEMORY_THRESHOLD:
                    checks["excessive_memory"].append(
                        {
                            "FunctionName": function_name,
                            "MemorySize": memory_size,
                            "Runtime": runtime,
                            "Architecture": architecture,
                            "Recommendation": f"{memory_size}MB memory may be excessive - rightsize for cost savings",
                            "EstimatedSavings": "30-50% with rightsizing",
                            "CheckCategory": "Lambda Excessive Memory",
                        }
                    )

                # Lambda Low Invocation finding removed: Lambda has no idle cost — only
                # invocations cost money — so "low invocation" functions already incur
                # ~$0 and there is nothing to save by deleting them.

                try:
                    provisioned = lambda_client.list_provisioned_concurrency_configs(FunctionName=function_name)
                    pc_configs = provisioned.get("ProvisionedConcurrencyConfigs", [])
                except Exception as e:
                    # A PC-config read failure must NOT skip the ARM check below;
                    # record once and treat this function as having no PC.
                    _note_config_failure(e, "provisioned concurrency")
                    pc_configs = []

                if pc_configs:
                    # One utilization read per function covers all of its PC
                    # configs (the metric is keyed by FunctionName). Skipped in
                    # fast mode; on no-data / error the adapter emits a $0 advisory.
                    max_util: float | None = None
                    if not fast_mode:
                        try:
                            max_util = _read_pc_max_utilization(cloudwatch, function_name)
                        except Exception as e:
                            _note_cw_failure(e)
                            max_util = None
                    for pc_config in pc_configs:
                        pc_rec: dict[str, Any] = {
                            "FunctionName": function_name,
                            "MemorySize": memory_size,
                            "Runtime": runtime,
                            "Architecture": architecture,
                            "ProvisionedConcurrency": pc_config["AllocatedProvisionedConcurrentExecutions"],
                            "Recommendation": "Provisioned concurrency is expensive - review necessity",
                            "EstimatedSavings": "Up to 90% if not needed",
                            "CheckCategory": "Lambda Provisioned Concurrency",
                        }
                        if max_util is not None:
                            pc_rec["MaxUtilization"] = round(float(max_util), 4)
                        checks["provisioned_concurrency"].append(pc_rec)

                # Lambda VPC configuration finding removed: mixed cost/performance ("improve
                # performance"); ENI savings exist but are not quantified per-function.
                # Lambda Reserved Concurrency finding removed: "Review actual concurrency
                # needs" — reserved concurrency itself has no cost (unlike provisioned).
                _ = (vpc_config, reserved_concurrency)

                if not fast_mode and "x86_64" in architectures and runtime in ARM_SUPPORTED_RUNTIMES:
                    try:
                        end_time = datetime.now(UTC)
                        start_time = end_time - timedelta(days=7)

                        invocation_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/Lambda",
                            MetricName="Invocations",
                            Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=INVOCATION_METRIC_PERIOD_1D,
                            Statistics=["Sum"],
                        )

                        total_invocations = sum(dp["Sum"] for dp in invocation_metrics.get("Datapoints", []))

                        if total_invocations > ARM_MIN_WEEKLY_INVOCATIONS:
                            checks["arm_migration"].append(
                                {
                                    "FunctionName": function_name,
                                    "MemorySize": memory_size,
                                    "Runtime": runtime,
                                    "Architecture": architecture,
                                    "CurrentArchitecture": "x86_64",
                                    "WeeklyInvocations": f"{total_invocations:.0f}",
                                    "Recommendation": (
                                        f"Active function ({total_invocations:.0f} invocations/week)"
                                        " - migrate to ARM/Graviton for better price-performance"
                                    ),
                                    "EstimatedSavings": "20% cost reduction with ARM architecture",
                                    "CheckCategory": "Lambda ARM Migration",
                                }
                            )
                    except Exception as e:
                        _note_cw_failure(e)

    except Exception as e:
        ctx.warn(f"Lambda checks failed: {e}", "lambda")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}
