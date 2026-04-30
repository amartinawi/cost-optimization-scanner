"""Flat-rate adapter for Transfer Family."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.transfer_svc import TRANSFER_OPTIMIZATION_DESCRIPTIONS, get_enhanced_transfer_checks


class TransferModule(BaseServiceModule):
    key: str = "transfer"
    cli_aliases: tuple[str, ...] = ("transfer",)
    display_name: str = "Transfer Family"

    def required_clients(self) -> tuple[str, ...]:
        return ("transfer",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/transfer.py] Transfer Family module active")
        result = get_enhanced_transfer_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 40 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Transfer Family",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=TRANSFER_OPTIMIZATION_DESCRIPTIONS,
        )
