"""CloudWatch state-transition pricing adapter for Step Functions."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.step_functions import STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS, get_enhanced_step_functions_checks

STEP_FUNCTIONS_IDLE_MONTHLY_FALLBACK: float = 150.0


class StepFunctionsModule(BaseServiceModule):
    """ServiceModule adapter for Step Functions. CloudWatch state-transition savings strategy."""

    key: str = "step_functions"
    cli_aliases: tuple[str, ...] = ("step_functions",)
    display_name: str = "Step Functions"
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Step Functions scanning."""
        return ("states",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Step Functions state machines for cost optimization opportunities.

        Consults enhanced Step Functions checks. Savings calculated via
        CloudWatch ExecutionsStarted metrics with $0.025/1K state transitions
        pricing (Standard→Express migration, 60% cost reduction).

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        print("\U0001f50d [services/adapters/step_functions.py] Step Functions module active")
        result = get_enhanced_step_functions_checks(ctx)
        recs = result.get("recommendations", [])

        STEP_FUNCTIONS_PER_1K_TRANSITIONS = 0.025
        AVG_STATES_PER_EXECUTION = 5

        savings = 0.0
        for rec in recs:
            if not ctx.fast_mode:
                state_machine_arn = rec.get("StateMachineArn", "")
                monthly_executions = rec.get("MonthlyExecutions", 0)

                if monthly_executions <= 0:
                    try:
                        from datetime import datetime, timedelta, timezone

                        cw = ctx.client("cloudwatch")
                        end = datetime.now(timezone.utc)
                        start = end - timedelta(days=30)
                        resp = cw.get_metric_statistics(
                            Namespace="AWS/States",
                            MetricName="ExecutionsStarted",
                            Dimensions=[{"Name": "StateMachineArn", "Value": state_machine_arn}]
                            if state_machine_arn
                            else [],
                            StartTime=start,
                            EndTime=end,
                            Period=2592000,
                            Statistics=["Sum"],
                        )
                        monthly_executions = sum(dp["Sum"] for dp in resp.get("Datapoints", []))
                    except Exception:
                        monthly_executions = 0

                state_count = rec.get("StateCount", AVG_STATES_PER_EXECUTION)
                avg_duration_sec = rec.get("AvgDurationSec", 0)
                eligible_for_migration = state_count > 25 and avg_duration_sec < 60

                if monthly_executions > 0:
                    monthly_transitions = monthly_executions * AVG_STATES_PER_EXECUTION
                    if eligible_for_migration:
                        savings += (
                            (monthly_transitions / 1000)
                            * STEP_FUNCTIONS_PER_1K_TRANSITIONS
                            * 0.60
                            * ctx.pricing_multiplier
                        )
                else:
                    savings += STEP_FUNCTIONS_IDLE_MONTHLY_FALLBACK * ctx.pricing_multiplier
            else:
                savings += STEP_FUNCTIONS_IDLE_MONTHLY_FALLBACK * ctx.pricing_multiplier

        return ServiceFindings(
            service_name="Step Functions",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS,
        )
