"""Flat-rate adapter for Batch."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.batch_svc import BATCH_OPTIMIZATION_DESCRIPTIONS, get_enhanced_batch_checks


class BatchModule(BaseServiceModule):
    key: str = "batch"
    cli_aliases: tuple[str, ...] = ("batch",)
    display_name: str = "Batch"

    def required_clients(self) -> tuple[str, ...]:
        return ("batch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/batch.py] Batch module active")
        result = get_enhanced_batch_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 150 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Batch",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=BATCH_OPTIMIZATION_DESCRIPTIONS,
        )
