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

    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    grouped_fs = {
        "EFS No Lifecycle": [],
        "EFS Archive Storage Missing": [],
        "EFS One Zone Migration": [],
        "FSx Optimization": [],
    }

    lifecycle_recs: List[Rec] = []
    enhanced_recs: List[Rec] = []
    for src_name, src_data in sources.items():
        if src_data.get("count", 0) > 0:
            for rec in src_data.get("recommendations", []):
                if "CheckCategory" in rec:
                    enhanced_recs.append(rec)
                else:
                    lifecycle_recs.append(rec)

    seen_fs: Dict[str, Rec] = {}
    for rec in lifecycle_recs:
        fs_id = rec.get("FileSystemId", "Unknown")
        if fs_id not in seen_fs:
            seen_fs[fs_id] = rec
        elif rec.get("Name") and rec.get("Name") != "Unnamed":
            seen_fs[fs_id] = rec

    for fs_id, rec in seen_fs.items():
        if "FileSystemType" in rec:
            grouped_fs["FSx Optimization"].append(rec)
        elif "HasIAPolicy" in rec or fs_id.startswith("fs-0"):
            if not rec.get("HasIAPolicy", True):
                grouped_fs["EFS No Lifecycle"].append(rec)
        else:
            if fs_id.startswith("fs-0"):
                if not rec.get("HasIAPolicy", True):
                    grouped_fs["EFS No Lifecycle"].append(rec)
            else:
                grouped_fs["FSx Optimization"].append(rec)

    for rec in enhanced_recs:
        category = rec.get("CheckCategory", "")
        if category == "EFS Archive Storage Missing":
            grouped_fs.setdefault("EFS Archive Storage Missing", []).append(rec)
        elif category == "EFS One Zone Migration":
            grouped_fs.setdefault("EFS One Zone Migration", []).append(rec)
        else:
            grouped_fs.setdefault("EFS Optimization", []).append(rec)

    content = '<div class="recommendation-list">'
    for group_name, filesystems in grouped_fs.items():
        if not filesystems:
            continue

        content += f'<div class="rec-item{_priority_class(filesystems[0])}">'
        label = "file system" if len(filesystems) == 1 else "file systems"
        content += f"<h4>{group_name} ({len(filesystems)} {label})</h4>"

        if group_name == "EFS No Lifecycle":
            content += "<p><strong>Recommendation:</strong> Enable lifecycle policies to move infrequently accessed files to IA storage (Save 80%)</p>"
        elif group_name == "EFS Archive Storage Missing":
            content += "<p><strong>Recommendation:</strong> Enable Archive storage class for rarely accessed data to reduce storage costs by up to 50%</p>"
        elif group_name == "EFS One Zone Migration":
            content += "<p><strong>Recommendation:</strong> Migrate non-critical workloads to One Zone storage for cost savings</p>"
        elif group_name == "FSx Optimization":
            content += "<p><strong>Recommendation:</strong> Review FSx configuration for optimization opportunities</p>"

        content += "<p><strong>File Systems:</strong></p><ul>"
        for fs in filesystems:
            fs_id = fs.get("FileSystemId", "Unknown")
            fs_name = fs.get("Name", "Unnamed")
            size = fs.get("SizeGB", 0)
            content += f"<li>{fs_id} - {fs_name} ({size:.2f} GB)</li>"
        content += "</ul></div>"

    content += "</div>"
    return content


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

            if savings_mode == "always":
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
