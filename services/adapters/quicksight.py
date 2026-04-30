"""Flat-rate adapter for QuickSight."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.quicksight import QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS, get_enhanced_quicksight_checks


class QuicksightModule(BaseServiceModule):
    key: str = "quicksight"
    cli_aliases: tuple[str, ...] = ("quicksight",)
    display_name: str = "QuickSight"

    def required_clients(self) -> tuple[str, ...]:
        return ("quicksight",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/quicksight.py] QuickSight module active")
        result = get_enhanced_quicksight_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 30 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="QuickSight",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS,
        )
