"""Flat-rate adapter for Redshift."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.redshift import REDSHIFT_OPTIMIZATION_DESCRIPTIONS, get_enhanced_redshift_checks


class RedshiftModule(BaseServiceModule):
    key: str = "redshift"
    cli_aliases: tuple[str, ...] = ("redshift",)
    display_name: str = "Redshift"

    def required_clients(self) -> tuple[str, ...]:
        return ("redshift",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/redshift.py] Redshift module active")
        result = get_enhanced_redshift_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 200 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Redshift",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=REDSHIFT_OPTIMIZATION_DESCRIPTIONS,
        )
