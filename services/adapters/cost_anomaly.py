"""Cost anomaly detection adapter using AWS Cost Explorer and CloudWatch.

Detects cost anomalies via the Cost Explorer Anomaly API, audits anomaly
monitor coverage, and checks for CloudWatch billing alarms. Produces
actionable recommendations for cost governance:

- Active cost anomalies with impact analysis (last 30 days)
- Anomaly monitor coverage gaps (missing monitors = blind spots)
- CloudWatch billing alarm coverage (missing alarms = no alerting)
- Anomaly subscription and best-practice recommendations

AWS API cost: Cost Explorer charges $0.01 per API request. This adapter
makes ~3 CE calls and ~1 CloudWatch call per scan. The calls are:

1. ``get_anomalies`` — active anomalies in the last 30 days
2. ``get_anomaly_monitors`` — configured anomaly monitors
3. ``get_anomaly_subscriptions`` — configured alert subscriptions
4. ``describe_alarms`` — CloudWatch billing alarms
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule


class CostAnomalyModule(BaseServiceModule):
    """ServiceModule adapter for cost anomaly detection and billing alerts.

    Uses Cost Explorer Anomaly API and CloudWatch to detect cost anomalies,
    audit monitor coverage, and verify billing alarm configuration.

    CE API cost: ~$0.03 per scan (3 calls).
    """

    key: str = "cost_anomaly"
    cli_aliases: tuple[str, ...] = ("cost_anomaly", "anomaly")
    display_name: str = "Cost Anomaly Detection"

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(
            label="Active Anomalies",
            source_path="extras.active_anomaly_count",
            formatter="int",
        ),
        StatCardSpec(
            label="Anomaly Impact (30d)",
            source_path="extras.total_anomaly_impact_30d",
            formatter="currency",
        ),
        StatCardSpec(
            label="Monthly Savings",
            source_path="total_monthly_savings",
            formatter="currency",
        ),
    )

    grouping = GroupingSpec(by="check_category")

    requires_cloudwatch: bool = False
    reads_fast_mode: bool = False

    def required_clients(self) -> tuple[str, ...]:
        """Returns Cost Explorer and CloudWatch client names."""
        return ("ce", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan for cost anomalies, monitor coverage, and billing alarms.

        Queries Cost Explorer for active anomalies in the last 30 days,
        audits anomaly monitor and subscription configuration, and checks
        CloudWatch for billing alarms.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with active_anomalies, anomaly_monitors,
            billing_alarms, and recommendations source blocks.
        """
        print("\U0001f50d [services/adapters/cost_anomaly.py] Cost Anomaly module active")

        ce = ctx.client("ce")
        cw = ctx.client("cloudwatch")
        if not ce:
            return self._empty_findings()

        anomaly_recs, total_impact = self._fetch_active_anomalies(ce)
        monitor_recs, monitor_count = self._check_anomaly_monitors(ce)
        billing_recs, billing_alarm_count = self._check_billing_alarms(cw)
        recommendation_recs = self._generate_recommendations(
            ce, len(anomaly_recs), total_impact, monitor_count, billing_alarm_count
        )

        all_recs = anomaly_recs + monitor_recs + billing_recs + recommendation_recs
        total_savings = sum(r.get("monthly_savings", 0.0) for r in all_recs)

        return ServiceFindings(
            service_name="Cost Anomaly Detection",
            total_recommendations=len(all_recs),
            total_monthly_savings=round(total_savings, 2),
            sources={
                "active_anomalies": SourceBlock(
                    count=len(anomaly_recs),
                    recommendations=tuple(anomaly_recs),
                ),
                "anomaly_monitors": SourceBlock(
                    count=len(monitor_recs),
                    recommendations=tuple(monitor_recs),
                ),
                "billing_alarms": SourceBlock(
                    count=len(billing_recs),
                    recommendations=tuple(billing_recs),
                ),
                "recommendations": SourceBlock(
                    count=len(recommendation_recs),
                    recommendations=tuple(recommendation_recs),
                ),
            },
            extras={
                "active_anomaly_count": len(anomaly_recs),
                "total_anomaly_impact_30d": round(total_impact, 2),
                "monitor_count": monitor_count,
                "billing_alarm_count": billing_alarm_count,
            },
            optimization_descriptions={
                "active_anomalies": {
                    "title": "Active Cost Anomalies",
                    "description": "Anomalous spend patterns detected by Cost Explorer in the last 30 days",
                },
                "anomaly_monitors": {
                    "title": "Anomaly Monitor Coverage",
                    "description": "Cost anomaly monitors tracking spend patterns across services",
                },
                "billing_alarms": {
                    "title": "Billing Alarm Gaps",
                    "description": "Missing CloudWatch billing alarms for spend threshold alerts",
                },
                "recommendations": {
                    "title": "Cost Optimization Recommendations",
                    "description": "Cost Optimization Hub recommendations for anomaly-related resources",
                },
            },
        )

    def _fetch_active_anomalies(self, ce: Any) -> tuple[list[dict[str, Any]], float]:
        """Query Cost Explorer for active cost anomalies in the last 30 days.

        Retrieves anomalies with positive total impact and builds
        recommendation dicts with severity based on impact amount.

        Args:
            ce: Cost Explorer boto3 client.

        Returns:
            Tuple of (anomaly recommendation list, total impact amount).
        """
        recs: list[dict[str, Any]] = []
        total_impact = 0.0

        try:
            end = date.today()
            start = end - timedelta(days=30)

            resp = ce.get_anomalies(
                DateInterval={"StartDate": start.isoformat(), "EndDate": end.isoformat()},
                TotalImpact={"NumericOperator": "GREATER_THAN", "StartValue": 0},
            )

            for idx, anomaly in enumerate(resp.get("Anomalies", [])):
                impact_info = anomaly.get("TotalImpact", {})
                max_impact = float(impact_info.get("MaxImpact", 0))
                total_amount = float(impact_info.get("TotalImpactAmount", 0))
                total_impact += total_amount

                dimension = anomaly.get("DimensionValue", "Unknown Service")
                start_date = anomaly.get("AnomalyStartDate", "N/A")
                end_date = anomaly.get("AnomalyEndDate", "N/A")
                anomaly_id = anomaly.get("AnomalyId", f"anomaly-{idx}")

                if max_impact > 100:
                    severity = "HIGH"
                elif max_impact > 10:
                    severity = "MEDIUM"
                else:
                    severity = "LOW"

                recs.append(
                    {
                        "resource_id": anomaly_id,
                        "check_type": "cost_anomaly",
                        "check_category": "Active Anomalies",
                        "current_value": (
                            f"Anomaly on {dimension}: ${max_impact:.2f} max impact "
                            f"(${total_amount:.2f} total), {start_date} to {end_date}"
                        ),
                        "recommended_value": "Investigate root cause and implement corrective action",
                        "monthly_savings": round(total_amount, 2),
                        "severity": severity,
                        "reason": (
                            f"Cost anomaly detected for {dimension} with ${max_impact:.2f} "
                            f"max impact and ${total_amount:.2f} total impact over 30 days"
                        ),
                    }
                )

        except Exception as e:
            print(f"Warning: Cost anomaly query failed: {e}")

        return recs, total_impact

    def _check_anomaly_monitors(self, ce: Any) -> tuple[list[dict[str, Any]], int]:
        """Audit Cost Explorer anomaly monitor configuration.

        Checks for existing anomaly monitors and recommends creating
        monitors if none are configured.

        Args:
            ce: Cost Explorer boto3 client.

        Returns:
            Tuple of (monitor recommendation list, monitor count).
        """
        recs: list[dict[str, Any]] = []
        monitor_count = 0

        try:
            monitors: list[dict[str, Any]] = []
            params: dict[str, Any] = {}
            while True:
                resp = ce.get_anomaly_monitors(**params)
                monitors.extend(resp.get("AnomalyMonitors", []))
                next_token = resp.get("NextPageToken")
                if not next_token:
                    break
                params["NextPageToken"] = next_token
            monitor_count = len(monitors)

            if monitor_count == 0:
                recs.append(
                    {
                        "resource_id": "anomaly-monitors",
                        "check_type": "anomaly_monitor",
                        "check_category": "Anomaly Monitors",
                        "current_value": "No Cost Anomaly monitors configured",
                        "recommended_value": (
                            "Create Cost Explorer anomaly monitors for AWS services, "
                            "linked accounts, or total account spend"
                        ),
                        "monthly_savings": 0.0,
                        "severity": "HIGH",
                        "reason": (
                            "No Cost Anomaly monitors configured. Without monitors, "
                            "cost anomalies go undetected and can lead to significant "
                            "unexpected charges"
                        ),
                    }
                )
            else:
                inactive = [
                    m for m in monitors if m.get("MonitorSpecification", {}).get("Dimension", {}).get("Key") is None
                ]
                if inactive:
                    recs.append(
                        {
                            "resource_id": "anomaly-monitors-review",
                            "check_type": "anomaly_monitor",
                            "check_category": "Anomaly Monitors",
                            "current_value": f"{monitor_count} anomaly monitor(s) configured",
                            "recommended_value": "Review monitor specifications for optimal coverage",
                            "monthly_savings": 0.0,
                            "severity": "LOW",
                            "reason": (
                                f"{monitor_count} anomaly monitor(s) exist. Review their "
                                f"specifications to ensure comprehensive service and "
                                f"account coverage"
                            ),
                        }
                    )

        except Exception as e:
            print(f"Warning: Anomaly monitor query failed: {e}")

        return recs, monitor_count

    def _check_billing_alarms(self, cw: Any) -> tuple[list[dict[str, Any]], int]:
        """Check for CloudWatch billing alarms.

        Looks for CloudWatch alarms monitoring the EstimatedCharges
        metric in the AWS/Billing namespace.

        Args:
            cw: CloudWatch boto3 client, or None if unavailable.

        Returns:
            Tuple of (billing alarm recommendation list, alarm count).
        """
        recs: list[dict[str, Any]] = []
        billing_alarm_count = 0

        if not cw:
            recs.append(
                {
                    "resource_id": "billing-alarms",
                    "check_type": "billing_alarm",
                    "check_category": "Billing Alarms",
                    "current_value": "CloudWatch client unavailable",
                    "recommended_value": "Configure CloudWatch billing alarms when available",
                    "monthly_savings": 0.0,
                    "severity": "LOW",
                    "reason": "CloudWatch client not available for billing alarm check",
                }
            )
            return recs, billing_alarm_count

        try:
            metric_alarms: list[dict[str, Any]] = []
            params: dict[str, Any] = {}
            while True:
                resp = cw.describe_alarms(**params)
                metric_alarms.extend(resp.get("MetricAlarms", []))
                next_token = resp.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token

            billing_alarms = [
                a
                for a in metric_alarms
                if "billing" in a.get("Namespace", "").lower() or "estimated" in a.get("MetricName", "").lower()
            ]
            billing_alarm_count = len(billing_alarms)

            if billing_alarm_count == 0:
                recs.append(
                    {
                        "resource_id": "billing-alarms",
                        "check_type": "billing_alarm",
                        "check_category": "Billing Alarms",
                        "current_value": "No CloudWatch billing alarms configured",
                        "recommended_value": (
                            "Create a CloudWatch billing alarm on the EstimatedCharges "
                            "metric (AWS/Billing namespace) to receive alerts when "
                            "spending exceeds thresholds"
                        ),
                        "monthly_savings": 0.0,
                        "severity": "HIGH",
                        "reason": (
                            "No CloudWatch billing alarms found. Without billing alarms, "
                            "unexpected cost increases may go unnoticed until the monthly "
                            "invoice arrives"
                        ),
                    }
                )

        except Exception as e:
            print(f"Warning: Billing alarm query failed: {e}")

        return recs, billing_alarm_count

    def _generate_recommendations(
        self,
        ce: Any,
        anomaly_count: int,
        total_impact: float,
        monitor_count: int,
        billing_alarm_count: int,
    ) -> list[dict[str, Any]]:
        """Synthesize best-practice recommendations based on anomaly findings.

        Checks anomaly subscription configuration and produces summary
        recommendations based on the overall anomaly detection posture.

        Args:
            ce: Cost Explorer boto3 client.
            anomaly_count: Number of active anomalies detected.
            total_impact: Total dollar impact of anomalies.
            monitor_count: Number of anomaly monitors configured.
            billing_alarm_count: Number of billing alarms configured.

        Returns:
            List of best-practice recommendation dicts.
        """
        recs: list[dict[str, Any]] = []

        subscription_count = 0
        try:
            resp = ce.get_anomaly_subscriptions()
            subscriptions = resp.get("AnomalySubscriptions", [])
            subscription_count = len(subscriptions)
        except Exception as e:
            print(f"Warning: Anomaly subscription query failed: {e}")

        if subscription_count == 0 and monitor_count > 0:
            recs.append(
                {
                    "resource_id": "anomaly-subscriptions",
                    "check_type": "anomaly_subscription",
                    "check_category": "Recommendations",
                    "current_value": (f"{monitor_count} monitor(s) exist but no alert subscriptions configured"),
                    "recommended_value": (
                        "Create anomaly subscriptions (SNS or email) to receive "
                        "notifications when anomalies are detected"
                    ),
                    "monthly_savings": 0.0,
                    "severity": "MEDIUM",
                    "reason": (
                        "Anomaly monitors are configured but no subscriptions exist. "
                        "Monitors detect anomalies but without subscriptions, nobody "
                        "is notified"
                    ),
                }
            )

        if anomaly_count > 0:
            recs.append(
                {
                    "resource_id": "anomaly-summary",
                    "check_type": "anomaly_summary",
                    "check_category": "Recommendations",
                    "current_value": (
                        f"{anomaly_count} anomaly(ies) detected with ${total_impact:.2f} "
                        f"total impact in the last 30 days"
                    ),
                    "recommended_value": (
                        "Review anomaly root causes, adjust budgets, and implement cost controls to prevent recurrence"
                    ),
                    "monthly_savings": round(total_impact, 2),
                    "severity": "HIGH" if total_impact > 100 else "MEDIUM",
                    "reason": (
                        f"{anomaly_count} cost anomaly(ies) with ${total_impact:.2f} "
                        f"total impact detected. Implementing corrective actions could "
                        f"eliminate this unexpected spend"
                    ),
                }
            )

        if monitor_count == 0 and billing_alarm_count == 0:
            recs.append(
                {
                    "resource_id": "cost-governance",
                    "check_type": "cost_governance",
                    "check_category": "Recommendations",
                    "current_value": "No cost anomaly monitors or billing alarms configured",
                    "recommended_value": (
                        "Implement Cost Explorer anomaly monitors with SNS subscriptions "
                        "and CloudWatch billing alarms for comprehensive cost governance"
                    ),
                    "monthly_savings": 0.0,
                    "severity": "HIGH",
                    "reason": (
                        "No cost detection or alerting mechanisms are in place. "
                        "Implementing anomaly monitors and billing alarms provides "
                        "essential cost governance and prevents bill surprises"
                    ),
                }
            )

        return recs

    @staticmethod
    def _empty_findings() -> ServiceFindings:
        """Return empty ServiceFindings when CE is unavailable.

        Returns:
            ServiceFindings with zero counts and empty source blocks.
        """
        return ServiceFindings(
            service_name="Cost Anomaly Detection",
            total_recommendations=0,
            total_monthly_savings=0.0,
            sources={
                "active_anomalies": SourceBlock(count=0, recommendations=()),
                "anomaly_monitors": SourceBlock(count=0, recommendations=()),
                "billing_alarms": SourceBlock(count=0, recommendations=()),
                "recommendations": SourceBlock(count=0, recommendations=()),
            },
            extras={
                "active_anomaly_count": 0,
                "total_anomaly_impact_30d": 0.0,
                "monitor_count": 0,
                "billing_alarm_count": 0,
            },
            optimization_descriptions={
                "cost_anomaly": {
                    "title": "Active Cost Anomalies",
                    "description": "Anomalous spend patterns detected by Cost Explorer in the last 30 days",
                },
                "anomaly_monitor": {
                    "title": "Anomaly Monitor Coverage",
                    "description": "Cost anomaly monitors tracking spend patterns across services",
                },
                "billing_alarm": {
                    "title": "Billing Alarm Gaps",
                    "description": "Missing CloudWatch billing alarms for spend threshold alerts",
                },
                "anomaly_subscription": {
                    "title": "Anomaly Alert Subscriptions",
                    "description": "Alert subscriptions for anomaly notifications to appropriate contacts",
                },
                "anomaly_summary": {
                    "title": "Anomaly Summary",
                    "description": "Overall anomaly detection health and configuration status",
                },
                "cost_governance": {
                    "title": "Cost Governance",
                    "description": "Assessment of anomaly detection and billing alert coverage",
                },
            },
        )
