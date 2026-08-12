"""Commitment analysis adapter for Savings Plans and Reserved Instance utilization.

Analyzes AWS Cost Explorer data to surface under-utilized commitments,
coverage gaps, expiring commitments, and purchase recommendations.

AWS API cost: Cost Explorer charges $0.01 per API request. This adapter
makes ~61 calls per scan (~$0.61/scan). The calls are:

1. ``get_savings_plans_utilization`` — overall SP utilization rate (1 call)
2. ``get_savings_plans_utilization_details`` — per-SP utilization (1 call).
   Expiry no longer reads CE at all: end timestamps are not on this shape, so
   _check_expiring uses savingsplans:DescribeSavingsPlans (free) — H3.
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
from services.commitment_scenarios import projected_region_split

# Compute Savings Plans cover Fargate (ECS + EKS); ECS-on-EC2 bills as EC2.
# Ephemeral storage and data transfer are NOT SP-eligible.
_SP_DURATION_TO_TERM: dict[int, str] = {31536000: "1yr", 94608000: "3yr"}
_SP_PAYMENT_OPTIONS: tuple[str, ...] = ("No Upfront", "Partial Upfront", "All Upfront")

# Under-utilised commitment is measured waste, but it is SUNK: the commitment
# bills for its whole 1- or 3-year term regardless of usage, unused hourly
# benefit never carries into the next hour, and a plan is returnable only
# within 7 days of purchase, in the same calendar month, at <= $100/hr. No
# action taken this month reduces the bill by this figure, so it is published
# as an advisory and never summed into the counted headline (bnc /
# ap-southeast-1, 2026-08-12: $390.32/mo, 13.8% of the headline, against $0 of
# in-family uncovered on-demand that could have absorbed it).
_SUNK_COMMITMENT_NOTE: str = (
    "This is a sunk cost, not a realizable saving: the commitment bills for its full "
    "term whether or not it is used and cannot be cancelled, so it is shown as an "
    "advisory and excluded from the counted total. The lever is to right-size the "
    "commitment at renewal, or to move matching on-demand usage onto it."
)


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

    CE API cost: ~$0.61 per scan (~61 calls at $0.01 each including the full
    RI (6 services) and SP (3 types) purchase matrices, each across 2 terms
    x 3 payment options).
    """

    key: str = "commitment_analysis"
    cli_aliases: tuple[str, ...] = ("commitment_analysis", "commitments", "savings_plans", "ri")
    display_name: str = "Commitment Analysis"

    UTILIZATION_THRESHOLD: float = 0.95

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
        region_share, region_total, offregion = projected_region_split(
            purchase_cards, getattr(ctx, "region", "")
        )
        fargate_sp_recs, fargate_sp_extras = self._check_fargate_savings_plan(ctx, ce, tp)

        # Commitment "buy" recs (CE matrix + unmatched CoH RI/SP recs) and
        # coverage gaps are a SEPARATE lever from rightsizing — they overlap
        # per-service rightsizing/Graviton recs on the SAME resource (e.g. an
        # RDS RI vs downsizing it), so summing both double-counts. Advisory
        # (Counted=False): shown, never summed. Under-utilization is likewise
        # advisory — it is measured waste but a SUNK cost (_SUNK_COMMITMENT_NOTE),
        # and marks itself Counted=False at emit. CoH recs merged into a card's
        # coh_concurs_monthly by _fetch_purchase_cards are not re-emitted here.
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
                # AFS-3 — CE's purchase-recommendation APIs are ACCOUNT-scoped
                # and take no region filter, so this projection routinely covers
                # regions this report does not. Publish the scan region's share
                # and which other regions appear, so the summary can say so and
                # a reader cannot add two regions' projections together.
                "projected_commitment_scan_region_share": region_share,
                "projected_commitment_total_ri": region_total,
                "projected_commitment_offregion": list(offregion),
                # None (JSON null) — not 0.0 — when there is no coverage data
                # to sum: a measured $0 must mean "zero uncovered on-demand",
                # never "coverage was unavailable".
                "uncovered_ondemand_monthly_total": (
                    round(sum(coverage.uncovered_on_demand.values()), 2)
                    if coverage is not None and coverage.uncovered_on_demand else None
                ),
            },
        )

    def _check_sp_utilization(self, ctx: Any, ce: Any, tp: dict[str, str]) -> tuple[list[dict[str, Any]], float | None]:
        """Check Savings Plans utilization rate and flag under-utilized plans.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.

        Returns:
            Tuple of (under-utilized recommendations list, overall rate 0-1).
        """
        recs: list[dict[str, Any]] = []
        overall_rate: float | None = None

        try:
            resp = ce.get_savings_plans_utilization(TimePeriod=tp)
            # REAL response shape (live-pinned on bnc, 2026-08-09): the figures
            # nest under Total.Utilization — the previous read of a nonexistent
            # "SavingsPlansUtilizations" top key silently stuck the rate at 0.0
            # while the account ran an active $1,953/mo commitment at 82.5%.
            util = resp.get("Total", {}).get("Utilization", {})
            if float(util.get("TotalCommitment", 0) or 0) > 0:
                overall_rate = self._parse_pct(util.get("UtilizationPercentage", "0"))
            # No commitment held -> None (stat renders "n/a", never a fake 0%).
        except Exception as e:
            _route_ce_error(ctx, "ce:GetSavingsPlansUtilization", e)
            return recs, overall_rate

        try:
            params: dict[str, Any] = {"TimePeriod": tp}
            while True:
                details = ce.get_savings_plans_utilization_details(**params)
                # Key is SavingsPlansUtilizationDetails (the old extra-"s"
                # spelling matched nothing, so per-SP waste never surfaced —
                # bnc: $342.49/mo unused commitment unreported).
                for detail in details.get("SavingsPlansUtilizationDetails", []):
                    sp_arn = detail.get("SavingsPlanArn", "")
                    d_util = detail.get("Utilization", {})
                    rate = self._parse_pct(d_util.get("UtilizationPercentage", "0"))

                    if rate < self.UTILIZATION_THRESHOLD:
                        # The window's UnusedCommitment IS the wasted spend for
                        # the 30-day period (~monthly) — measured, not derived.
                        waste = float(d_util.get("UnusedCommitment", 0) or 0)
                        recs.append(
                            {
                                "resource_id": sp_arn,
                                "check_type": "sp_utilization",
                                "check_category": "SP Under-utilization",
                                "current_value": f"{rate:.1%}",
                                "recommended_value": f"{self.UTILIZATION_THRESHOLD:.0%}+",
                                # SUNK COST — advisory, never counted. See
                                # _SUNK_COMMITMENT_NOTE.
                                "monthly_savings": 0.0,
                                "Counted": False,
                                "AdvisoryEstimate": round(waste, 2),
                                "EstimatedSavings": "$0.00/month — advisory (not counted toward total)",
                                "severity": "HIGH" if rate < 0.50 else "MEDIUM",
                                "reason": (
                                    f"Savings Plan utilized at {rate:.1%} (below "
                                    f"{self.UTILIZATION_THRESHOLD:.0%} threshold): "
                                    f"${waste:,.2f}/mo of commitment is going unused. "
                                    + _SUNK_COMMITMENT_NOTE
                                ),
                            }
                        )
                next_token = details.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token
        except Exception as e:
            _route_ce_error(ctx, "ce:GetSavingsPlansUtilizationDetails", e)

        return recs, overall_rate

    def _check_sp_coverage(self, ctx: Any, ce: Any, tp: dict[str, str]) -> tuple[list[dict[str, Any]], float | None]:
        """Account-wide Savings Plans coverage rate. Emits no recommendations.

        LS-2 — this used to emit one "SP Coverage Gap" card per under-covered
        service, carrying ``od * (1.0 - rate) * AVG_SP_DISCOUNT_RATE`` where
        that last factor was a flat ``0.30``. Three defects in one expression:

        * the flat ``0.30`` was a fabricated fraction with no account-specific
          input — the ATH-1 defect class, and the rec's own reason string
          conceded "verify against live offering rates";
        * ``(1.0 - rate)`` double-discounted. CE's ``OnDemandCost`` is ALREADY
          the uncovered residue (``TotalCost == OnDemandCost +
          SpendCoveredBySavingsPlans``, and ``CoveragePercentage ==
          covered / TotalCost`` — both pinned by the live fixture in
          ``tests/test_commitment_utilization.py``), so on a row with $200
          uncovered at 88.9% coverage the old formula reported $22.20;
        * the result was stored under ``monthly_savings``, which
          ``reporter_phase_b._advisory_line`` never reads — it looks at
          ``AdvisoryEstimate`` / ``PotentialMonthlySavings``. The figure was
          silently dropped, which is why the defect survived unnoticed.

        Removing the projection leaves a card with no dollar: a service name, a
        coverage percentage and an "80%+" target. That is a best-practice
        nudge, and this project's scope is strictly cost — every emitted
        recommendation must produce a concrete account-specific $ saving. So
        the emission goes, as the MediaStore storage lever and the OpenSearch
        version-upgrade nudges did.

        ``OnDemandCost`` cannot rescue the card. CE documents it as the cost of
        usage *at the public On-Demand rate* — a rate equivalent, not billed
        spend, and this query is not region-filtered. Live on 597637668689 the
        EC2 row read $13.37 while actual unblended on-demand was ~$0.0000009
        (free tier). Rendering it would contradict the "Uncovered On-Demand
        ($/mo)" stat card on this same tab, which is built from
        ``UnblendedCost`` filtered by REGION and PURCHASE_TYPE.

        What remains is the honest half: the account-wide coverage RATE behind
        the "SP Coverage" stat card. A stat is context, not a recommendation.
        The accumulators below therefore run for EVERY returned service,
        including ones that would previously have been skipped — moving a
        suppression above them would silently redefine that stat card.

        Coverage gap (filed, LS-7): SP coverage for the DATABASE_SP services
        (DynamoDB, RDS/Aurora, ElastiCache, OpenSearch, Neptune, DocumentDB,
        Keyspaces, Timestream, DMS) is now unattributed, because
        ``commitment_scenarios.SP_TYPES`` fetches only COMPUTE_SP /
        EC2_INSTANCE_SP / SAGEMAKER_SP. Database Savings Plans went GA
        2025-12 and ``DATABASE_SP`` is in the live CE enum, but adding it needs
        its own verification: that plan ships a 1-year No-Upfront term only, so
        the existing 2-term x 3-payment matrix cannot be fanned out blindly.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.

        Returns:
            Tuple of (always-empty recommendations list, overall rate 0-1).
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
                    covered = float(cov.get("SpendCoveredBySavingsPlans", "0"))  # real CE key (was nonexistent "CoveredCost")
                    # Accumulate only — no per-service card is emitted (LS-2,
                    # see the docstring). These two lines feed the "SP Coverage"
                    # stat card and must stay unconditional: a gate placed here
                    # would report account-wide coverage over a filtered subset
                    # of services under the same label.
                    total_od += od
                    total_covered += covered
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

    def _check_ri_utilization(self, ctx: Any, ce: Any, tp: dict[str, str]) -> tuple[list[dict[str, Any]], float | None]:
        """Check Reserved Instance utilization rate by service.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.
            pricing_multiplier: Regional pricing multiplier.

        Returns:
            Tuple of (under-utilized RI recommendations list, overall rate 0-1
            or None when no reservation is visible in the window).
        """
        recs: list[dict[str, Any]] = []
        overall_rate: float | None = None

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
                        rid = (
                            attrs.get("subscriptionId")
                            or group.get("Value")  # the documented identifier slot
                            or next(iter(attrs.values()), "Reserved Instance")
                        )
                        if rate < self.UTILIZATION_THRESHOLD:
                            # H4 — ReservationAggregates has NO TotalAmortizedCost
                            # member (the old read yielded 0.0 on every account:
                            # real RI waste never surfaced AND every rec was an
                            # emitted counted-$0 row). Prefer the MEASURED
                            # RICostForUnusedHours; fall back to the derived
                            # TotalAmortizedFee x (1 - rate).
                            unused_str = util.get("RICostForUnusedHours")
                            if unused_str is not None:
                                waste = float(unused_str or 0)
                            else:
                                waste = float(util.get("TotalAmortizedFee", "0") or 0) * (1.0 - rate)
                            if waste <= 1.0:
                                # Immaterial or fields absent — never a counted-$0 row (D4).
                                continue
                            recs.append(
                                {
                                    "resource_id": str(rid),
                                    "check_type": "ri_utilization",
                                    "check_category": "RI Under-utilization",
                                    "current_value": f"{rate:.1%}",
                                    "recommended_value": f"{self.UTILIZATION_THRESHOLD:.0%}+",
                                    # SUNK COST — advisory, never counted. An RI
                                    # is as non-cancellable as a Savings Plan.
                                    "monthly_savings": 0.0,
                                    "Counted": False,
                                    "AdvisoryEstimate": round(waste, 2),
                                    "EstimatedSavings": "$0.00/month — advisory (not counted toward total)",
                                    "severity": "HIGH" if rate < 0.50 else "MEDIUM",
                                    "reason": (
                                        f"Reserved Instance {rid} utilized at {rate:.1%} (below "
                                        f"{self.UTILIZATION_THRESHOLD:.0%} threshold): "
                                        f"${waste:,.2f}/mo of reservation is going unused. "
                                        + _SUNK_COMMITMENT_NOTE
                                    ),
                                }
                            )

                next_token = resp.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token

            total = resp.get("Total", {})
            if float(total.get("PurchasedHours", 0) or 0) > 0:
                overall_rate = self._parse_pct(total.get("UtilizationPercentage", "0"))
            # Zero purchased hours (no RIs visible to this role/account view)
            # -> None so the stat renders "n/a", never a fabricated 0%.
        except Exception as e:
            _route_ce_error(ctx, "ce:GetReservationUtilization", e)

        return recs, overall_rate

    # Coverage gap (intentional): this only ever returns recs=[] plus the
    # overall stat-card rate. GetReservationCoverage rejects a SERVICE DIMENSION
    # groupBy, so no per-service RI coverage-gap rec is emitted — and such a rec
    # would overlap the RI purchase recommendations anyway.
    def _check_ri_coverage(self, ctx: Any, ce: Any, tp: dict[str, str]) -> tuple[list[dict[str, Any]], float | None]:
        """Check Reserved Instance coverage by service.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.

        Returns:
            Tuple of (RI coverage gap recommendations list, overall rate 0-1
            or None when no reservation-eligible hours ran in the window).
        """
        recs: list[dict[str, Any]] = []
        overall_rate: float | None = None

        try:
            # GetReservationCoverage rejects a SERVICE DIMENSION groupBy (only
            # AZ / INSTANCE_TYPE(_FAMILY) / REGION / PLATFORM / TENANCY etc. are
            # valid). The per-service coverage-gap rec overlaps the purchase
            # recommendations anyway, so we take the overall coverage rate only
            # (used by the stat card) without a groupBy.
            resp = ce.get_reservation_coverage(TimePeriod=tp)
            # Real shape: Total.CoverageHours.CoverageHoursPercentage (the flat
            # "CoveragePercentage" key never existed -> rate was stuck at 0.0).
            hours = resp.get("Total", {}).get("CoverageHours", {})
            if float(hours.get("TotalRunningHours", 0) or 0) > 0:
                overall_rate = self._parse_pct(hours.get("CoverageHoursPercentage", "0"))
        except Exception as e:
            _route_ce_error(ctx, "ce:GetReservationCoverage", e)

        return recs, overall_rate

    # Coverage gap (intentional): expiry detection is Savings-Plans-only —
    # RI expirations would come from ec2:DescribeReservedInstances `End`,
    # which is not queried here.
    def _check_expiring(self, ctx: Any, ce: Any, tp: dict[str, str]) -> list[dict[str, Any]]:
        """$0 advisories for Savings Plans expiring within 90 days.

        H3 — the previous implementation read a MISSPELLED response key
        (``SavingsPlansUtilizationsDetails``) and a field
        (``EndDateTime``) that ``SavingsPlansUtilizationDetail`` does not
        have, so it could never emit anything on any account. End dates
        actually live on ``savingsplans:DescribeSavingsPlans`` (``end``) —
        the API the coverage prefetch already uses.
        """
        _ = (ce, tp)  # CE utilization shapes carry no end timestamps.
        recs: list[dict[str, Any]] = []
        now = datetime.now(UTC)

        try:
            sp_client = ctx.client("savingsplans")
        except Exception:
            return recs
        if not sp_client:
            return recs

        try:
            params: dict[str, Any] = {"states": ["active"]}
            while True:
                resp = sp_client.describe_savings_plans(**params)
                for sp in resp.get("savingsPlans", []):
                    sp_id = sp.get("savingsPlanId") or sp.get("savingsPlanArn", "")
                    end_str = str(sp.get("end") or "")
                    if not end_str:
                        continue
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        continue

                    days_left = (end_dt - now).days
                    # states=["active"] should preclude a past end date;
                    # never render a negative countdown if one slips through.
                    if 0 <= days_left <= 90:
                        severity = "HIGH" if days_left <= 30 else ("MEDIUM" if days_left <= 60 else "LOW")
                        recs.append(
                            {
                                "resource_id": sp_id,
                                "check_type": "expiry",
                                "check_category": "Expiring Commitment",
                                "current_value": f"{days_left} days remaining",
                                "recommended_value": "Plan renewal or migration",
                                "monthly_savings": 0.0,
                                # Born-advisory: an expiry alert is a date, not
                                # a counted resource saving (D4).
                                "Counted": False,
                                "severity": severity,
                                "reason": (
                                    f"Savings Plan {sp_id} "
                                    f"({sp.get('savingsPlanType', 'unknown type')}) expires in "
                                    f"{days_left} days ({end_dt.strftime('%Y-%m-%d')})"
                                ),
                            }
                        )
                token = resp.get("nextToken")
                # Only follow a REAL continuation token: a non-str truthy value
                # (e.g. a test double's auto-attribute) would loop forever.
                if not isinstance(token, str) or not token:
                    break
                params["nextToken"] = token
        except Exception as e:
            _route_ce_error(ctx, "savingsplans:DescribeSavingsPlans", e)

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
                "sp_utilization_rate": None,
                "sp_coverage_rate": None,
                "ri_utilization_rate": None,
                "ri_coverage_rate": None,
                "projected_commitment_monthly_savings": 0.0,
                "projected_commitment_basis": "",
                "uncovered_ondemand_monthly_total": None,
            },
        )
