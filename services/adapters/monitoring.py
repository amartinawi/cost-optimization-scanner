"""Multi-source adapter for monitoring and logging (CloudWatch, CloudTrail, Backup, Route53) optimization."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.backup import get_backup_checks
from services.monitoring import get_cloudwatch_checks, get_cloudtrail_checks
from services.route53 import get_route53_checks


class MonitoringModule(BaseServiceModule):
    """ServiceModule adapter for CloudWatch, CloudTrail, Backup, and Route53. Composite savings strategy."""

    key: str = "monitoring"
    cli_aliases: tuple[str, ...] = ("monitoring",)
    display_name: str = "Monitoring & Logging"
    # Shim hits cloudwatch.describe_alarms + list_metrics for custom-metrics
    # tier pricing; flag must reflect that.
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for monitoring and logging scanning."""
        return ("cloudwatch", "logs", "cloudtrail", "backup", "route53")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan CloudWatch, CloudTrail, Backup, and Route53 for cost optimization.

        Consults CloudWatch checks, CloudTrail checks, Backup checks, and
        Route53 checks. Savings parsed from dollar-amount strings.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with cloudwatch_checks, cloudtrail_checks,
            backup_checks, and route53_checks SourceBlock entries.
        """

        multiplier = ctx.pricing_multiplier
        cw_result = get_cloudwatch_checks(ctx, multiplier)
        ct_result = get_cloudtrail_checks(ctx)
        backup_result = get_backup_checks(ctx)
        r53_result = get_route53_checks(ctx, multiplier)

        cw_recs = cw_result.get("recommendations", [])
        ct_recs = ct_result.get("recommendations", [])
        backup_recs = backup_result.get("recommendations", [])
        r53_recs = r53_result.get("recommendations", [])

        all_recs = cw_recs + ct_recs + backup_recs + r53_recs

        # Prefer the numeric `EstimatedMonthlySavings` field when present
        # (shim now emits it on quantified recs); fall back to string parse
        # for sub-shims that haven't been migrated yet.
        from services._savings import parse_dollar_savings

        savings = 0.0
        for rec in all_recs:
            numeric = rec.get("EstimatedMonthlySavings")
            if isinstance(numeric, (int, float)) and numeric > 0:
                savings += float(numeric)
                continue
            savings += parse_dollar_savings(rec.get("EstimatedSavings", ""))

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
