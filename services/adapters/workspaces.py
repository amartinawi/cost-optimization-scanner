"""Flat-rate adapter for WorkSpaces."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.workspaces import WORKSPACES_OPTIMIZATION_DESCRIPTIONS, get_enhanced_workspaces_checks


class WorkspacesModule(BaseServiceModule):
    key: str = "workspaces"
    cli_aliases: tuple[str, ...] = ("workspaces",)
    display_name: str = "WorkSpaces"

    def required_clients(self) -> tuple[str, ...]:
        return ("workspaces",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/workspaces.py] WorkSpaces module active")
        result = get_enhanced_workspaces_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 35 * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="WorkSpaces",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=WORKSPACES_OPTIMIZATION_DESCRIPTIONS,
        )
