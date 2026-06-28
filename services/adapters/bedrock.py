"""Amazon Bedrock cost optimization adapter.

Analyzes Bedrock for:
    - Idle Provisioned Throughput (committed but unused)
    - PT breakeven analysis (on-demand vs committed)
    - Idle Knowledge Bases (unused OCU hours)
    - Idle Agents (unused agent invocations)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule

PT_HOURLY_PRICE: dict[str, float] = {
    "anthropic.claude-3-haiku": 0.80,
    "anthropic.claude-3-sonnet": 4.00,
    "anthropic.claude-3-5-sonnet": 8.00,
    "anthropic.claude-3-opus": 21.50,
    "amazon.titan-text-lite": 0.30,
}
KB_OCU_HOURLY: float = 0.20
HOURS_PER_MONTH: int = 730
CW_NAMESPACE: str = "AWS/Bedrock"
CW_LOOKBACK_DAYS: int = 30
CW_PERIOD_1D: int = 86400  # AWS CloudWatch max Period for ≤15-day queries.


def _empty_findings() -> ServiceFindings:
    return ServiceFindings(
        service_name="Bedrock",
        total_recommendations=0,
        total_monthly_savings=0.0,
        sources={},
        extras={"pt_count": 0, "idle_pt_count": 0, "kb_count": 0, "agent_count": 0},
    )


def _list_provisioned_throughputs(bedrock: Any) -> list[dict[str, Any]]:
    pts: list[dict[str, Any]] = []
    try:
        paginator = bedrock.get_paginator("list_provisioned_model_throughputs")
        for page in paginator.paginate():
            for pt in page.get("provisionedModelSummaries", []):
                pts.append(pt)
    except Exception:
        try:
            resp = bedrock.list_provisioned_model_throughputs()
            pts = resp.get("provisionedModelSummaries", [])
        except Exception:
            pass
    return pts


def _derive_model_id(pt: dict[str, Any]) -> str:
    """Foundation model id backing a Provisioned Throughput (bedrock C1).

    The botocore ``ProvisionedModelSummary`` shape has **no** ``modelId``
    member (members are ``foundationModelArn`` / ``currentModelArn`` /
    ``modelArn`` / ``desiredModelArn`` / ``requestModelArn`` / ``basisModelArn``
    + ``provisionedModelArn`` / ``provisionedModelId`` / ``modelUnits`` …), so
    the previous ``pt.get("modelId", "")`` always returned ``""`` — which made
    ``PT_HOURLY_PRICE.get("", default)`` fabricate $1/hr and the CloudWatch
    ``Invocations``/``InputTokenCount``/``OutputTokenCount`` queries (dimension
    ``ModelId``) match nothing, short-circuiting both counted checks. The model
    identity is the final path segment of the foundation-model ARN
    (e.g. ``arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku``
    → ``anthropic.claude-3-haiku``) — that is both the ``PT_HOURLY_PRICE`` key
    and the CloudWatch ``ModelId`` dimension value.

    Versioned ARNs (e.g. ``.../anthropic.claude-3-haiku-20240307-v1:0``) carry a
    ``-YYYYMMDD-vN:N`` suffix that would NOT match the bare PT_HOURLY_PRICE
    keys, so the counted idle-PT path would still rarely fire. Strip the suffix
    to recover the base model id.
    """
    raw = ""
    for key in (
        "currentModelArn",
        "foundationModelArn",
        "modelArn",
        "desiredModelArn",
        "requestModelArn",
        "basisModelArn",
    ):
        arn = pt.get(key, "")
        if arn and "/" in arn:
            raw = str(arn).rsplit("/", 1)[-1]
            break
    if not raw:
        return ""
    # Strip a trailing ``-YYYYMMDD-vN:N`` (or ``-YYYYMMDD-vN``) version suffix.
    return re.sub(r"-\d{8}-v\d+(?::\d+)?$", "", raw)


def _get_pt_invocation_sum(cw: Any, model_id: str) -> tuple[float | None, bool]:
    """Total invocations of a PT'd model over the lookback window.

    Returns ``(invocation_sum, definitive)`` so the caller can tell a proven-idle
    PT from a merely-suspected one (bedrock C1 follow-up):

    - ``(None, False)`` — the CloudWatch read FAILED (exception). Idle status is
      unknown, so the caller abstains (never recommends deleting a PT it could
      not measure).
    - ``(0.0, False)`` — the read SUCCEEDED but returned **no datapoints**. AWS
      does not emit zero-value ``Invocations`` datapoints, so an absent metric is
      a *candidate* idle signal — strong but not proof (the metric may simply not
      have been published). The caller surfaces it as a ``$0`` advisory, not a
      counted deletion.
    - ``(sum, True)`` — explicit datapoints were returned. ``sum == 0`` is then a
      DEFINITIVE idle reading and the caller may count the recoverable commitment.

    Uses Period=86400 (the CW max for queries ≤15 days); aggregates the per-day
    Sum datapoints. Larger Period values silently fail.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=CW_LOOKBACK_DAYS)
    try:
        resp = cw.get_metric_statistics(
            Namespace=CW_NAMESPACE,
            MetricName="Invocations",
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=now,
            Period=CW_PERIOD_1D,
            Statistics=["Sum"],
        )
    except Exception:
        return None, False
    dps = resp.get("Datapoints", [])
    if not dps:
        return 0.0, False
    return sum(d["Sum"] for d in dps), True


def _get_pt_token_counts(cw: Any, model_id: str) -> tuple[float | None, float | None]:
    """Total InputTokenCount / OutputTokenCount over the lookback window.

    Same Period=86400 constraint as ``_get_pt_invocation_sum``.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=CW_LOOKBACK_DAYS)
    input_total: float | None = None
    output_total: float | None = None
    try:
        resp_in = cw.get_metric_statistics(
            Namespace=CW_NAMESPACE,
            MetricName="InputTokenCount",
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=now,
            Period=CW_PERIOD_1D,
            Statistics=["Sum"],
        )
        dps_in = resp_in.get("Datapoints", [])
        if dps_in:
            input_total = sum(d["Sum"] for d in dps_in)
    except Exception:
        pass
    try:
        resp_out = cw.get_metric_statistics(
            Namespace=CW_NAMESPACE,
            MetricName="OutputTokenCount",
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=now,
            Period=CW_PERIOD_1D,
            Statistics=["Sum"],
        )
        dps_out = resp_out.get("Datapoints", [])
        if dps_out:
            output_total = sum(d["Sum"] for d in dps_out)
    except Exception:
        pass
    return input_total, output_total


def _check_idle_pt(
    pt: dict[str, Any],
    cw: Any,
    pricing_multiplier: float,
    fast_mode: bool,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    if fast_mode:
        return recs

    pt_id = pt.get("provisionedModelId", pt.get("provisionedModelArn", "unknown"))
    model_id = _derive_model_id(pt)
    model_units = pt.get("modelUnits", 1)
    status = pt.get("status", "Unknown")

    invocation_sum, definitive = _get_pt_invocation_sum(cw, model_id)

    if invocation_sum is None:
        # CloudWatch read failed — idle status unprovable; abstain (fail safe:
        # never recommend deleting a PT we could not measure).
        return recs

    if invocation_sum > 0:
        # Active PT — not idle.
        return recs

    # invocation_sum == 0: idle. ``definitive`` separates an explicit Sum=0
    # datapoint (proven idle → counted) from absent datapoints (candidate idle →
    # $0 advisory). Either path now SURFACES the idle PT — previously absent
    # datapoints returned None and the PT was invisible (bedrock C1 follow-up).
    window = f"{CW_LOOKBACK_DAYS} days"
    base = {
        "provisioned_model_id": pt_id,
        "model_id": model_id,
        "model_units": model_units,
        "status": status,
        "check_category": "Provisioned Throughput",
        "current_value": f"PT '{pt_id}' with {model_units} unit(s), 0 invocations in {window}",
        "recommended_value": "Delete idle Provisioned Throughput",
    }

    hourly = PT_HOURLY_PRICE.get(model_id)
    if hourly is None:
        # H1 — never fabricate the $1/hr default for an unknown-rate PT.
        # Surface the idle commitment as a $0 advisory so it renders without
        # inventing a dollar (the committed rate is account-specific and not
        # in the PT_HOURLY_PRICE table).
        recs.append(
            {
                **base,
                "monthly_savings": 0.0,
                "Counted": False,
                "pricing_warning": (
                    "idle PT but committed rate unknown — account-specific "
                    "commitment not in PT_HOURLY_PRICE; verify in Billing"
                ),
                "reason": f"Provisioned Throughput '{pt_id}' has zero invocations in "
                f"{window} — delete to recover the committed spend "
                f"(rate not quantified: '$0.00/month — advisory').",
            }
        )
        return recs

    monthly_waste = hourly * model_units * HOURS_PER_MONTH * pricing_multiplier
    if definitive:
        # Explicit Sum=0 datapoint — proven idle. Count the recoverable spend
        # (still gated at >$1/mo to suppress trivial sub-dollar noise).
        if monthly_waste > 1.0:
            recs.append(
                {
                    **base,
                    "monthly_savings": round(monthly_waste, 2),
                    "reason": f"Provisioned Throughput '{pt_id}' has zero invocations in "
                    f"{window} (explicit metric) — wasting ${monthly_waste:.2f}/mo",
                }
            )
        return recs

    # Candidate idle: no Invocations datapoints. AWS does not publish zero-value
    # datapoints, so absence is suggestive but not proof — surface as a $0
    # advisory naming the estimated recoverable spend, never a counted dollar.
    recs.append(
        {
            **base,
            "monthly_savings": 0.0,
            "Counted": False,
            "pricing_warning": (
                "no Invocations datapoints — idle inferred from metric absence, "
                "not an explicit zero; verify before deleting"
            ),
            "reason": f"Provisioned Throughput '{pt_id}' shows no Invocations in {window} "
            f"— likely idle (~${monthly_waste:.2f}/mo committed); verify and delete "
            f"to recover the spend ('$0.00/month — advisory').",
        }
    )
    return recs


def _check_pt_breakeven(
    pt: dict[str, Any],
    cw: Any,
    pricing_multiplier: float,
    fast_mode: bool,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    if fast_mode:
        return recs

    pt_id = pt.get("provisionedModelId", pt.get("provisionedModelArn", "unknown"))
    model_id = _derive_model_id(pt)
    model_units = pt.get("modelUnits", 1)

    hourly = PT_HOURLY_PRICE.get(model_id)
    if hourly is None:
        # H1 — cannot compute breakeven without the committed rate; skip rather
        # than fabricate the $1/hr default (the on-demand vs PT comparison would
        # be meaningless against an invented commitment cost).
        return recs
    pt_monthly = hourly * model_units * HOURS_PER_MONTH

    if pt_monthly <= 0:
        return recs

    input_tokens, output_tokens = _get_pt_token_counts(cw, model_id)
    if input_tokens is None or output_tokens is None:
        return recs

    od_monthly_estimate = (input_tokens + output_tokens) * 0.000_003
    if od_monthly_estimate <= 0:
        return recs

    if od_monthly_estimate < pt_monthly:
        savings = (pt_monthly - od_monthly_estimate) * pricing_multiplier
        if savings > 1.0:
            recs.append(
                {
                    "provisioned_model_id": pt_id,
                    "model_id": model_id,
                    "model_units": model_units,
                    "check_category": "PT Analysis",
                    "current_value": f"PT commitment ${pt_monthly:.2f}/mo, estimated on-demand ${od_monthly_estimate:.2f}/mo",
                    "recommended_value": "Switch to on-demand pricing",
                    "monthly_savings": round(savings, 2),
                    "reason": f"On-demand estimated at ${od_monthly_estimate:.2f}/mo vs PT cost "
                    f"${pt_monthly:.2f}/mo — save ${savings:.2f}/mo by switching",
                }
            )

    return recs


def _check_idle_knowledge_bases(ctx: Any, pricing_multiplier: float) -> list[dict[str, Any]]:
    """Surface Knowledge Bases for review.

    Bedrock KB OCU pricing scales with actual query / ingestion activity,
    not constant 730 hr/month. Without CW utilization data the adapter
    cannot quantify savings, so each rec emits monthly_savings = 0.0 plus
    a pricing_warning indicating what data is needed. This is honest:
    previously every KB reported $146/mo idle which was fictitious for
    actively-used KBs.
    """
    _ = pricing_multiplier  # No quantified savings emitted; multiplier unused.
    recs: list[dict[str, Any]] = []
    agent_client = None
    try:
        agent_client = ctx.client("bedrock-agent")
    except Exception:
        return recs
    if not agent_client:
        return recs

    kbs: list[dict[str, Any]] = []
    try:
        paginator = agent_client.get_paginator("list_knowledge_bases")
        for page in paginator.paginate():
            for kb in page.get("knowledgeBaseSummaries", []):
                kbs.append(kb)
    except Exception:
        try:
            resp = agent_client.list_knowledge_bases()
            kbs = resp.get("knowledgeBaseSummaries", [])
        except Exception:
            return recs

    for kb in kbs:
        kb_id = kb.get("knowledgeBaseId", "unknown")
        kb_name = kb.get("name", kb_id)
        status = kb.get("status", "Unknown")
        recs.append(
            {
                "knowledge_base_id": kb_id,
                "knowledge_base_name": kb_name,
                "status": status,
                "check_category": "Knowledge Base",
                "current_value": f"KB '{kb_name}' active",
                "recommended_value": "Review query volume and delete if unused",
                "monthly_savings": 0.0,
                "pricing_warning": "requires KB query/ingest CW metrics for quantified savings",
                "reason": f"Knowledge Base '{kb_name}' detected; verify utilization via "
                f"CloudWatch query metrics before deleting",
            }
        )

    return recs


def _check_idle_agents(ctx: Any, pricing_multiplier: float) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    agent_client = None
    try:
        agent_client = ctx.client("bedrock-agent")
    except Exception:
        return recs
    if not agent_client:
        return recs

    agents: list[dict[str, Any]] = []
    try:
        paginator = agent_client.get_paginator("list_agents")
        for page in paginator.paginate():
            for agent in page.get("agentSummaries", []):
                agents.append(agent)
    except Exception:
        try:
            resp = agent_client.list_agents()
            agents = resp.get("agentSummaries", [])
        except Exception:
            return recs

    # Bedrock Agent idle finding removed: agents themselves accrue no AWS charge
    # (MCP confirmed) — the $5/month was a hardcoded placeholder, not a real cost.
    # Real Bedrock spend is via Provisioned Throughput and per-token invocations,
    # both of which are checked elsewhere in this adapter.
    _ = (agents, pricing_multiplier)
    return recs


class BedrockModule(BaseServiceModule):
    """ServiceModule adapter for Amazon Bedrock cost optimization.

    Analyzes Bedrock Provisioned Throughput for idle commitments and
    breakeven against on-demand pricing, plus Knowledge Base and Agent
    idle resource detection.
    """

    key: str = "bedrock"
    cli_aliases: tuple[str, ...] = ("bedrock",)
    display_name: str = "Bedrock"

    # Mirror the cards the HTML report renders from _SERVICE_STATS_CONFIG['bedrock']
    # (all from extras) so this declaration does not diverge from what is shown
    # (bedrock L2).
    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="Provisioned Throughputs", source_path="extras.pt_count", formatter="int"),
        StatCardSpec(label="Idle PTs", source_path="extras.idle_pt_count", formatter="int"),
        StatCardSpec(label="Knowledge Bases", source_path="extras.kb_count", formatter="int"),
        StatCardSpec(label="Agents", source_path="extras.agent_count", formatter="int"),
    )

    grouping = GroupingSpec(by="check_category", label_path="check_category")

    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        return ("bedrock", "bedrock-agent", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:

        bedrock = ctx.client("bedrock")
        cw = ctx.client("cloudwatch")

        if not bedrock:
            return _empty_findings()

        multiplier = ctx.pricing_multiplier
        fast_mode = getattr(ctx, "fast_mode", False)

        pts = _list_provisioned_throughputs(bedrock)

        idle_pt_recs: list[dict[str, Any]] = []
        breakeven_recs: list[dict[str, Any]] = []
        kb_recs: list[dict[str, Any]] = []
        agent_recs: list[dict[str, Any]] = []

        for pt in pts:
            try:
                idle_pt_recs.extend(_check_idle_pt(pt, cw, multiplier, fast_mode))
                breakeven_recs.extend(_check_pt_breakeven(pt, cw, multiplier, fast_mode))
            except Exception:
                continue

        kb_recs = _check_idle_knowledge_bases(ctx, multiplier)
        agent_recs = _check_idle_agents(ctx, multiplier)

        all_recs = idle_pt_recs + breakeven_recs + kb_recs + agent_recs
        total_savings = sum(r.get("monthly_savings", 0.0) for r in all_recs)

        kb_count = len(kb_recs)
        agent_count = len(agent_recs)

        return ServiceFindings(
            service_name="Bedrock",
            total_recommendations=len(all_recs),
            total_monthly_savings=round(total_savings, 2),
            sources={
                "idle_provisioned_throughput": SourceBlock(
                    count=len(idle_pt_recs),
                    recommendations=tuple(idle_pt_recs),
                ),
                "pt_breakeven_analysis": SourceBlock(
                    count=len(breakeven_recs),
                    recommendations=tuple(breakeven_recs),
                ),
                "idle_knowledge_bases": SourceBlock(
                    count=len(kb_recs),
                    recommendations=tuple(kb_recs),
                ),
                "idle_agents": SourceBlock(
                    count=len(agent_recs),
                    recommendations=tuple(agent_recs),
                ),
            },
            extras={
                "pt_count": len(pts),
                # bedrock L2: surface the cost-relevant idle-PT count as a stat card
                # (the idle PTs are the only Bedrock resources carrying a saving).
                "idle_pt_count": len(idle_pt_recs),
                "kb_count": kb_count,
                "agent_count": agent_count,
            },
            optimization_descriptions={
                "idle_provisioned_throughput": {
                    "title": "Idle Provisioned Throughput",
                    "description": "Provisioned Throughputs with zero invocations over 30 days",
                },
                "pt_breakeven_analysis": {
                    "title": "PT Breakeven Analysis",
                    "description": "Provisioned Throughputs that cost more than on-demand pricing",
                },
                "idle_knowledge_bases": {
                    "title": "Idle Knowledge Bases",
                    "description": "Knowledge Bases with potentially unused OCU hours",
                },
                "idle_agents": {
                    "title": "Idle Agents",
                    "description": "Bedrock Agents with no invocation activity detected",
                },
            },
        )
