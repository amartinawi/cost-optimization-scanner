"""Commitment analysis adapter for Savings Plans and Reserved Instance utilization.

Analyzes AWS Cost Explorer data to surface under-utilized commitments,
coverage gaps, expiring commitments, and purchase recommendations.

AWS API cost: Cost Explorer charges $0.01 per API request. This adapter
makes ~62 calls per scan (~$0.62/scan). The calls are:

1. ``get_savings_plans_utilization`` — overall SP utilization rate (1 call)
2. ``get_savings_plans_utilization_details`` — per-SP utilization and the
   expiry scan (2 calls: one in _check_sp_utilization, one in _check_expiring)
3. ``get_savings_plans_coverage`` — SP coverage rate by service (1 call)
4. ``get_reservation_utilization`` — RI utilization rate (1 call)
5. ``get_reservation_coverage`` — RI coverage rate (1 call)
6. ``get_savings_plans_purchase_recommendation`` — SP purchase matrix
   (18 calls: 3 SP types x 2 terms x 3 payment options)
7. ``get_reservation_purchase_recommendation`` — RI purchase matrix
   (36 calls: 6 RI services x 2 terms x 3 payment options)
8. ``get_savings_plans_purchase_recommendation`` — account coverage-ratio
   proxy for the Fargate SP view (1 call)
9. ``get_cost_and_usage`` — SP-eligible Fargate on-demand legs (1 call)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule
from services.commitment_logic import DEFAULT_COVERAGE_RATIO, fargate_sp_analysis
from services.commitment_purchase_fetch import fetch_purchase_cards

# Compute Savings Plans cover Fargate (ECS + EKS); ECS-on-EC2 bills as EC2.
# Ephemeral storage and data transfer are NOT SP-eligible.
_SP_DURATION_TO_TERM: dict[int, str] = {31536000: "1yr", 94608000: "3yr"}
_SP_PAYMENT_OPTIONS: tuple[str, ...] = ("No Upfront", "Partial Upfront", "All Upfront")


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
        if code == "DataUnavailableException":
            # LW-06: DataUnavailableException is the EXPECTED response when the
            # account holds no Savings Plans / Reserved capacity for the queried
            # window — there is simply no utilization data, not a fault. Emit an
            # informational note rather than an error-styled warning.
            ctx.warn(
                f"{action}: no data — account has no active Savings Plans / Reserved capacity in the window",
                "commitment_analysis",
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

    CE API cost: ~$0.62 per scan (~62 calls at $0.01 each including the full
    RI (6 services) and SP (3 types) purchase matrices, each across 2 terms
    x 3 payment options).
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
        StatCardSpec(label="RI Coverage", source_path="extras.ri_coverage_rate", formatter="percent"),
    )

    grouping = GroupingSpec(by="check_category")

    requires_cloudwatch: bool = False

    def required_clients(self) -> tuple[str, ...]:
        """Returns Cost Explorer and SavingsPlans client names."""
        return ("ce", "savingsplans")

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

        ce = ctx.client("ce")
        if not ce:
            ctx.warn("Cost Explorer client unavailable; commitment analysis skipped", "commitment_analysis")
            return self._empty_findings()

        tp = _time_period()

        sp_util_recs, sp_util_rate = self._check_sp_utilization(ctx, ce, tp)
        sp_cov_recs, sp_cov_rate = self._check_sp_coverage(ctx, ce, tp)
        ri_util_recs, ri_util_rate = self._check_ri_utilization(ctx, ce, tp)
        ri_cov_recs, ri_cov_rate = self._check_ri_coverage(ctx, ce, tp)
        expiry_recs = self._check_expiring(ctx, ce, tp)
        purchase_cards, projected, basis, cost_hub_recs = self._fetch_purchase_cards(ctx, ce)
        fargate_sp_recs, fargate_sp_extras = self._check_fargate_savings_plan(ctx, ce, tp)

        # Commitment "buy" recs (CE matrix + unmatched CoH RI/SP recs) and
        # coverage gaps are a SEPARATE lever from rightsizing — they overlap
        # per-service rightsizing/Graviton recs on the SAME resource (e.g. an
        # RDS RI vs downsizing it), so summing both double-counts. Advisory
        # (Counted=False): shown, never summed. Existing-commitment waste
        # (under-utilization, expiring) stays counted. CoH recs merged into a
        # card's coh_concurs_monthly by _fetch_purchase_cards are not
        # re-emitted here.
        for r in sp_cov_recs + ri_cov_recs + cost_hub_recs:
            r["Counted"] = False

        all_recs = sp_util_recs + sp_cov_recs + ri_util_recs + ri_cov_recs + expiry_recs + purchase_cards
        total_savings = sum(r.get("monthly_savings", 0.0) for r in all_recs if r.get("Counted", True))

        exp_30 = sum(1 for r in expiry_recs if r.get("severity") == "HIGH")
        exp_60 = sum(1 for r in expiry_recs if r.get("severity") == "MEDIUM")
        exp_90 = sum(1 for r in expiry_recs if r.get("severity") == "LOW")

        coverage = getattr(ctx, "commitment_coverage", None)

        return ServiceFindings(
            service_name="Commitment Analysis",
            total_recommendations=len(all_recs) + len(cost_hub_recs),
            total_monthly_savings=round(total_savings, 2),
            sources={
                "cost_optimization_hub": SourceBlock(count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)),
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
                    count=len(purchase_cards),
                    recommendations=tuple(purchase_cards),
                ),
                "fargate_savings_plan": SourceBlock(
                    count=len(fargate_sp_recs),
                    recommendations=tuple(fargate_sp_recs),
                    extras=fargate_sp_extras,
                ),
            },
            extras={
                "sp_utilization_rate": sp_util_rate,
                "sp_coverage_rate": sp_cov_rate,
                "ri_utilization_rate": ri_util_rate,
                "ri_coverage_rate": ri_cov_rate,
                "projected_commitment_monthly_savings": projected,
                "projected_commitment_basis": basis,
                # None (JSON null) — not 0.0 — when there is no coverage data
                # to sum: a measured $0 must mean "zero uncovered on-demand",
                # never "coverage was unavailable".
                "uncovered_ondemand_monthly_total": (
                    round(sum(coverage.uncovered_on_demand.values()), 2)
                    if coverage is not None and coverage.uncovered_on_demand else None
                ),
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
                        # An unattributable coverage gap (CE returned no service —
                        # typical when the account holds NO active Savings Plans, so
                        # all on-demand spend aggregates under "Unknown") is not
                        # account-specific or actionable: the concrete buy scenarios
                        # already come from purchase_recommendations. Emitting a
                        # flat-30%-of-everything "$X potential" tied to "Unknown" is
                        # misleading noise (it can exceed the whole counted headline),
                        # so skip it. The spend still fed overall_rate above.
                        if not svc or svc == "Unknown":
                            continue
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
                                "reason": f"{svc} has {rate:.1%} SP coverage (below {self.COVERAGE_GAP_THRESHOLD:.0%} threshold; potential estimated using {self.AVG_SP_DISCOUNT_RATE:.0%} flat avg SP discount — verify against live offering rates)",
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
            # GetReservationUtilization only supports SUBSCRIPTION_ID for a
            # DIMENSION groupBy (SERVICE raises ValidationException), so group
            # per-RI subscription to flag individual under-utilized reservations.
            params: dict[str, Any] = {
                "TimePeriod": tp,
                "GroupBy": [{"Type": "DIMENSION", "Key": "SUBSCRIPTION_ID"}],
            }
            while True:
                resp = ce.get_reservation_utilization(**params)
                utilizations = resp.get("UtilizationsByTime", [])
                for entry in utilizations:
                    groups = entry.get("Groups", [])
                    for group in groups:
                        util = group.get("Utilization", {})
                        rate = self._parse_pct(util.get("UtilizationPercentage", "0"))
                        attrs = group.get("Attributes", {})
                        rid = attrs.get("subscriptionId") or next(iter(attrs.values()), "Reserved Instance")
                        if rate < self.UTILIZATION_THRESHOLD:
                            total_cost = float(util.get("TotalAmortizedCost", "0"))
                            waste = total_cost * (1.0 - rate)
                            recs.append(
                                {
                                    "resource_id": str(rid),
                                    "check_type": "ri_utilization",
                                    "check_category": "RI Under-utilization",
                                    "current_value": f"{rate:.1%}",
                                    "recommended_value": f"{self.UTILIZATION_THRESHOLD:.0%}+",
                                    "monthly_savings": round(waste, 2),
                                    "severity": "HIGH" if rate < 0.50 else "MEDIUM",
                                    "reason": f"Reserved Instance {rid} utilized at {rate:.1%} (below {self.UTILIZATION_THRESHOLD:.0%} threshold)",
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

    # Coverage gap (intentional): this only ever returns recs=[] plus the
    # overall stat-card rate. GetReservationCoverage rejects a SERVICE DIMENSION
    # groupBy, so no per-service RI coverage-gap rec is emitted — and such a rec
    # would overlap the RI purchase recommendations anyway.
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
            # GetReservationCoverage rejects a SERVICE DIMENSION groupBy (only
            # AZ / INSTANCE_TYPE(_FAMILY) / REGION / PLATFORM / TENANCY etc. are
            # valid). The per-service coverage-gap rec overlaps the purchase
            # recommendations anyway, so we take the overall coverage rate only
            # (used by the stat card) without a groupBy.
            resp = ce.get_reservation_coverage(TimePeriod=tp)
            total = resp.get("Total", {})
            overall_rate = self._parse_pct(total.get("CoveragePercentage", "0"))
        except Exception as e:
            _route_ce_error(ctx, "ce:GetReservationCoverage", e)

        return recs, overall_rate

    # Coverage gap (intentional): expiry detection is Savings-Plans-only.
    # GetSavingsPlansUtilizationDetails exposes per-SP end timestamps; there is
    # no Reserved Instance equivalent queried here, so RI expirations are not
    # flagged.
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

    def _fetch_purchase_cards(
        self, ctx: Any, ce: Any
    ) -> tuple[list[dict[str, Any]], float, str, list[dict[str, Any]]]:
        """Delegate the CE purchase-matrix fan-out to the fetch module.

        See ``services/commitment_purchase_fetch.fetch_purchase_cards`` for the
        full contract: (cards, projected_monthly, basis, unmatched_coh_recs).
        """
        return fetch_purchase_cards(ctx, ce, _route_ce_error)

    # ── Fargate Savings Plan view ──────────────────────────────────────────────

    def _check_fargate_savings_plan(
        self, ctx: Any, ce: Any, tp: dict[str, str]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build the Fargate-isolated Compute Savings Plan analysis.

        Isolates Fargate's SP-eligible on-demand spend (which AWS's aggregate
        purchase recommendation does not break out), reconciles it against the
        Containers tab's rightsizing total, and prices the full 2x3 (term x
        payment) matrix from the live SavingsPlans offering rates.

        Returns:
            Tuple of (per-cell advisory recs, source extras with baseline figures).
        """
        legs = self._fargate_legs(ctx, ce, tp)
        if not legs:
            return [], {}

        sp_client = ctx.client("savingsplans", region="us-east-1")
        if not sp_client:
            ctx.warn("SavingsPlans client unavailable; skipping Fargate SP view", "commitment_analysis")
            return [], {}
        rate_matrix = self._fargate_sp_rates(ctx, sp_client)
        if not rate_matrix:
            return [], {}

        # Prefer the Containers adapter's hand-off (full scan); otherwise compute
        # a lightweight ECS-only estimate so an isolated --scan-only
        # commitment_analysis still models the SP against the rightsized baseline.
        handoff = getattr(ctx, "fargate_rightsizing_monthly", None)
        if handoff is None:
            try:
                from services.containers import estimate_fargate_rightsizing_monthly

                handoff = estimate_fargate_rightsizing_monthly(ctx)
            except Exception as e:
                ctx.warn(f"Fargate rightsizing estimate failed: {e}", "commitment_analysis")
                handoff = 0.0
        rightsizing = float(handoff or 0.0)
        coverage = self._account_coverage_ratio(ctx, ce)
        analysis = fargate_sp_analysis(
            legs, rate_matrix, rightsizing_monthly=rightsizing, coverage_ratio=coverage
        )

        recs: list[dict[str, Any]] = []
        for c in analysis["cells"]:
            recs.append(
                {
                    "resource_id": f"fargate-compute-sp-{c['term']}-{c['payment'].replace(' ', '-').lower()}",
                    "check_category": "Fargate Savings Plan",
                    "term": c["term"],
                    "payment": c["payment"],
                    "discount_pct": c["discount_pct"],
                    "ceiling_saving": c["ceiling_saving"],
                    "recommended_saving": c["recommended_saving"],
                    "monthly_savings": 0.0,  # advisory: overlaps account SP rec
                    "Counted": False,
                    "severity": "MEDIUM",
                    "reason": (
                        f"Compute SP ({c['term']} {c['payment']}) on Fargate: {c['discount_pct']}% off; "
                        f"~${c['recommended_saving']:.2f}/mo at {round(analysis['coverage_ratio'] * 100)}% coverage "
                        f"of the rightsized baseline (ceiling ${c['ceiling_saving']:.2f}/mo)"
                    ),
                }
            )

        extras = {
            "eligible_od": analysis["eligible_od"],
            "rightsized_od": analysis["rightsized_od"],
            "rightsizing_monthly": analysis["rightsizing_monthly"],
            "coverage_ratio": analysis["coverage_ratio"],
        }
        return recs, extras

    # Coverage gap (intentional): the leg query filters on SERVICE = "Amazon
    # Elastic Container Service" (ECS) only, so EKS-on-Fargate spend is excluded
    # even though Compute Savings Plans cover both ECS and EKS Fargate (see the
    # module-level note: "Compute Savings Plans cover Fargate (ECS + EKS)").
    def _fargate_legs(self, ctx: Any, ce: Any, tp: dict[str, str]) -> dict[str, dict[str, float]]:
        """Fetch SP-eligible Fargate on-demand cost + usage per usage type, for ctx.region."""
        legs: dict[str, dict[str, float]] = {}
        try:
            resp = ce.get_cost_and_usage(
                TimePeriod=tp,
                Granularity="MONTHLY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                Filter={
                    "And": [
                        {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Container Service"]}},
                        {"Dimensions": {"Key": "REGION", "Values": [ctx.region]}},
                    ]
                },
                GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            )
        except Exception as e:
            _route_ce_error(ctx, "ce:GetCostAndUsage", e)
            return {}

        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                ut = g["Keys"][0]
                # SP-eligible Fargate = vCPU-Hours + GB-Hours legs; exclude
                # ephemeral storage (…EphemeralStorage-GB-Hours) and transfer.
                if "Fargate" not in ut or "Ephemeral" in ut:
                    continue
                if "vCPU-Hours" not in ut and "GB-Hours" not in ut:
                    continue
                try:
                    od = float(g["Metrics"]["UnblendedCost"]["Amount"])
                    qty = float(g["Metrics"]["UsageQuantity"]["Amount"])
                except (KeyError, TypeError, ValueError):
                    continue
                if od <= 0:
                    continue
                entry = legs.setdefault(ut, {"od": 0.0, "qty": 0.0})
                entry["od"] += od
                entry["qty"] += qty
        return legs

    def _fargate_sp_rates(self, ctx: Any, sp_client: Any) -> dict[tuple[str, str], dict[str, float]]:
        """Build {(term, payment): {usage_type: sp_rate}} from live SavingsPlans offering rates."""
        matrix: dict[tuple[str, str], dict[str, float]] = {}
        for payment in _SP_PAYMENT_OPTIONS:
            try:
                results = sp_client.describe_savings_plans_offering_rates(
                    savingsPlanPaymentOptions=[payment],
                    savingsPlanTypes=["Compute"],
                    products=["Fargate"],
                    serviceCodes=["AmazonECS"],
                    filters=[{"name": "region", "values": [ctx.region]}],
                ).get("searchResults", [])
            except Exception as e:
                ctx.warn(f"SavingsPlans offering rates failed ({payment}): {e}", "commitment_analysis")
                continue
            for x in results:
                dur = x.get("savingsPlanOffering", {}).get("durationSeconds")
                term = _SP_DURATION_TO_TERM.get(dur)
                if not term:
                    continue
                try:
                    rate = float(x.get("rate", 0) or 0)
                except (TypeError, ValueError):
                    continue
                matrix.setdefault((term, payment), {})[x.get("usageType", "")] = rate
        return matrix

    def _account_coverage_ratio(self, ctx: Any, ce: Any) -> float:
        """Estimate the steady-baseline coverage ratio from the account's aggregate SP rec.

        Uses HourlyCommitmentToPurchase / average-hourly-on-demand from the
        account-wide Compute SP recommendation as a proxy for how much of the
        Fargate baseline is worth committing. Falls back to the default on error.
        """
        try:
            rec = ce.get_savings_plans_purchase_recommendation(
                SavingsPlansType="COMPUTE_SP",
                TermInYears="ONE_YEAR",
                PaymentOption="NO_UPFRONT",
                LookbackPeriodInDays="THIRTY_DAYS",
            )
            summ = (
                rec.get("SavingsPlansPurchaseRecommendation", {})
                .get("SavingsPlansPurchaseRecommendationSummary", {})
            )
            hourly_commit = float(summ.get("HourlyCommitmentToPurchase", 0) or 0)
            avg_hourly_od = float(summ.get("CurrentOnDemandSpend", 0) or 0) / (30 * 24)
            if avg_hourly_od <= 0:
                return DEFAULT_COVERAGE_RATIO
            return min(1.0, max(0.1, hourly_commit / avg_hourly_od))
        except Exception:
            return DEFAULT_COVERAGE_RATIO

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
                "cost_optimization_hub": SourceBlock(count=0, recommendations=()),
                "sp_utilization": SourceBlock(count=0, recommendations=(), extras={"overall_utilization_rate": 0.0}),
                "sp_coverage_gaps": SourceBlock(count=0, recommendations=(), extras={"overall_coverage_rate": 0.0}),
                "ri_utilization": SourceBlock(count=0, recommendations=()),
                "ri_coverage_gaps": SourceBlock(count=0, recommendations=()),
                "expiring_commitments": SourceBlock(
                    count=0, recommendations=(),
                    extras={"expiring_30d": 0, "expiring_60d": 0, "expiring_90d": 0}),
                "purchase_recommendations": SourceBlock(count=0, recommendations=()),
                "fargate_savings_plan": SourceBlock(count=0, recommendations=(), extras={}),
            },
            extras={
                "sp_utilization_rate": 0.0,
                "sp_coverage_rate": 0.0,
                "ri_utilization_rate": 0.0,
                "ri_coverage_rate": 0.0,
                "projected_commitment_monthly_savings": 0.0,
                "projected_commitment_basis": "",
                "uncovered_ondemand_monthly_total": None,
            },
        )
