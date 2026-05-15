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
from services._base import BaseServiceModule

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


def _get_cloudwatch_invocations_sum(cw: Any, endpoint_name: str, days: int = IDLE_ENDPOINT_DAYS) -> float | None:
    """Total endpoint invocations over the lookback window.

    Uses Period=86400 (the CW maximum); aggregates per-day Sum datapoints.
    Larger Period values silently fail at the GetMetricStatistics API.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/SageMaker",
            MetricName="Invocations",
            Dimensions=[{"Name": "EndpointName", "Value": endpoint_name}],
            StartTime=start,
            EndTime=now,
            Period=_CW_PERIOD_1D,
            Statistics=["Sum"],
        )
        dps = resp.get("Datapoints", [])
        if dps:
            return sum(d["Sum"] for d in dps)
    except Exception:
        pass
    return None


def _list_endpoints(sm: Any) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []
    try:
        paginator = sm.get_paginator("list_endpoints")
        for page in paginator.paginate():
            for ep in page.get("Endpoints", []):
                endpoints.append(ep)
    except Exception:
        try:
            resp = sm.list_endpoints()
            endpoints = resp.get("Endpoints", [])
        except Exception:
            pass
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
    endpoints = _list_endpoints(sm)
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

            invocations = _get_cloudwatch_invocations_sum(cw, name)
            if invocations is None or invocations > 0:
                continue

            detail = _describe_endpoint(sm, name)
            config_name = ""
            instance_type = "unknown"
            if detail:
                config_name = detail.get("EndpointConfigName", "")
            if config_name:
                config = _describe_endpoint_config(sm, config_name)
                if config:
                    variants = config.get("ProductionVariants", [])
                    if variants:
                        instance_type = variants[0].get("InstanceType", "unknown")

            # PricingEngine returns region-correct prices already; do not
            # re-multiply by pricing_multiplier (would double-count).
            _ = pricing_multiplier
            instance_monthly = _get_instance_monthly(ctx, instance_type)

            recs.append(
                {
                    "endpoint_name": name,
                    "instance_type": instance_type,
                    "check_category": "Idle Endpoints",
                    "current_value": f"Endpoint '{name}' ({instance_type}) has 0 invocations in {IDLE_ENDPOINT_DAYS} days",
                    "recommended_value": "Delete endpoint or reduce instance count",
                    "monthly_savings": round(instance_monthly, 2),
                    "reason": f"SageMaker endpoint '{name}' is active but received zero invocations "
                    f"in the last {IDLE_ENDPOINT_DAYS} days",
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
    except Exception:
        return recs, 0

    _ = pricing_multiplier  # PricingEngine values are already region-correct.
    for nb in notebooks:
        try:
            name = nb.get("NotebookInstanceName", "")
            instance_type = nb.get("InstanceType", "unknown")

            instance_monthly = _get_instance_monthly(ctx, instance_type)

            recs.append(
                {
                    "notebook_name": name,
                    "instance_type": instance_type,
                    "check_category": "Idle Notebooks",
                    "current_value": f"Notebook '{name}' ({instance_type}) is running",
                    "recommended_value": "Stop or delete idle notebook instance",
                    "monthly_savings": round(instance_monthly, 2),
                    "reason": f"SageMaker notebook '{name}' ({instance_type}) is running; "
                    f"consider stopping if not actively in use",
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
    except Exception:
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

            recs.append(
                {
                    "job_name": job_name,
                    "instance_type": instance_type,
                    "training_hours": round(training_hours, 1),
                    "check_category": "Spot Training",
                    "current_value": f"Training job '{job_name}' ran {training_hours:.1f}h on-demand ({instance_type})",
                    "recommended_value": "Enable Managed Spot Training for similar future jobs",
                    "monthly_savings": round(savings, 2),
                    "reason": f"Training job '{job_name}' ran {training_hours:.1f}h on-demand; "
                    f"Spot Training could save ~{SPOT_SAVINGS_RATE:.0%} "
                    f"(${savings:.2f} per equivalent run)",
                }
            )
        except Exception:
            continue

    return recs


def _check_multi_model_consolidation(
    sm: Any,
    ctx: Any,
    pricing_multiplier: float,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    endpoints = _list_endpoints(sm)
    active = [ep for ep in endpoints if ep.get("EndpointStatus") == "InService"]

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
        savings = (len(group) - 1) * instance_monthly * CONSOLIDATION_SAVINGS_RATE

        recs.append(
            {
                "instance_type": instance_type,
                "endpoint_count": len(group),
                "endpoints": ", ".join(endpoint_names[:5]),
                "check_category": "Consolidation",
                "current_value": f"{len(group)} endpoints using {instance_type}",
                "recommended_value": f"Consolidate into fewer multi-model endpoints",
                "monthly_savings": round(savings, 2),
                "reason": f"{len(group)} SageMaker endpoints use {instance_type}; "
                f"consolidating onto multi-model endpoints could save ~{CONSOLIDATION_SAVINGS_RATE:.0%}",
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
        print(f"\U0001f50d [services/adapters/sagemaker.py] SageMaker module active")

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
        consolidation_recs = _check_multi_model_consolidation(sm, ctx, multiplier)

        all_recs = idle_ep_recs + notebook_recs + spot_recs + consolidation_recs
        total_savings = sum(r.get("monthly_savings", 0.0) for r in all_recs)

        return ServiceFindings(
            service_name="SageMaker",
            total_recommendations=len(all_recs),
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
                "active_endpoint_count": active_ep_count,
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
