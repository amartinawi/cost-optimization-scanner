"""Flat-rate adapter for Glue."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.glue import GLUE_OPTIMIZATION_DESCRIPTIONS, get_enhanced_glue_checks


class GlueModule(BaseServiceModule):
    key: str = "glue"
    cli_aliases: tuple[str, ...] = ("glue",)
    display_name: str = "Glue"

    def required_clients(self) -> tuple[str, ...]:
        return ("glue",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/glue.py] Glue module active")
        result = get_enhanced_glue_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 100 * len(recs)

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}

        return ServiceFindings(
            service_name="Glue",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=GLUE_OPTIMIZATION_DESCRIPTIONS,
        )
