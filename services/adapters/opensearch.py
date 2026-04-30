"""Keyword-rate adapter for OpenSearch."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.opensearch import OPENSEARCH_OPTIMIZATION_DESCRIPTIONS, get_enhanced_opensearch_checks


class OpensearchModule(BaseServiceModule):
    key: str = "opensearch"
    cli_aliases: tuple[str, ...] = ("opensearch",)
    display_name: str = "OpenSearch"

    def required_clients(self) -> tuple[str, ...]:
        return ("opensearch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/opensearch.py] OpenSearch module active")
        result = get_enhanced_opensearch_checks(ctx)
        recs = result.get("recommendations", [])

        savings = 0.0
        for rec in recs:
            est = rec.get("EstimatedSavings", "")
            if "Reserved" in est:
                savings += 300
            elif "Graviton" in est or "20-40%" in est:
                savings += 120
            elif "storage" in est.lower():
                savings += 50

        return ServiceFindings(
            service_name="OpenSearch",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=OPENSEARCH_OPTIMIZATION_DESCRIPTIONS,
        )
