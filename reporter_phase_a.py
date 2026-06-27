"""Phase A renderer — descriptor-driven grouped service recommendations.

Provides ``render_grouped_by_category`` and per-service detail extractors
used by ``HTMLReportGenerator._get_detailed_recommendations`` for services
that follow the Phase A rendering pattern.
"""

from typing import Any, Callable, Dict, List, Tuple

Rec = Dict[str, Any]
ExtractDetailFn = Callable[[Rec], Tuple[str, str]]


def _priority_class(rec: Rec) -> str:
    p = str(rec.get("priority") or rec.get("Priority") or rec.get("severity") or "").strip().lower()
    if p in ("high", "critical"):
        return " high-priority"
    if p in ("medium", "warning"):
        return " medium-priority"
    if p in ("low", "info", "informational"):
        return " low-priority"
    return ""


def render_file_systems(sources: Dict[str, Any]) -> str:
    """Render EFS and FSx findings grouped by optimisation category.

    Counted findings (sources ``efs_lifecycle_analysis`` / ``fsx_optimization_analysis``)
    are grouped by CheckCategory and show their per-finding dollar saving, so the
    rendered cards sum to the tab's counted total. The uncounted ``advisory``
    source renders as a separate, clearly-labelled informational group.

    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    counted: List[Rec] = []
    advisory: List[Rec] = []
    for src_name, src_data in sources.items():
        if src_data.get("count", 0) <= 0:
            continue
        bucket = advisory if src_name == "advisory" else counted
        bucket.extend(src_data.get("recommendations", []))

    content = '<div class="recommendation-list">'

    # Counted findings, grouped by category, each line showing its dollar saving.
    grouped: Dict[str, List[Rec]] = {}
    for rec in counted:
        grouped.setdefault(rec.get("CheckCategory", "File System Optimization"), []).append(rec)

    for category, recs in grouped.items():
        total = sum(_fs_savings(r) for r in recs)
        label = "file system" if len(recs) == 1 else "file systems"
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>{category} ({len(recs)} {label})</h4>"
        content += f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Optimize file system')}</p>"
        content += f'<p class="savings"><strong>Estimated Monthly Savings:</strong> ${total:.2f}</p>'
        content += "<p><strong>File Systems:</strong></p><ul>"
        for fs in recs:
            fs_id = fs.get("FileSystemId", fs.get("FileCacheId", "Unknown"))
            detail = fs.get("Name") or fs.get("FileSystemType") or ""
            size = fs.get("SizeGB", fs.get("StorageCapacity", 0))
            savings = fs.get("EstimatedSavings", "")
            content += (
                f"<li>{fs_id}"
                f"{f' - {detail}' if detail else ''} ({size:.2f} GB) — "
                f'<span class="savings">{savings}</span></li>'
                if isinstance(size, (int, float))
                else f'<li>{fs_id}{f" - {detail}" if detail else ""} — <span class="savings">{savings}</span></li>'
            )
        content += "</ul></div>"

    # Advisory (uncounted) — best practice, no account-specific dollar figure.
    if advisory:
        adv_groups: Dict[str, List[Rec]] = {}
        for rec in advisory:
            adv_groups.setdefault(rec.get("CheckCategory", "Advisory"), []).append(rec)
        content += '<div class="rec-item">'
        content += f"<h4>Advisory — best-practice opportunities ({len(advisory)})</h4>"
        content += (
            "<p><strong>Note:</strong> these are not counted toward savings — their dollar value "
            "requires usage/backup-size evidence to quantify.</p><ul>"
        )
        for category, recs in adv_groups.items():
            ids = ", ".join(str(r.get("FileSystemId", r.get("FileCacheId", "?"))) for r in recs)
            content += f"<li><strong>{category}</strong> ({len(recs)}): {recs[0].get('Recommendation', '')} — {ids}</li>"
        content += "</ul></div>"

    content += "</div>"
    return content


def _fs_savings(rec: Rec) -> float:
    """Numeric monthly saving for a counted file-system finding."""
    val = rec.get("_savings")
    if isinstance(val, (int, float)):
        return float(val)
    savings = str(rec.get("EstimatedSavings", ""))
    import re

    m = re.search(r"\$(\d+[\d,]*\.?\d*)", savings)
    return float(m.group(1).replace(",", "")) if m else 0.0


def _extract_lambda_details(rec: Rec) -> Tuple[str, str]:
    """Extract Lambda function name and config details. Called by: render_grouped_by_category."""
    func_name = rec.get("FunctionName") or rec.get("resourceId", "Unknown")
    memory = rec.get("MemorySize", "")
    timeout = rec.get("Timeout", "")
    runtime = rec.get("Runtime", "")

    if "currentResourceDetails" in rec:
        lambda_config = rec.get("currentResourceDetails", {}).get("lambdaFunction", {}).get("configuration", {})
        compute_config = lambda_config.get("compute", {})
        if not memory and "memorySizeInMB" in compute_config:
            memory = compute_config["memorySizeInMB"]
        if not runtime and "architecture" in compute_config:
            runtime = compute_config["architecture"]

    details = []
    if memory:
        details.append(f"{memory}MB")
    if timeout:
        details.append(f"{timeout}s timeout")
    if runtime:
        details.append(runtime)

    detail_str = f" ({', '.join(details)})" if details else ""
    return func_name, detail_str


def _extract_cloudfront_details(rec: Rec) -> Tuple[str, str]:
    """Extract CloudFront distribution ID and metadata. Called by: render_grouped_by_category."""
    dist_id = rec.get("DistributionId", "Unknown")
    domain_name = rec.get("DomainName", "")
    status = rec.get("Status", "")
    price_class = rec.get("PriceClass", "")

    details = []
    if domain_name:
        details.append(domain_name)
    if status:
        details.append(f"Status: {status}")
    if price_class:
        details.append(f"Price Class: {price_class}")

    detail_str = f" ({', '.join(details)})" if details else ""
    return dist_id, detail_str


def _extract_rds_details(rec: Rec) -> Tuple[str, str]:
    """Extract RDS resource identifier and engine details. Called by: render_grouped_by_category."""
    resource_id = (
        rec.get("DBInstanceIdentifier")
        or rec.get("DBClusterIdentifier")
        or rec.get("dbClusterIdentifier")
        or rec.get("dbInstanceIdentifier")
        or rec.get("SnapshotId")
        or rec.get("ResourceId")
        or rec.get("resourceArn", "").split(":")[-1]
        if rec.get("resourceArn")
        else "Unknown"
    )

    instance_class = rec.get("DBInstanceClass", "")
    engine = rec.get("Engine", rec.get("engine", ""))

    details = []
    if instance_class:
        details.append(instance_class)
    if engine:
        details.append(engine)

    detail_str = f" ({', '.join(details)})" if details else ""
    return resource_id, detail_str


def _extract_lightsail_details(rec: Rec) -> Tuple[str, str]:
    """Extract Lightsail resource name and IP address. Called by: render_grouped_by_category."""
    resource_id = rec.get("StaticIpName", rec.get("InstanceName", "Unknown"))
    ip = rec.get("IpAddress", "")
    detail_str = f" ({ip})" if ip else ""
    return resource_id, detail_str


def _extract_dms_details(rec: Rec) -> Tuple[str, str]:
    """Extract DMS instance ID and class details. Called by: render_grouped_by_category."""
    resource_id = rec.get("InstanceId", "Unknown")
    instance_class = rec.get("InstanceClass", "")
    cpu = rec.get("AvgCPU", "")
    details = []
    if instance_class:
        details.append(instance_class)
    if cpu:
        details.append(f"{cpu} CPU")
    detail_str = f" ({', '.join(details)})" if details else ""
    return resource_id, detail_str


def _extract_glue_details(rec: Rec) -> Tuple[str, str]:
    """Extract Glue job name and worker config. Called by: render_grouped_by_category."""
    resource_id = rec.get("JobName", "Unknown")
    worker_type = rec.get("WorkerType", "")
    num_workers = rec.get("NumberOfWorkers", "")
    details = []
    if worker_type:
        details.append(worker_type)
    if num_workers:
        details.append(f"{num_workers} workers")
    detail_str = f" ({', '.join(details)})" if details else ""
    return resource_id, detail_str


def _extract_api_gateway_details(rec: Rec) -> Tuple[str, str]:
    """Extract API Gateway resource identifier. Called by: render_grouped_by_category."""
    resource_id = rec.get("ApiId", rec.get("RestApiId", rec.get("ApiName", "Unknown")))
    return resource_id, ""


def _extract_step_functions_details(rec: Rec) -> Tuple[str, str]:
    """Extract Step Functions state machine name. Called by: render_grouped_by_category."""
    resource_id = (
        rec.get("StateMachineArn", "Unknown").split(":")[-1]
        if rec.get("StateMachineArn")
        else rec.get("StateMachineName", "Unknown")
    )
    return resource_id, ""


def _extract_auto_scaling_details(rec: Rec) -> Tuple[str, str]:
    """Extract Auto Scaling group name. Called by: render_grouped_by_category."""
    resource_id = rec.get("AutoScalingGroupName", rec.get("GroupName", "Unknown"))
    return resource_id, ""


def _extract_backup_details(rec: Rec) -> Tuple[str, str]:
    """Extract Backup plan or vault name. Called by: render_grouped_by_category."""
    resource_id = rec.get("BackupPlanName", rec.get("BackupVaultName", rec.get("PlanName", "Unknown")))
    return resource_id, ""


def _extract_route53_details(rec: Rec) -> Tuple[str, str]:
    """Extract Route 53 hosted zone or health check ID. Called by: render_grouped_by_category."""
    resource_id = rec.get("HostedZoneId", rec.get("HealthCheckId", rec.get("ZoneId", "Unknown")))
    return resource_id, ""


def _extract_monitoring_details(rec: Rec) -> Tuple[str, str]:
    """Extract CloudWatch/CloudTrail resource identifier. Called by: render_grouped_by_category."""
    resource_id = rec.get(
        "AlarmName",
        rec.get(
            "LogGroupName",
            rec.get(
                "TrailName",
                rec.get("Namespace", rec.get("HostedZoneId", rec.get("HealthCheckId", "Unknown"))),
            ),
        ),
    )
    return resource_id, ""


_PhaseADescriptor = Dict[str, Any]

PHASE_A_DESCRIPTORS: Dict[str, _PhaseADescriptor] = {
    "lambda": {
        "extract_detail": _extract_lambda_details,
        "singular": "function",
        "plural": "functions",
        "list_label": "Functions",
        "fallback_category": "Lambda Optimization",
        "savings_mode": "always",
        "close_div_location": "outer",
    },
    "cloudfront": {
        "extract_detail": _extract_cloudfront_details,
        "singular": "distribution",
        "plural": "distributions",
        "list_label": "Distributions",
        "fallback_category": "CloudFront Optimization",
        "savings_mode": "always",
        "close_div_location": "outer",
    },
    "lightsail": {
        "extract_detail": _extract_lightsail_details,
        "singular": "resource",
        "plural": "resources",
        "list_label": "Resources",
        "fallback_category": "Lightsail Optimization",
        "savings_mode": "conditional",
        "close_div_location": "inner",
    },
    "dms": {
        "extract_detail": _extract_dms_details,
        "singular": "resource",
        "plural": "resources",
        "list_label": "Resources",
        "fallback_category": "DMS Optimization",
        "savings_mode": "conditional",
        "close_div_location": "inner",
    },
    "glue": {
        "extract_detail": _extract_glue_details,
        "singular": "resource",
        "plural": "resources",
        "list_label": "Resources",
        "fallback_category": "Glue Optimization",
        "savings_mode": "conditional",
        "close_div_location": "inner",
    },
    "api_gateway": {
        "extract_detail": _extract_api_gateway_details,
        "singular": "resource",
        "plural": "resources",
        "list_label": "Resources",
        "fallback_category": "API Gateway Optimization",
        "savings_mode": "always",
        "close_div_location": "outer",
    },
    "step_functions": {
        "extract_detail": _extract_step_functions_details,
        "singular": "resource",
        "plural": "resources",
        "list_label": "Resources",
        "fallback_category": "Step Functions Optimization",
        "savings_mode": "always",
        "close_div_location": "outer",
    },
    "auto_scaling": {
        "extract_detail": _extract_auto_scaling_details,
        "singular": "resource",
        "plural": "resources",
        "list_label": "Resources",
        "fallback_category": "Auto Scaling Optimization",
        "savings_mode": "always",
        "close_div_location": "outer",
    },
    "backup": {
        "extract_detail": _extract_backup_details,
        "singular": "resource",
        "plural": "resources",
        "list_label": "Resources",
        "fallback_category": "Backup Optimization",
        "savings_mode": "always",
        "close_div_location": "outer",
    },
    "route53": {
        "extract_detail": _extract_route53_details,
        "singular": "resource",
        "plural": "resources",
        "list_label": "Resources",
        "fallback_category": "Route53 Optimization",
        "savings_mode": "always",
        "close_div_location": "outer",
    },
}

PHASE_A_INNER_CLOSE_GROUP = {"lightsail", "dms", "glue"}


def _render_rds_group(content: str, category: str, resources: List[Rec], descriptor: _PhaseADescriptor) -> str:
    """Render RDS recommendations grouped by check category. Called by: render_grouped_by_category."""
    extract_detail = descriptor["extract_detail"]
    label = "resource" if len(resources) == 1 else "resources"

    content += f'<div class="rec-item{_priority_class(resources[0])}">'
    content += f"<h4>{category} ({len(resources)} {label})</h4>"

    if resources:
        recommendations = [r.get("Recommendation", "") for r in resources if r.get("Recommendation")]
        if recommendations:
            content += f"<p><strong>Recommendation:</strong> {recommendations[0]}</p>"
        else:
            content += f"<p><strong>Recommendation:</strong> Review RDS instances for rightsizing, Reserved Instance opportunities, and Graviton migration. Consider Aurora for better performance per dollar.</p>"

        savings = resources[0].get("EstimatedSavings", "")
        if savings and savings != "Cost optimization":
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings}</p>'
        else:
            content += '<p class="savings"><strong>Estimated Savings:</strong> 20-72% potential cost reduction through optimization</p>'

    content += "<p><strong>Resources:</strong></p><ul>"
    for res in resources:
        resource_id, detail_str = extract_detail(res)
        content += f"<li>{resource_id}{detail_str}</li>"
    content += "</ul></div>"

    return content


def _group_counted_savings(resources: List[Rec]) -> Tuple[float, bool]:
    """Sum the *counted* ``EstimatedMonthlySavings`` across a category group.

    Returns ``(total, has_numeric)`` for the SR-1 single-source rule:

    - ``has_numeric`` is True when at least one rec in the group carries an
      ``EstimatedMonthlySavings`` key — i.e. the adapter single-sourced a computed
      dollar, so the card must render that dollar (matching the tab headline)
      rather than the free-text ``EstimatedSavings`` percentage / hardcoded string.
    - A rec with ``Counted is False`` is advisory and excluded from ``total`` (it
      never fed the headline), so an advisory-only group totals ``0.0``.
    """
    total = 0.0
    has_numeric = False
    for rec in resources:
        if "EstimatedMonthlySavings" not in rec:
            continue
        has_numeric = True
        if rec.get("Counted") is not False:
            try:
                total += float(rec.get("EstimatedMonthlySavings") or 0.0)
            except (TypeError, ValueError):
                continue
    return total, has_numeric


def render_grouped_by_category(
    service_key: str,
    sources: Dict[str, Any],
    descriptor: _PhaseADescriptor,
) -> str:
    """Render service recommendations as grouped HTML cards.

    Main entry point for Phase A rendering. Groups recommendations by
    ``CheckCategory`` and delegates to per-service detail extractors.

    Args:
        service_key: Service identifier (e.g. ``"lambda"``).
        sources: Dict mapping source names to recommendation data.
        descriptor: Phase A descriptor with extract/label config.

    Returns:
        HTML string of grouped recommendation cards.
    """
    grouped: Dict[str, List[Rec]] = {}

    all_recs: List[Rec] = []
    for src_name, src_data in sources.items():
        if isinstance(src_data, dict):
            if src_data.get("count", 0) > 0:
                all_recs.extend(src_data.get("recommendations", []))
        elif isinstance(src_data, list):
            all_recs.extend(src_data)

    fallback_category = descriptor["fallback_category"]
    for rec in all_recs:
        category = rec.get("CheckCategory", fallback_category)
        if category not in grouped:
            grouped[category] = []
        grouped[category].append(rec)

    extract_detail = descriptor["extract_detail"]
    singular = descriptor["singular"]
    plural = descriptor["plural"]
    list_label = descriptor["list_label"]
    savings_mode = descriptor["savings_mode"]

    content = '<div class="recommendation-list">'

    for category, resources in grouped.items():
        if not resources:
            continue

        if service_key == "rds":
            content = _render_rds_group(content, category, resources, descriptor)
            continue

        content += f'<div class="rec-item{_priority_class(resources[0])}">'
        label = singular if len(resources) == 1 else plural
        content += f"<h4>{category} ({len(resources)} {label})</h4>"

        if resources:
            content += (
                f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"
            )

            # SR-1 — single-source the card dollar. When the adapter computed a
            # numeric EstimatedMonthlySavings, render the group's *counted* sum so
            # the card equals the tab headline (instead of a free-text percentage
            # or hardcoded "$316/month" string); an advisory-only group renders the
            # honest "$0.00/month — advisory". Services that compute no numeric
            # saving keep the legacy string behaviour.
            group_total, has_numeric = _group_counted_savings(resources)
            if has_numeric:
                if group_total > 0:
                    content += f'<p class="savings"><strong>Estimated Savings:</strong> ${group_total:,.2f}/month</p>'
                else:
                    content += '<p class="savings"><strong>Estimated Savings:</strong> $0.00/month — advisory</p>'
            elif savings_mode == "always":
                content += f'<p class="savings"><strong>Estimated Savings:</strong> {resources[0].get("EstimatedSavings", "Cost optimization")}</p>'
            elif savings_mode == "conditional":
                if resources[0].get("EstimatedSavings"):
                    content += f'<p class="savings"><strong>Estimated Savings:</strong> {resources[0].get("EstimatedSavings", "Cost optimization")}</p>'

        content += f"<p><strong>{list_label}:</strong></p><ul>"
        for res in resources:
            resource_id, detail_str = extract_detail(res)
            content += f"<li>{resource_id}{detail_str}</li>"
        content += "</ul></div>"

        if service_key in PHASE_A_INNER_CLOSE_GROUP:
            content += "</div>"

    if service_key not in PHASE_A_INNER_CLOSE_GROUP:
        content += "</div>"
    else:
        content += "</div>"

    return content
