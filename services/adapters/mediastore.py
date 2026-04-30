"""Flat-rate adapter for MediaStore."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.mediastore import MEDIASTORE_OPTIMIZATION_DESCRIPTIONS, get_enhanced_mediastore_checks


class MediastoreModule(BaseServiceModule):
    key: str = "mediastore"
    cli_aliases: tuple[str, ...] = ("mediastore",)
    display_name: str = "MediaStore"

    def required_clients(self) -> tuple[str, ...]:
        return ("mediastore",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/mediastore.py] MediaStore module active")
        result = get_enhanced_mediastore_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 20 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="MediaStore",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=MEDIASTORE_OPTIMIZATION_DESCRIPTIONS,
        )
