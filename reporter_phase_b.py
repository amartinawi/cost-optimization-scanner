"""Phase B renderer — function registry for source-specific HTML handlers.

Provides specialised renderers for complex services (EC2, EBS, RDS, S3, etc.)
and generic fallback renderers used by ``HTMLReportGenerator``.
"""

import logging
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

Rec = Dict[str, Any]


def _priority_class(rec: Rec) -> str:
    p = str(rec.get("priority") or rec.get("Priority") or rec.get("severity") or "").strip().lower()
    if p in ("high", "critical"):
        return " high-priority"
    if p in ("medium", "warning"):
        return " medium-priority"
    if p in ("low", "info", "informational"):
        return " low-priority"
    return ""


def _render_ec2_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders EC2 enhanced-check recommendations grouped by category. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_recs: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        resource_id = rec.get(
            "InstanceId",
            rec.get(
                "ImageId",
                rec.get("AllocationId", rec.get("ResourceId", rec.get("resourceId", "Resource"))),
            ),
        )
        resource_name = rec.get("Name", rec.get("ResourceName", ""))

        all_text = f"{resource_id} {resource_name}".lower()
        if (
            "ecs-cluster" in all_text
            or "/cronjob" in all_text
            or "forwarder" in all_text
            or "lambda" in all_text
            or "ecs" in all_text
        ):
            continue

        # Spot-related recommendations are intentionally rendered: they count
        # toward total_recommendations and total_monthly_savings at the adapter
        # layer, so dropping them here would desync the headline from the table.
        finding = rec.get("finding", rec.get("instanceFinding", rec.get("InstanceFinding", ""))).lower()
        if finding == "optimized":
            continue

        category = rec.get("CheckCategory", "Other")
        if category not in grouped_recs:
            grouped_recs[category] = []
        grouped_recs[category].append(rec)

    content = ""
    for category, recs in grouped_recs.items():
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>{category} ({len(recs)} resources)</h4>"
        content += f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Optimize resource')}</p>"
        content += "<p><strong>Affected Resources:</strong></p><ul>"
        for rec in recs:
            resource_id = rec.get("InstanceId", rec.get("ImageId", rec.get("AllocationId", "Resource")))
            instance_type = rec.get("InstanceType", "")
            savings = rec.get("EstimatedSavings", "")
            # Per-instance utilization details when available (idle/rightsizing/burstable).
            details: List[str] = []
            if instance_type:
                details.append(instance_type)
            if rec.get("AvgCPU"):
                details.append(f"avg CPU {rec['AvgCPU']}")
            if rec.get("MaxCPU"):
                details.append(f"max CPU {rec['MaxCPU']}")
            detail_str = f" ({', '.join(details)})" if details else ""
            savings_str = f" — <span class=\"savings\">{savings}</span>" if savings else ""
            content += f"<li>{resource_id}{detail_str}{savings_str}</li>"
        content += "</ul></div>"
    return content


def _render_ec2_cost_hub(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders EC2 Cost Optimization Hub recommendations grouped by action. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_actions: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        if "actionType" not in rec:
            continue
        resource_details = rec.get("currentResourceDetails", {})
        if "ecsService" in resource_details or "ecsCluster" in resource_details:
            continue
        resource_id = rec.get("resourceId", "N/A")
        resource_name = rec.get("Name", rec.get("ResourceName", ""))
        all_text = f"{resource_id} {resource_name}".lower()
        if (
            "ecs-cluster" in all_text
            or "/cronjob" in all_text
            or "forwarder" in all_text
            or "lambda" in all_text
            or "ecs" in all_text
        ):
            continue
        # Spot recs are kept (adapter already counted them); see _render_ec2_enhanced_checks.
        finding = rec.get("finding", "").lower()
        if finding == "optimized":
            continue

        action = rec.get("actionType", "Other")
        if action not in grouped_actions:
            grouped_actions[action] = []
        grouped_actions[action].append(rec)

    content = ""
    for action, recs in grouped_actions.items():
        total_savings = sum(r.get("estimatedMonthlySavings", 0) for r in recs)
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>Action: {action} ({len(recs)} resources)</h4>"
        content += f'<p class="savings"><strong>Total Monthly Savings:</strong> ${total_savings:.2f}</p>'
        content += "<p><strong>Resources:</strong></p><ul>"
        for rec in recs:
            resource_id = rec.get("resourceId", "N/A")
            current_type = (
                rec.get("currentResourceDetails", {})
                .get("ec2Instance", {})
                .get("configuration", {})
                .get("instance", {})
                .get("type", "N/A")
            )
            rec_type = (
                rec.get("recommendedResourceDetails", {})
                .get("ec2Instance", {})
                .get("configuration", {})
                .get("instance", {})
                .get("type", "N/A")
            )
            savings = rec.get("estimatedMonthlySavings", 0)

            if current_type != "N/A" and rec_type != "N/A":
                content += f"<li>{resource_id}: {current_type} → {rec_type} (${savings:.2f}/month)</li>"
            else:
                content += f"<li>{resource_id} (${savings:.2f}/month)</li>"
        content += "</ul></div>"
    return content


def _render_ec2_compute_optimizer(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders EC2 Compute Optimizer findings grouped by finding type. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_findings: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        if "instanceArn" not in rec:
            continue
        finding = rec.get("finding", "Unknown")
        if finding.lower() == "optimized" or finding.upper() == "UNDER_PROVISIONED":
            continue

        if finding not in grouped_findings:
            grouped_findings[finding] = []
        grouped_findings[finding].append(rec)

    content = ""
    for finding, recs in grouped_findings.items():
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>Finding: {finding} ({len(recs)} instances)</h4>"
        content += "<p><strong>Instances:</strong></p><ul>"
        for rec in recs:
            instance_name = rec.get("instanceName", "N/A")
            instance_id = rec.get("instanceArn", "").split("/")[-1] if rec.get("instanceArn") else "N/A"
            current_type = rec.get("currentInstanceType", "N/A")

            rec_type = "N/A"
            if rec.get("recommendationOptions"):
                rec_type = rec["recommendationOptions"][0].get("instanceType", "N/A")

            display_name = instance_name if instance_name != "N/A" else instance_id
            if rec_type != "N/A":
                content += f"<li>{display_name}: {current_type} → {rec_type}</li>"
            else:
                content += f"<li>{display_name}: {current_type}</li>"
        content += "</ul></div>"
    return content


def _render_ebs_cost_hub(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders EBS Cost Optimization Hub recommendations grouped by action. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_actions: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        if "actionType" not in rec or "ebsVolume" not in rec.get("currentResourceDetails", {}):
            continue
        finding = rec.get("finding", "").lower()
        if finding == "optimized":
            continue

        action = rec.get("actionType", "Other")
        if action not in grouped_actions:
            grouped_actions[action] = []
        grouped_actions[action].append(rec)

    content = ""
    for action, recs in grouped_actions.items():
        total_savings = sum(r.get("estimatedMonthlySavings", 0) for r in recs)
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>Action: {action} ({len(recs)} volumes)</h4>"
        content += f'<p class="savings"><strong>Total Monthly Savings:</strong> ${total_savings:.2f}</p>'
        content += "<p><strong>Volumes:</strong></p><ul>"
        for rec in recs:
            resource_id = rec.get("resourceId", "N/A")
            ebs_config = rec.get("currentResourceDetails", {}).get("ebsVolume", {}).get("configuration", {})
            volume_type = ebs_config.get("storage", {}).get("type", "N/A")
            volume_size = ebs_config.get("storage", {}).get("sizeInGb", 0)
            savings = rec.get("estimatedMonthlySavings", 0)

            content += f"<li>{resource_id}: {volume_type} ({volume_size} GB) - ${savings:.2f}/month</li>"
        content += "</ul></div>"
    return content


def _render_ebs_unattached(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders unattached EBS volumes with deletion recommendations. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    total_cost = sum(r.get("EstimatedMonthlyCost", 0) for r in recommendations)
    content = f'<div class="rec-item{_priority_class(recommendations[0])}">'
    content += f"<h4>Unattached Volumes ({len(recommendations)} volumes)</h4>"
    content += f"<p><strong>Recommendation:</strong> Delete unattached volumes (create snapshots first if needed)</p>"
    content += f'<p class="savings"><strong>Total Monthly Savings:</strong> ${total_cost:.2f}</p>'
    content += "<p><strong>Volumes:</strong></p><ul>"

    for rec in recommendations:
        volume_id = rec.get("VolumeId", "N/A")
        volume_type = rec.get("VolumeType", "N/A")
        size = rec.get("Size", 0)
        cost = rec.get("EstimatedMonthlyCost", 0)
        content += f"<li>{volume_id}: {volume_type} ({size} GB) - ${cost:.2f}/month</li>"

    content += "</ul></div>"
    return content


def _render_ebs_gp2_migration(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders gp2-to-gp3 migration recommendations for EBS. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    if not recommendations:
        return ""
    # Sum the per-volume EstimatedSavings strings written by the adapter.
    total_savings = 0.0
    for rec in recommendations:
        savings_str = rec.get("EstimatedSavings", "")
        if "$" in savings_str:
            try:
                total_savings += float(savings_str.replace("$", "").split("/")[0])
            except (ValueError, AttributeError) as e:
                logger.debug("Could not parse gp2 migration savings %r: %s", savings_str, e)
    content = f'<div class="rec-item{_priority_class(recommendations[0])}">'
    content += f"<h4>gp2 to gp3 Migration ({len(recommendations)} volumes)</h4>"
    content += "<p><strong>Recommendation:</strong> Migrate gp2 volumes to gp3 for 20% cost savings</p>"
    content += f'<p class="savings"><strong>Estimated Savings:</strong> ${total_savings:.2f}/month</p>'
    content += "<p><strong>Volumes:</strong></p><ul>"
    for rec in recommendations:
        volume_id = rec.get("VolumeId", "N/A")
        size = rec.get("Size", 0)
        per_vol = rec.get("EstimatedSavings", "")
        if "$" in per_vol:
            content += f"<li>{volume_id}: {size} GB — <span class=\"savings\">{per_vol}</span></li>"
        else:
            content += f"<li>{volume_id}: {size} GB</li>"
    content += "</ul></div>"
    return content


def _render_ebs_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders EBS enhanced-check recommendations grouped by category.

    Called by: HTMLReportGenerator._get_detailed_recommendations.

    ``"Unattached Volumes"`` is the only category filtered out — it has a
    dedicated ``unattached_volumes`` source / renderer to avoid double rendering.
    Snapshot and encrypted-volume categories are rendered here alongside other
    enhanced checks (previously dropped, see audit finding L3-EBS-001).
    """
    grouped_checks: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        if "CheckCategory" not in rec:
            continue
        category = rec.get("CheckCategory", "Other")
        # Unattached is rendered by _render_ebs_unattached against its own source.
        if "unattached" in category.lower():
            continue
        # Snapshot findings are consolidated into the dedicated Snapshots tab
        # (see HTMLReportGenerator._extract_snapshots_data /
        # _get_snapshots_content). Skipping them here prevents the same
        # SnapshotId from appearing once per CheckCategory in EBS and again in
        # Snapshots, which previously inflated the visual savings rollup.
        if "snapshot" in category.lower():
            continue
        if category not in grouped_checks:
            grouped_checks[category] = []
        grouped_checks[category].append(rec)

    content = ""
    for category, recs in grouped_checks.items():
        total_savings = 0
        for r in recs:
            savings_str = r.get("EstimatedSavings", "")
            if "$" in savings_str:
                try:
                    total_savings += float(savings_str.replace("$", "").split("/")[0])
                except (ValueError, AttributeError) as e:
                    logger.debug("Could not parse EBS savings %r: %s", savings_str, e)

        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>{category} ({len(recs)} volumes)</h4>"
        content += f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Optimize volumes')}</p>"
        if total_savings > 0:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> ${total_savings:.2f}/month</p>'
        else:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {recs[0].get("EstimatedSavings", "Cost optimization")}</p>'
        content += "<p><strong>Volumes:</strong></p><ul>"

        for rec in recs:
            volume_id = rec.get("VolumeId", rec.get("SnapshotId", "N/A"))
            if "Size" in rec:
                content += f"<li>{volume_id}: {rec.get('Size')} GB"
                if "CurrentType" in rec:
                    content += f" ({rec.get('CurrentType')} → {rec.get('RecommendedType')})"
                content += "</li>"
            else:
                content += f"<li>{volume_id}</li>"

        content += "</ul></div>"
    return content


def _render_ebs_compute_optimizer(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders EBS Compute Optimizer findings grouped by finding type. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_findings: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        if "volumeArn" not in rec:
            continue
        finding = rec.get("finding", "Unknown")
        if finding.lower() == "optimized" or finding.upper() == "UNDER_PROVISIONED":
            continue

        if finding not in grouped_findings:
            grouped_findings[finding] = []
        grouped_findings[finding].append(rec)

    content = ""
    for finding, recs in grouped_findings.items():
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>Finding: {finding} ({len(recs)} volumes)</h4>"
        content += "<p><strong>Volumes:</strong></p><ul>"
        for rec in recs:
            volume_id = rec.get("volumeArn", "N/A").split("/")[-1] if rec.get("volumeArn") else "N/A"
            current_config = rec.get("currentConfiguration", {})
            volume_type = current_config.get("volumeType", "N/A")
            volume_size_data = current_config.get("volumeSize", 0)
            volume_size = volume_size_data.get("value", 0) if isinstance(volume_size_data, dict) else volume_size_data

            content += f"<li>{volume_id}: {volume_type} ({volume_size} GB)</li>"
        content += "</ul></div>"
    return content


def _render_rds_compute_optimizer(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders RDS Compute Optimizer findings grouped by finding type. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_findings: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        if "resourceArn" not in rec:
            continue
        finding = rec.get("instanceFinding", rec.get("finding", "Unknown"))
        reason_codes = rec.get("instanceFindingReasonCodes", [])

        if finding.lower() == "optimized":
            has_savings = False
            if rec.get("instanceRecommendationOptions"):
                for option in rec["instanceRecommendationOptions"]:
                    savings = option.get("savingsOpportunity", {}).get("estimatedMonthlySavings", {}).get("value", 0)
                    if savings > 0:
                        has_savings = True
                        break
            if not has_savings:
                continue

        if finding == "Unknown" and not rec.get("instanceRecommendationOptions"):
            continue

        if finding not in grouped_findings:
            grouped_findings[finding] = []
        grouped_findings[finding].append(rec)

    content = ""
    for finding, recs in grouped_findings.items():
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        count = len(recs)
        label = "database" if count == 1 else "databases"
        content += f"<h4>Finding: {finding} ({count} {label})</h4>"

        if recs[0].get("instanceRecommendationOptions"):
            content += (
                "<p><strong>Recommendation:</strong> Optimize instance class for better performance or cost savings</p>"
            )

        content += "<p><strong>Databases:</strong></p><ul>"
        for rec in recs:
            resource_arn = rec.get("resourceArn", "N/A")
            db_name = resource_arn.split(":")[-1] if resource_arn != "N/A" else "N/A"
            engine = rec.get("engine", "Unknown")
            engine_version = rec.get("engineVersion", "")
            current_class = rec.get("currentDBInstanceClass", "N/A")

            rec_class = "N/A"
            if rec.get("instanceRecommendationOptions"):
                rec_class = rec["instanceRecommendationOptions"][0].get("dbInstanceClass", "N/A")

            display_str = f"{db_name} ({engine}"
            if engine_version:
                display_str += f" {engine_version}"
            display_str += ")"

            if current_class != "N/A":
                display_str += f": {current_class}"
                if rec_class != "N/A":
                    display_str += f" → {rec_class}"

            content += f"<li>{display_str}</li>"
        content += "</ul></div>"
    return content


def _render_rds_ri_scenarios_table(recs: List[Rec]) -> str:
    """Render a per-database RI scenario table (1yr / 3yr × payment options).

    Each rec is expected to carry an ``RIScenarios`` list of
    ``{term, payment_option, monthly_savings, discount_pct, ondemand_monthly_estimate}``
    dicts produced by ``services/rds.py:get_enhanced_rds_checks``. Replaces the
    legacy single-line "$X/month with 1-yr no-upfront RI" disclosure with the
    full purchase matrix so the FinOps reader can compare upfront vs. recurring
    commitments without leaving the report.
    """
    out = ""
    for rec in recs:
        db_id = rec.get("DBInstanceIdentifier") or rec.get("DBClusterIdentifier") or "Unknown"
        engine = rec.get("engine") or rec.get("Engine") or ""
        instance_class = rec.get("DBInstanceClass") or ""
        scenarios = rec.get("RIScenarios") or []
        ondemand = rec.get("OnDemandMonthlyEstimate", 0.0)

        header_parts = [db_id]
        if instance_class:
            header_parts.append(instance_class)
        if engine:
            header_parts.append(engine)
        header = " · ".join(header_parts)

        out += '<div class="ri-scenarios">'
        out += f"<p class='ri-scenarios__header'><strong>{header}</strong>"
        if ondemand:
            out += f' <span class="ri-scenarios__base">on-demand ≈ ${ondemand:,.2f}/month</span>'
        out += "</p>"

        if not scenarios:
            out += "<p><em>Scenario matrix unavailable.</em></p></div>"
            continue

        out += "<table class='rec-table ri-scenarios__table'><thead><tr>"
        out += "<th>Term</th><th>Payment</th><th>Discount</th><th>Monthly savings</th>"
        out += "</tr></thead><tbody>"

        best_idx = max(range(len(scenarios)), key=lambda i: scenarios[i].get("monthly_savings", 0))
        for idx, sc in enumerate(scenarios):
            term = sc.get("term", "")
            payment = sc.get("payment_option", "")
            discount = sc.get("discount_pct", 0)
            monthly = sc.get("monthly_savings", 0)
            row_class = " class='ri-scenarios__row--best'" if idx == best_idx else ""
            out += (
                f"<tr{row_class}>"
                f"<td>{term}</td>"
                f"<td>{payment}</td>"
                f"<td>{discount:.1f}%</td>"
                f"<td>${monthly:,.2f}</td>"
                f"</tr>"
            )
        out += "</tbody></table>"
        out += "</div>"
    return out


def _render_rds_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders RDS enhanced-check recommendations grouped by category. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_categories: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("CheckCategory", "Other")
        if category not in grouped_categories:
            grouped_categories[category] = []
        grouped_categories[category].append(rec)

    content = ""
    for category, recs in grouped_categories.items():
        content += f'<div class="rec-item{_priority_class(recs[0])}">'

        count = len(recs)
        if "Snapshot" in category:
            label = "snapshot" if count == 1 else "snapshots"
            content += f"<h4>{category} ({count} {label})</h4>"
        else:
            label = "database" if count == 1 else "databases"
            content += f"<h4>{category} ({count} {label})</h4>"

        content += f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Optimize configuration')}</p>"

        is_ri_category = category == "Reserved Instance Opportunities" and any(r.get("RIScenarios") for r in recs)

        if is_ri_category:
            best_total = sum(max(s.get("monthly_savings", 0) for s in r.get("RIScenarios", [])) for r in recs)
            content += (
                '<p class="savings"><strong>Estimated Savings:</strong> '
                f'up to ${best_total:,.2f}/month at the best-tier scenario per database'
                '</p>'
            )
            content += _render_rds_ri_scenarios_table(recs)
            content += "</div>"
            continue

        total_savings = 0
        has_numeric_savings = False
        for rec in recs:
            savings_str = rec.get("EstimatedSavings", "")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    clean_str = savings_str.replace("$", "").replace("/month", "").split("(")[0].strip()
                    savings_val = float(clean_str)
                    total_savings += savings_val
                    has_numeric_savings = True
                except (ValueError, AttributeError) as e:
                    logger.debug("Could not parse grouped savings %r: %s", savings_str, e)

        if has_numeric_savings:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> ${total_savings:.2f}/month</p>'
        else:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {recs[0].get("EstimatedSavings", "Cost optimization")}</p>'

        if "Snapshot" in category:
            content += "<p><strong>Affected Snapshots:</strong></p><ul>"
        else:
            content += "<p><strong>Affected Databases:</strong></p><ul>"

        for rec in recs:
            db_id = (
                rec.get("DBInstanceIdentifier") or rec.get("DBClusterIdentifier") or rec.get("SnapshotId") or "Unknown"
            )
            engine = rec.get("Engine", rec.get("engine", ""))
            engine_version = rec.get("EngineVersion", rec.get("engineVersion", ""))
            finding = rec.get("instanceFinding", rec.get("storageFinding", ""))

            if "Snapshot" in category:
                display_str = db_id
                if finding:
                    display_str += f" - {finding}"
            else:
                display_str = db_id
                if engine:
                    display_str += f" ({engine}"
                    if engine_version:
                        display_str += f" {engine_version}"
                    display_str += ")"
                if finding:
                    display_str += f" - {finding}"

            content += f"<li>{display_str}</li>"
        content += "</ul></div>"
    return content


def _render_s3_bucket_analysis(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders S3 bucket-level recommendations grouped by optimisation type.

    Handles the ``s3_bucket_analysis`` source shape (Name + SizeGB +
    EstimatedMonthlyCost + SavingsDelta). Per-group savings sum the
    per-bucket ``SavingsDelta`` values computed by ``services/s3.py`` from
    ``S3_SAVINGS_FACTORS`` (audit L2-S3-001) — replaces the legacy hard-coded
    "40-95%" prose strings.

    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    grouped_s3: Dict[str, List[Rec]] = {
        "No Lifecycle Policy": [],
        "No Intelligent Tiering": [],
        "Static Website Optimization": [],
        "Both Missing": [],
        "Other Optimizations": [],
    }

    for rec in recommendations:
        bucket_name = rec.get("Name") or rec.get("BucketName", "Unknown")
        bucket_size = rec.get("SizeGB", 0)
        bucket_cost = rec.get("EstimatedMonthlyCost", 0)

        if bucket_name == "Unknown" or not bucket_name:
            continue
        if bucket_size == 0 and bucket_cost == 0 and rec.get("CheckCategory"):
            # Defensive: enhanced-check records shouldn't reach this handler
            # anymore (split into _render_s3_enhanced_checks) — skip just in
            # case stale fixtures arrive.
            continue

        has_lifecycle = rec.get("HasLifecyclePolicy", False)
        has_tiering = rec.get("HasIntelligentTiering", False)
        is_static_website = rec.get("IsStaticWebsite", False)

        if is_static_website:
            grouped_s3["Static Website Optimization"].append(rec)
        elif not has_lifecycle and not has_tiering:
            grouped_s3["Both Missing"].append(rec)
        elif not has_lifecycle:
            grouped_s3["No Lifecycle Policy"].append(rec)
        elif not has_tiering:
            grouped_s3["No Intelligent Tiering"].append(rec)
        else:
            grouped_s3["Other Optimizations"].append(rec)

    content = ""
    for group_name, buckets in grouped_s3.items():
        if not buckets:
            continue

        total_size = sum(b.get("SizeGB", 0) for b in buckets)
        total_cost = sum(b.get("EstimatedMonthlyCost", 0) for b in buckets)
        # Real per-group dollars from per-bucket SavingsDelta.
        total_savings = sum(float(b.get("SavingsDelta", 0.0) or 0.0) for b in buckets)

        content += f'<div class="rec-item{_priority_class(buckets[0])}">'
        content += f"<h4>{group_name} ({len(buckets)} buckets, {total_size:.2f} GB total)</h4>"

        if group_name == "No Lifecycle Policy":
            content += "<p><strong>Recommendation:</strong> Implement lifecycle policies to automatically transition objects to cheaper storage classes</p>"
        elif group_name == "No Intelligent Tiering":
            content += (
                "<p><strong>Recommendation:</strong> Enable Intelligent Tiering for automatic cost optimization</p>"
            )
        elif group_name == "Static Website Optimization":
            content += "<p><strong>Recommendation:</strong> Enable CloudFront CDN for reduced data transfer costs and improved performance</p>"
        elif group_name == "Both Missing":
            content += (
                "<p><strong>Recommendation:</strong> Implement lifecycle policies AND enable Intelligent Tiering</p>"
            )
        else:
            content += "<p><strong>Recommendation:</strong> Review other optimization opportunities</p>"

        if total_savings > 0:
            content += (
                f'<p class="savings"><strong>Estimated Savings:</strong> ${total_savings:.2f}/month</p>'
            )
        else:
            content += (
                '<p class="savings"><strong>Estimated Savings:</strong> $0.00/month — data transfer dependent</p>'
            )

        if total_cost > 0:
            content += f"<p><strong>Current Monthly Cost:</strong> ${total_cost:.2f}</p>"

        content += "<p><strong>Buckets:</strong></p><ul>"
        for bucket in buckets:
            bucket_name = bucket.get("Name") or bucket.get("BucketName", "Unknown")
            bucket_size = bucket.get("SizeGB", 0)
            bucket_cost = bucket.get("EstimatedMonthlyCost", 0)
            bucket_savings = float(bucket.get("SavingsDelta", 0.0) or 0.0)
            content += f"<li>{bucket_name}: {bucket_size:.2f} GB"
            if bucket_cost > 0:
                content += f" (${bucket_cost:.2f}/month"
                if bucket_savings > 0:
                    content += f", save ${bucket_savings:.2f}/month"
                content += ")"
            content += "</li>"
        content += "</ul></div>"
    return content


def _render_s3_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders S3 enhanced-check recommendations grouped by CheckCategory.

    Handles the ``enhanced_checks`` source shape (BucketName + CheckCategory
    + EstimatedSavings as ``$X.YY/month`` or ``$0.00/month - <reason>``).
    Previously these records were silently dropped by the bucket-analysis
    renderer (audit L3-S3-001); now they get a dedicated handler.

    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    grouped: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("CheckCategory", "Other")
        grouped.setdefault(category, []).append(rec)

    content = ""
    for category, recs in grouped.items():
        total_savings = 0.0
        for r in recs:
            savings_str = str(r.get("EstimatedSavings", ""))
            if "$" in savings_str:
                try:
                    total_savings += float(
                        savings_str.replace("$", "").split("/")[0].replace(",", "")
                    )
                except (ValueError, AttributeError) as e:
                    logger.debug("Could not parse S3 savings %r: %s", savings_str, e)

        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>{category} ({len(recs)} buckets)</h4>"
        content += (
            f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Review configuration')}</p>"
        )
        if total_savings > 0:
            content += (
                f'<p class="savings"><strong>Estimated Savings:</strong> ${total_savings:.2f}/month</p>'
            )
        else:
            content += (
                f'<p class="savings"><strong>Estimated Savings:</strong> {recs[0].get("EstimatedSavings", "$0.00/month")}</p>'
            )
        content += "<p><strong>Buckets:</strong></p><ul>"
        for rec in recs:
            bucket_name = rec.get("BucketName", rec.get("Name", "Unknown"))
            content += f"<li>{bucket_name}"
            if rec.get("IncompleteUploads"):
                content += f" — {rec['IncompleteUploads']} incomplete uploads"
            if rec.get("AgeDays"):
                content += f" — {rec['AgeDays']} days old"
            content += "</li>"
        content += "</ul></div>"
    return content


def _render_dynamodb_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders DynamoDB recommendations grouped by billing optimisation. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_dynamo = {
        "Provisioned to On-Demand": [],
        "On-Demand to Provisioned": [],
        "Enable Auto Scaling": [],
        "Reserved Capacity": [],
        "Other Optimizations": [],
    }

    for rec in recommendations:
        billing_mode = rec.get("BillingMode", "Unknown")
        opportunities = rec.get("OptimizationOpportunities", [])
        check_category = rec.get("CheckCategory", "")

        if "Switch to On-Demand" in str(opportunities) or "On-Demand" in check_category:
            grouped_dynamo["Provisioned to On-Demand"].append(rec)
        elif "Switch to Provisioned" in str(opportunities) or "Provisioned" in check_category:
            grouped_dynamo["On-Demand to Provisioned"].append(rec)
        elif "Enable Auto Scaling" in str(opportunities) or "Auto Scaling" in check_category:
            grouped_dynamo["Enable Auto Scaling"].append(rec)
        elif "Reserved Capacity" in str(opportunities) or "Reserved" in check_category:
            grouped_dynamo["Reserved Capacity"].append(rec)
        elif opportunities:
            grouped_dynamo["Other Optimizations"].append(rec)
        else:
            continue

    content = ""
    for group_name, tables in grouped_dynamo.items():
        if not tables:
            continue

        content += f'<div class="rec-item{_priority_class(tables[0])}">'
        content += f"<h4>{group_name} ({len(tables)} tables)</h4>"

        if group_name == "Provisioned to On-Demand":
            content += "<p><strong>Recommendation:</strong> Switch to On-Demand billing for unpredictable workloads</p>"
        elif group_name == "On-Demand to Provisioned":
            content += "<p><strong>Recommendation:</strong> Switch to Provisioned mode for predictable workloads (Save 20-60%)</p>"
        elif group_name == "Enable Auto Scaling":
            content += "<p><strong>Recommendation:</strong> Enable Auto Scaling to optimize capacity</p>"
        elif group_name == "Reserved Capacity":
            content += (
                "<p><strong>Recommendation:</strong> Purchase Reserved Capacity for steady workloads (Save 53-76%)</p>"
            )

        content += "<p><strong>Tables:</strong></p><ul>"
        for table in tables:
            table_name = table.get("TableName", "Unknown")
            billing = table.get("BillingMode", "Unknown")
            content += f"<li>{table_name} ({billing})</li>"
        content += "</ul></div>"
    return content


def _render_containers_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders ECS/EKS/ECR recommendations grouped by check category. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_containers = {
        "ECS Container Insights Required": [],
        "ECS Rightsizing - Metric-Backed": [],
        "ECS Over-Provisioned Services": [],
        "Unused ECS Clusters": [],
        "Unused EKS Clusters": [],
        "ECR Lifecycle Missing": [],
        "Other Optimizations": [],
    }

    for rec in recommendations:
        check_category = rec.get("CheckCategory", "")

        if check_category in grouped_containers:
            grouped_containers[check_category].append(rec)
        elif "ClusterName" in rec:
            if "Version" in rec:
                if rec.get("Status") == "INACTIVE" or rec.get("NodeGroupsCount", 0) == 0:
                    grouped_containers["Unused EKS Clusters"].append(rec)
                else:
                    grouped_containers["Other Optimizations"].append(rec)
            else:
                if rec.get("CheckCategory") == "Unused ECS Clusters":
                    grouped_containers["Unused ECS Clusters"].append(rec)
                else:
                    grouped_containers["Other Optimizations"].append(rec)
        elif "RepositoryName" in rec:
            grouped_containers["ECR Lifecycle Missing"].append(rec)
        else:
            grouped_containers["Other Optimizations"].append(rec)

    content = ""
    for group_name, resources in grouped_containers.items():
        if not resources:
            continue

        content += f'<div class="rec-item{_priority_class(resources[0])}">'
        content += f"<h4>{group_name} ({len(resources)} resources)</h4>"

        if group_name == "ECS Container Insights Required":
            content += "<p><strong>Recommendation:</strong> Enable Container Insights to get metric-backed rightsizing recommendations</p>"
        elif group_name == "ECS Rightsizing - Metric-Backed":
            content += "<p><strong>Recommendation:</strong> Downsize task definitions based on measured low utilization over 7 days</p>"
        elif group_name == "ECS Over-Provisioned Services":
            content += "<p><strong>Recommendation:</strong> Reduce desired task count to match actual running tasks</p>"
        elif group_name == "Unused ECS Clusters":
            content += "<p><strong>Recommendation:</strong> Delete unused ECS clusters with no running tasks</p>"
        elif group_name == "Unused EKS Clusters":
            content += "<p><strong>Recommendation:</strong> Delete unused EKS clusters with no node groups</p>"
        elif group_name == "ECR Lifecycle Missing":
            content += "<p><strong>Recommendation:</strong> Implement lifecycle policies to automatically clean up old images and reduce storage costs</p>"
        elif group_name == "Other Optimizations":
            content += "<p><strong>Recommendation:</strong> Optimize container resources through rightsizing, Spot instances, and efficient scheduling</p>"

        content += "<p><strong>Resources:</strong></p><ul>"
        cluster_names: set = set()
        for res in resources:
            if "ClusterName" in res:
                if "ServiceName" in res:
                    content += (
                        f"<li>{res.get('ServiceName', 'Unknown')} (Cluster: {res.get('ClusterName', 'Unknown')})</li>"
                    )
                else:
                    cluster_name = res.get("ClusterName", "Unknown")
                    if cluster_name not in cluster_names:
                        content += f"<li>{cluster_name}</li>"
                        cluster_names.add(cluster_name)
            elif "RepositoryName" in res:
                content += f"<li>{res.get('RepositoryName', 'Unknown')} ({res.get('ImageCount', 0)} images)</li>"
        content += "</ul></div>"
    return content


def _render_elasticache_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders ElastiCache recommendations grouped by category. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_elasticache: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("CheckCategory", "Other")
        if category not in grouped_elasticache:
            grouped_elasticache[category] = []
        grouped_elasticache[category].append(rec)

    content = ""
    for category, clusters in grouped_elasticache.items():
        if not clusters:
            continue

        content += f'<div class="rec-item{_priority_class(clusters[0])}">'
        label = "cluster" if len(clusters) == 1 else "clusters"
        content += f"<h4>{category} ({len(clusters)} {label})</h4>"
        content += f"<p><strong>Recommendation:</strong> {clusters[0].get('Recommendation', 'Optimize cluster')}</p>"

        savings_str = clusters[0].get("EstimatedSavings", "")
        if savings_str:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

        content += "<p><strong>Clusters:</strong></p><ul>"
        for cluster in clusters:
            cluster_id = cluster.get("ClusterId", "Unknown")
            node_type = cluster.get("NodeType", "")
            avg_cpu = cluster.get("AvgCPU")

            display_str = cluster_id
            if node_type:
                display_str += f" ({node_type})"
            if avg_cpu is not None:
                display_str += f" - {avg_cpu}% CPU"

            content += f"<li>{display_str}</li>"
        content += "</ul></div>"
    return content


def _render_opensearch_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders OpenSearch recommendations grouped by category. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_opensearch: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("CheckCategory", "Other")
        if category not in grouped_opensearch:
            grouped_opensearch[category] = []
        grouped_opensearch[category].append(rec)

    content = ""
    for category, domains in grouped_opensearch.items():
        if not domains:
            continue

        content += f'<div class="rec-item{_priority_class(domains[0])}">'
        label = "domain" if len(domains) == 1 else "domains"
        content += f"<h4>{category} ({len(domains)} {label})</h4>"
        content += f"<p><strong>Recommendation:</strong> {domains[0].get('Recommendation', 'Optimize domain')}</p>"

        savings_str = domains[0].get("EstimatedSavings", "")
        if savings_str:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

        content += "<p><strong>Domains:</strong></p><ul>"
        for domain in domains:
            domain_name = domain.get("DomainName", "Unknown")
            instance_type = domain.get("InstanceType", "")
            avg_cpu = domain.get("AvgCPU")

            display_str = domain_name
            if instance_type:
                display_str += f" ({instance_type})"
            if avg_cpu is not None:
                display_str += f" - {avg_cpu}% CPU"

            content += f"<li>{display_str}</li>"
        content += "</ul></div>"
    return content


def _render_network_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders network (EIP/NAT/LB/VPC) recommendations grouped by category. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_network: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("CheckCategory", "Other")
        if category not in grouped_network:
            grouped_network[category] = []
        grouped_network[category].append(rec)

    content = ""
    for category, resources in grouped_network.items():
        if not resources:
            continue

        content += f'<div class="rec-item{_priority_class(resources[0])}">'
        content += f"<h4>{category} ({len(resources)} resources)</h4>"
        content += f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"

        savings_str = resources[0].get("EstimatedSavings", "")
        if savings_str:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

        content += "<p><strong>Resources:</strong></p><ul>"
        for res in resources:
            if category == "Duplicate VPC Endpoints" and res.get("EndpointIds"):
                service_name = res.get("ServiceName", "").split(".")[-1] if res.get("ServiceName") else "unknown"
                for endpoint_id in res.get("EndpointIds", []):
                    content += f"<li>VPC Endpoint {endpoint_id} ({service_name})</li>"
                continue

            resource_name = res.get("ResourceName")
            if not resource_name:
                resource_id = (
                    res.get("AllocationId")
                    or res.get("NatGatewayId")
                    or res.get("LoadBalancerName")
                    or res.get("VpcEndpointId")
                    or res.get("VpcId")
                    or res.get("AutoScalingGroupName")
                    or res.get("InstanceId")
                    or (f"{res['ALBCount']} ALBs" if res.get("ALBCount") else None)
                    or (f"{res['BackupPlanCount']} backup plans" if res.get("BackupPlanCount") else None)
                    or "Unknown"
                )

                if resource_id.startswith("eipalloc-"):
                    public_ip = res.get("PublicIp", "")
                    resource_name = f"EIP {public_ip} ({resource_id})" if public_ip else resource_id
                elif resource_id.startswith("i-"):
                    instance_name = res.get("InstanceName", "Unknown")
                    instance_type = res.get("InstanceType", "unknown")
                    if instance_name != "Unknown":
                        resource_name = f"{instance_name} ({instance_type})"
                    else:
                        resource_name = f"{instance_type} ({resource_id})"
                elif resource_id.startswith("nat-"):
                    az = res.get("AvailabilityZone", "")
                    resource_name = f"NAT Gateway {resource_id} ({az})" if az else resource_id
                elif resource_id.startswith("vpc-"):
                    if res.get("ServiceName"):
                        service_name = res.get("ServiceName", "").split(".")[-1]
                        resource_name = f"VPC {resource_id} ({service_name} endpoint)"
                    else:
                        resource_name = f"VPC {resource_id}"
                elif resource_id.startswith("vpce-"):
                    if res.get("ServiceName"):
                        service_name = res.get("ServiceName", "").split(".")[-1]
                        resource_name = f"VPC Endpoint {resource_id} ({service_name})"
                    else:
                        resource_name = f"VPC Endpoint {resource_id}"
                elif resource_id.startswith("arn:aws:elasticloadbalancing"):
                    lb_name = resource_id.split("/")[-1] if "/" in resource_id else resource_id
                    resource_name = f"Load Balancer {lb_name}"
                else:
                    resource_name = resource_id

            content += f"<li>{resource_name}</li>"
        content += "</ul></div>"
    return content


def _render_monitoring_enhanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders CloudWatch/CloudTrail recommendations grouped by category. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_monitoring: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("CheckCategory", "Other")
        if category not in grouped_monitoring:
            grouped_monitoring[category] = []
        grouped_monitoring[category].append(rec)

    content = ""
    for category, resources in grouped_monitoring.items():
        if not resources:
            continue

        content += f'<div class="rec-item{_priority_class(resources[0])}">'
        content += f"<h4>{category} ({len(resources)} resources)</h4>"
        content += f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"

        savings_str = resources[0].get("EstimatedSavings", "")
        if savings_str:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

        content += "<p><strong>Resources:</strong></p><ul>"
        for res in resources:
            if "TrailNames" in res and isinstance(res["TrailNames"], list):
                resource_id = ", ".join(res["TrailNames"])
            else:
                resource_id = (
                    res.get("LogGroupName")
                    or res.get("TrailName")
                    or res.get("AlarmName")
                    or res.get("Namespace")
                    or res.get("BackupPlanName")
                    or res.get("HostedZoneId")
                    or res.get("HealthCheckId")
                    or (f"{res['BackupPlanCount']} backup plans" if res.get("BackupPlanCount") else None)
                    or "Unknown"
                )
            content += f"<li>{resource_id}</li>"
        content += "</ul></div>"
    return content


def _render_additional_services(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders miscellaneous service recommendations grouped by category. Called by: HTMLReportGenerator._get_detailed_recommendations."""
    grouped_additional: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("CheckCategory", "Other")
        if category not in grouped_additional:
            grouped_additional[category] = []
        grouped_additional[category].append(rec)

    content = ""
    for category, resources in grouped_additional.items():
        if not resources:
            continue

        content += f'<div class="rec-item{_priority_class(resources[0])}">'
        content += f"<h4>{category} ({len(resources)} resources)</h4>"
        content += f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"

        savings_str = resources[0].get("EstimatedSavings", "")
        if savings_str:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

        content += "<p><strong>Resources:</strong></p><ul>"
        for res in resources:
            resource_id = res.get(
                "DistributionId",
                res.get("ApiId", res.get("StateMachineArn", res.get("FunctionName", "Unknown"))),
            )
            if isinstance(resource_id, str) and ":" in resource_id:
                resource_id = resource_id.split(":")[-1]
            content += f"<li>{resource_id}</li>"
        content += "</ul></div>"
    return content


def _render_compute_optimizer_source(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders Compute Optimizer normalized recommendations grouped by finding type.

    Works for all four CO source blocks: ebs_recommendations, lambda_recommendations,
    ecs_recommendations, asg_recommendations. Expects recs with resource_id, resource_name,
    finding, current_config, recommended_config, estimatedMonthlySavings.
    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    grouped: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        finding = rec.get("finding", "Unknown")
        if finding.lower() == "optimized":
            continue
        if finding not in grouped:
            grouped[finding] = []
        grouped[finding].append(rec)

    if not grouped:
        return ""

    content = ""
    for finding, recs in grouped.items():
        total_savings = sum(r.get("estimatedMonthlySavings", 0) for r in recs)
        label = "resource" if len(recs) == 1 else "resources"
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        content += f"<h4>Finding: {finding} ({len(recs)} {label})</h4>"
        if total_savings > 0:
            content += f'<p class="savings"><strong>Estimated Monthly Savings:</strong> ${total_savings:.2f}</p>'
        content += "<p><strong>Resources:</strong></p><ul>"
        for rec in recs:
            resource_name = rec.get("resource_name") or rec.get("resource_id", "N/A")
            current = rec.get("current_config", {})
            recommended = rec.get("recommended_config", {})
            savings = rec.get("estimatedMonthlySavings", 0)

            line = resource_name
            if isinstance(current, dict) and isinstance(recommended, dict) and recommended:
                cur_val = current.get("instanceType") or current.get("memorySize") or current.get("volumeType")
                rec_val = (
                    recommended.get("instanceType") or recommended.get("memorySize") or recommended.get("volumeType")
                )
                if cur_val and rec_val and str(cur_val) != str(rec_val):
                    line += f": {cur_val} → {rec_val}"
            if savings > 0:
                line += f" (${savings:.2f}/month)"
            content += f"<li>{line}</li>"
        content += "</ul></div>"
    return content


def render_generic_per_rec(service_key: str, recommendations: List[Rec], source_name: str = "") -> str:
    """Render individual recommendations as separate HTML cards.

    Dispatches to service-specific generic renderers based on *service_key*.
    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    content = ""
    for rec in recommendations:
        # Spot recs are kept (adapter already counted them); see _render_ec2_enhanced_checks.
        finding = rec.get("finding", rec.get("instanceFinding", rec.get("InstanceFinding", ""))).lower()
        if finding == "optimized":
            continue

        content += f'<div class="rec-item{_priority_class(rec)}">'

        if service_key == "ec2":
            content = _render_generic_ec2_rec(content, rec)
        elif service_key == "ebs":
            content = _render_generic_ebs_rec(content, rec)
        elif service_key == "rds":
            should_skip, content = _render_generic_rds_rec(content, rec)
            if should_skip:
                continue
        elif service_key == "file_systems":
            content = _render_generic_file_systems_rec(content, rec)
        elif service_key == "s3":
            should_skip, content = _render_generic_s3_rec(content, rec)
            if should_skip:
                continue
        elif service_key == "dynamodb":
            content = _render_generic_dynamodb_rec(content, rec)
        elif service_key == "containers":
            content = _render_generic_containers_rec(content, rec)
        elif service_key == "lambda":
            content = _render_generic_lambda_rec(content, rec)
        else:
            content = _render_generic_other_rec(content, rec, source_name)

        content += "</div>"

    return content


def _render_generic_ec2_rec(content: str, rec: Rec) -> str:
    """Render a single EC2 recommendation card. Called by: render_generic_per_rec."""
    if "CheckCategory" in rec:
        content += f"<h4>{rec.get('CheckCategory', 'EC2 Optimization')}: {rec.get('InstanceId', rec.get('ImageId', rec.get('AllocationId', 'Resource')))}</h4>"
        if "InstanceType" in rec:
            content += f"<p><strong>Instance Type:</strong> {rec.get('InstanceType')}</p>"
        if "CurrentType" in rec:
            content += f"<p><strong>Current:</strong> {rec.get('CurrentType')} → <strong>Recommended:</strong> {rec.get('RecommendedType')}</p>"
        if "PublicIp" in rec:
            content += f"<p><strong>Elastic IP:</strong> {rec.get('PublicIp')}</p>"
        if "AgeDays" in rec:
            content += f"<p><strong>Age:</strong> {rec.get('AgeDays')} days</p>"
        content += f"<p><strong>Recommendation:</strong> {rec.get('Recommendation', 'Optimize resource')}</p>"
        content += f'<p class="savings"><strong>Estimated Savings:</strong> {rec.get("EstimatedSavings", "Cost optimization")}</p>'
    else:
        if "actionType" in rec:
            content += f"<h4>Resource: {rec.get('resourceId', 'N/A')}</h4>"
            content += f"<p><strong>Action:</strong> {rec.get('actionType', 'N/A')}</p>"
            content += (
                f'<p class="savings"><strong>Monthly Savings:</strong> ${rec.get("estimatedMonthlySavings", 0):.2f}</p>'
            )

            current_type = (
                rec.get("currentResourceDetails", {})
                .get("ec2Instance", {})
                .get("configuration", {})
                .get("instance", {})
                .get("type", "N/A")
            )
            if current_type != "N/A":
                content += f"<p><strong>Current Type:</strong> {current_type}</p>"

            rec_type = (
                rec.get("recommendedResourceDetails", {})
                .get("ec2Instance", {})
                .get("configuration", {})
                .get("instance", {})
                .get("type", "N/A")
            )
            if rec_type != "N/A":
                content += f"<p>Recommended Type: {rec_type}</p>"
        elif "instanceArn" in rec:
            instance_name = rec.get("instanceName", "N/A")
            instance_id = rec.get("instanceArn", "").split("/")[-1] if rec.get("instanceArn") else "N/A"
            content += f"<h4>Instance: {instance_name or instance_id}</h4>"
            content += f"<p><strong>Finding:</strong> {rec.get('finding', 'N/A')}</p>"
            content += f"<p><strong>Current Type:</strong> {rec.get('currentInstanceType', 'N/A')}</p>"
    return content


def _render_generic_ebs_rec(content: str, rec: Rec) -> str:
    """Render a single EBS recommendation card. Called by: render_generic_per_rec."""
    if "CheckCategory" in rec:
        content += f"<h4>{rec.get('CheckCategory', 'EBS Optimization')}: {rec.get('VolumeId', rec.get('SnapshotId', 'Resource'))}</h4>"
        if "Size" in rec:
            content += f"<p><strong>Size:</strong> {rec.get('Size')} GB</p>"
        if "CurrentType" in rec:
            content += f"<p><strong>Migration:</strong> {rec.get('CurrentType')} → {rec.get('RecommendedType')}</p>"
        if "CurrentIOPS" in rec:
            content += (
                f"<p><strong>IOPS:</strong> {rec.get('CurrentIOPS')} → {rec.get('RecommendedIOPS')} (recommended)</p>"
            )
        if "AgeDays" in rec:
            content += f"<p><strong>Age:</strong> {rec.get('AgeDays')} days</p>"
        content += f"<p><strong>Recommendation:</strong> {rec.get('Recommendation', 'Optimize resource')}</p>"
        content += f'<p class="savings"><strong>Estimated Savings:</strong> {rec.get("EstimatedSavings", "Cost optimization")}</p>'
    else:
        if "actionType" in rec and "ebsVolume" in rec.get("currentResourceDetails", {}):
            content += f"<h4>Resource: {rec.get('resourceId', 'N/A')}</h4>"
            content += f"<p><strong>Action:</strong> {rec.get('actionType', 'N/A')}</p>"
            content += (
                f'<p class="savings"><strong>Monthly Savings:</strong> ${rec.get("estimatedMonthlySavings", 0):.2f}</p>'
            )

            ebs_config = rec.get("currentResourceDetails", {}).get("ebsVolume", {}).get("configuration", {})
            storage = ebs_config.get("storage", {})
            current_type = storage.get("type", "N/A")
            current_size = storage.get("sizeInGb", 0)

            if current_type != "N/A":
                content += f"<p><strong>Current:</strong> {current_type} ({current_size} GB)</p>"

            rec_ebs_config = rec.get("recommendedResourceDetails", {}).get("ebsVolume", {}).get("configuration", {})
            rec_storage = rec_ebs_config.get("storage", {})
            rec_type = rec_storage.get("type", "N/A")
            rec_size = rec_storage.get("sizeInGb", 0)

            if rec_type != "N/A":
                content += f"<p>Recommended: {rec_type} ({rec_size} GB)</p>"
        elif "VolumeId" in rec:
            content += f"<h4>Volume: {rec.get('VolumeId', 'N/A')}</h4>"
            content += f"<p>Type: {rec.get('VolumeType', 'N/A')} - {rec.get('Size', 0)} GB</p>"
            content += f'<p class="savings">Monthly Cost: ${rec.get("EstimatedMonthlyCost", 0):.2f}</p>'
            content += f"<p><strong>Recommended Action:</strong> Delete unattached volume (create snapshot first if needed)</p>"
        else:
            volume_id = rec.get("volumeArn", "N/A").split("/")[-1] if rec.get("volumeArn") else "N/A"
            content += f"<h4>Volume: {volume_id}</h4>"
            content += f"<p>Finding: {rec.get('finding', 'N/A')}</p>"

            current_config = rec.get("currentConfiguration", {})
            content += (
                f"<p>Current: {current_config.get('volumeType', 'N/A')} - {current_config.get('volumeSize', 0)} GB"
            )
            if current_config.get("volumeBaselineIOPS"):
                content += f" - {current_config.get('volumeBaselineIOPS', 0)} IOPS"
            if current_config.get("volumeBaselineThroughput"):
                content += f" - {current_config.get('volumeBaselineThroughput', 0)} MB/s"
            content += "</p>"

            if rec.get("volumeRecommendationOptions"):
                content += "<p><strong>Recommended Actions:</strong></p><ul>"
                for i, option in enumerate(rec["volumeRecommendationOptions"][:2], 1):
                    config = option.get("configuration", {})
                    risk = option.get("performanceRisk", 0)

                    action_desc = f"Option {i}: "
                    changes = []

                    if config.get("volumeType") != current_config.get("volumeType"):
                        changes.append(f"Change type to {config.get('volumeType', 'N/A')}")
                    if config.get("volumeSize") != current_config.get("volumeSize"):
                        changes.append(f"Resize to {config.get('volumeSize', 0)} GB")
                    if config.get("volumeBaselineIOPS") != current_config.get("volumeBaselineIOPS"):
                        changes.append(f"Adjust IOPS to {config.get('volumeBaselineIOPS', 0)}")
                    if config.get("volumeBaselineThroughput") != current_config.get("volumeBaselineThroughput"):
                        changes.append(f"Adjust throughput to {config.get('volumeBaselineThroughput', 0)} MB/s")

                    if changes:
                        action_desc += ", ".join(changes)
                    else:
                        action_desc += "Optimize configuration"

                    action_desc += f" (Performance Risk: {risk})"
                    content += f"<li>{action_desc}</li>"
                content += "</ul>"
    return content


def _render_generic_rds_rec(content: str, rec: Rec) -> Tuple[bool, str]:
    """Render a single RDS recommendation card. Called by: render_generic_per_rec."""
    instance_finding = rec.get("instanceFinding", "N/A")
    storage_finding = rec.get("storageFinding", "N/A")
    has_recommendations = rec.get("instanceRecommendationOptions") or rec.get("storageRecommendationOptions")

    if instance_finding == "N/A" and storage_finding == "N/A" and not has_recommendations:
        return True, content

    resource_arn = rec.get("resourceArn", "N/A")
    db_name = resource_arn.split(":")[-1] if resource_arn != "N/A" else "N/A"
    content += f"<h4>Database: {db_name}</h4>"
    content += f"<p><strong>Engine:</strong> {rec.get('engine', 'N/A')} {rec.get('engineVersion', '')}</p>"

    if instance_finding != "N/A":
        content += f"<p><strong>Instance Finding:</strong> {instance_finding}</p>"

    if rec.get("instanceRecommendationOptions"):
        content += "<p><strong>Instance Recommendations:</strong></p><ul>"
        current_class = rec.get("dbInstanceClass", "N/A")
        content += f"<li>Current: {current_class}</li>"

        for i, option in enumerate(rec["instanceRecommendationOptions"][:2], 1):
            recommended_class = option.get("dbInstanceClass", "N/A")
            rank = option.get("rank", i)
            content += f"<li>Option {rank}: Migrate to {recommended_class}</li>"
        content += "</ul>"

    if storage_finding != "N/A":
        content += f"<p><strong>Storage Finding:</strong> {storage_finding}</p>"

        if rec.get("storageRecommendationOptions"):
            content += "<p><strong>Storage Recommendations:</strong></p><ul>"
            for option in rec["storageRecommendationOptions"][:1]:
                storage_config = option.get("storageConfiguration", {})
                storage_type = storage_config.get("storageType", "N/A")
                allocated_storage = storage_config.get("allocatedStorage", "N/A")
                iops = storage_config.get("iops", "N/A")
                content += f"<li>Optimize to: {storage_type}"
                if allocated_storage != "N/A":
                    content += f" - {allocated_storage} GB"
                if iops != "N/A":
                    content += f" - {iops} IOPS"
                content += "</li>"
            content += "</ul>"

    if rec.get("utilizationMetrics"):
        content += "<p><strong>Current Utilization:</strong></p><ul>"
        for metric in rec["utilizationMetrics"][:3]:
            metric_name = metric.get("name", "N/A")
            metric_value = metric.get("value", 0)
            statistic = metric.get("statistic", "N/A")
            content += f"<li>{metric_name} ({statistic}): {metric_value:.2f}</li>"
        content += "</ul>"

    return False, content


def _render_generic_file_systems_rec(content: str, rec: Rec) -> str:
    """Render a single file-system recommendation card. Called by: render_generic_per_rec."""
    if "FileSystemId" in rec and rec.get("FileSystemType"):
        fs_id = rec.get("FileSystemId", "N/A")
        fs_type = rec.get("FileSystemType", "N/A")
        content += f"<h4>FSx {fs_type}: {fs_id}</h4>"
        content += f"<p>Capacity: {rec.get('StorageCapacity', 0)} GB</p>"
        content += f"<p>Storage Type: {rec.get('StorageType', 'N/A')}</p>"
        content += f'<p class="savings">Monthly Cost: ${rec.get("EstimatedMonthlyCost", 0):.2f}</p>'

        opportunities = rec.get("OptimizationOpportunities", [])
        if opportunities:
            content += "<p><strong>Recommended Actions:</strong></p><ul>"
            for opp in opportunities:
                content += f"<li>{opp}</li>"
            content += "</ul>"

        potential_savings = rec.get("EstimatedMonthlyCost", 0) * 0.3
        if fs_type.upper() == "ONTAP":
            content += "<p><strong>ONTAP Optimizations:</strong></p><ul>"
            content += (
                f"<li>Enable data deduplication and compression (Save ~${potential_savings * 0.5:.2f}/month)</li>"
            )
            content += f"<li>Configure capacity pool for cold data (Save ~${potential_savings * 0.3:.2f}/month)</li>"
            content += "<li>Use SnapMirror for efficient replication</li>"
            content += "</ul>"
        elif fs_type.upper() == "LUSTRE":
            content += "<p><strong>Lustre Optimizations:</strong></p><ul>"
            content += f"<li>Consider scratch file systems for temporary workloads (Save ~${potential_savings * 0.6:.2f}/month)</li>"
            content += f"<li>Enable LZ4 data compression (Save ~${potential_savings * 0.2:.2f}/month)</li>"
            content += "<li>Optimize metadata configuration</li>"
            content += "</ul>"
        elif fs_type.upper() == "OPENZFS":
            content += "<p><strong>OpenZFS Optimizations:</strong></p><ul>"
            content += f"<li>Enable Intelligent-Tiering (Save ~${potential_savings * 0.5:.2f}/month)</li>"
            content += "<li>Use zero-copy snapshots and clones</li>"
            content += "<li>Configure user/group quotas</li>"
            content += "</ul>"

    else:
        fs_name = rec.get("Name") or rec.get("FileSystemId", "N/A")
        if fs_name == "Unnamed":
            fs_name = rec.get("FileSystemId", fs_name)
        content += f"<h4>EFS: {fs_name}</h4>"
        size_gb = rec.get("SizeGB", 0)
        size_display = f"{size_gb:.2f} GB" if isinstance(size_gb, float) else f"{size_gb} GB"
        if size_gb == 0 or (isinstance(size_gb, float) and size_gb < 0.1):
            size_display = "Nearly empty (< 0.1 GB)"
        content += f"<p>Size: {size_display}</p>"
        content += f"<p>Storage Class: {rec.get('StorageClass', 'N/A')}</p>"
        content += f"<p>Mount Targets: {rec.get('MountTargets', 0)}</p>"
        content += f'<p class="savings">Monthly Cost: ${rec.get("EstimatedMonthlyCost", 0):.2f}</p>'

        content += "<p><strong>Recommended Actions:</strong></p><ul>"

        if not rec.get("HasIAPolicy", True):
            ia_savings = rec.get("EstimatedMonthlyCost", 0) * 0.8
            content += f"<li>Enable Transition to IA after 30 days (Save ~${ia_savings:.2f}/month)</li>"

        if not rec.get("HasArchivePolicy", True):
            archive_savings = rec.get("EstimatedMonthlyCost", 0) * 0.9
            content += f"<li>Enable Transition to Archive after 90 days (Save ~${archive_savings:.2f}/month)</li>"

        if rec.get("StorageClass") == "Standard" and rec.get("SizeGB", 0) > 1:
            one_zone_savings = rec.get("EstimatedMonthlyCost", 0) * 0.47
            content += (
                f"<li>Consider One Zone storage if Multi-AZ not required (Save ~${one_zone_savings:.2f}/month)</li>"
            )

        if rec.get("MountTargets", 0) == 0 and rec.get("SizeGB", 0) < 0.1:
            content += f"<li>Delete unused file system (Save ${rec.get('EstimatedMonthlyCost', 0):.2f}/month)</li>"

        content += "</ul>"
    return content


def _render_generic_s3_rec(content: str, rec: Rec) -> Tuple[bool, str]:
    """Render a single S3 bucket recommendation card. Called by: render_generic_per_rec."""
    bucket_name = rec.get("Name") or rec.get("BucketName", "Unknown")
    bucket_size = rec.get("SizeGB", 0)
    bucket_cost = rec.get("EstimatedMonthlyCost", 0)

    if bucket_name == "Unknown" or not bucket_name:
        return True, content

    if bucket_name == "Unknown" and bucket_size == 0 and bucket_cost == 0:
        return True, content

    content += f"<h4>S3 Bucket: {bucket_name}</h4>"
    content += f"<p><strong>Size:</strong> {bucket_size:.2f} GB</p>"
    content += f"<p><strong>Monthly Cost:</strong> ${bucket_cost:.2f}</p>"
    content += f"<p><strong>Created:</strong> {rec.get('CreationDate', 'Unknown')}</p>"
    content += f"<p><strong>Lifecycle Policy:</strong> {'Yes' if rec.get('HasLifecyclePolicy') else 'No'}</p>"
    content += f"<p><strong>Intelligent Tiering:</strong> {'Yes' if rec.get('HasIntelligentTiering') else 'No'}</p>"

    opportunities = rec.get("OptimizationOpportunities", [])
    if opportunities:
        content += "<p><strong>Optimization Opportunities:</strong></p><ul>"
        for opp in opportunities:
            content += f"<li>{opp}</li>"
        content += "</ul>"

        if not rec.get("HasLifecyclePolicy"):
            content += "<p><strong>Lifecycle Policy Benefits:</strong></p><ul>"
            content += "<li>Transition to Standard-IA after 30 days (Save 40%)</li>"
            content += "<li>Transition to Glacier after 90 days (Save 68%)</li>"
            content += "<li>Transition to Deep Archive after 180 days (Save 95%)</li>"
            content += "</ul>"

        if not rec.get("HasIntelligentTiering"):
            content += "<p><strong>Intelligent Tiering Benefits:</strong></p><ul>"
            content += "<li>Automatic optimization based on access patterns</li>"
            content += "<li>Archive tiers for long-term storage (Save up to 95%)</li>"
            content += "<li>Small monitoring fee ($0.0025 per 1,000 objects)</li>"
            content += "</ul>"
    return False, content


def _render_generic_dynamodb_rec(content: str, rec: Rec) -> str:
    """Render a single DynamoDB table recommendation card. Called by: render_generic_per_rec."""
    content += f"<h4>DynamoDB Table: {rec.get('TableName', 'Unknown')}</h4>"
    content += f"<p><strong>Billing Mode:</strong> {rec.get('BillingMode', 'Unknown')}</p>"
    content += f"<p><strong>Status:</strong> {rec.get('TableStatus', 'Unknown')}</p>"
    content += f"<p><strong>Item Count:</strong> {rec.get('ItemCount', 0):,}</p>"
    content += f"<p><strong>Table Size:</strong> {rec.get('TableSizeBytes', 0) / (1024**2):.2f} MB</p>"

    if rec.get("BillingMode") == "PROVISIONED":
        content += f"<p><strong>Read Capacity:</strong> {rec.get('ReadCapacityUnits', 0)} RCU</p>"
        content += f"<p><strong>Write Capacity:</strong> {rec.get('WriteCapacityUnits', 0)} WCU</p>"
        content += f"<p><strong>Monthly Cost:</strong> ${rec.get('EstimatedMonthlyCost', 0):.2f}</p>"

    opportunities = rec.get("OptimizationOpportunities", [])
    if opportunities:
        content += "<p><strong>Optimization Opportunities:</strong></p><ul>"
        for opp in opportunities:
            content += f"<li>{opp}</li>"
        content += "</ul>"

        if rec.get("BillingMode") == "PROVISIONED":
            content += "<p><strong>Provisioned Mode Optimizations:</strong></p><ul>"
            content += "<li>Enable Auto Scaling for dynamic capacity adjustment</li>"
            content += "<li>Monitor consumed vs provisioned capacity</li>"
            content += "<li>Consider Reserved Capacity for steady workloads (Save 53-76%)</li>"
            content += "</ul>"
        else:
            content += "<p><strong>On-Demand Mode Considerations:</strong></p><ul>"
            content += "<li>Monitor request patterns for potential Provisioned savings</li>"
            content += "<li>Implement efficient access patterns</li>"
            content += "<li>Consider Provisioned mode if usage is predictable</li>"
            content += "</ul>"
    return content


def _render_generic_containers_rec(content: str, rec: Rec) -> str:
    """Render a single container (ECS/EKS/ECR) recommendation card. Called by: render_generic_per_rec."""
    if "ClusterName" in rec:
        if "Version" in rec:
            content += f"<h4>EKS Cluster: {rec.get('ClusterName', 'Unknown')}</h4>"
            content += f"<p><strong>Version:</strong> {rec.get('Version', 'Unknown')}</p>"
            content += f"<p><strong>Node Groups:</strong> {rec.get('NodeGroupsCount', 0)}</p>"
            content += f"<p><strong>Monthly Cost:</strong> ${rec.get('EstimatedMonthlyCost', 0):.2f}</p>"
        else:
            content += f"<h4>ECS Cluster: {rec.get('ClusterName', 'Unknown')}</h4>"
            content += f"<p><strong>Running Tasks:</strong> {rec.get('RunningTasksCount', 0)}</p>"
            content += f"<p><strong>Services:</strong> {rec.get('ServicesCount', 0)}</p>"

        content += f"<p><strong>Status:</strong> {rec.get('Status', 'Unknown')}</p>"

    elif "RepositoryName" in rec:
        content += f"<h4>ECR Repository: {rec.get('RepositoryName', 'Unknown')}</h4>"
        content += f"<p><strong>Images:</strong> {rec.get('ImageCount', 0)}</p>"
        content += f"<p><strong>Created:</strong> {rec.get('CreatedAt', 'Unknown')}</p>"

    opportunities = rec.get("OptimizationOpportunities", [])
    if opportunities:
        content += "<p><strong>Optimization Opportunities:</strong></p><ul>"
        for opp in opportunities:
            content += f"<li>{opp}</li>"
        content += "</ul>"
    return content


def _render_generic_lambda_rec(content: str, rec: Rec) -> str:
    """Render a single Lambda function recommendation card. Called by: render_generic_per_rec."""
    function_name = rec.get("FunctionName") or rec.get("resourceId", "Unknown")
    check_category = rec.get("CheckCategory", "Lambda Optimization")

    if "actionType" in rec:
        check_category = f"Lambda {rec['actionType']}"

    content += f"<h4>{check_category}: {function_name}</h4>"

    if "MemorySize" in rec:
        content += f"<p><strong>Memory Size:</strong> {rec['MemorySize']} MB</p>"
    elif "currentResourceDetails" in rec:
        lambda_config = rec.get("currentResourceDetails", {}).get("lambdaFunction", {}).get("configuration", {})
        compute_config = lambda_config.get("compute", {})
        if "memorySizeInMB" in compute_config:
            content += f"<p><strong>Memory Size:</strong> {compute_config['memorySizeInMB']} MB</p>"
        if "architecture" in compute_config:
            content += f"<p><strong>Architecture:</strong> {compute_config['architecture']}</p>"

    if "Timeout" in rec:
        content += f"<p><strong>Timeout:</strong> {rec['Timeout']} seconds</p>"
    if "Runtime" in rec:
        content += f"<p><strong>Runtime:</strong> {rec['Runtime']}</p>"
    if "Architecture" in rec:
        content += f"<p><strong>Architecture:</strong> {rec['Architecture']}</p>"

    if "Recommendation" in rec:
        content += f"<p><strong>Recommendation:</strong> {rec['Recommendation']}</p>"
    elif "actionType" in rec:
        if rec["actionType"] == "Rightsize":
            content += f"<p><strong>Recommendation:</strong> Right-size Lambda function memory allocation based on usage patterns</p>"
        else:
            content += (
                f"<p><strong>Recommendation:</strong> {rec['actionType']} Lambda function for cost optimization</p>"
            )

    if "EstimatedSavings" in rec:
        content += f'<p class="savings"><strong>Estimated Savings:</strong> {rec["EstimatedSavings"]}</p>'
    elif "estimatedMonthlySavings" in rec:
        monthly_savings = rec["estimatedMonthlySavings"]
        savings_pct = rec.get("estimatedSavingsPercentage", 0)
        content += f'<p class="savings"><strong>Estimated Savings:</strong> ${monthly_savings:.2f}/month ({savings_pct:.1f}%)</p>'
    return content


def _render_generic_other_rec(content: str, rec: Rec, source_name: str) -> str:
    """Render a generic recommendation card for any service. Called by: render_generic_per_rec."""
    check_category = rec.get("CheckCategory", source_name.replace("_", " ").title())
    resource_id = (
        rec.get("resource_id")
        or rec.get("LoadBalancerName")
        or rec.get("AutoScalingGroupName")
        or rec.get("VpcEndpointId")
        or rec.get("NatGatewayId")
        or rec.get("AllocationId")
        or rec.get("LogGroupName")
        or rec.get("TrailName")
        or rec.get("FunctionName")
        or rec.get("DistributionId")
        or rec.get("ApiId")
        or rec.get("VpcId")
        or rec.get("StateMachineArn", "").split(":")[-1]
        or rec.get("BackupPlanName")
        or rec.get("BackupVaultName")
        or rec.get("HostedZoneId")
        or rec.get("HealthCheckId")
        or rec.get("GroupName")
        or rec.get("PlanName")
        or rec.get("ResourceId")
        or rec.get("SnapshotId")
        or rec.get("DBClusterIdentifier")
        or rec.get("DBInstanceIdentifier")
        or rec.get("dbClusterIdentifier")
        or rec.get("dbInstanceIdentifier")
        or rec.get("ClusterName")
        or rec.get("DomainName")
        or rec.get("EndpointName")
        or rec.get("NotebookInstanceName")
        or rec.get("ProvisionedModelId")
        or rec.get("KnowledgeBaseId")
        or rec.get("AgentId")
        or rec.get("ServerId")
        or rec.get("ClusterArn", "").split("/")[-1]
        or rec.get("ContainerName")
        or rec.get("WorkgroupName")
        or rec.get("WorkspaceId")
        or rec.get("Namespace")
        or rec.get("FileSystemId")
        or rec.get("anomaly_id")
        or (f"{rec['BackupPlanCount']} backup plans" if rec.get("BackupPlanCount") else None)
        or (f"{rec['ALBCount']} ALBs" if rec.get("ALBCount") else None)
        or rec.get("resourceArn", "").split(":")[-1]
        if rec.get("resourceArn")
        else "Resource"
    )

    content += f"<h4>{check_category}: {resource_id}</h4>"

    for key, value in rec.items():
        if key not in ["CheckCategory", "Recommendation", "EstimatedSavings"] and not key.endswith("Arn"):
            if isinstance(value, (str, int, float)) and value:
                formatted_key = key.replace("_", " ").title()
                content += f"<p><strong>{formatted_key}:</strong> {value}</p>"

    if "Recommendation" in rec:
        content += f"<p><strong>Recommendation:</strong> {rec['Recommendation']}</p>"

    if "EstimatedSavings" in rec:
        content += f'<p class="savings"><strong>Estimated Savings:</strong> {rec["EstimatedSavings"]}</p>'
    return content


def render_s3_top_tables(service_data: Dict) -> str:
    """Render S3 top-bucket table with cost/size sort toggle.

    The previous version rendered two separate Top-10 tables (by cost and by
    size); for accounts where the largest bucket is also the most expensive,
    the two tables show the same rows in the same order — redundant and read
    as inattention. We now merge into one table whose rows are the union of
    both cuts, with sort buttons exposed so the reader can pivot in place.

    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    content = ""
    sources = service_data.get("sources", {})
    s3_data = sources.get("s3_bucket_analysis", {})

    top_cost = s3_data.get("top_cost_buckets", []) or []
    top_size = s3_data.get("top_size_buckets", []) or []

    if not top_cost and not top_size:
        return content

    # Union by bucket Name preserving first occurrence (cost-ordered first).
    merged: Dict[str, Dict[str, Any]] = {}
    for bucket in top_cost:
        name = bucket.get("Name", "N/A")
        if name not in merged:
            merged[name] = bucket
    for bucket in top_size:
        name = bucket.get("Name", "N/A")
        if name not in merged:
            merged[name] = bucket

    rows = list(merged.values())
    cost_max = max((b.get("EstimatedMonthlyCost", 0) or 0) for b in rows) or 0
    size_max = max((b.get("SizeGB", 0) or 0) for b in rows) or 0

    duplicate_note = ""
    if top_cost and top_size:
        # Compute whether the two ordered lists are identical in their first N rows.
        cost_names = [b.get("Name") for b in top_cost]
        size_names = [b.get("Name") for b in top_size]
        if cost_names == size_names:
            duplicate_note = (
                '<p class="info-note" style="margin: 12px 0;">'
                "Top buckets by cost and by size are the same in this account — "
                "the largest bucket is also the most expensive."
                "</p>"
            )

    content += "<h4>Top Buckets</h4>"
    content += duplicate_note
    content += (
        '<div class="bucket-sort-toggle" role="group" aria-label="Sort top buckets">'
        '<button type="button" class="bucket-sort-btn active" data-bucket-sort="cost" aria-pressed="true">Sort by cost</button>'
        '<button type="button" class="bucket-sort-btn" data-bucket-sort="size" aria-pressed="false">Sort by size</button>'
        "</div>"
    )
    content += '<div class="top-buckets-table">'
    content += "<table><thead><tr><th>Bucket Name</th><th>Size (GB)</th><th>Monthly Cost</th><th>Lifecycle</th><th>Intelligent Tiering</th></tr></thead><tbody>"
    for bucket in rows:
        name = bucket.get("Name", "N/A")
        size = bucket.get("SizeGB", 0) or 0
        cost = bucket.get("EstimatedMonthlyCost", 0) or 0
        # Encode sort keys as data attributes so the JS toggle can sort client-side
        # without re-rendering.
        content += (
            f'<tr data-cost="{cost:.6f}" data-size="{size:.6f}">'
            f"<td>{name}</td>"
            f'<td>{size:.2f}</td>'
            f'<td>${cost:.2f}</td>'
            f"<td>{'✓' if bucket.get('HasLifecyclePolicy') else '✗'}</td>"
            f"<td>{'✓' if bucket.get('HasIntelligentTiering') else '✗'}</td>"
            f"</tr>"
        )
    # The two `_max` values are not surfaced visually today but are kept on the
    # wrapping element so future sparkline/bar overlays can size against them
    # without a second pass through the data.
    content += "</tbody></table></div>"
    _ = (cost_max, size_max)  # quiet linters

    return content


def _render_ec2_advanced_checks(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders EC2 advanced-check recommendations grouped by category.

    Handles data from ``services.ec2.get_advanced_ec2_checks`` which produces
    records with InstanceId, InstanceType, Name, CheckCategory, Recommendation,
    EstimatedSavings.
    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    grouped: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("CheckCategory", "Other")
        if category not in grouped:
            grouped[category] = []
        grouped[category].append(rec)

    content = ""
    for category, recs in grouped.items():
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        label = "resource" if len(recs) == 1 else "resources"
        content += f"<h4>{category} ({len(recs)} {label})</h4>"
        content += f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Optimize resource')}</p>"

        total_savings = 0.0
        has_numeric = False
        for rec in recs:
            savings_str = rec.get("EstimatedSavings", "")
            if isinstance(savings_str, (int, float)):
                total_savings += float(savings_str)
                has_numeric = True
            elif isinstance(savings_str, str) and "$" in savings_str:
                try:
                    clean = savings_str.replace("$", "").replace("/month", "").split("(")[0].strip()
                    total_savings += float(clean)
                    has_numeric = True
                except (ValueError, AttributeError):
                    pass

        if has_numeric and total_savings > 0:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> ${total_savings:.2f}/month</p>'
        else:
            content += f'<p class="savings"><strong>Estimated Savings:</strong> {recs[0].get("EstimatedSavings", "Cost optimization")}</p>'

        content += "<p><strong>Affected Resources:</strong></p><ul>"
        for rec in recs:
            resource_id = rec.get("InstanceId", rec.get("resourceId", "Resource"))
            instance_type = rec.get("InstanceType", "")
            name = rec.get("Name", "")
            display = resource_id
            if name:
                display = f"{name} ({resource_id})"
            if instance_type:
                display += f" [{instance_type}]"
            content += f"<li>{display}</li>"
        content += "</ul></div>"
    return content


def _render_eks_source(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders EKS cost visibility recommendations grouped by check_category.

    Handles all 5 EKS source blocks: cluster_costs, node_group_optimization,
    fargate_analysis, addon_costs, cost_hub_recommendations.  Records have
    resource_id, check_type, check_category, monthly_savings, severity, reason.
    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    grouped: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        category = rec.get("check_category", "Other")
        if category not in grouped:
            grouped[category] = []
        grouped[category].append(rec)

    content = ""
    for category, recs in grouped.items():
        total_savings = sum(r.get("monthly_savings", 0.0) for r in recs)
        content += f'<div class="rec-item{_priority_class(recs[0])}">'
        label = "finding" if len(recs) == 1 else "findings"
        content += f"<h4>{category} ({len(recs)} {label})</h4>"
        if total_savings > 0:
            content += f'<p class="savings"><strong>Estimated Monthly Savings:</strong> ${total_savings:.2f}</p>'
        content += "<p><strong>Resources:</strong></p><ul>"
        for rec in recs:
            resource_id = rec.get("resource_id", "N/A")
            severity = rec.get("severity", "")
            reason = rec.get("reason", rec.get("recommended_value", ""))
            line = resource_id
            if severity:
                line += f" [{severity}]"
            if reason:
                line += f" — {reason}"
            savings = rec.get("monthly_savings", 0.0)
            if savings > 0:
                line += f" (${savings:.2f}/month)"
            content += f"<li>{line}</li>"
        content += "</ul></div>"
    return content


# Standard AWS RI / SP discount-tier ratios relative to 1yr No Upfront. Used to
# scale a single CoH-recommended scenario into the full purchase matrix so the
# reader can compare upfront-vs-recurring trade-offs without leaving the report.
# Treat as planning estimates; confirm exact savings in the AWS RI marketplace.
_COH_COMMITMENT_TIER_RATIOS: Tuple[Tuple[str, str, float], ...] = (
    ("1yr", "No Upfront", 1.00),
    ("1yr", "Partial Upfront", 1.08),
    ("1yr", "All Upfront", 1.16),
    ("3yr", "No Upfront", 1.45),
    ("3yr", "Partial Upfront", 1.53),
    ("3yr", "All Upfront", 1.63),
)


def _is_commitment_rec(rec: Rec) -> bool:
    """Return True if the CoH rec describes a Reserved Instance or Savings Plan."""
    rt = (rec.get("currentResourceType") or "").lower()
    return "reservedinstance" in rt or "savingsplan" in rt


def _coh_recommended_scenario(rec: Rec) -> Tuple[str, str]:
    """Extract AWS's recommended (term, payment_option) from a CoH commitment rec.

    Looks at ``recommendedResourceSummary`` (the AWS CoH API field) for term
    (``1_YEAR`` / ``3_YEARS``) and payment option (``NO_UPFRONT`` / ``PARTIAL_UPFRONT``
    / ``ALL_UPFRONT``). Falls back to a flat (``"1yr"``, ``"No Upfront"``) anchor
    when those fields are missing so the matrix still renders.
    """
    summary = rec.get("recommendedResourceSummary") or rec.get("recommendedResourceDetails") or {}
    if isinstance(summary, dict):
        nested = (
            summary.get("reservedInstances")
            or summary.get("savingsPlans")
            or summary.get("ec2ReservedInstances")
            or summary.get("rdsReservedInstances")
            or summary.get("elastiCacheReservedInstances")
            or summary.get("openSearchReservedInstances")
            or summary.get("redshiftReservedInstances")
            or summary.get("computeSavingsPlans")
            or summary.get("ec2InstanceSavingsPlans")
            or summary.get("sageMakerSavingsPlans")
            or {}
        )
        if isinstance(nested, dict) and nested:
            summary = nested

    raw_term = (summary.get("term") if isinstance(summary, dict) else "") or ""
    raw_payment = (summary.get("paymentOption") if isinstance(summary, dict) else "") or ""

    term = "3yr" if "3" in str(raw_term) else "1yr"
    payment_token = str(raw_payment).upper()
    if "ALL" in payment_token:
        payment = "All Upfront"
    elif "PARTIAL" in payment_token:
        payment = "Partial Upfront"
    else:
        payment = "No Upfront"
    return term, payment


def _render_coh_commitment_scenarios(rec: Rec) -> str:
    """Build the full term × payment scenario matrix for a CoH commitment rec.

    AWS Cost Optimization Hub returns a single recommended (term, payment) per
    resource. This helper scales the rec's monthly savings via standard tier
    ratios so the reader can compare all six purchase scenarios inline. The
    AWS-recommended scenario row is highlighted as the anchor.
    """
    base_savings = float(rec.get("estimatedMonthlySavings", 0) or 0)
    if base_savings <= 0:
        return ""

    anchor_term, anchor_payment = _coh_recommended_scenario(rec)
    anchor_ratio = 1.0
    for term, payment, ratio in _COH_COMMITMENT_TIER_RATIOS:
        if term == anchor_term and payment == anchor_payment:
            anchor_ratio = ratio
            break
    # Express each scenario relative to the anchor so the AWS-recommended row
    # equals base_savings exactly and the rest scale up or down from there.
    scenarios = [
        (term, payment, round(base_savings * (ratio / anchor_ratio), 2), round((ratio / anchor_ratio - 1) * 100, 1))
        for term, payment, ratio in _COH_COMMITMENT_TIER_RATIOS
    ]

    resource_label = (rec.get("currentResourceType") or "Commitment").replace("ReservedInstances", " RIs").replace(
        "SavingsPlans", " SP"
    )

    out = '<div class="ri-scenarios">'
    out += (
        '<p class="ri-scenarios__header"><strong>Scenario matrix</strong> '
        f'<span class="ri-scenarios__base">AWS-recommended: {anchor_term} {anchor_payment} '
        f'· {resource_label} · base ${base_savings:,.2f}/month</span></p>'
    )
    out += "<table class='rec-table ri-scenarios__table'><thead><tr>"
    out += "<th>Term</th><th>Payment</th><th>Monthly savings</th><th>vs. recommended</th>"
    out += "</tr></thead><tbody>"
    for term, payment, monthly, delta_pct in scenarios:
        is_anchor = term == anchor_term and payment == anchor_payment
        row_class = " class='ri-scenarios__row--best'" if is_anchor else ""
        delta_display = "—" if is_anchor else (f"{'+' if delta_pct > 0 else ''}{delta_pct:.1f}%")
        out += (
            f"<tr{row_class}>"
            f"<td>{term}</td>"
            f"<td>{payment}</td>"
            f"<td>${monthly:,.2f}</td>"
            f"<td>{delta_display}</td>"
            f"</tr>"
        )
    out += "</tbody></table>"
    out += "</div>"
    return out


def _render_cost_hub_source(recommendations: List[Rec], source_name: str, service_data: Dict) -> str:
    """Renders Cost Optimization Hub recommendations as a human-readable table.

    Groups recommendations by action type and displays resource details,
    savings, and implementation effort in a structured card layout. For
    commitment recommendations (Reserved Instances / Savings Plans), appends
    a per-rec scenario matrix expanding AWS's single recommended (term,
    payment) into the full six-cell purchase matrix so the FinOps reader can
    weigh upfront-vs-recurring trade-offs without leaving the report.
    Called by: HTMLReportGenerator._get_detailed_recommendations.
    """
    if not recommendations:
        return ""

    grouped: Dict[str, List[Rec]] = {}
    for rec in recommendations:
        action = rec.get("actionType", "Unknown")
        if action not in grouped:
            grouped[action] = []
        grouped[action].append(rec)

    content = ""
    for action_type, recs in grouped.items():
        total_savings = sum(r.get("estimatedMonthlySavings", 0) for r in recs)
        label = "recommendation" if len(recs) == 1 else "recommendations"
        content += f'<div class="rec-item{_priority_class(recs[0])}">'

        action_display = action_type.replace("_", " ").replace("Purchase", "Purchase ")
        content += f"<h4>{action_display} ({len(recs)} {label})</h4>"

        if total_savings > 0:
            content += f'<p class="savings"><strong>Estimated Monthly Savings:</strong> ${total_savings:.2f}</p>'

        effort = recs[0].get("implementationEffort", "")
        if effort:
            content += f"<p><strong>Implementation Effort:</strong> {effort}</p>"

        content += "<table class='rec-table'><thead><tr>"
        content += "<th>Action</th><th>Resource</th><th>Region</th><th>Monthly Savings</th>"
        content += "</tr></thead><tbody>"

        for rec in recs:
            action = rec.get("actionType", "Unknown").replace("_", " ").replace("Purchase", "Purchase ")
            resource_id = rec.get("resourceId", "") or ""
            res_type = rec.get("currentResourceType", "N/A")
            if resource_id and resource_id != "N/A":
                resource_display = resource_id.split("/")[-1]
            else:
                resource_display = res_type
            region = rec.get("region", "N/A")
            savings = rec.get("estimatedMonthlySavings", 0)
            content += f"<tr><td>{action}</td><td>{resource_display}</td><td>{region}</td>"
            content += f"<td>${savings:.2f}</td></tr>"

        content += "</tbody></table>"

        commitment_recs = [r for r in recs if _is_commitment_rec(r)]
        for crec in commitment_recs:
            content += _render_coh_commitment_scenarios(crec)

        rec_first = recs[0]
        lookback = rec_first.get("costCalculationLookbackPeriodInDays", 30)
        savings_pct = rec_first.get("estimatedSavingsPercentage", 0)
        if savings_pct:
            content += f"<p><small>Based on {lookback}-day analysis | {savings_pct:.0f}% estimated savings</small></p>"

        content += "</div>"
    return content


# Single source of truth for the source-confidence badge taxonomy. Must match
# the glossary rendered by HTMLReportGenerator._get_glossary_section. Any label
# not in this set is rejected by source_type_badge so the body cannot drift
# from the legend (DESIGN.md: glossary-as-source-of-truth).
VALID_SOURCE_BADGES: frozenset = frozenset(
    {"Metric Backed", "ML Backed", "Cost Hub", "Audit Based"}
)

_SOURCE_BADGE_CSS: Dict[str, str] = {
    "Metric Backed": "badge-success",
    "ML Backed": "badge-info",
    "Cost Hub": "badge-warning",
    "Audit Based": "badge-danger",
}

SOURCE_TYPE_MAP: Dict[Tuple[str, str], str] = {
    # Legacy bindings retained for any in-flight scan JSON that predates
    # the compute_optimizer adapter retirement (services/__init__.py,
    # 2026-05-14). New scans never emit these source pairs because the
    # standalone Compute Optimizer tab no longer renders.
    ("compute_optimizer", "ebs_recommendations"): "ML Backed",
    ("compute_optimizer", "lambda_recommendations"): "ML Backed",
    ("compute_optimizer", "ecs_recommendations"): "ML Backed",
    ("compute_optimizer", "asg_recommendations"): "ML Backed",
    ("cost_optimization_hub", "savings_plans"): "Cost Hub",
    ("cost_optimization_hub", "cross_service"): "Cost Hub",
    # CO recs that now flow through per-service adapters after the standalone
    # Compute Optimizer tab retirement.
    ("lambda", "compute_optimizer"): "ML Backed",
    ("containers", "compute_optimizer"): "ML Backed",
    ("ec2", "asg_compute_optimizer"): "ML Backed",
    # S3 enhanced checks are config-pattern (no CloudWatch). Override the
    # generic "enhanced_checks" → "Metric Backed" default (audit L3-S3-003).
    ("s3", "enhanced_checks"): "Audit Based",
}

_GENERIC_SOURCE_TYPES: Dict[str, str] = {
    "enhanced_checks": "Metric Backed",
    "lifecycle_analysis": "Metric Backed",
    "rightsizing": "ML Backed",
    "idle_resources": "Audit Based",
    "cost_findings": "Audit Based",
    "general_recommendations": "Audit Based",
    "recommendations": "Cost Hub",
    "sp_analysis": "Cost Hub",
    "ri_analysis": "Cost Hub",
    "reserved_instances": "Cost Hub",
    "savings_plans": "Cost Hub",
    "cost_optimization_hub": "Cost Hub",
    "compute_optimizer": "Metric Backed",
    "s3_bucket_analysis": "Metric Backed",
    "dynamodb_table_analysis": "Metric Backed",
    "efs_lifecycle_analysis": "Audit Based",
    "old_amis": "Audit Based",
    "gp2_migration": "Metric Backed",
    "unattached_volumes": "Audit Based",
    "tgw_vs_peering": "Metric Backed",
    "node_group_optimization": "Metric Backed",
    "fargate_analysis": "Metric Backed",
    "cluster_costs": "Metric Backed",
    "addon_costs": "Metric Backed",
    "cost_hub_recommendations": "Cost Hub",
    "cross_service": "Cost Hub",
}


def source_type_badge(service_key: str, source_name: str) -> str:
    label = SOURCE_TYPE_MAP.get((service_key, source_name))
    if not label:
        label = _GENERIC_SOURCE_TYPES.get(source_name, "")
    if not label:
        return ""
    if label not in VALID_SOURCE_BADGES:
        # Guard: refuse to render a badge label that the glossary does not define.
        # Anything reaching here is a taxonomy drift bug, not a runtime condition.
        return ""
    css_class = _SOURCE_BADGE_CSS[label]
    return f' <span class="badge {css_class}">{label}</span>'


def source_type_label(service_key: str, source_name: str) -> str:
    """Return the bare source-confidence label for the (service, source) pair.

    Used to drive the typographic prefix that renders on every rec-item title
    (CSS :before via data-source attribute). Returns the empty string when no
    taxonomy entry applies, so callers can omit the wrapper entirely.
    """
    label = SOURCE_TYPE_MAP.get((service_key, source_name))
    if not label:
        label = _GENERIC_SOURCE_TYPES.get(source_name, "")
    if not label or label not in VALID_SOURCE_BADGES:
        return ""
    return label


PHASE_B_HANDLERS: Dict[Tuple[str, str], Callable] = {
    # Legacy bindings retained for any in-flight scan JSON that predates
    # the compute_optimizer adapter retirement (services/__init__.py,
    # 2026-05-14). New scans never emit these source pairs.
    ("compute_optimizer", "ebs_recommendations"): _render_compute_optimizer_source,
    ("compute_optimizer", "lambda_recommendations"): _render_compute_optimizer_source,
    ("compute_optimizer", "ecs_recommendations"): _render_compute_optimizer_source,
    ("compute_optimizer", "asg_recommendations"): _render_compute_optimizer_source,
    # CO recs that now flow through per-service adapters after the standalone
    # Compute Optimizer tab retirement. Each binding reuses the unified
    # renderer because the rec schema is identical (resource_name, finding,
    # current_config, recommended_config, estimatedMonthlySavings).
    ("lambda", "compute_optimizer"): _render_compute_optimizer_source,
    ("containers", "compute_optimizer"): _render_compute_optimizer_source,
    ("ec2", "asg_compute_optimizer"): _render_compute_optimizer_source,
    # Legacy bindings retained for any in-flight scan JSON that predates
    # the cost_optimization_hub adapter retirement (services/__init__.py,
    # 2026-05-14). New scans never emit these source pairs.
    ("cost_optimization_hub", "savings_plans"): _render_cost_hub_source,
    ("cost_optimization_hub", "cross_service"): _render_cost_hub_source,
    # CoH recommendations the orchestrator routes into per-service tabs.
    ("containers", "cost_optimization_hub"): _render_cost_hub_source,
    ("commitment_analysis", "cost_optimization_hub"): _render_cost_hub_source,
    ("ec2", "enhanced_checks"): _render_ec2_enhanced_checks,
    ("ec2", "cost_optimization_hub"): _render_ec2_cost_hub,
    ("ec2", "compute_optimizer"): _render_ec2_compute_optimizer,
    ("ec2", "advanced_ec2_checks"): _render_ec2_advanced_checks,
    ("ebs", "cost_optimization_hub"): _render_ebs_cost_hub,
    ("ebs", "unattached_volumes"): _render_ebs_unattached,
    ("ebs", "gp2_migration"): _render_ebs_gp2_migration,
    ("ebs", "enhanced_checks"): _render_ebs_enhanced_checks,
    ("ebs", "compute_optimizer"): _render_ebs_compute_optimizer,
    ("rds", "compute_optimizer"): _render_rds_compute_optimizer,
    ("rds", "enhanced_checks"): _render_rds_enhanced_checks,
    ("s3", "enhanced_checks"): _render_s3_enhanced_checks,
    ("s3", "s3_bucket_analysis"): _render_s3_bucket_analysis,
    ("dynamodb", "enhanced_checks"): _render_dynamodb_enhanced_checks,
    ("dynamodb", "dynamodb_table_analysis"): _render_dynamodb_enhanced_checks,
    ("containers", "enhanced_checks"): _render_containers_enhanced_checks,
    ("elasticache", "enhanced_checks"): _render_elasticache_enhanced_checks,
    ("opensearch", "enhanced_checks"): _render_opensearch_enhanced_checks,
    ("network", "enhanced_checks"): _render_network_enhanced_checks,
    ("monitoring", "cloudwatch_checks"): _render_monitoring_enhanced_checks,
    ("monitoring", "cloudtrail_checks"): _render_monitoring_enhanced_checks,
    ("monitoring", "backup_checks"): _render_monitoring_enhanced_checks,
    ("monitoring", "route53_checks"): _render_monitoring_enhanced_checks,
    ("eks_cost", "cluster_costs"): _render_eks_source,
    ("eks_cost", "node_group_optimization"): _render_eks_source,
    ("eks_cost", "fargate_analysis"): _render_eks_source,
    ("eks_cost", "addon_costs"): _render_eks_source,
    ("eks_cost", "cost_hub_recommendations"): _render_eks_source,
}

_PHASE_A_SERVICES = frozenset(
    {
        "file_systems",
        "lambda",
        "cloudfront",
        "lightsail",
        "dms",
        "glue",
        "api_gateway",
        "step_functions",
        "auto_scaling",
        "backup",
        "route53",
    }
)

_PHASE_B_SKIP_PER_REC = frozenset(
    {
        "ebs",
        "ec2",
        "s3",
        "dynamodb",
        "containers",
        "file_systems",
        "network",
        "monitoring",
        "rds",
        "eks_cost",
        "cost_optimization_hub",
    }
)


def should_skip_section_header(service_key: str) -> bool:
    """Return True if the section header should be omitted for this service."""
    return service_key in (_PHASE_B_SKIP_PER_REC | {"lightsail", "dms", "glue", "redshift"})


def should_skip_source_loop(service_key: str) -> bool:
    """Return True if Phase A rendering handles this service instead of source-loop."""
    return service_key in _PHASE_A_SERVICES


def should_use_handler(service_key: str, source_name: str) -> bool:
    """Return True if a Phase B handler exists for this (service, source) pair."""
    return (service_key, source_name) in PHASE_B_HANDLERS


def should_fallback_to_per_rec(service_key: str) -> bool:
    """Return True if the generic per-record renderer should be used for this service."""
    return service_key not in _PHASE_B_SKIP_PER_REC
