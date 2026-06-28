"""Advisory adapter for Step Functions (Standard->Express delta is unmeasurable $0)."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.step_functions import STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS, get_enhanced_step_functions_checks

# Documented Step Functions rates (us-east-1; not in the AWS Pricing API — these
# are the stable public OnDemand rates). Recorded so the advisory AuditBasis is
# defensible; no counted dollar is derived from them because the Express side of
# the Standard→Express delta (per GB-s = duration × memory consumed) cannot be
# measured at scan time — Step Functions publishes no CloudWatch metric for
# states-per-execution or memory consumed. (step_functions C1)
SF_STANDARD_PER_1K_TRANSITIONS: float = 0.025
SF_EXPRESS_PER_M_REQUESTS: float = 1.0
SF_EXPRESS_PER_GB_SECOND: float = 0.00001667


class StepFunctionsModule(BaseServiceModule):
    """ServiceModule adapter for Step Functions. CloudWatch state-transition savings strategy."""

    key: str = "step_functions"
    cli_aliases: tuple[str, ...] = ("step_functions",)
    display_name: str = "Step Functions"
    reads_fast_mode: bool = True
    # Adapter consults CloudWatch for ExecutionsStarted metric per state machine.
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Step Functions scanning."""
        return ("stepfunctions", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Step Functions state machines for cost optimization opportunities.

        Consults enhanced Step Functions checks. The Standard→Express lever is
        emitted as a **$0 advisory** (``Counted=False``): Express bills per
        request + GB-s (duration × memory consumed), but Step Functions exposes
        no CloudWatch metric for states-per-execution or memory consumed, so the
        delta cannot be quantified from evidence at scan time. The previous
        ``eligible_for_migration`` gate (``state_count > 25 and avg_duration <
        60``) was fed by fields no producer ever set, so the counted block was
        structurally dead and the tab was always $0 — which in turn triggered
        the reporter's flat-$50 fabrication (SR-2). Emitting an honest $0
        advisory removes the fabrication trigger (step_functions C1).

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        result = get_enhanced_step_functions_checks(ctx)
        recs = result.get("recommendations", [])

        for rec in recs:
            # Advisory only: render the high-volume flag without a fabricated $.
            rec["Counted"] = False
            rec["EstimatedMonthlySavings"] = 0.0
            rec["EstimatedSavings"] = (
                "$0.00/month — advisory: Standard→Express migration savings are "
                "execution-shape dependent (Express = $1.0/M requests + "
                "$0.00001667/GB-s vs Standard $0.025/1K transitions) and cannot "
                "be quantified without states-per-execution and memory-consumed "
                "data, which Step Functions does not expose."
            )
            rec["AuditBasis"] = {
                "rate_source": "documented Step Functions OnDemand rates (not in Pricing API)",
                "standard_rate_per_1k_transitions": SF_STANDARD_PER_1K_TRANSITIONS,
                "express_rate_per_m_requests": SF_EXPRESS_PER_M_REQUESTS,
                "express_rate_per_gb_second": SF_EXPRESS_PER_GB_SECOND,
                "unmeasured_inputs": ["states_per_execution", "memory_consumed_gb"],
                "reason": "delta underdetermined — advisory per cost-scope rule",
            }

        # Idle state machines incur NO AWS charges (Step Functions bills only per
        # state transition for Standard, per request+duration for Express), so $0
        # is the only honest tab total until evidence-backed pricing is wired.
        # Count hygiene (mirror mediastore H1 / lambda): advisory ($0 Counted=False)
        # recs render but are excluded from the rec-count headline.
        counted_recs = sum(1 for r in recs if r.get("Counted") is not False)
        return ServiceFindings(
            service_name="Step Functions",
            total_recommendations=counted_recs,
            total_monthly_savings=0.0,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS,
        )
