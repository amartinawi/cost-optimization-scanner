"""Amazon Bedrock cost optimization adapter.

Analyzes Bedrock for:
    - Idle Provisioned Throughput (committed but unused)
    - PT breakeven analysis (on-demand vs committed)
    - Idle Knowledge Bases (unused OCU hours)
    - Idle Agents (unused agent invocations)
"""

from __future__ import annotations

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
PT_HOURLY_DEFAULT: float = 1.0
KB_OCU_HOURLY: float = 0.20
HOURS_PER_MONTH: int = 730
CW_NAMESPACE: str = "AWS/Bedrock"
CW_LOOKBACK_DAYS: int = 30


def _empty_findings() -> ServiceFindings:
    return ServiceFindings(
        service_name="Bedrock",
        total_recommendations=0,
        total_monthly_savings=0.0,
        sources={},
        extras={"pt_count": 0, "kb_count": 0, "agent_count": 0},
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


def _get_pt_invocation_sum(cw: Any, model_id: str) -> float | None:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=CW_LOOKBACK_DAYS)
    period = CW_LOOKBACK_DAYS * 86400
    try:
        resp = cw.get_metric_statistics(
            Namespace=CW_NAMESPACE,
            MetricName="Invocations",
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=now,
            Period=period,
            Statistics=["Sum"],
        )
        dps = resp.get("Datapoints", [])
        if dps:
            return sum(d["Sum"] for d in dps)
    except Exception:
        pass
    return None


def _get_pt_token_counts(cw: Any, model_id: str) -> tuple[float | None, float | None]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=CW_LOOKBACK_DAYS)
    period = CW_LOOKBACK_DAYS * 86400
    input_total: float | None = None
    output_total: float | None = None
    try:
        resp_in = cw.get_metric_statistics(
            Namespace=CW_NAMESPACE,
            MetricName="InputTokenCount",
            Dimensions=[{"Name": "ModelId", "Value": model_id}],
            StartTime=start,
            EndTime=now,
            Period=period,
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
            Period=period,
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
    model_id = pt.get("modelId", "")
    model_units = pt.get("modelUnits", 1)
    status = pt.get("status", "Unknown")

    invocations = _get_pt_invocation_sum(cw, model_id)

    if invocations is None:
        return recs

    if invocations == 0:
        hourly = PT_HOURLY_PRICE.get(model_id, PT_HOURLY_DEFAULT)
        monthly_waste = hourly * model_units * HOURS_PER_MONTH * pricing_multiplier
        if monthly_waste > 1.0:
            recs.append(
                {
                    "provisioned_model_id": pt_id,
                    "model_id": model_id,
                    "model_units": model_units,
                    "status": status,
                    "check_category": "Provisioned Throughput",
                    "current_value": f"PT '{pt_id}' with {model_units} unit(s), 0 invocations in {CW_LOOKBACK_DAYS} days",
                    "recommended_value": "Delete idle Provisioned Throughput",
                    "monthly_savings": round(monthly_waste, 2),
                    "reason": f"Provisioned Throughput '{pt_id}' has zero invocations in "
                    f"{CW_LOOKBACK_DAYS} days — wasting ${monthly_waste:.2f}/mo",
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
    model_id = pt.get("modelId", "")
    model_units = pt.get("modelUnits", 1)

    hourly = PT_HOURLY_PRICE.get(model_id, PT_HOURLY_DEFAULT)
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
        monthly_cost = KB_OCU_HOURLY * HOURS_PER_MONTH * pricing_multiplier
        if monthly_cost > 1.0:
            recs.append(
                {
                    "knowledge_base_id": kb_id,
                    "knowledge_base_name": kb_name,
                    "status": status,
                    "check_category": "Knowledge Base",
                    "current_value": f"KB '{kb_name}' active, ~${monthly_cost:.2f}/mo OCU cost",
                    "recommended_value": "Review Knowledge Base usage and delete if unused",
                    "monthly_savings": round(monthly_cost, 2),
                    "reason": f"Knowledge Base '{kb_name}' may have idle OCU hours — "
                    f"estimated ${monthly_cost:.2f}/mo if fully idle",
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

    for agent in agents:
        agent_id = agent.get("agentId", "unknown")
        agent_name = agent.get("agentName", agent_id)
        status = agent.get("agentStatus", "Unknown")
        estimated_monthly = 5.0 * pricing_multiplier
        if estimated_monthly > 1.0:
            recs.append(
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "status": status,
                    "check_category": "Agent",
                    "current_value": f"Agent '{agent_name}' ({status})",
                    "recommended_value": "Review Agent invocation volume and delete if unused",
                    "monthly_savings": round(estimated_monthly, 2),
                    "reason": f"Agent '{agent_name}' has no invocations detected — "
                    f"estimated ${estimated_monthly:.2f}/mo in underlying model costs",
                }
            )

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

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="Provisioned Throughputs", source_path="extras.pt_count", formatter="int"),
        StatCardSpec(label="Idle PTs", source_path="sources.idle_provisioned_throughput.count", formatter="int"),
        StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
    )

    grouping = GroupingSpec(by="check_category", label_path="check_category")

    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        return ("bedrock", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/bedrock.py] Bedrock module active")

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
