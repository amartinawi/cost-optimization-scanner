"""Keyword-rate adapter for ElastiCache."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.elasticache import get_enhanced_elasticache_checks


class ElasticacheModule(BaseServiceModule):
    key: str = "elasticache"
    cli_aliases: tuple[str, ...] = ("elasticache",)
    display_name: str = "ElastiCache"

    def required_clients(self) -> tuple[str, ...]:
        return ("elasticache",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/elasticache.py] ElastiCache module active")
        result = get_enhanced_elasticache_checks(ctx)
        recs = result.get("recommendations", [])

        savings = 0.0
        for rec in recs:
            est = rec.get("EstimatedSavings", "")
            if "Reserved" in est:
                savings += 200
            elif "Graviton" in est or "20-40%" in est:
                savings += 80
            elif "Valkey" in est:
                savings += 50
            elif "Underutilized" in est:
                savings += 100

        return ServiceFindings(
            service_name="ElastiCache",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
        )
