"""Provisioned-memory pricing adapter for App Runner."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.apprunner import APPRUNNER_OPTIMIZATION_DESCRIPTIONS, get_enhanced_apprunner_checks

# App Runner provisioned memory is billed 24/7 while a service exists, at
# $0.007/GB-hr (us-east-1). Validated live against the AWS Pricing API on
# 2026-06-27: AWSAppRunner, usagetype USE1-AppRunner-Provisioned-GB-hours,
# SKU YYWUT3AQDJMRWWGA, pricePerUnit USD 0.0070000000. For a truly idle service
# (0 Requests over the lookback window — see services.apprunner._monthly_requests,
# gated on the real ServiceName+ServiceID CloudWatch dimensions) this is the
# recoverable waste when the service is paused/deleted. The active (per-request)
# vCPU+memory charge is NOT quantified here: it needs request-handling duration
# metrics App Runner does not expose, so counting it would fabricate a dollar.
APP_RUNNER_MEM_GB_HOURLY: float = 0.007
HOURS_PER_MONTH: int = 730


class AppRunnerModule(BaseServiceModule):
    """App Runner cost optimization adapter."""

    key: str = "apprunner"
    cli_aliases: tuple[str, ...] = ("apprunner",)
    display_name: str = "App Runner"
    reads_fast_mode: bool = True

    requires_cloudwatch: bool = True  # shim queries CW Requests per service.

    def required_clients(self) -> tuple[str, ...]:
        return ("apprunner", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        result = get_enhanced_apprunner_checks(ctx)
        recs = result.get("recommendations", [])
        multiplier = ctx.pricing_multiplier

        savings = 0.0
        for rec in recs:
            instance_config = rec.get("InstanceConfiguration", {}) or {}
            mem_str = instance_config.get("Memory", "2 GB")
            try:
                mem_gb = float(str(mem_str).split()[0])
            except (ValueError, IndexError):
                mem_gb = 2.0
            provisioned_monthly = mem_gb * APP_RUNNER_MEM_GB_HOURLY * HOURS_PER_MONTH * multiplier
            rec["EstimatedMonthlySavings"] = round(provisioned_monthly, 2)
            rec["AuditBasis"] = {
                "rate_source": (
                    "AWS Pricing API (validated 2026-06-27): AWSAppRunner "
                    "USE1-AppRunner-Provisioned-GB-hours, SKU YYWUT3AQDJMRWWGA, "
                    "$0.007/GB-hr (us-east-1)"
                ),
                "rate_per_gb_hour": APP_RUNNER_MEM_GB_HOURLY,
                "region_multiplier": round(multiplier, 4),
                "hours_per_month": HOURS_PER_MONTH,
                "memory_gb": mem_gb,
                "evidence": (
                    "0 Requests over lookback window (CloudWatch "
                    "AWS/AppRunner Requests, ServiceName+ServiceID dimensions)"
                ),
                "formula": "memory_gb × rate × 730 × pricing_multiplier",
            }
            savings += provisioned_monthly

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="App Runner",
            total_recommendations=len(recs),
            total_monthly_savings=round(savings, 2),
            sources=sources,
            optimization_descriptions=APPRUNNER_OPTIMIZATION_DESCRIPTIONS,
        )
