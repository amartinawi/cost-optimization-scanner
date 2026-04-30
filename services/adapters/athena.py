"""Flat-rate adapter for Athena."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.athena import ATHENA_OPTIMIZATION_DESCRIPTIONS, get_enhanced_athena_checks


class AthenaModule(BaseServiceModule):
    key: str = "athena"
    cli_aliases: tuple[str, ...] = ("athena",)
    display_name: str = "Athena"

    def required_clients(self) -> tuple[str, ...]:
        return ("athena",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/athena.py] Athena module active")
        result = get_enhanced_athena_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 50 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Athena",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=ATHENA_OPTIMIZATION_DESCRIPTIONS,
        )
