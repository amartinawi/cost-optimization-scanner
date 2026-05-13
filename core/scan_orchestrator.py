"""Scan orchestration: error-isolated module execution and advisor prefetch.

Provides safe_scan for per-module error isolation and ScanOrchestrator
to coordinate module selection, Cost Hub prefetch, and parallel execution.
"""

from typing import Any

from core.contracts import ServiceFindings, ServiceModule
from core.filtering import resolve_cli_keys
from core.scan_context import ScanContext
from services.advisor import get_detailed_cost_hub_recommendations


def safe_scan(module: ServiceModule, ctx: ScanContext) -> ServiceFindings:
    """Execute module.scan() with error isolation, returning empty findings on failure.

    All-or-nothing by design: if any part of a module's scan raises, the entire
    module produces zero recommendations rather than partial results. This avoids
    misleading savings estimates from incomplete data.
    """
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
    """Coordinates module selection, advisor prefetch, and parallel scan execution."""

    def __init__(self, ctx: ScanContext, modules: list[ServiceModule]) -> None:
        """Initialise with a scan context and the full list of service modules."""
        self.ctx = ctx
        self.modules = modules

    def _prefetch_advisor_data(self, selected: set[str]) -> None:
        """Fetch Cost Optimization Hub recommendations and bucket them per service.

        With the standalone Cost Optimization Hub service tab retired in
        services/__init__.py (2026-05-14), this prefetch is now the *only*
        path that CoH data takes into the report. Every recommendation must
        land in an existing service's bucket; anything that does not is
        logged and dropped, which is acceptable because the type_map covers
        every CoH resourceType AWS currently returns.
        """
        _HUB_SERVICES = {
            "ec2",
            "lambda",
            "ebs",
            "rds",
            "elasticache",
            "opensearch",
            "redshift",
            "s3",
            "eks",
            # Catch ECS / EKS service-level recs and any cross-service CoH
            # findings that previously lived in the dedicated CoH tab.
            "containers",
            # Catch RI / SP purchase recommendations that CoH surfaces. The
            # commitment_analysis adapter renders them alongside its own CE
            # API-derived SP / RI data.
            "commitment_analysis",
        }
        needs_hub = _HUB_SERVICES & selected
        if not needs_hub:
            return

        try:
            all_recs = get_detailed_cost_hub_recommendations(self.ctx)
        except Exception:
            self.ctx.cost_hub_splits = {svc: [] for svc in _HUB_SERVICES}
            return

        splits: dict[str, list[dict[str, Any]]] = {svc: [] for svc in _HUB_SERVICES}
        type_map = {
            "Ec2Instance": "ec2",
            "LambdaFunction": "lambda",
            "EbsVolume": "ebs",
            "RdsDbInstance": "rds",
            "RdsDbCluster": "rds",
            "ElastiCacheCluster": "elasticache",
            "OpenSearchDomain": "opensearch",
            "RedshiftCluster": "redshift",
            "S3Bucket": "s3",
            "EksCluster": "eks",
            # ECS / container-level
            "EcsService": "containers",
            "EcsTask": "containers",
            "EcsCluster": "containers",
            # Reservation / Savings Plans recommendations all live under
            # the commitment_analysis tab from now on.
            "EC2ReservedInstances": "commitment_analysis",
            "RdsReservedInstances": "commitment_analysis",
            "ElastiCacheReservedInstances": "commitment_analysis",
            "OpenSearchReservedInstances": "commitment_analysis",
            "RedshiftReservedInstances": "commitment_analysis",
            "EsReservedInstances": "commitment_analysis",
            "ComputeSavingsPlans": "commitment_analysis",
            "EC2InstanceSavingsPlans": "commitment_analysis",
            "SageMakerSavingsPlans": "commitment_analysis",
        }
        unbucketed_types: set[str] = set()
        for rec in all_recs:
            rec_type = rec.get("currentResourceType", "")
            bucket = type_map.get(rec_type, "")
            if bucket and bucket in splits:
                splits[bucket].append(rec)
            else:
                unbucketed_types.add(rec_type or "<unknown>")
        if unbucketed_types:
            self.ctx.warn(
                f"Cost Optimization Hub: {len(unbucketed_types)} recommendation "
                f"type(s) had no service bucket and were dropped: "
                f"{', '.join(sorted(unbucketed_types))}. Extend "
                f"scan_orchestrator._prefetch_advisor_data type_map to surface them.",
                service="cost_optimization_hub",
            )

        self.ctx.cost_hub_splits = splits

    def run(
        self,
        scan_only: set[str] | None = None,
        skip: set[str] | None = None,
    ) -> dict[str, ServiceFindings]:
        """Run safe_scan for each selected module and return keyed findings.

        Args:
            scan_only: If set, only scan modules matching these keys.
            skip: If set, exclude modules matching these keys.

        Returns:
            Dict mapping module key to its ServiceFindings.
        """
        selected = resolve_cli_keys(self.modules, scan_only, skip)
        self._prefetch_advisor_data(selected)
        return {m.key: safe_scan(m, self.ctx) for m in self.modules if m.key in selected}
