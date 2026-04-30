"""Flat-rate adapter for Lightsail."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.lightsail import LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS, get_enhanced_lightsail_checks


class LightsailModule(BaseServiceModule):
    key: str = "lightsail"
    cli_aliases: tuple[str, ...] = ("lightsail",)
    display_name: str = "Lightsail"

    def required_clients(self) -> tuple[str, ...]:
        return ("lightsail",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/lightsail.py] Lightsail module active")
        result = get_enhanced_lightsail_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 12 * len(recs)

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}

        return ServiceFindings(
            service_name="Lightsail",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS,
        )
