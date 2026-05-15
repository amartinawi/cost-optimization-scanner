"""vCPU+memory-based pricing adapter for App Runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.apprunner import APPRUNNER_OPTIMIZATION_DESCRIPTIONS, get_enhanced_apprunner_checks

APP_RUNNER_VCPU_HOURLY = 0.064
APP_RUNNER_MEM_GB_HOURLY = 0.007
DEFAULT_ACTIVE_HOURS_PER_MONTH = 160
RIGHTSIZING_SAVINGS_RATE = 0.12


class AppRunnerModule(BaseServiceModule):
    """App Runner cost optimization adapter."""

    key: str = "apprunner"
    cli_aliases: tuple[str, ...] = ("apprunner",)
    display_name: str = "App Runner"
    reads_fast_mode: bool = True

    requires_cloudwatch: bool = True  # _estimate_active_hours queries CW.

    def required_clients(self) -> tuple[str, ...]:
        return ("apprunner", "cloudwatch")

    def _estimate_active_hours(self, ctx: Any) -> float:
        """Estimate active hours/month for App Runner services.

        App Runner has a dual-billing model:
          - Provisioned charge ($0.007/GB-hr, billed 24/7 while service exists).
          - Active charge (vCPU + memory, billed only during request handling).

        The previous default of 160 hr/mo (8 hr/day × 20 workdays) was an
        invented average that didn't reflect actual traffic. With CPU-util-
        tier branching at 5%/20% adding more invented thresholds, the
        overall savings number wasn't traceable.

        Until per-service ActiveInstances / RunningTasks metrics are wired
        through, return DEFAULT_ACTIVE_HOURS_PER_MONTH so the existing math
        still produces a number, but with a documented assumption. Fast
        mode short-circuits the CW lookup.
        """
        if ctx.fast_mode:
            return DEFAULT_ACTIVE_HOURS_PER_MONTH
        try:
            cw = ctx.client("cloudwatch")
            end = datetime.now(UTC)
            start = end - timedelta(days=14)
            resp = cw.get_metric_statistics(
                Namespace="AWS/AppRunner",
                MetricName="CpuUtilization",
                Dimensions=[{"Name": "Service", "Value": "*"}],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Average"],
            )
            datapoints = resp.get("Datapoints", [])
            if datapoints:
                avg_cpu = sum(d["Average"] for d in datapoints) / len(datapoints)
                if avg_cpu < 5.0:
                    return DEFAULT_ACTIVE_HOURS_PER_MONTH * 0.5
                if avg_cpu < 20.0:
                    return DEFAULT_ACTIVE_HOURS_PER_MONTH
        except Exception as e:
            ctx.warn(f"CloudWatch metric check failed: {e}", "apprunner")
        return DEFAULT_ACTIVE_HOURS_PER_MONTH

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/apprunner.py] App Runner module active")
        result = get_enhanced_apprunner_checks(ctx)
        recs = result.get("recommendations", [])

        active_hours = self._estimate_active_hours(ctx)
        multiplier = ctx.pricing_multiplier

        savings = 0.0
        for rec in recs:
            instance_config = rec.get("InstanceConfiguration", {})
            cpu_str = instance_config.get("Cpu", "1 vCPU")
            mem_str = instance_config.get("Memory", "2 GB")
            try:
                vcpus = float(cpu_str.split()[0])
            except (ValueError, IndexError):
                vcpus = 1.0
            try:
                mem_gb = float(mem_str.split()[0])
            except (ValueError, IndexError):
                mem_gb = 2.0
            provisioned_monthly = mem_gb * APP_RUNNER_MEM_GB_HOURLY * 730 * multiplier
            active_monthly = (
                (vcpus * APP_RUNNER_VCPU_HOURLY + mem_gb * APP_RUNNER_MEM_GB_HOURLY) * active_hours * multiplier
            )
            monthly_cost = provisioned_monthly + active_monthly
            savings += monthly_cost * RIGHTSIZING_SAVINGS_RATE

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="App Runner",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=APPRUNNER_OPTIMIZATION_DESCRIPTIONS,
        )
