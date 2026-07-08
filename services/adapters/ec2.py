"""Multi-source adapter for EC2 with Cost Hub, Compute Optimizer, and enhanced checks."""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._aws_errors import record_aws_error
from services._base import BaseServiceModule
from services._savings import compute_optimizer_savings, parse_dollar_savings
from services.advisor import (
    get_asg_compute_optimizer_recommendations,
    get_ec2_compute_optimizer_recommendations,
)
from services.commitment_coverage import split_by_commitment
from services.ec2 import get_advanced_ec2_checks, get_ec2_instance_count, get_enhanced_ec2_checks

logger = logging.getLogger(__name__)

# ec2 H2 — the enhanced CloudWatch checks only emit these categories when a
# metric read shows genuine low utilization, so an instance appearing under any
# of them is corroborating evidence that its tag-based advanced lever (cron/
# batch/instance-store/non-prod) reflects a real, measured opportunity.
_CW_LOW_UTIL_CATEGORIES: frozenset[str] = frozenset(
    {"Idle Instances", "Rightsizing Opportunities", "Burstable Instance Optimization"}
)


def _coh_is_renderable(rec: dict[str, Any]) -> bool:
    """Mirror the reporter's EC2 Cost-Hub render filter.

    The reporter (``_render_ec2_cost_hub``) drops recs with no ``actionType``
    and recs whose ``finding`` is ``optimized``, plus EBS-volume recs,
    Reserved-Instance purchase recs, and N/A-resource recs from the EC2 table.
    Applying the same predicate here means the savings/count the adapter reports
    match exactly what the EC2 tab renders — no counted-but-not-shown dollars.
    """
    if "actionType" not in rec:
        return False
    if str(rec.get("finding", "")).lower() == "optimized":
        return False
    if "ebsVolume" in (rec.get("currentResourceDetails") or {}):
        return False
    if rec.get("actionType") == "PurchaseReservedInstances":
        return False
    if rec.get("resourceId") == "N/A":
        return False
    return True


def _coh_instance_id(rec: dict[str, Any]) -> str:
    """Instance id for a Cost Optimization Hub EC2 recommendation."""
    return str(rec.get("resourceId", "") or "")


def _coh_instance_type(rec: dict[str, Any]) -> str:
    """Current instance type of a Cost Optimization Hub EC2 recommendation."""
    return str(
        (rec.get("currentResourceDetails") or {})
        .get("ec2Instance", {})
        .get("configuration", {})
        .get("instance", {})
        .get("type", "")
        or ""
    )


def _co_instance_id(rec: dict[str, Any]) -> str:
    """Instance id for a Compute Optimizer EC2 recommendation (from instanceArn)."""
    arn = str(rec.get("instanceArn", "") or "")
    return arn.split("/")[-1] if "/" in arn else arn


def _co_asg_name(rec: dict[str, Any]) -> str:
    """Auto Scaling Group name of a Compute Optimizer EC2 rec, from its tags.

    Cost Optimization Hub surfaces ASG-backed findings under the ASG *name*
    (``_coh_instance_id``), while Compute Optimizer surfaces each member under
    its bare instance id. Without comparing the CO rec's ``aws:autoscaling:
    groupName`` tag against the CoH-covered ASG names, a member of an
    already-covered ASG slips past the dedup and double-counts (EC2 ASG-dedup).
    """
    for tag in rec.get("tags", []) or []:
        if tag.get("key") == "aws:autoscaling:groupName":
            return str(tag.get("value", "") or "")
    return ""


def _co_is_cost_actionable(rec: dict[str, Any]) -> bool:
    """True only for Compute Optimizer EC2 recs that yield a realizable saving.

    Strict-cost scope: ``UNDER_PROVISIONED`` is a performance upsize (the
    instance is resource-starved and CO recommends a *larger* type) — not a cost
    reduction, so it is excluded even when a recommended option is marginally
    cheaper. A non-``running`` instance has no live compute cost, so its
    rightsizing saving is not realizable. Only downsizes on running instances
    are cost recommendations.
    """
    if str(rec.get("finding", "")).upper() == "UNDER_PROVISIONED":
        return False
    state = str(rec.get("instanceState", "") or "").lower()
    if state and state != "running":
        return False
    return True


def _asg_member_instance_ids(ctx: Any) -> set[str]:
    """Instance ids that belong to an Auto Scaling Group.

    ASG members are sized via their launch template and covered by ASG Compute
    Optimizer, so per-instance heuristics must defer to that source rather than
    recommend rightsizing an individual managed instance.

    H1 — a *silent* empty set re-enables the per-instance heuristics AND the
    ``asg_compute_optimizer`` block on managed instances (double-counting managed
    dollars). On a failed read we classify the error so the degraded dedup is
    visible, and return the partial set gathered so far rather than wiping it.
    """
    ids: set[str] = set()
    try:
        autoscaling = ctx.client("autoscaling")
        if not autoscaling:
            return ids
        paginator = autoscaling.get_paginator("describe_auto_scaling_groups")
        for page in paginator.paginate():
            for group in page.get("AutoScalingGroups", []):
                for member in group.get("Instances", []):
                    iid = member.get("InstanceId")
                    if iid:
                        ids.add(iid)
    except Exception as e:
        record_aws_error(
            ctx,
            e,
            service="ec2",
            context="autoscaling:DescribeAutoScalingGroups failed (ASG-member dedup degraded)",
        )
    return ids


class EC2Module(BaseServiceModule):
    """ServiceModule adapter for EC2. Multi-source savings strategy."""

    key: str = "ec2"
    cli_aliases: tuple[str, ...] = ("ec2",)
    display_name: str = "EC2"
    reads_fast_mode: bool = True
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for EC2 scanning."""
        return ("ec2", "compute-optimizer", "autoscaling")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan EC2 instances for cost optimization opportunities.

        Consults Cost Optimization Hub, Compute Optimizer, enhanced checks,
        and advanced EC2 service modules. Savings aggregated from all sources
        using estimatedMonthlySavings and parse_dollar_savings.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with "cost_optimization_hub", "compute_optimizer",
            "enhanced_checks", and "advanced_ec2_checks" SourceBlock entries.
        """
        logger.debug("EC2 adapter scan starting")

        # --- Gather raw recommendations from every source ----------------------
        cost_hub_recs = [r for r in ctx.cost_hub_splits.get("ec2", []) if _coh_is_renderable(r)]
        co_raw = get_ec2_compute_optimizer_recommendations(ctx)
        # The advisor returns a synthetic $0 "enable Compute Optimizer" placeholder
        # when CO is not opted in. That is an informational signal, not a cost
        # recommendation — surface it as a warning instead of a $0-savings finding
        # that would inflate the recommendation count.
        if any(r.get("ResourceId") == "compute-optimizer-service" for r in co_raw):
            ctx.warn(
                "AWS Compute Optimizer is not enabled — EC2 rightsizing recommendations from "
                "Compute Optimizer are unavailable (enable it for additional savings detection).",
                service="ec2",
            )
        # Strict-cost scope: keep only realizable downsizes (drop UNDER_PROVISIONED
        # performance upsizes and non-running instances — EC2 scope filter).
        co_recs_all = [
            r
            for r in co_raw
            if r.get("ResourceId") != "compute-optimizer-service" and _co_is_cost_actionable(r)
        ]
        asg_co_recs = get_asg_compute_optimizer_recommendations(ctx)
        enhanced_recs = get_enhanced_ec2_checks(ctx, ctx.pricing_multiplier, ctx.fast_mode).get(
            "recommendations", []
        )
        # ec2 H2 — the four tag-based advanced levers (cron/batch/instance-store/
        # non-prod) may only count a dollar when corroborated by a measured
        # low-utilization signal. The CloudWatch idle/rightsizing/burstable checks
        # only emit those categories when CW shows low util, so an instance present
        # under one of them IS the corroboration. (In fast_mode no CW runs, so the
        # set is empty and every tag lever is advisory — the honest outcome.)
        corroborated_ids = frozenset(
            str(r.get("InstanceId", "") or "")
            for r in enhanced_recs
            if r.get("CheckCategory") in _CW_LOW_UTIL_CATEGORIES and r.get("InstanceId")
        )
        advanced_recs = get_advanced_ec2_checks(
            ctx, ctx.pricing_multiplier, ctx.fast_mode, corroborated_ids
        ).get("recommendations", [])

        # --- Cross-source de-duplication by instance id ------------------------
        # Cost Optimization Hub surfaces Compute Optimizer's own rightsizing
        # findings, and the heuristic CloudWatch checks re-detect the same
        # instances. Counting all three would inflate savings 2-3x for one
        # instance. Authority order: Cost Hub > Compute Optimizer > heuristics.
        covered: set[str] = {_coh_instance_id(r) for r in cost_hub_recs if _coh_instance_id(r)}

        # Drop CO recs already owned by CoH — by bare instance id AND by the rec's
        # ASG name (CoH surfaces ASG findings under the group name, so a CO rec for
        # a member of a CoH-covered ASG must defer; previously it slipped past the
        # id-only check and double-counted — EC2 ASG-dedup).
        co_recs = [
            r
            for r in co_recs_all
            if _co_instance_id(r) not in covered and _co_asg_name(r) not in covered
        ]
        covered |= {_co_instance_id(r) for r in co_recs if _co_instance_id(r)}

        # ASG members defer to ASG Compute Optimizer / launch-template sizing —
        # never rightsize a managed instance individually.
        covered |= _asg_member_instance_ids(ctx)

        # ASG CO recs: drop the non-actionable ones (NOT_OPTIMIZED / $0, which
        # carry no opportunity yet inflate the count) and any whose ASG is already
        # owned by CoH (the resource_id is the ASG name — same key as `covered`),
        # so the ASG block neither double-counts nor pads the headline count.
        asg_co_recs = [
            r
            for r in asg_co_recs
            if float(r.get("estimatedMonthlySavings", 0.0) or 0.0) > 0
            and r.get("resource_id", "") not in covered
        ]

        # Heuristic recs (enhanced + advanced): drop any instance already covered
        # by an AWS source, then keep at most ONE finding per instance — the
        # highest-savings one — so overlapping checks (idle + prev-gen + cron …)
        # on the same instance never stack.
        # ec2 H2 — advisory advanced recs (Counted=False) still render as visible
        # architectural nudges but never compete for the per-instance slot and are
        # never summed; only counted heuristics contend in best_by_instance.
        counted_advanced = [r for r in advanced_recs if r.get("Counted", True) is not False]
        advisory_advanced = [r for r in advanced_recs if r.get("Counted", True) is False]

        best_by_instance: dict[str, tuple[str, dict[str, Any], float]] = {}
        for origin, rec in (
            [("enhanced", r) for r in enhanced_recs] + [("advanced", r) for r in counted_advanced]
        ):
            iid = str(rec.get("InstanceId", "") or "")
            if iid and iid in covered:
                continue
            sav = parse_dollar_savings(rec.get("EstimatedSavings", ""))
            if sav <= 0:
                continue
            key = iid or f"_anon_{id(rec)}"
            existing = best_by_instance.get(key)
            if existing is None or sav > existing[2]:
                best_by_instance[key] = (origin, rec, sav)

        enhanced_final = [rec for origin, rec, _ in best_by_instance.values() if origin == "enhanced"]
        advanced_counted_final = [rec for origin, rec, _ in best_by_instance.values() if origin == "advanced"]
        # Advisory recs render unless the instance is already owned by an AWS source
        # (CoH/CO/ASG). They carry "$0.00" EstimatedSavings, so the savings sum below
        # leaves them at $0 — rendered, never counted.
        advisory_final = [r for r in advisory_advanced if str(r.get("InstanceId", "") or "") not in covered]

        # --- Active-commitment demotion ----------------------------------------
        # Rightsizing / Graviton-migration / idle recs are computed on an
        # on-demand basis. When an active Savings Plan or Reserved Instance
        # already covers the instance's family, that figure is NOT the realizable
        # saving: family-locked EC2-Instance SPs strand on cross-family migration
        # (e.g. m5 -> r6g), and freed same-family commitment only saves if
        # reabsorbed. Demote covered recs to advisory (Counted=False) so they
        # still render with their indicative on-demand figure but never inflate
        # the counted headline. Empty/absent coverage → no demotion (counts as
        # before), so accounts with no commitments are unaffected.
        coverage = getattr(ctx, "commitment_coverage", None)
        demote = coverage is not None and coverage.has_any_commitment

        def _coh_gross(r: dict[str, Any]) -> float:
            return float(r.get("estimatedMonthlySavings", 0.0) or 0.0)

        def _heur_gross(r: dict[str, Any]) -> float:
            return parse_dollar_savings(r.get("EstimatedSavings", ""))

        def _split(recs: list[dict[str, Any]], family_of, gross_of):
            if not demote or coverage is None:
                return list(recs), []
            cov = coverage  # non-optional local so the closures type-narrow
            return split_by_commitment(
                recs,
                is_covered=lambda r: cov.covers_ec2(family_of(r)),
                gross_of=gross_of,
                note_of=lambda r, g: cov.ec2_note(family_of(r), g),
            )

        def _co_type(r: dict[str, Any]) -> str:
            return str(r.get("currentInstanceType", "") or "")

        def _heur_type(r: dict[str, Any]) -> str:
            return str(r.get("InstanceType", "") or "")

        coh_counted, coh_adv = _split(cost_hub_recs, _coh_instance_type, _coh_gross)
        co_counted, co_adv = _split(co_recs, _co_type, compute_optimizer_savings)
        asg_counted, asg_adv = _split(asg_co_recs, _co_type, _coh_gross)
        enh_counted, enh_adv = _split(enhanced_final, _heur_type, _heur_gross)
        adv_counted, adv_cov_adv = _split(advanced_counted_final, _heur_type, _heur_gross)

        # --- Savings (counted recs only; each instance counted once) -----------
        savings = 0.0
        savings += sum(_coh_gross(rec) for rec in coh_counted)
        savings += sum(compute_optimizer_savings(rec) for rec in co_counted)
        savings += sum(_coh_gross(rec) for rec in asg_counted)
        savings += sum(_heur_gross(rec) for rec in enh_counted)
        savings += sum(_heur_gross(rec) for rec in adv_counted)

        # Source blocks carry counted + advisory (advisory = Counted=False):
        # rendered as cards, split out in the tab header, excluded from the
        # counted total (result_builder) and from ``savings`` above.
        cost_hub_out = coh_counted + coh_adv
        co_out = co_counted + co_adv
        asg_out = asg_counted + asg_adv
        enhanced_out = enh_counted + enh_adv
        advanced_out = adv_counted + adv_cov_adv + advisory_final

        total_recs = (
            len(coh_counted) + len(co_counted) + len(asg_counted) + len(enh_counted) + len(adv_counted)
        )

        return ServiceFindings(
            service_name="EC2",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "cost_optimization_hub": SourceBlock(count=len(cost_hub_out), recommendations=tuple(cost_hub_out)),
                "compute_optimizer": SourceBlock(count=len(co_out), recommendations=tuple(co_out)),
                "asg_compute_optimizer": SourceBlock(count=len(asg_out), recommendations=tuple(asg_out)),
                "enhanced_checks": SourceBlock(count=len(enhanced_out), recommendations=tuple(enhanced_out)),
                "advanced_ec2_checks": SourceBlock(count=len(advanced_out), recommendations=tuple(advanced_out)),
            },
            extras={"instance_count": get_ec2_instance_count(ctx)},
        )
