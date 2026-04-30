"""Flat-rate adapter for App Runner."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.apprunner import APPRUNNER_OPTIMIZATION_DESCRIPTIONS, get_enhanced_apprunner_checks


class AppRunnerModule(BaseServiceModule):
    key: str = "apprunner"
    cli_aliases: tuple[str, ...] = ("apprunner",)
    display_name: str = "App Runner"

    def required_clients(self) -> tuple[str, ...]:
        return ("apprunner",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/apprunner.py] App Runner module active")
        result = get_enhanced_apprunner_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 25 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="App Runner",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=APPRUNNER_OPTIMIZATION_DESCRIPTIONS,
        )
