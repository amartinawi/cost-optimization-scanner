"""Commitment analysis adapter for Savings Plans and Reserved Instance utilization.

Analyzes AWS Cost Explorer data to surface under-utilized commitments,
coverage gaps, expiring commitments, and purchase recommendations.

AWS API cost: Cost Explorer charges $0.01 per API request. This adapter
makes ~7 calls per scan (~$0.07/scan). The calls are:

1. ``get_savings_plans_utilization`` — overall SP utilization rate
2. ``get_savings_plans_utilization_details`` — per-SP utilization
3. ``get_savings_plans_coverage`` — SP coverage rate by service
4. ``get_reservation_utilization`` — RI utilization rate
5. ``get_reservation_coverage`` — RI coverage rate by service
6. ``get_savings_plans_purchase_recommendation`` — SP purchase recs
7. ``get_reservation_purchase_recommendation`` — RI purchase recs
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule


def _route_ce_error(ctx: Any, action: str, exc: Exception) -> None:
    """Classify Cost Explorer errors into ctx.permission_issue vs ctx.warn.

    CE returns AccessDeniedException when the caller lacks ce:Get* perms.
    Routing to permission_issue surfaces it in the JSON output's
    permission_issues array (vs a generic warning that hides IAM gaps).
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            ctx.permission_issue(
                f"{action} blocked: {code}", "commitment_analysis", action=action
            )
            return
    ctx.warn(f"{action} failed: {exc}", "commitment_analysis")


def _time_period() -> dict[str, str]:
    """Build a 30-day Cost Explorer time period ending today.

    Returns:
        Dict with ``Start`` and ``End`` ISO-format date strings.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=30)
    return {"Start": start.isoformat(), "End": end.isoformat()}


class CommitmentAnalysisModule(BaseServiceModule):
    """ServiceModule adapter for Savings Plans and Reserved Instance analysis.

    Uses Cost Explorer to detect under-utilized commitments, coverage gaps,
    expiring commitments, and purchase recommendations.

    CE API cost: ~$0.07 per scan (7 calls at $0.01 each).
    """

    key: str = "commitment_analysis"
    cli_aliases: tuple[str, ...] = ("commitment_analysis", "commitments", "savings_plans", "ri")
    display_name: str = "Commitment Analysis"

    AVG_SP_DISCOUNT_RATE: float = 0.30
    UTILIZATION_THRESHOLD: float = 0.95
    COVERAGE_GAP_THRESHOLD: float = 0.80

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="SP Utilization", source_path="extras.sp_utilization_rate", formatter="percent"),
        StatCardSpec(label="SP Coverage", source_path="extras.sp_coverage_rate", formatter="percent"),
        StatCardSpec(label="RI Utilization", source_path="extras.ri_utilization_rate", formatter="percent"),
        StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
    )

    grouping = GroupingSpec(by="check_category")

    requires_cloudwatch: bool = False

    def required_clients(self) -> tuple[str, ...]:
        """Returns Cost Explorer client name."""
        return ("ce",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Savings Plans and Reserved Instance utilization and coverage.

        Uses Cost Explorer APIs to analyze commitment usage over the last
        30 days. Returns findings grouped by check category.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with sp_utilization, sp_coverage_gaps,
            ri_utilization, ri_coverage_gaps, expiring_commitments,
            and purchase_recommendations source blocks.
        """
        print("\U0001f50d [services/adapters/commitment_analysis.py] Commitment Analysis module active")

        ce = ctx.client("ce")
        if not ce:
            return self._empty_findings()

        tp = _time_period()

        sp_util_recs, sp_util_rate = self._check_sp_utilization(ctx, ce, tp)
        sp_cov_recs, sp_cov_rate = self._check_sp_coverage(ctx, ce, tp)
        ri_util_recs, ri_util_rate = self._check_ri_utilization(ctx, ce, tp)
        ri_cov_recs, ri_cov_rate = self._check_ri_coverage(ctx, ce, tp)
        expiry_recs = self._check_expiring(ctx, ce, tp)
        purchase_recs = self._check_purchase_recommendations(ctx, ce)

        all_recs = sp_util_recs + sp_cov_recs + ri_util_recs + ri_cov_recs + expiry_recs + purchase_recs
        total_savings = sum(r.get("monthly_savings", 0.0) for r in all_recs)

        # Cost Optimization Hub RI / Savings Plans purchase recommendations
        # the orchestrator routed here after the standalone CoH tab retired.
        # Render alongside the CE-API derived data so commitment_analysis is
        # the single home for every reservation / SP signal.
        cost_hub_recs = ctx.cost_hub_splits.get("commitment_analysis", [])
        cost_hub_savings = sum(
            float(r.get("estimatedMonthlySavings", 0) or 0) for r in cost_hub_recs
        )

        exp_30 = sum(1 for r in expiry_recs if r.get("severity") == "HIGH")
        exp_60 = sum(1 for r in expiry_recs if r.get("severity") == "MEDIUM")
        exp_90 = sum(1 for r in expiry_recs if r.get("severity") == "LOW")

        return ServiceFindings(
            service_name="Commitment Analysis",
            total_recommendations=len(all_recs) + len(cost_hub_recs),
            total_monthly_savings=round(total_savings + cost_hub_savings, 2),
            sources={
                "cost_optimization_hub": SourceBlock(
                    count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)
                ),
                "sp_utilization": SourceBlock(
                    count=len(sp_util_recs),
                    recommendations=tuple(sp_util_recs),
                    extras={"overall_utilization_rate": sp_util_rate},
                ),
                "sp_coverage_gaps": SourceBlock(
                    count=len(sp_cov_recs),
                    recommendations=tuple(sp_cov_recs),
                    extras={"overall_coverage_rate": sp_cov_rate},
                ),
                "ri_utilization": SourceBlock(
                    count=len(ri_util_recs),
                    recommendations=tuple(ri_util_recs),
                ),
                "ri_coverage_gaps": SourceBlock(
                    count=len(ri_cov_recs),
                    recommendations=tuple(ri_cov_recs),
                ),
                "expiring_commitments": SourceBlock(
                    count=len(expiry_recs),
                    recommendations=tuple(expiry_recs),
                    extras={"expiring_30d": exp_30, "expiring_60d": exp_60, "expiring_90d": exp_90},
                ),
                "purchase_recommendations": SourceBlock(
                    count=len(purchase_recs),
                    recommendations=tuple(purchase_recs),
                ),
            },
            extras={
                "sp_utilization_rate": sp_util_rate,
                "sp_coverage_rate": sp_cov_rate,
                "ri_utilization_rate": ri_util_rate,
                "ri_coverage_rate": ri_cov_rate,
            },
        )

    def _check_sp_utilization(self, ctx: Any, ce: Any, tp: dict[str, str]) -> tuple[list[dict[str, Any]], float]:
        """Check Savings Plans utilization rate and flag under-utilized plans.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.

        Returns:
            Tuple of (under-utilized recommendations list, overall rate 0-1).
        """
        recs: list[dict[str, Any]] = []
        overall_rate = 0.0

        try:
            resp = ce.get_savings_plans_utilization(TimePeriod=tp)
            util = resp.get("SavingsPlansUtilizations", {})
            overall_rate = self._parse_pct(util.get("Total", {}).get("UtilizationPercentage", "0"))
        except Exception as e:
            _route_ce_error(ctx, "ce:GetSavingsPlansUtilization", e)
            return recs, overall_rate

        try:
            params: dict[str, Any] = {"TimePeriod": tp}
            while True:
                details = ce.get_savings_plans_utilization_details(**params)
                for detail in details.get("SavingsPlansUtilizationsDetails", []):
                    sp_arn = detail.get("SavingsPlanArn", "")
                    rate = self._parse_pct(detail.get("UtilizationPercentage", "0"))

                    if rate < self.UTILIZATION_THRESHOLD:
                        hourly = float(detail.get("AmortizedCommitment", {}).get("TotalHourlyCommitment", "0"))
                        waste = hourly * (1.0 - rate) * 730
                        recs.append(
                            {
                                "resource_id": sp_arn,
                                "check_type": "sp_utilization",
                                "check_category": "SP Under-utilization",
                                "current_value": f"{rate:.1%}",
                                "recommended_value": f"{self.UTILIZATION_THRESHOLD:.0%}+",
                                "monthly_savings": round(waste, 2),
                                "severity": "HIGH" if rate < 0.50 else "MEDIUM",
                                "reason": f"Savings Plan utilized at {rate:.1%} (below {self.UTILIZATION_THRESHOLD:.0%} threshold)",
                            }
                        )
                next_token = details.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token
        except Exception as e:
            _route_ce_error(ctx, "ce:GetSavingsPlansUtilizationDetails", e)

        return recs, overall_rate

    def _check_sp_coverage(self, ctx: Any, ce: Any, tp: dict[str, str]) -> tuple[list[dict[str, Any]], float]:
        """Check Savings Plans coverage by service and flag coverage gaps.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.

        Returns:
            Tuple of (coverage gap recommendations list, overall rate 0-1).
        """
        recs: list[dict[str, Any]] = []
        overall_rate = 0.0

        total_od = 0.0
        total_covered = 0.0
        try:
            params: dict[str, Any] = {
                "TimePeriod": tp,
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            while True:
                resp = ce.get_savings_plans_coverage(**params)
                coverages = resp.get("SavingsPlansCoverages", [])
                for entry in coverages:
                    cov = entry.get("Coverage", {})
                    od = float(cov.get("OnDemandCost", "0"))
                    covered = float(cov.get("CoveredCost", "0"))
                    total_od += od
                    total_covered += covered
                    rate = self._parse_pct(cov.get("CoveragePercentage", "0"))
                    if rate < self.COVERAGE_GAP_THRESHOLD:
                        svc = entry.get("Attributes", {}).get("service", "Unknown")
                        potential = od * (1.0 - rate) * self.AVG_SP_DISCOUNT_RATE
                        recs.append(
                            {
                                "resource_id": svc,
                                "check_type": "sp_coverage",
                                "check_category": "SP Coverage Gap",
                                "current_value": f"{rate:.1%}",
                                "recommended_value": f"{self.COVERAGE_GAP_THRESHOLD:.0%}+",
                                "monthly_savings": round(potential, 2),
                                "severity": "MEDIUM",
                                "reason": f"{svc} has {rate:.1%} SP coverage (below {self.COVERAGE_GAP_THRESHOLD:.0%} threshold)",
                            }
                        )
                next_token = resp.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token

            total_spend = total_od + total_covered
            if total_spend > 0:
                overall_rate = total_covered / total_spend
        except Exception as e:
            _route_ce_error(ctx, "ce:GetSavingsPlansCoverage", e)

        return recs, overall_rate

    def _check_ri_utilization(self, ctx: Any, ce: Any, tp: dict[str, str]) -> tuple[list[dict[str, Any]], float]:
        """Check Reserved Instance utilization rate by service.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.
            pricing_multiplier: Regional pricing multiplier.

        Returns:
            Tuple of (under-utilized RI recommendations list, overall rate 0-1).
        """
        recs: list[dict[str, Any]] = []
        overall_rate = 0.0

        try:
            params: dict[str, Any] = {
                "TimePeriod": tp,
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            while True:
                resp = ce.get_reservation_utilization(**params)
                utilizations = resp.get("UtilizationsByTime", [])
                for entry in utilizations:
                    groups = entry.get("Groups", [])
                    for group in groups:
                        util = group.get("Utilization", {})
                        rate = self._parse_pct(util.get("UtilizationPercentage", "0"))
                        svc = group.get("Attributes", {}).get("service", "Unknown")
                        if rate < self.UTILIZATION_THRESHOLD:
                            total_cost = float(util.get("TotalAmortizedCost", "0"))
                            used_cost = float(util.get("AmortizedUpfrontCost", "0"))
                            waste = total_cost * (1.0 - rate)
                            recs.append(
                                {
                                    "resource_id": svc,
                                    "check_type": "ri_utilization",
                                    "check_category": "RI Under-utilization",
                                    "current_value": f"{rate:.1%}",
                                    "recommended_value": f"{self.UTILIZATION_THRESHOLD:.0%}+",
                                    "monthly_savings": round(waste, 2),
                                    "severity": "HIGH" if rate < 0.50 else "MEDIUM",
                                    "reason": f"{svc} RI utilized at {rate:.1%} (below {self.UTILIZATION_THRESHOLD:.0%} threshold)",
                                }
                            )

                next_token = resp.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token

            total = resp.get("Total", {})
            overall_rate = self._parse_pct(total.get("UtilizationPercentage", "0"))
        except Exception as e:
            _route_ce_error(ctx, "ce:GetReservationUtilization", e)

        return recs, overall_rate

    def _check_ri_coverage(self, ctx: Any, ce: Any, tp: dict[str, str]) -> tuple[list[dict[str, Any]], float]:
        """Check Reserved Instance coverage by service.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.

        Returns:
            Tuple of (RI coverage gap recommendations list, overall rate 0-1).
        """
        recs: list[dict[str, Any]] = []
        overall_rate = 0.0

        try:
            params: dict[str, Any] = {
                "TimePeriod": tp,
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            while True:
                resp = ce.get_reservation_coverage(**params)
                coverages = resp.get("CoveragesByTime", [])
                for entry in coverages:
                    groups = entry.get("Groups", [])
                    for group in groups:
                        cov = group.get("Coverage", {})
                        rate = self._parse_pct(cov.get("CoveragePercentage", "0"))
                        svc = group.get("Attributes", {}).get("service", "Unknown")
                        if rate < self.COVERAGE_GAP_THRESHOLD:
                            od_cost = float(cov.get("OnDemandCost", "0"))
                            potential = od_cost * (1.0 - rate) * self.AVG_SP_DISCOUNT_RATE
                            recs.append(
                                {
                                    "resource_id": svc,
                                    "check_type": "ri_coverage",
                                    "check_category": "RI Coverage Gap",
                                    "current_value": f"{rate:.1%}",
                                    "recommended_value": f"{self.COVERAGE_GAP_THRESHOLD:.0%}+",
                                    "monthly_savings": round(potential, 2),
                                    "severity": "MEDIUM",
                                    "reason": f"{svc} has {rate:.1%} RI coverage (below {self.COVERAGE_GAP_THRESHOLD:.0%} threshold)",
                                }
                            )

                next_token = resp.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token

            total = resp.get("Total", {})
            overall_rate = self._parse_pct(total.get("CoveragePercentage", "0"))
        except Exception as e:
            _route_ce_error(ctx, "ce:GetReservationCoverage", e)

        return recs, overall_rate

    def _check_expiring(self, ctx: Any, ce: Any, tp: dict[str, str]) -> list[dict[str, Any]]:
        """Check for expiring Savings Plans using utilization details.

        Uses ``get_savings_plans_utilization_details`` which includes
        start/end timestamps for each Savings Plan.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.

        Returns:
            List of expiry alert recommendation dicts.
        """
        recs: list[dict[str, Any]] = []
        now = datetime.now(UTC)

        try:
            params: dict[str, Any] = {"TimePeriod": tp}
            while True:
                details = ce.get_savings_plans_utilization_details(**params)
                for detail in details.get("SavingsPlansUtilizationsDetails", []):
                    sp_arn = detail.get("SavingsPlanArn", "")
                    end_str = detail.get("EndDateTime", "")
                    if not end_str:
                        continue

                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        continue

                    days_left = (end_dt - now).days
                    if days_left <= 90:
                        severity = "HIGH" if days_left <= 30 else ("MEDIUM" if days_left <= 60 else "LOW")
                        recs.append(
                            {
                                "resource_id": sp_arn,
                                "check_type": "expiry",
                                "check_category": "Expiring Commitment",
                                "current_value": f"{days_left} days remaining",
                                "recommended_value": "Plan renewal or migration",
                                "monthly_savings": 0.0,
                                "severity": severity,
                                "reason": f"Savings Plan expires in {days_left} days ({end_dt.strftime('%Y-%m-%d')})",
                            }
                        )
                next_token = details.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token
        except Exception as e:
            _route_ce_error(ctx, "ce:GetSavingsPlansUtilizationDetails", e)

        return recs

    def _check_purchase_recommendations(self, ctx: Any, ce: Any) -> list[dict[str, Any]]:
        """Fetch Savings Plans and RI purchase recommendations from Cost Explorer.

        Args:
            ce: Cost Explorer boto3 client.

        Returns:
            List of purchase recommendation dicts.
        """
        recs: list[dict[str, Any]] = []

        recs.extend(self._fetch_sp_recommendations(ctx, ce))
        recs.extend(self._fetch_ri_recommendations(ctx, ce))

        return recs

    # Reservation / Savings Plans purchase recommendations fan out across
    # the full (term, payment) matrix so the operator can compare scenarios
    # rather than relying on a single AWS-default combination. Each call is
    # independent: if any combination is denied (SCP) or throttles, the
    # others continue. The Cost Explorer API is rate-limited; the matrix
    # is small (12 calls per scan) and cached server-side.
    _SP_TERMS: tuple[tuple[str, str], ...] = (("ONE_YEAR", "1yr"), ("THREE_YEARS", "3yr"))
    _SP_PAYMENTS: tuple[tuple[str, str], ...] = (
        ("NO_UPFRONT", "No Upfront"),
        ("PARTIAL_UPFRONT", "Partial Upfront"),
        ("ALL_UPFRONT", "All Upfront"),
    )
    _SP_TYPES: tuple[str, ...] = ("COMPUTE_SP",)
    _RI_SERVICES: tuple[str, ...] = ("Amazon EC2",)

    def _fetch_sp_recommendations(self, ctx: Any, ce: Any) -> list[dict[str, Any]]:
        """Fetch Compute Savings Plans purchase recommendations across the
        full (term, payment) matrix.

        Six calls per SavingsPlansType: 2 terms times 3 payment options.
        Each rec carries explicit term and payment metadata so the renderer
        can surface them on the rec-item title.

        Args:
            ce: Cost Explorer boto3 client.

        Returns:
            List of SP purchase recommendation dicts, one per matching
            (sp_type, term, payment) cell that returned a non-empty result.
        """
        recs: list[dict[str, Any]] = []

        for sp_type in self._SP_TYPES:
            for term_api, term_label in self._SP_TERMS:
                for payment_api, payment_label in self._SP_PAYMENTS:
                    scenario = f"({term_label}, {payment_label})"
                    try:
                        resp = ce.get_savings_plans_purchase_recommendation(
                            SavingsPlansType=sp_type,
                            TermInYears=term_api,
                            PaymentOption=payment_api,
                            LookbackPeriodInDays="THIRTY_DAYS",
                        )
                    except Exception as e:
                        _route_ce_error(
                            ctx,
                            f"ce:GetSavingsPlansPurchaseRecommendation[{sp_type}/{scenario}]",
                            e,
                        )
                        continue

                    for rec in resp.get("SavingsPlansPurchaseRecommendation", []):
                        savings_detail = rec.get("SavingsPlansPurchaseRecommendationSummary", {})
                        monthly_savings = float(savings_detail.get("EstimatedMonthlySavings", "0"))
                        hourly_commit = float(savings_detail.get("HourlyCommitment", "0"))
                        upfront = float(savings_detail.get("EstimatedUpfrontCost", "0"))

                        if monthly_savings <= 0 and hourly_commit <= 0:
                            continue

                        recs.append(
                            {
                                "resource_id": f"{sp_type}_{term_label}_{payment_label.lower().replace(' ', '_')}",
                                "check_type": "purchase",
                                "check_category": f"SP Purchase Recommendation {scenario}",
                                "term": term_label,
                                "payment_option": payment_label,
                                "sp_type": sp_type,
                                "current_value": f"${hourly_commit:.4f}/hr commitment",
                                "recommended_value": (
                                    f"${hourly_commit:.4f}/hr Compute SP "
                                    f"({term_label}, {payment_label})"
                                ),
                                "monthly_savings": round(monthly_savings, 2),
                                "severity": "LOW",
                                "reason": (
                                    f"{term_label} {payment_label}: "
                                    f"${monthly_savings:.2f}/mo at "
                                    f"${hourly_commit:.4f}/hr commitment, "
                                    f"upfront ${upfront:,.2f}"
                                ),
                                "upfront_cost": upfront,
                            }
                        )

                    for rec_detail in resp.get("SavingsPlansPurchaseRecommendationDetail", []):
                        monthly_savings = float(rec_detail.get("EstimatedMonthlySavings", "0"))
                        hourly_commit = float(rec_detail.get("HourlyCommitment", "0"))
                        if hourly_commit <= 0 and monthly_savings <= 0:
                            continue

                        recs.append(
                            {
                                "resource_id": rec_detail.get("AccountId", sp_type),
                                "check_type": "purchase",
                                "check_category": f"SP Purchase Recommendation {scenario}",
                                "term": term_label,
                                "payment_option": payment_label,
                                "sp_type": sp_type,
                                "current_value": "On-Demand",
                                "recommended_value": (
                                    f"${hourly_commit:.4f}/hr Compute SP "
                                    f"({term_label}, {payment_label})"
                                ),
                                "monthly_savings": round(monthly_savings, 2),
                                "severity": "LOW",
                                "reason": (
                                    f"Account {rec_detail.get('AccountId', 'N/A')} "
                                    f"({term_label} {payment_label}): "
                                    f"${monthly_savings:.2f}/mo savings"
                                ),
                            }
                        )

        return recs

    def _fetch_ri_recommendations(self, ctx: Any, ce: Any) -> list[dict[str, Any]]:
        """Fetch Reserved Instance purchase recommendations across the
        full (term, payment) matrix per service.

        Six calls per service: 2 terms times 3 payment options. Each rec
        carries explicit term and payment metadata so the renderer can
        surface them on the rec-item title.

        Args:
            ce: Cost Explorer boto3 client.

        Returns:
            List of RI purchase recommendation dicts.
        """
        recs: list[dict[str, Any]] = []

        for service in self._RI_SERVICES:
            for term_api, term_label in self._SP_TERMS:
                for payment_api, payment_label in self._SP_PAYMENTS:
                    scenario = f"({term_label}, {payment_label})"
                    try:
                        resp = ce.get_reservation_purchase_recommendation(
                            Service=service,
                            LookbackPeriodInDays="THIRTY_DAYS",
                            TermInYears=term_api,
                            PaymentOption=payment_api,
                        )
                    except Exception as e:
                        _route_ce_error(
                            ctx,
                            f"ce:GetReservationPurchaseRecommendation[{service}/{scenario}]",
                            e,
                        )
                        continue

                    for rec in resp.get("Recommendations", []):
                        details = rec.get("RecommendationDetails", [])
                        if not details:
                            continue

                        monthly_savings = sum(
                            float(d.get("EstimatedMonthlySavings", "0")) for d in details
                        )
                        upfront_cost = sum(
                            float(d.get("UpfrontCost", "0")) for d in details
                        )
                        instance_type = details[0].get("InstanceType", "Unknown")

                        if monthly_savings <= 0:
                            continue

                        recs.append(
                            {
                                "resource_id": (
                                    f"{service.replace(' ', '')}_RI_{instance_type}_"
                                    f"{term_label}_{payment_label.lower().replace(' ', '_')}"
                                ),
                                "check_type": "purchase",
                                "check_category": (
                                    f"RI Purchase Recommendation {scenario}"
                                ),
                                "term": term_label,
                                "payment_option": payment_label,
                                "service": service,
                                "current_value": "On-Demand",
                                "recommended_value": (
                                    f"Reserved Instance {instance_type} "
                                    f"({term_label}, {payment_label})"
                                ),
                                "monthly_savings": round(monthly_savings, 2),
                                "severity": "LOW",
                                "reason": (
                                    f"{service} RI {instance_type} "
                                    f"{term_label} {payment_label}: "
                                    f"${monthly_savings:.2f}/mo savings, "
                                    f"upfront ${upfront_cost:,.2f}"
                                ),
                                "upfront_cost": upfront_cost,
                            }
                        )

        return recs

    @staticmethod
    def _parse_pct(value: Any) -> float:
        """Parse a percentage value to a 0-1 float.

        Args:
            value: Percentage as string or numeric (e.g. "95.5" or 95.5).

        Returns:
            Float between 0 and 1 (e.g. 0.955).
        """
        try:
            return float(value) / 100.0
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _empty_findings() -> ServiceFindings:
        """Return empty ServiceFindings when Cost Explorer is unavailable.

        Returns:
            ServiceFindings with zero counts and rates.
        """
        return ServiceFindings(
            service_name="Commitment Analysis",
            total_recommendations=0,
            total_monthly_savings=0.0,
            sources={
                "sp_utilization": SourceBlock(count=0, recommendations=(), extras={"overall_utilization_rate": 0.0}),
                "sp_coverage_gaps": SourceBlock(count=0, recommendations=(), extras={"overall_coverage_rate": 0.0}),
                "ri_utilization": SourceBlock(count=0, recommendations=()),
                "ri_coverage_gaps": SourceBlock(count=0, recommendations=()),
                "expiring_commitments": SourceBlock(
                    count=0,
                    recommendations=(),
                    extras={"expiring_30d": 0, "expiring_60d": 0, "expiring_90d": 0},
                ),
                "purchase_recommendations": SourceBlock(count=0, recommendations=()),
            },
            extras={
                "sp_utilization_rate": 0.0,
                "sp_coverage_rate": 0.0,
                "ri_utilization_rate": 0.0,
                "ri_coverage_rate": 0.0,
            },
        )
