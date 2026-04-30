"""Flat-rate adapter for CloudFront."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.cloudfront import get_enhanced_cloudfront_checks


class CloudfrontModule(BaseServiceModule):
    key: str = "cloudfront"
    cli_aliases: tuple[str, ...] = ("cloudfront",)
    display_name: str = "CloudFront"

    def required_clients(self) -> tuple[str, ...]:
        return ("cloudfront",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/cloudfront.py] CloudFront module active")
        result = get_enhanced_cloudfront_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 25 * len(recs)

        return ServiceFindings(
            service_name="CloudFront",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
        )
