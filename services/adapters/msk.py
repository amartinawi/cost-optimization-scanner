"""Flat-rate adapter for MSK."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.msk import MSK_OPTIMIZATION_DESCRIPTIONS, get_enhanced_msk_checks


class MskModule(BaseServiceModule):
    key: str = "msk"
    cli_aliases: tuple[str, ...] = ("msk",)
    display_name: str = "MSK"

    def required_clients(self) -> tuple[str, ...]:
        return ("kafka",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/msk.py] MSK module active")
        result = get_enhanced_msk_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 150 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="MSK",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=MSK_OPTIMIZATION_DESCRIPTIONS,
        )
