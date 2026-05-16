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
        result = get_enhanced_transfer_checks(ctx)
        recs = result.get("recommendations", [])

        # AWS Transfer Family protocol endpoint rate: $0.30/protocol/hour
        # (us-east-1, verified via AWS pricing page). Region-scaled via
        # pricing_multiplier per L2.3.2.
        TRANSFER_PER_PROTOCOL_HOUR: float = 0.30

        savings = 0.0
        for rec in recs:
            # Shim should explicitly indicate how many protocols can be
            # removed via `RemovableProtocols` field. Fall back to the
            # legacy "len(Protocols) - 1" heuristic only when the explicit
            # field is missing — surface a warning either way.
            explicit_removable = rec.get("RemovableProtocols")
            if isinstance(explicit_removable, int) and explicit_removable >= 0:
                removable = explicit_removable
            else:
                protocols = rec.get("Protocols", [])
                if not isinstance(protocols, list):
                    # Not an endpoint-shaped rec (e.g., connector) — skip.
                    rec.setdefault("EstimatedMonthlySavings", 0.0)
                    continue
                removable = max(0, len(protocols) - 1)
            endpoint_monthly = (
                removable * TRANSFER_PER_PROTOCOL_HOUR * 730 * ctx.pricing_multiplier
            )
            rec["EstimatedMonthlySavings"] = round(endpoint_monthly, 2)
            savings += endpoint_monthly

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Transfer Family",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=TRANSFER_OPTIMIZATION_DESCRIPTIONS,
        )
