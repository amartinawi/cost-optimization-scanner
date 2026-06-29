"""CloudWatch-metric adapter for API Gateway."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.api_gateway import API_GATEWAY_OPTIMIZATION_DESCRIPTIONS, get_enhanced_api_gateway_checks


class ApiGatewayModule(BaseServiceModule):
    """ServiceModule adapter for API Gateway (REST APIs only).

    CloudWatch request-volume savings: a REST→HTTP migration is priced at the
    first-tier request-rate delta ($3.50/M REST − $1.00/M HTTP = $2.50/M) ×
    measured monthly request count; an API with no measured volume is a $0
    advisory.
    """

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
        """Scan API Gateway REST APIs only for cost optimization opportunities.

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

        # The per-rec EstimatedMonthlySavings already carries the REST→HTTP
        # request-rate delta from the shim's us-east-1 constants ($3.50/M REST −
        # $1.00/M HTTP). Do NOT re-scale by pricing_multiplier: API Gateway
        # request pricing is regional but NOT proportional to the generic
        # multiplier (eu-west-1 = $3.50/M same as us-east-1; eu-central-1 =
        # $3.70/M ≈ +5.7%, not +12%), so multiplying overstated the saving AND
        # broke counted==rendered (headline was scaled, the rendered cards were
        # not). The us-east-1 constants stand as a conservative floor that never
        # exceeds the real regional saving (api_gateway region fix).
        savings = sum(rec.get("EstimatedMonthlySavings", 0.0) for rec in recs)

        return ServiceFindings(
            service_name="API Gateway",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=API_GATEWAY_OPTIMIZATION_DESCRIPTIONS,
        )
