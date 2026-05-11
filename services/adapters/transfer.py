"""Protocol-based pricing adapter for Transfer Family."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.transfer_svc import TRANSFER_OPTIMIZATION_DESCRIPTIONS, get_enhanced_transfer_checks


class TransferModule(BaseServiceModule):
    """ServiceModule adapter for Transfer Family. Protocol-based pricing strategy."""

    key: str = "transfer"
    cli_aliases: tuple[str, ...] = ("transfer",)
    display_name: str = "Transfer Family"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Transfer Family scanning."""
        return ("transfer",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Transfer Family servers for cost optimization opportunities.

        Consults enhanced Transfer Family checks. Savings calculated via
        protocol-based pricing ($0.30/protocol/hour per endpoint).

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        print("\U0001f50d [services/adapters/transfer.py] Transfer Family module active")
        result = get_enhanced_transfer_checks(ctx)
        recs = result.get("recommendations", [])

        TRANSFER_PER_PROTOCOL_HOUR = 0.30

        savings = 0.0
        for rec in recs:
            protocols = rec.get("Protocols", ["SFTP"])
            num_protocols = len(protocols) if isinstance(protocols, list) else 1
            removable = max(0, num_protocols - 1)
            endpoint_monthly = removable * TRANSFER_PER_PROTOCOL_HOUR * 730 * ctx.pricing_multiplier
            savings += endpoint_monthly

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Transfer Family",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=TRANSFER_OPTIMIZATION_DESCRIPTIONS,
        )
