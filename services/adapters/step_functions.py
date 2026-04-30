"""Keyword-rate adapter for Step Functions."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.step_functions import STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS, get_enhanced_step_functions_checks


class StepFunctionsModule(BaseServiceModule):
    key: str = "step_functions"
    cli_aliases: tuple[str, ...] = ("step_functions",)
    display_name: str = "Step Functions"

    def required_clients(self) -> tuple[str, ...]:
        return ("states",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/step_functions.py] Step Functions module active")
        result = get_enhanced_step_functions_checks(ctx)
        recs = result.get("recommendations", [])

        savings = 0.0
        for rec in recs:
            est = rec.get("EstimatedSavings", "")
            if "Up to 90%" in est:
                savings += 200
            elif "65-75%" in est:
                savings += 150

        return ServiceFindings(
            service_name="Step Functions",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS,
        )
