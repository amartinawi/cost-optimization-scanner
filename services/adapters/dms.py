"""Flat-rate adapter for DMS."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.dms import DMS_OPTIMIZATION_DESCRIPTIONS, get_enhanced_dms_checks


class DmsModule(BaseServiceModule):
    key: str = "dms"
    cli_aliases: tuple[str, ...] = ("dms",)
    display_name: str = "DMS"

    def required_clients(self) -> tuple[str, ...]:
        return ("dms",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/dms.py] DMS module active")
        result = get_enhanced_dms_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 50 * len(recs)

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}

        return ServiceFindings(
            service_name="DMS",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=DMS_OPTIMIZATION_DESCRIPTIONS,
        )
