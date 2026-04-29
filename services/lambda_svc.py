"""Lambda cost optimization checks.

Extracted from CostOptimizer.get_enhanced_lambda_checks() as a free function.
This module will later become LambdaModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

print("\U0001f50d [services/lambda_svc.py] Lambda module active")

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


def get_enhanced_lambda_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Lambda cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "excessive_memory": [],
        "low_invocation": [],
        "provisioned_concurrency": [],
        "vpc_without_need": [],
        "high_reserved_concurrency": [],
        "arm_migration": [],
    }

    try:
        lambda_client = ctx.client("lambda")
        cloudwatch = ctx.client("cloudwatch")

        paginator = lambda_client.get_paginator("list_functions")

        for page in paginator.paginate():
            for function in page["Functions"]:
                function_name = function["FunctionName"]
                _function_arn = function["FunctionArn"]
                memory_size = function["MemorySize"]
                _timeout = function["Timeout"]
                runtime = function.get("Runtime", "Unknown")
                architectures = function.get("Architectures", ["x86_64"])

                try:
                    config = lambda_client.get_function_configuration(FunctionName=function_name)
                    vpc_config = config.get("VpcConfig", {})
                    reserved_concurrency = config.get("ReservedConcurrentExecutions")
                except Exception as e:
                    print(f"\u26a0\ufe0f Error getting Lambda function config for {function_name}: {str(e)}")
                    vpc_config = {}
                    reserved_concurrency = None

                if memory_size >= EXCESSIVE_MEMORY_THRESHOLD:
                    checks["excessive_memory"].append(
                        {
                            "FunctionName": function_name,
                            "MemorySize": memory_size,
                            "Runtime": runtime,
                            "Recommendation": f"{memory_size}MB memory may be excessive - rightsize for cost savings",
                            "EstimatedSavings": "30-50% with rightsizing",
                            "CheckCategory": "Lambda Excessive Memory",
                        }
                    )

                try:
                    end_time = datetime.now(UTC)
                    start_time = end_time - timedelta(days=30)
                    metrics = cloudwatch.get_metric_statistics(
                        Namespace="AWS/Lambda",
                        MetricName="Invocations",
                        Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=INVOCATION_METRIC_PERIOD_30D,
                        Statistics=["Sum"],
                    )
                    invocations = metrics["Datapoints"][0]["Sum"] if metrics["Datapoints"] else 0
                    if invocations < LOW_INVOCATION_30DAY_THRESHOLD:
                        checks["low_invocation"].append(
                            {
                                "FunctionName": function_name,
                                "MemorySize": memory_size,
                                "Runtime": runtime,
                                "Invocations30Days": int(invocations),
                                "Recommendation": "Low usage - consider consolidation or deletion",
                                "EstimatedSavings": "Eliminate unused costs",
                                "CheckCategory": "Lambda Low Invocation",
                            }
                        )
                except Exception as e:
                    print(f"Warning: Could not get metrics for function {function_name}: {e}")
                    continue

                try:
                    provisioned = lambda_client.list_provisioned_concurrency_configs(FunctionName=function_name)
                    if provisioned["ProvisionedConcurrencyConfigs"]:
                        for pc_config in provisioned["ProvisionedConcurrencyConfigs"]:
                            checks["provisioned_concurrency"].append(
                                {
                                    "FunctionName": function_name,
                                    "MemorySize": memory_size,
                                    "Runtime": runtime,
                                    "ProvisionedConcurrency": pc_config["AllocatedProvisionedConcurrentExecutions"],
                                    "Recommendation": "Provisioned concurrency is expensive - review necessity",
                                    "EstimatedSavings": "Up to 90% if not needed",
                                    "CheckCategory": "Lambda Provisioned Concurrency",
                                }
                            )
                except Exception as e:
                    print(f"Warning: Could not check provisioned concurrency for {function_name}: {e}")
                    continue

                if vpc_config and vpc_config.get("SubnetIds"):
                    checks["vpc_without_need"].append(
                        {
                            "FunctionName": function_name,
                            "MemorySize": memory_size,
                            "Runtime": runtime,
                            "VpcId": vpc_config.get("VpcId", "N/A"),
                            "Recommendation": "VPC adds ENI costs and cold start latency - remove if not needed",
                            "EstimatedSavings": "Reduce ENI costs and improve performance",
                            "CheckCategory": "Lambda VPC Configuration",
                        }
                    )

                if reserved_concurrency and reserved_concurrency > HIGH_RESERVED_CONCURRENCY_THRESHOLD:
                    checks["high_reserved_concurrency"].append(
                        {
                            "FunctionName": function_name,
                            "MemorySize": memory_size,
                            "Runtime": runtime,
                            "ReservedConcurrency": reserved_concurrency,
                            "Recommendation": f"{reserved_concurrency} reserved concurrency may be excessive",
                            "EstimatedSavings": "Review actual concurrency needs",
                            "CheckCategory": "Lambda Reserved Concurrency",
                        }
                    )

                if "x86_64" in architectures and runtime in ARM_SUPPORTED_RUNTIMES:
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
                    except Exception:
                        pass

    except Exception as e:
        ctx.warn(f"Lambda checks failed: {e}", "lambda")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}
