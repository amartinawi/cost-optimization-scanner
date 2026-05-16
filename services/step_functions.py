"""Step Functions cost optimization checks.

Extracted from CostOptimizer.get_enhanced_step_functions_checks() as a free function.
This module will later become StepFunctionsModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "standard_vs_express": {
        "title": "Migrate High-Volume Standard Workflows to Express",
        "description": "Express Workflows cost less per execution and per state transition for high-volume workloads.",
        "action": "Migrate Standard state machines with >100 daily executions to Express type",
    },
    "nonprod_24x7": {
        "title": "Schedule Non-Production State Machines",
        "description": "Non-prod state machines running 24/7 incur transition costs during idle hours.",
        "action": "Implement shutdown schedules for dev/test/staging state machines",
    },
}


def get_enhanced_step_functions_checks(ctx: ScanContext) -> dict[str, Any]:
    checks: dict[str, list[dict[str, Any]]] = {
        "standard_vs_express": [],
        "excessive_transitions": [],
        "polling_workflows": [],
        "nonprod_24x7": [],
    }

    try:
        sfn = ctx.client("stepfunctions")
        paginator = sfn.get_paginator("list_state_machines")
        for page in paginator.paginate():
            for sm in page.get("stateMachines", []):
                sm_arn = sm.get("stateMachineArn", "")
                sm_name = sm.get("name", "Unknown")
                sm_type = sm.get("type", "STANDARD")

                if sm_type == "STANDARD":
                    try:
                        end_time = datetime.now(UTC)
                        start_time = end_time - timedelta(days=7)

                        cloudwatch = ctx.client("cloudwatch")
                        execution_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/States",
                            MetricName="ExecutionsStarted",
                            Dimensions=[{"Name": "StateMachineArn", "Value": sm_arn}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=86400,
                            Statistics=["Sum"],
                        )

                        total_executions = sum(dp["Sum"] for dp in execution_metrics.get("Datapoints", []))
                        daily_avg = total_executions / 7 if total_executions > 0 else 0

                        if daily_avg > 100:
                            checks["standard_vs_express"].append(
                                {
                                    "StateMachineArn": sm_arn,
                                    "StateMachineName": sm_name,
                                    "Type": sm_type,
                                    "DailyExecutions": f"{daily_avg:.0f}",
                                    "Recommendation": (
                                        f"High-volume workflow ({daily_avg:.0f} executions/day) - consider Express type"
                                    ),
                                    "EstimatedSavings": "Up to 90% cost reduction for high-volume workflows",
                                    "CheckCategory": "Step Functions Type Optimization",
                                }
                            )
                    except Exception:
                        pass

                # Non-prod 24/7 finding removed: emitted "65-75%" percentage range without
                # concrete per-state-machine cost baseline.

    except Exception as e:
        ctx.warn(f"Could not perform Step Functions checks: {e}", "stepfunctions")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
