"""SageMaker cost optimization adapter.

Analyzes SageMaker resources for:
    - Idle endpoints (zero invocations over 7 days)
    - Idle notebook instances (running with no connections)
    - Spot training savings opportunities
    - Multi-model endpoint consolidation
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._aws_errors import record_aws_error
from services._base import BaseServiceModule
from services._savings import mark_zero_savings_advisory
from services.commitment_coverage import demote_recs_in_place

SPOT_SAVINGS_RATE: float = 0.70
HOURS_PER_MONTH: int = 730
IDLE_ENDPOINT_DAYS: int = 7
MIN_TRAINING_SECONDS: int = 3600
CONSOLIDATION_SAVINGS_RATE: float = 0.30


def _empty_findings(**extras: Any) -> ServiceFindings:
    return ServiceFindings(
        service_name="SageMaker",
        total_recommendations=0,
        total_monthly_savings=0.0,
        sources={},
        extras=extras,
    )


def _get_instance_monthly(ctx: Any, instance_type: str) -> float:
    """Return SageMaker instance monthly price.

    PricingEngine.get_sagemaker_instance_monthly() queries the
    ``AmazonSageMaker`` service code, so the value already reflects the
    SageMaker-vs-EC2 managed-ML premium AWS applies — no additional
    multiplier needed. Returns 0.0 when Pricing API is unavailable so the
    caller can skip the rec rather than fabricate a fallback constant.
    """
    try:
        price = ctx.pricing_engine.get_sagemaker_instance_monthly(instance_type)
        if price and price > 0:
            return float(price)
    except Exception:
        pass
    return 0.0


_CW_PERIOD_1D: int = 86400  # AWS CloudWatch max Period for ≤15-day queries.


def _variant_invocations_sum(
    cw: Any,
    endpoint_name: str,
    variants: list[dict[str, Any]],
    days: int = IDLE_ENDPOINT_DAYS,
) -> float | None:
    """Total invocations across the endpoint's variants over the window.

    SageMaker publishes ``Invocations`` only with the (EndpointName,
    VariantName) dimension set (plus richer supersets) — never EndpointName
    alone — and GetMetricStatistics matches the EXACT set, so the old
    single-dimension read returned empty on every real endpoint and the idle
    lever never fired (sweep SM-1). Semantics: any read exception -> None
    (abstain); a successful read with no datapoints contributes 0.0 — the
    metric publishes only on invocations, so absence over the window IS the
    idle signal (the caller additionally guards endpoints younger than the
    window, C8). Uses Period=86400 (the CW maximum).
    """
    if not variants:
        return None
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    total = 0.0
    for variant in variants:
        vname = str(variant.get("VariantName") or "")
        if not vname:
            return None  # cannot dimension the read — abstain
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/SageMaker",
                MetricName="Invocations",
                Dimensions=[
                    {"Name": "EndpointName", "Value": endpoint_name},
                    {"Name": "VariantName", "Value": vname},
                ],
                StartTime=start,
                EndTime=now,
                Period=_CW_PERIOD_1D,
                Statistics=["Sum"],
            )
        except Exception:
            return None
        total += sum(d["Sum"] for d in resp.get("Datapoints", []))
    return total


def _list_endpoints(sm: Any, ctx: Any = None) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []
    try:
        paginator = sm.get_paginator("list_endpoints")
        for page in paginator.paginate():
            for ep in page.get("Endpoints", []):
                endpoints.append(ep)
    except Exception:
        # Paginator unavailable: fall back to a manual NextToken loop so the
        # fallback still walks every page instead of silently capping at one
        # (~100 endpoints).
        try:
            params: dict[str, Any] = {}
            while True:
                resp = sm.list_endpoints(**params)
                for ep in resp.get("Endpoints", []):
                    endpoints.append(ep)
                token = resp.get("NextToken")
                if not token:
                    break
                params["NextToken"] = token
        except Exception as e:
            # Both paths failed — a real enumeration failure (AccessDenied /
            # throttle), not "no endpoints". Classify so the permission gap
            # surfaces instead of hiding the idle-endpoint counted savings.
            if ctx is not None:
                record_aws_error(ctx, e, service="sagemaker", context="list_endpoints failed")
    return endpoints


def _describe_endpoint(sm: Any, endpoint_name: str) -> dict[str, Any] | None:
    try:
        resp = sm.describe_endpoint(EndpointName=endpoint_name)
        return resp
    except Exception:
        return None


def _describe_endpoint_config(sm: Any, config_name: str) -> dict[str, Any] | None:
    try:
        resp = sm.describe_endpoint_config(EndpointConfigName=config_name)
        return resp
    except Exception:
        return None


def _check_idle_endpoints(
    sm: Any,
    cw: Any,
    ctx: Any,
    pricing_multiplier: float,
    fast_mode: bool,
) -> tuple[list[dict[str, Any]], int]:
    recs: list[dict[str, Any]] = []
    endpoints = _list_endpoints(sm, ctx)
    active_count = 0

    for ep in endpoints:
        try:
            name = ep.get("EndpointName", "")
            status = ep.get("EndpointStatus", "")
            if status != "InService":
                continue
            active_count += 1

            if fast_mode:
                continue

            detail = _describe_endpoint(sm, name) or {}
            config_name = detail.get("EndpointConfigName", "")
            config = _describe_endpoint_config(sm, config_name) if config_name else None
            cfg_variants = (config or {}).get("ProductionVariants", []) or []
            if not cfg_variants:
                # Cannot dimension the metric read or price the fleet — abstain.
                continue

            invocations = _variant_invocations_sum(cw, name, cfg_variants)
            if invocations is None or invocations > 0:
                continue

            # C8 guard — an endpoint younger than the lookback window has an
            # empty metric series for benign reasons; never a delete rec.
            created = ep.get("CreationTime")
            if created is not None:
                try:
                    if (datetime.now(timezone.utc) - created).days < IDLE_ENDPOINT_DAYS:
                        continue
                except TypeError:
                    continue  # unreadable creation time — abstain

            # PricingEngine returns region-correct prices already; do not
            # re-multiply by pricing_multiplier (would double-count).
            _ = pricing_multiplier
            # SM-3 — the recoverable cost is the whole fleet: every variant's
            # instance price x its instance count (live CurrentInstanceCount
            # when present, else the config's initial count). Any unpriceable
            # variant abstains the whole rec (sagemaker C2 — no partial-fleet
            # placeholder dollar).
            desc_counts = {
                str(v.get("VariantName") or ""): v.get("CurrentInstanceCount")
                for v in detail.get("ProductionVariants", []) or []
            }
            instance_monthly = 0.0
            variant_summary: list[str] = []
            for variant in cfg_variants:
                vtype = str(variant.get("InstanceType") or "")
                vname = str(variant.get("VariantName") or "")
                count = int(desc_counts.get(vname) or variant.get("InitialInstanceCount") or 1)
                per_instance = _get_instance_monthly(ctx, vtype)
                if per_instance <= 0:
                    instance_monthly = 0.0
                    break
                instance_monthly += per_instance * count
                variant_summary.append(f"{vname or vtype}: {count}x {vtype}")
            if instance_monthly <= 0:
                continue
            instance_type = str(cfg_variants[0].get("InstanceType") or "unknown")

            recs.append(
                {
                    "endpoint_name": name,
                    "instance_type": instance_type,
                    "check_category": "Idle Endpoints",
                    "Counted": True,
                    "current_value": f"Endpoint '{name}' ({instance_type}) has 0 invocations in {IDLE_ENDPOINT_DAYS} days",
                    "recommended_value": "Delete endpoint or reduce instance count",
                    "EstimatedMonthlySavings": round(instance_monthly, 2),
                    "EstimatedSavings": f"${instance_monthly:,.2f}/month",
                    "reason": f"SageMaker endpoint '{name}' is active but received zero invocations "
                    f"in the last {IDLE_ENDPOINT_DAYS} days",
                    "AuditBasis": {
                        "rate": "AmazonSageMaker Hosting on-demand instance-hour, region-correct via "
                        "PricingEngine (e.g. ml.m5.xlarge us-east-1 = $0.23/hr -> $167.90/mo; "
                        "live-validated 2026-06-27)",
                        "region": getattr(ctx, "region", "unknown"),
                        "metric_window": (
                            f"{IDLE_ENDPOINT_DAYS}d AWS/SageMaker Invocations Sum == 0, read per "
                            "(EndpointName, VariantName) — the only dimension set AWS publishes"
                        ),
                        "formula": "sum(variant instance_monthly x instance_count) (delete idle endpoint)",
                        "variants": variant_summary,
                        "instance_monthly": round(instance_monthly, 2),
                    },
                }
            )
        except Exception:
            continue

    return recs, active_count


def _check_idle_notebooks(
    sm: Any,
    ctx: Any,
    pricing_multiplier: float,
) -> tuple[list[dict[str, Any]], int]:
    recs: list[dict[str, Any]] = []
    notebooks: list[dict[str, Any]] = []
    try:
        params: dict[str, Any] = {"StatusEquals": "InService"}
        while True:
            resp = sm.list_notebook_instances(**params)
            notebooks.extend(resp.get("NotebookInstances", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token
    except Exception as e:
        # list_notebook_instances failed (AccessDenied / throttle) — classify so
        # the gap surfaces rather than reporting "0 notebooks" silently.
        record_aws_error(ctx, e, service="sagemaker", context="list_notebook_instances failed")
        return recs, 0

    _ = pricing_multiplier  # PricingEngine values are already region-correct.
    for nb in notebooks:
        try:
            name = nb.get("NotebookInstanceName", "")
            instance_type = nb.get("InstanceType", "unknown")

            instance_monthly = _get_instance_monthly(ctx, instance_type)

            # Every InService notebook was previously flagged idle and counted at
            # full monthly cost with zero idle evidence — most notebooks are
            # intentionally left running. Demote to $0 advisory: count only when
            # proven idle (LastModifiedTime age or a CW agent metric), which is
            # not available at scan time (sagemaker C1).
            recs.append(
                {
                    "notebook_name": name,
                    "instance_type": instance_type,
                    "check_category": "Idle Notebooks",
                    "Counted": False,
                    "current_value": f"Notebook '{name}' ({instance_type}) is InService",
                    "recommended_value": "Stop or delete if not actively in use",
                    "EstimatedMonthlySavings": 0.0,
                    "EstimatedSavings": (
                        f"$0.00/month — advisory: notebook running (~${instance_monthly:.2f}/mo); "
                        f"verify idle before stopping"
                    ),
                    "reason": (
                        f"SageMaker notebook '{name}' ({instance_type}) is running "
                        f"(~${instance_monthly:.2f}/mo); verify idle via "
                        f"LastModifiedTime age before stopping"
                    ),
                    "AuditBasis": {
                        "instance_monthly": round(instance_monthly, 2),
                        "unmeasured_inputs": ["last_modified_age", "cw_idle_signal"],
                        "reason": "not proven idle; advisory per cost-scope rule",
                    },
                }
            )
        except Exception:
            continue

    return recs, len(notebooks)


def _check_spot_training(
    sm: Any,
    ctx: Any,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    try:
        params: dict[str, Any] = {
            "StatusEquals": "Completed",
            "SortBy": "CreationTime",
            "MaxResults": 100,
        }
        while True:
            resp = sm.list_training_jobs(**params)
            jobs.extend(resp.get("TrainingJobSummaries", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token
    except Exception as e:
        # list_training_jobs failed (AccessDenied / throttle) — classify so the
        # gap surfaces rather than silently skipping spot-training analysis.
        record_aws_error(ctx, e, service="sagemaker", context="list_training_jobs failed")
        return recs

    pricing_multiplier = ctx.pricing_multiplier

    for job in jobs:
        try:
            job_name = job.get("TrainingJobName", "")
            training_time = job.get("TrainingTimeInSeconds", 0)
            if training_time < MIN_TRAINING_SECONDS:
                continue

            detail: dict[str, Any] = {}
            try:
                detail = sm.describe_training_job(TrainingJobName=job_name)
            except Exception:
                continue

            spot_enabled = detail.get("EnableManagedSpotTraining", False)
            if spot_enabled:
                continue

            instance_type = "unknown"
            try:
                resource_config = detail.get("ResourceConfig", {})
                instance_type = resource_config.get("InstanceType", "unknown")
            except Exception:
                pass

            if instance_type == "unknown":
                continue

            instance_monthly = _get_instance_monthly(ctx, instance_type)
            if instance_monthly <= 0:
                continue

            hourly_rate = instance_monthly / HOURS_PER_MONTH
            training_hours = training_time / 3600.0
            on_demand_cost = hourly_rate * training_hours
            # `hourly_rate` derives from PricingEngine (region-correct);
            # do NOT apply pricing_multiplier here.
            _ = pricing_multiplier
            savings = on_demand_cost * SPOT_SAVINGS_RATE

            if savings < 0.50:
                continue

            # sagemaker H1 — a completed training job's Spot saving is a
            # ONE-TIME per-run figure (speculative future spend if the job is
            # re-run with Managed Spot enabled), not a recurring monthly cost.
            # Summing it into the monthly headline overstates savings and
            # compounds across historical jobs. Emit it as a $0 advisory
            # (Counted=False) and label the figure "per-run, one-time".
            recs.append(
                {
                    "job_name": job_name,
                    "instance_type": instance_type,
                    "training_hours": round(training_hours, 1),
                    "check_category": "Spot Training",
                    "Counted": False,
                    "current_value": f"Training job '{job_name}' ran {training_hours:.1f}h on-demand ({instance_type})",
                    "recommended_value": "Enable Managed Spot Training for similar future jobs",
                    "EstimatedMonthlySavings": 0.0,
                    "EstimatedSavings": (
                        f"$0.00/month — advisory: ~${savings:.2f} per-run, one-time "
                        f"(speculative future spend, not a recurring monthly saving)"
                    ),
                    "reason": f"Training job '{job_name}' ran {training_hours:.1f}h on-demand; "
                    f"Managed Spot Training could save ~{SPOT_SAVINGS_RATE:.0%} "
                    f"(~${savings:.2f} per equivalent run, one-time — not summed into the monthly headline)",
                    "AuditBasis": {
                        "rate": f"SPOT_SAVINGS_RATE={SPOT_SAVINGS_RATE} of on-demand run cost "
                        f"(Managed Spot Training; AWS advertises up to 90%, 0.70 is a conservative documented constant)",
                        "training_hours": round(training_hours, 1),
                        "hourly_rate": round(hourly_rate, 4),
                        "on_demand_run_cost": round(on_demand_cost, 2),
                        "per_run_savings": round(savings, 2),
                        "basis": "one-time per-run saving; speculative future spend; advisory $0 (Counted=False)",
                    },
                }
            )
        except Exception:
            continue

    return recs


def _check_multi_model_consolidation(
    sm: Any,
    ctx: Any,
    pricing_multiplier: float,
    idle_endpoint_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    endpoints = _list_endpoints(sm, ctx)
    # sagemaker H2 — an idle endpoint is already counted at full cost for
    # deletion; excluding it from the consolidation grouping population
    # prevents the same compute being counted a second time as a
    # consolidation saving.
    idle_names = idle_endpoint_names or set()
    active = [
        ep
        for ep in endpoints
        if ep.get("EndpointStatus") == "InService" and ep.get("EndpointName", "") not in idle_names
    ]

    instance_groups: dict[str, list[dict[str, Any]]] = {}
    for ep in active:
        try:
            name = ep.get("EndpointName", "")
            config_name = ""
            detail = _describe_endpoint(sm, name)
            if detail:
                config_name = detail.get("EndpointConfigName", "")
            if not config_name:
                continue
            config = _describe_endpoint_config(sm, config_name)
            if not config:
                continue
            variants = config.get("ProductionVariants", [])
            if not variants:
                continue
            instance_type = variants[0].get("InstanceType", "unknown")
            if instance_type == "unknown":
                continue
            instance_groups.setdefault(instance_type, []).append({"name": name, "instance_type": instance_type})
        except Exception:
            continue

    for instance_type, group in instance_groups.items():
        if len(group) <= 1:
            continue

        instance_monthly = _get_instance_monthly(ctx, instance_type)
        if instance_monthly <= 0:
            continue

        endpoint_names = [g["name"] for g in group]
        # PricingEngine returns region-correct value; do not re-multiply.
        _ = pricing_multiplier
        # SM-2 — the flat 30% factor is a C9 shape (a percentage against a
        # price, not two live prices) and the grouping tests NO consolidation
        # feasibility: no invocation-load headroom, no model-memory fit, no
        # container/framework compatibility (MME requires a shared framework).
        # The figure is indicative only -> $0 Counted=False advisory carrying
        # it in PotentialMonthlySavings (C10: a rec you would have to revert
        # is not a saving).
        potential = (len(group) - 1) * instance_monthly * CONSOLIDATION_SAVINGS_RATE

        recs.append(
            {
                "instance_type": instance_type,
                "endpoint_count": len(group),
                "endpoints": ", ".join(endpoint_names[:5]),
                "check_category": "Consolidation",
                "Counted": False,
                "current_value": f"{len(group)} endpoints using {instance_type}",
                "recommended_value": "Consolidate into fewer multi-model endpoints",
                "EstimatedMonthlySavings": 0.0,
                "PotentialMonthlySavings": round(potential, 2),
                "EstimatedSavings": (
                    f"$0.00/month — advisory: consolidation could recover roughly "
                    f"${potential:,.2f}/mo (~{CONSOLIDATION_SAVINGS_RATE:.0%} indicative), but "
                    "invocation-load, model-memory, and framework-compatibility feasibility "
                    "are unmeasured"
                ),
                "reason": f"{len(group)} active (non-idle) SageMaker endpoints use {instance_type}; "
                "multi-model consolidation is only executable when the models share a framework "
                "and fit one endpoint's load/memory — evidence this scan does not read",
                "AuditBasis": {
                    "rate": "AmazonSageMaker Hosting on-demand instance-hour, region-correct via PricingEngine",
                    "region": getattr(ctx, "region", "unknown"),
                    "factor": CONSOLIDATION_SAVINGS_RATE,
                    "advisory_reason": (
                        "flat factor with no feasibility gate (C9/C10) — indicative figure kept in "
                        "PotentialMonthlySavings, never counted"
                    ),
                    "formula": "(endpoint_count - 1) * instance_monthly * CONSOLIDATION_SAVINGS_RATE; "
                    "idle endpoints excluded from the group so idle compute is not double-counted (sagemaker H2)",
                    "instance_monthly": round(instance_monthly, 2),
                    "endpoint_count": len(group),
                },
            }
        )

    return recs


class SageMakerModule(BaseServiceModule):
    """ServiceModule adapter for SageMaker cost optimization.

    Analyzes SageMaker endpoints, notebooks, training jobs, and
    multi-model endpoint consolidation opportunities.
    """

    key: str = "sagemaker"
    cli_aliases: tuple[str, ...] = ("sagemaker",)
    display_name: str = "SageMaker"

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="Active Endpoints", source_path="extras.active_endpoint_count", formatter="int"),
        StatCardSpec(label="Idle Endpoints", source_path="sources.idle_endpoints.count", formatter="int"),
        StatCardSpec(label="Running Notebooks", source_path="extras.running_notebook_count", formatter="int"),
        StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
    )

    grouping = GroupingSpec(by="check_category")

    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        return ("sagemaker", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:

        sm = ctx.client("sagemaker")
        if not sm:
            return _empty_findings(
                active_endpoint_count=0,
                idle_endpoint_count=0,
                running_notebook_count=0,
            )

        cw = ctx.client("cloudwatch")
        multiplier = ctx.pricing_multiplier
        fast_mode = getattr(ctx, "fast_mode", False)

        idle_ep_recs, active_ep_count = _check_idle_endpoints(sm, cw, ctx, multiplier, fast_mode)
        notebook_recs, notebook_count = _check_idle_notebooks(sm, ctx, multiplier)
        spot_recs = _check_spot_training(sm, ctx)
        # sagemaker H2 — feed the idle-endpoint names into consolidation so an
        # idle endpoint (already counted at full cost for deletion) is excluded
        # from the consolidation grouping and never counted twice.
        idle_endpoint_names = {r["endpoint_name"] for r in idle_ep_recs}
        consolidation_recs = _check_multi_model_consolidation(sm, ctx, multiplier, idle_endpoint_names)

        all_recs = idle_ep_recs + notebook_recs + spot_recs + consolidation_recs

        # sagemaker C2 — gate any $0 rec as advisory (Counted=False) so $0
        # placeholders are excluded from BOTH the dollar total and the counted
        # rec count. counted == rendered: total sums only Counted!=False recs.
        mark_zero_savings_advisory(all_recs, lambda r: r.get("EstimatedMonthlySavings", 0.0))

        # Active-commitment demotion: a SageMaker Savings Plan is fixed pre-paid
        # spend that continues after an idle endpoint is removed or a notebook
        # rightsized, so the on-demand figure is not realizable. Demote every
        # counted SageMaker rec to advisory. (A Compute SP does NOT cover
        # SageMaker.) No SageMaker SP -> no change.
        coverage = getattr(ctx, "commitment_coverage", None)
        if coverage is not None and coverage.covers_sagemaker():
            demote_recs_in_place(all_recs, lambda g: coverage.plan_note("SageMaker Savings Plan", g))

        total_savings = sum(
            r.get("EstimatedMonthlySavings", 0.0) for r in all_recs if r.get("Counted") is not False
        )
        total_recs = sum(1 for r in all_recs if r.get("Counted") is not False)

        return ServiceFindings(
            service_name="SageMaker",
            total_recommendations=total_recs,
            total_monthly_savings=round(total_savings, 2),
            sources={
                "idle_endpoints": SourceBlock(
                    count=len(idle_ep_recs),
                    recommendations=tuple(idle_ep_recs),
                ),
                "idle_notebooks": SourceBlock(
                    count=len(notebook_recs),
                    recommendations=tuple(notebook_recs),
                ),
                "spot_training": SourceBlock(
                    count=len(spot_recs),
                    recommendations=tuple(spot_recs),
                ),
                "multi_model_consolidation": SourceBlock(
                    count=len(consolidation_recs),
                    recommendations=tuple(consolidation_recs),
                ),
            },
            extras={
                # _check_idle_endpoints counts every InService endpoint as active
                # before the zero-invocations test, so subtract the idle ones the
                # report breaks out separately — else the "Active Endpoints" stat
                # double-represents an idle endpoint (sagemaker L3).
                "active_endpoint_count": active_ep_count - len(idle_ep_recs),
                "idle_endpoint_count": len(idle_ep_recs),
                "running_notebook_count": notebook_count,
            },
            optimization_descriptions={
                "idle_endpoints": {
                    "title": "Idle Endpoints",
                    "description": "Active endpoints with zero invocations over 7 days",
                },
                "idle_notebooks": {
                    "title": "Idle Notebooks",
                    "description": "Running notebook instances that may not be in use",
                },
                "spot_training": {
                    "title": "Spot Training Opportunities",
                    "description": "On-demand training jobs that could use Managed Spot Training",
                },
                "multi_model_consolidation": {
                    "title": "Multi-Model Endpoint Consolidation",
                    "description": "Endpoints with the same instance type that could be consolidated",
                },
            },
        )
