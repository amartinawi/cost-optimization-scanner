from __future__ import annotations

from core.contracts import ServiceFindings, ServiceModule
from core.filtering import resolve_cli_keys
from core.scan_context import ScanContext


def safe_scan(module: ServiceModule, ctx: ScanContext) -> ServiceFindings:
    try:
        return module.scan(ctx)
    except Exception as exc:
        ctx.warn(f"[{module.key}] scan failed: {exc}", service=module.key)
        return ServiceFindings(
            service_name=module.display_name,
            total_recommendations=0,
            total_monthly_savings=0.0,
            sources={},
        )


class ScanOrchestrator:
    def __init__(self, ctx: ScanContext, modules: list[ServiceModule]) -> None:
        self.ctx = ctx
        self.modules = modules

    def run(
        self,
        scan_only: set[str] | None = None,
        skip: set[str] | None = None,
    ) -> dict[str, ServiceFindings]:
        selected = resolve_cli_keys(self.modules, scan_only, skip)
        return {m.key: safe_scan(m, self.ctx) for m in self.modules if m.key in selected}
