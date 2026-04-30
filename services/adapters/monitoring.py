"""Multi-source adapter for monitoring and logging (CloudWatch, CloudTrail, Backup, Route53) optimization."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.backup import get_backup_checks
from services.monitoring import get_cloudwatch_checks, get_cloudtrail_checks
from services.route53 import get_route53_checks


class MonitoringModule(BaseServiceModule):
    key: str = "monitoring"
    cli_aliases: tuple[str, ...] = ("monitoring",)
    display_name: str = "Monitoring & Logging"

    def required_clients(self) -> tuple[str, ...]:
        return ("cloudwatch", "logs", "cloudtrail", "backup", "route53")

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/monitoring.py] Monitoring module active")

        cw_result = get_cloudwatch_checks(ctx)
        ct_result = get_cloudtrail_checks(ctx)
        backup_result = get_backup_checks(ctx)
        r53_result = get_route53_checks(ctx)

        cw_recs = cw_result.get("recommendations", [])
        ct_recs = ct_result.get("recommendations", [])
        backup_recs = backup_result.get("recommendations", [])
        r53_recs = r53_result.get("recommendations", [])

        all_recs = cw_recs + ct_recs + backup_recs + r53_recs

        savings = 0.0
        for rec in all_recs:
            savings_str = rec.get("EstimatedSavings", "")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    savings_val = float(savings_str.replace("$", "").split("/")[0])
                    savings += savings_val
                except (ValueError, AttributeError):
                    pass

        total_recs = len(all_recs)

        return ServiceFindings(
            service_name="Monitoring & Logging",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "cloudwatch_checks": SourceBlock(count=len(cw_recs), recommendations=tuple(cw_recs)),
                "cloudtrail_checks": SourceBlock(count=len(ct_recs), recommendations=tuple(ct_recs)),
                "backup_checks": SourceBlock(count=len(backup_recs), recommendations=tuple(backup_recs)),
                "route53_checks": SourceBlock(count=len(r53_recs), recommendations=tuple(r53_recs)),
            },
        )
