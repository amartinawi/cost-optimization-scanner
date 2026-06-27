"""Flat-rate adapter for API Gateway."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.api_gateway import API_GATEWAY_OPTIMIZATION_DESCRIPTIONS, get_enhanced_api_gateway_checks


class ApiGatewayModule(BaseServiceModule):
    """ServiceModule adapter for API Gateway. Keyword-rate savings strategy."""

    key: str = "api_gateway"
    cli_aliases: tuple[str, ...] = ("api_gateway",)
    display_name: str = "API Gateway"
    # Shim correctly honors ctx.fast_mode at services/api_gateway.py:73.
    reads_fast_mode: bool = True
    # Shim queries CW `Count` metric per API.
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for API Gateway scanning."""
        return ("apigateway", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan API Gateway REST/HTTP APIs for cost optimization opportunities.

        Consults the api_gateway service module for REST-to-HTTP migration
        and caching recommendations. Savings calculated from CloudWatch Count
        metrics: (REST $3.50/M - HTTP $1.00/M) × monthly_requests.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "enhanced_checks" SourceBlock entry.
        """
        result = get_enhanced_api_gateway_checks(ctx)
        recs = result.get("recommendations", [])

        # A rec with no quantified saving (e.g. monthly_requests == 0 in fast
        # mode or a throttled CW read) is advisory, not counted — mark it so the
        # card renders under "advisory" rather than "counted" (api_gateway C1
        # label; zero dollar impact now that the SR-2 flat-$50 fabrication is
        # gone, but keeps the counted/advisory split honest).
        for rec in recs:
            if (rec.get("EstimatedMonthlySavings", 0.0) or 0.0) <= 0:
                rec["Counted"] = False

        savings = sum(rec.get("EstimatedMonthlySavings", 0.0) for rec in recs) * ctx.pricing_multiplier

        return ServiceFindings(
            service_name="API Gateway",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=API_GATEWAY_OPTIMIZATION_DESCRIPTIONS,
        )
