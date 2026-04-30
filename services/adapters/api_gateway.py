"""Flat-rate adapter for API Gateway."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.api_gateway import API_GATEWAY_OPTIMIZATION_DESCRIPTIONS, get_enhanced_api_gateway_checks


class ApiGatewayModule(BaseServiceModule):
    key: str = "api_gateway"
    cli_aliases: tuple[str, ...] = ("api_gateway",)
    display_name: str = "API Gateway"

    def required_clients(self) -> tuple[str, ...]:
        return ("apigateway",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/api_gateway.py] API Gateway module active")
        result = get_enhanced_api_gateway_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 15 * len(recs)

        return ServiceFindings(
            service_name="API Gateway",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=API_GATEWAY_OPTIMIZATION_DESCRIPTIONS,
        )
