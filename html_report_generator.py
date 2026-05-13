#!/usr/bin/env python3
"""
HTML Report Generator for AWS Cost Optimization Scanner v2.5.9

This module generates professional, interactive HTML reports from cost optimization
scan results. The reports feature a multi-tab interface with smart grouping,
zero duplication, and consistent styling across all 31 AWS services.

Key Features:
- Interactive multi-tab interface for easy navigation
- Smart grouping by optimization category for better organization
- Zero duplication across all data sources
- Professional styling with consistent formatting
- Empty tab hiding for clean presentation
- Profile-based filenames for multi-account management
- Responsive design for desktop and mobile viewing
- Enhanced error handling with proper logging
- Cross-region support with accurate reporting

The generator processes scan results from 31 AWS services and creates:
- Service-specific tabs with recommendations
- Statistics cards showing resource counts and savings
- Grouped recommendations by category for better readability
- Consistent styling and formatting across all services
- Interactive elements for enhanced user experience
- Warnings and permission issues display

Author: AWS Cost Optimization Team
Version: 2.5.9
Last Updated: 2026-01-24
"""

import base64
import copy
import html
import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)


def _format_savings_chip(amount: float) -> str:
    """Compact dollar format for tab chips. '$267', '$1.2k', '$14.3k'.

    Negative or zero values return the empty string so callers can decide to
    omit the chip entirely. Anything under $1 collapses to '<$1' (we still
    want to differentiate 'a recommendation exists' from 'no savings').
    """
    if amount <= 0:
        return ""
    if amount < 1:
        return "&lt;$1"
    if amount < 1000:
        return f"${amount:.0f}"
    return f"${amount / 1000:.1f}k"


_SAVINGS_KEYWORDS: Dict[str, List[Tuple[str, float]]] = {
    "ec2": [
        ("previous generation", 50),
        ("dedicated tenancy", 200),
        ("burstable", 30),
        ("spot", 100),
        ("schedule", 150),
    ],
    "dynamodb": [
        ("on-demand", 100),
        ("provisioned", 75),
        ("reserved", 200),
    ],
}

_SAVINGS_FALLBACK_TO_ESTIMATED: set = {"ec2"}

_DEFAULT_SAVINGS: Dict[str, float] = {
    "ec2": 25,
    "dynamodb": 50,
}

_FLAT_SAVINGS_SERVICES: set = {"opensearch", "api_gateway", "step_functions"}

_StatCard = Tuple[str, str]

_SERVICE_STATS_CONFIG: Dict[str, Dict[str, Any]] = {
    "ec2": {
        "direct_key": "instance_count",
        "cards": [("EC2 Instances", "instance_count")],
    },
    "ebs": {
        "count_key": "volume_counts",
        "cards": [
            ("Total Volumes", "total"),
            ("Unattached", "unattached"),
            ("gp2 Volumes", "gp2"),
        ],
    },
    "rds": {
        "count_key": "instance_counts",
        "cards": [
            ("Total Instances", "total"),
            ("Running", "running"),
            ("MySQL", "mysql"),
        ],
    },
    "file_systems": {
        "multi_source_cards": [
            ("EFS Systems", "efs_counts", "total"),
            ("FSx Systems", "fsx_counts", "total"),
            ("EFS Size (GB)", "efs_counts", "total_size_gb"),
        ],
    },
    "s3": {
        "count_key": "bucket_counts",
        "cards": [
            ("Total Buckets", "total"),
            ("No Lifecycle", "without_lifecycle"),
            ("No Intelligent Tiering", "without_intelligent_tiering"),
        ],
        "extra_stats": "s3_bucket_analysis",
    },
    "dynamodb": {
        "count_key": "table_counts",
        "cards": [
            ("Total Tables", "total"),
            ("Provisioned", "provisioned"),
            ("On-Demand", "on_demand"),
        ],
    },
    "containers": {
        "count_key": "service_counts",
        "cards": [
            ("ECS Clusters", "ecs_clusters"),
            ("EKS Clusters", "eks_clusters"),
            ("ECR Repositories", "ecr_repositories"),
            ("ECS Services", "ecs_services"),
        ],
    },
    "cost_anomaly": {
        "multi_source_cards": [
            ("Active Anomalies", "extras", "active_anomaly_count"),
            ("30-Day Impact", "extras", "total_anomaly_impact_30d"),
            ("Anomaly Monitors", "extras", "monitor_count"),
            ("Billing Alarms", "extras", "billing_alarm_count"),
        ],
    },
    "eks_cost": {
        "multi_source_cards": [
            ("EKS Clusters", "extras", "cluster_count"),
            ("Node Groups", "extras", "node_group_count"),
            ("Fargate Profiles", "extras", "fargate_profile_count"),
            ("Add-ons", "extras", "addon_count"),
            ("Monthly Control Plane", "extras", "monthly_control_plane_cost"),
        ],
    },
    "aurora": {
        "multi_source_cards": [
            ("Aurora Clusters", "extras", "cluster_count"),
            ("Serverless v2", "extras", "serverless_cluster_count"),
            ("Global Clusters", "extras", "global_cluster_count"),
        ],
    },
    "bedrock": {
        "multi_source_cards": [
            ("Provisioned Throughputs", "extras", "pt_count"),
            ("Knowledge Bases", "extras", "kb_count"),
            ("Agents", "extras", "agent_count"),
        ],
    },
    "sagemaker": {
        "multi_source_cards": [
            ("Active Endpoints", "extras", "active_endpoint_count"),
            ("Idle Endpoints", "extras", "idle_endpoint_count"),
            ("Running Notebooks", "extras", "running_notebook_count"),
        ],
    },
    "network_cost": {
        "multi_source_cards": [
            ("30-Day Transfer Spend", "extras", "total_data_transfer_spend_30d"),
            ("Cross-Region Spend", "extras", "cross_region_spend_30d"),
            ("VPC Peerings", "extras", "peering_count"),
            ("TGW Attachments", "extras", "tgw_count"),
        ],
    },
    "commitment_analysis": {
        "multi_source_cards": [
            ("SP Utilization", "extras", "sp_utilization_rate"),
            ("SP Coverage", "extras", "sp_coverage_rate"),
            ("RI Utilization", "extras", "ri_utilization_rate"),
            ("RI Coverage", "extras", "ri_coverage_rate"),
        ],
    },
    "compute_optimizer": {
        "multi_source_cards": [
            ("EBS Findings", "extras", "ebs_count"),
            ("Lambda Findings", "extras", "lambda_count"),
            ("ECS Findings", "extras", "ecs_count"),
            ("ASG Findings", "extras", "asg_count"),
        ],
    },
    "cost_optimization_hub": {
        "multi_source_cards": [
            ("Total Recommendations", "extras", "total_recommendations_in_hub"),
        ],
    },
}


def _extract_ec2_resources(rec: Dict[str, Any], resource_groups: Dict[str, list]) -> None:
    """Extract EC2 instance resource IDs into grouped lists. Called by: _get_affected_resources_list."""
    if "actionType" in rec:
        if "ebsVolume" in rec.get("currentResourceDetails", {}):
            return
        if rec.get("actionType") == "PurchaseReservedInstances":
            return
        resource_details = rec.get("currentResourceDetails", {})
        if "ecsService" in resource_details or "ecsCluster" in resource_details:
            return
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
            return
        if "ec2Instance" not in resource_details:
            return
        action_type = rec.get("actionType", "Unknown")
        resource_id = rec.get("resourceId", "N/A")
        if resource_id == "N/A":
            return
        instance_type = (
            rec.get("currentResourceDetails", {})
            .get("ec2Instance", {})
            .get("configuration", {})
            .get("instance", {})
            .get("type", "N/A")
        )
        savings = rec.get("estimatedMonthlySavings", 0)
        if action_type not in resource_groups:
            resource_groups[action_type] = []
        resource_groups[action_type].append({"id": resource_id, "type": instance_type, "savings": savings})
    elif "instanceArn" in rec:
        finding = rec.get("finding", "Unknown")
        if finding.lower() in ["optimized", "over_provisioned"]:
            return
        instance_name = rec.get("instanceName", "N/A")
        instance_id = rec.get("instanceArn", "").split("/")[-1] if rec.get("instanceArn") else "N/A"
        current_type = rec.get("currentInstanceType", "N/A")
        recommended_type = "N/A"
        if rec.get("recommendationOptions"):
            recommended_type = rec["recommendationOptions"][0].get("instanceType", "N/A")
        group_name = f"Rightsizing - {finding}"
        if group_name not in resource_groups:
            resource_groups[group_name] = []
        resource_groups[group_name].append(
            {
                "id": instance_name or instance_id,
                "type": f"{current_type} → {recommended_type}",
                "savings": 0,
            }
        )


def _extract_ebs_resources(rec: Dict[str, Any], source_name: str, resource_groups: Dict[str, list]) -> None:
    """Extract EBS volume resource IDs into grouped lists. Called by: _get_affected_resources_list."""
    if "actionType" in rec and "ebsVolume" in rec.get("currentResourceDetails", {}):
        action_type = rec.get("actionType", "Unknown")
        resource_id = rec.get("resourceId", "N/A")
        ebs_config = rec.get("currentResourceDetails", {}).get("ebsVolume", {}).get("configuration", {})
        volume_type = ebs_config.get("storage", {}).get("type", "N/A")
        volume_size = ebs_config.get("storage", {}).get("sizeInGb", 0)
        savings = rec.get("estimatedMonthlySavings", 0)
        if action_type not in resource_groups:
            resource_groups[action_type] = []
        resource_groups[action_type].append(
            {"id": resource_id, "type": f"{volume_type} ({volume_size} GB)", "savings": savings}
        )
    elif rec.get("CheckCategory") == "Volume Type Optimization" and rec.get("CurrentType") == "gp2":
        if "gp2 to gp3 Migration" not in resource_groups:
            resource_groups["gp2 to gp3 Migration"] = []
        resource_groups["gp2 to gp3 Migration"].append(
            {
                "id": rec.get("VolumeId", "N/A"),
                "type": f"{rec.get('Size', 0)} GB (20% savings)",
                "savings": 0,
            }
        )
    elif source_name == "unattached_volumes" and "VolumeId" in rec:
        if "Unattached Volumes" not in resource_groups:
            resource_groups["Unattached Volumes"] = []
        resource_groups["Unattached Volumes"].append(
            {
                "id": rec.get("VolumeId", "N/A"),
                "type": f"{rec.get('VolumeType', 'N/A')} ({rec.get('Size', 0)} GB)",
                "savings": rec.get("EstimatedMonthlyCost", 0),
            }
        )
    elif rec.get("finding") == "NotOptimized":
        if "Volume Optimization" not in resource_groups:
            resource_groups["Volume Optimization"] = []
        volume_id = rec.get("volumeArn", "N/A").split("/")[-1] if rec.get("volumeArn") else "N/A"
        resource_groups["Volume Optimization"].append(
            {
                "id": volume_id,
                "type": rec.get("finding", "N/A"),
                "savings": 0,
            }
        )
    elif rec.get("finding", "").lower() == "optimized":
        pass
    else:
        # Catch-all for enhanced_checks recs (Underutilized Volumes, Over-Provisioned IOPS,
        # Old/Orphaned Snapshots, Unused Encrypted Volumes, Snapshot Lifecycle, etc.)
        # These have VolumeId or SnapshotId but no actionType/ebsVolume structure.
        resource_id = rec.get("VolumeId") or rec.get("SnapshotId", "N/A")
        if resource_id == "N/A" and rec.get("volumeArn"):
            resource_id = rec["volumeArn"].split("/")[-1]
        if resource_id != "N/A" or rec.get("CheckCategory"):
            check_cat = rec.get("CheckCategory", "Enhanced Checks")
            if check_cat not in resource_groups:
                resource_groups[check_cat] = []
            size = rec.get("Size") or rec.get("VolumeSize", 0)
            vol_type = rec.get("VolumeType", "N/A")
            if size:
                type_label = f"{vol_type} ({size} GB)"
            else:
                type_label = rec.get("Recommendation", rec.get("Description", "N/A"))
            raw_savings = rec.get("EstimatedSavings", 0)
            savings_val = 0
            if isinstance(raw_savings, (int, float)):
                savings_val = raw_savings
            elif isinstance(raw_savings, str):
                digits = "".join(c for c in raw_savings if c.isdigit() or c == ".")
                savings_val = float(digits) if digits else 0
            resource_groups[check_cat].append({"id": resource_id, "type": type_label, "savings": savings_val})


def _extract_rds_resources(rec: Dict[str, Any], resource_groups: Dict[str, list]) -> None:
    """Extract RDS database resource IDs into grouped lists. Called by: _get_affected_resources_list."""
    finding = (
        rec.get("instanceFinding")
        or rec.get("InstanceFinding")
        or rec.get("finding")
        or rec.get("CheckCategory")
        or rec.get("Recommendation")
        or "Optimization Opportunity"
    )
    if finding.lower() == "optimized" or finding.lower() == "underprovisioned":
        return
    resource_arn = rec.get("resourceArn") or rec.get("ResourceArn", "N/A")
    if resource_arn != "N/A":
        db_name = resource_arn.split(":")[-1]
    else:
        db_name = rec.get("DBInstanceIdentifier") or rec.get("Database") or rec.get("resourceId", "N/A")
    engine = rec.get("engine") or rec.get("Engine") or rec.get("engineVersion") or "Unknown"
    if "SnapshotId" in rec or "snapshot" in db_name.lower():
        return
    category = f"Aurora Clusters - {finding}" if "aurora" in engine.lower() else f"Standalone Instances - {finding}"
    if category not in resource_groups:
        resource_groups[category] = []
    resource_groups[category].append(
        {
            "id": db_name,
            "type": engine,
            "savings": 0,
        }
    )


def _extract_file_systems_resources(rec: Dict[str, Any], resource_groups: Dict[str, list]) -> None:
    """Extract EFS/FSx file-system resource IDs into grouped lists. Called by: _get_affected_resources_list."""
    if "FileSystemType" in rec:
        fs_type = rec.get("FileSystemType", "Unknown")
        if f"FSx {fs_type}" not in resource_groups:
            resource_groups[f"FSx {fs_type}"] = []
        resource_groups[f"FSx {fs_type}"].append(
            {
                "id": rec.get("FileSystemId", "N/A"),
                "type": f"{rec.get('StorageCapacity', 0)} GB",
                "savings": rec.get("EstimatedMonthlyCost", 0) * 0.3,
            }
        )
    else:
        if not rec.get("HasIAPolicy", True):
            if "EFS Lifecycle Optimization" not in resource_groups:
                resource_groups["EFS Lifecycle Optimization"] = []
            resource_groups["EFS Lifecycle Optimization"].append(
                {
                    "id": rec.get("Name", rec.get("FileSystemId", "N/A")),
                    "type": f"{rec.get('SizeGB', 0)} GB",
                    "savings": rec.get("EstimatedMonthlyCost", 0) * 0.8,
                }
            )


_RESOURCE_EXTRACTORS: Dict[str, Callable[..., None]] = {
    "ec2": _extract_ec2_resources,
    "ebs": _extract_ebs_resources,
    "rds": _extract_rds_resources,
    "file_systems": _extract_file_systems_resources,
}


def _filter_ec2_recommendations(recommendations: list) -> list:
    """Filter out non-EC2 recommendations (EBS volumes, Reserved Instance suggestions, N/A resources)."""
    filtered: list = []
    for rec in recommendations:
        if "actionType" in rec and "ebsVolume" in rec.get("currentResourceDetails", {}):
            continue
        if rec.get("actionType") == "PurchaseReservedInstances":
            continue
        if rec.get("actionType") and rec.get("resourceId") == "N/A":
            continue
        filtered.append(rec)
    return filtered


def _enrich_s3_stats(stats_html: str, service_data: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Append S3 top-cost and top-size bucket stat cards."""
    sources = service_data.get("sources", {})
    s3_data = sources.get(config.get("extra_stats", ""), {})
    top_cost = s3_data.get("top_cost_buckets", [])
    top_size = s3_data.get("top_size_buckets", [])

    if top_cost:
        stats_html += f'<div class="stat-card"><div class="stat-label">Highest Cost Bucket</div><div class="value">${top_cost[0].get("EstimatedMonthlyCost", 0):.2f}/mo</div></div>'
    if top_size:
        stats_html += f'<div class="stat-card"><div class="stat-label">Largest Bucket</div><div class="value">{top_size[0].get("SizeGB", 0):.1f} GB</div></div>'
    return stats_html


_RECOMMENDATION_FILTERS: Dict[str, Callable[[list], list]] = {
    "ec2": _filter_ec2_recommendations,
}

_STATS_ENRICHMENTS: Dict[str, Callable[[str, Dict[str, Any], Dict[str, Any]], str]] = {
    "s3": _enrich_s3_stats,
}


class HTMLReportGenerator:
    """
    Professional HTML report generator for AWS cost optimization scan results.

    This class transforms structured JSON scan results into interactive HTML reports
    with professional styling, smart grouping, and zero duplication across services.

    The generator handles:
    - Multi-tab interface creation for 31 AWS services
    - Smart grouping of recommendations by category
    - Deduplication of findings across multiple data sources
    - Consistent styling and formatting
    - Empty tab hiding for clean presentation
    - Statistics calculation and display
    - Profile-based filename generation

    Usage:
        generator = HTMLReportGenerator(scan_results)
        report_path = generator.generate_html_report()
    """

    def __init__(self, scan_results: Dict[str, Any]):
        """
        Initialize the HTML report generator with scan results.

        Args:
            scan_results (Dict[str, Any]): Complete scan results from CostOptimizer.scan_region()
                                         containing services data, statistics, and metadata
        """
        self.scan_results = scan_results

    def generate_html_report(self, output_file: str | None = None) -> str:
        """
        Generate complete interactive HTML report from scan results.

        Creates a professional HTML report with multi-tab interface, smart grouping,
        and consistent styling. Automatically generates profile-based filename if
        not specified.

        Args:
            output_file (str, optional): Custom output filename. If not provided,
                                       generates filename as 'profile_region.html'

        Returns:
            str: Path to the generated HTML report file

        Note:
            - Automatically hides tabs for services with no recommendations
            - Uses smart grouping for 11 services with high recommendation volumes
            - Applies consistent styling across all service tabs
            - Generates responsive design for desktop and mobile viewing
        """
        if not output_file:
            # Generate profile-based filename for multi-account management
            profile = self.scan_results.get("profile", "default")
            region = self.scan_results["region"]
            output_file = f"{profile}_{region}.html"

        html_content = self._build_html()

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html_content)

        return output_file

    def _build_html(self) -> str:
        """Build complete HTML content"""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AWS Cost Optimization Report - {self.scan_results["region"]}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,300;6..72,400;6..72,500;6..72,600&family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    {self._get_css()}
</head>
<body>
    <svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true" focusable="false">
<symbol id="icon-chart" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="18" y="3" width="4" height="18"/><rect x="10" y="8" width="4" height="13"/><rect x="2" y="13" width="4" height="8"/></symbol>
<symbol id="icon-lightbulb" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4M15.09 14.41c.38-.38.67-.84.85-1.35A4.5 4.5 0 0 0 12 6.5a4.5 4.5 0 0 0-3.94 6.56c.18.51.47.97.85 1.35V17h6v-2.59z"/></symbol>
<symbol id="icon-clipboard" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/></symbol>
<symbol id="icon-dollar" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></symbol>
<symbol id="icon-trending" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></symbol>
<symbol id="icon-alert" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></symbol>
<symbol id="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></symbol>
<symbol id="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></symbol>
<symbol id="icon-arrow-up" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></symbol>
<symbol id="icon-download" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></symbol>
</svg>
    <button class="theme-toggle" onclick="toggleTheme()" title="Toggle Dark Mode" aria-pressed="false">
        <svg class="icon" id="theme-icon-svg"><use href="#icon-moon"/></svg>
        <span id="theme-text">Dark</span>
    </button>
    <button class="export-btn" onclick="window.print()" aria-label="Export report as PDF" title="Export as PDF">
        <svg class="icon" width="16" height="16"><use href="#icon-download"/></svg>
    </button>
    <div class="container">
        {self._get_header()}
        {self._get_summary()}
        {self._get_tabs()}
        {self._get_footer()}
    </div>
    {self._get_javascript()}
    <button class="back-to-top" aria-label="Back to top" title="Back to top"><svg width="20" height="20"><use href="#icon-arrow-up"/></svg></button>
</body>
</html>"""

    def _get_css(self) -> str:
        """Get Material Design CSS styles"""
        return """<style>
        /* Material Design Base */
        * { 
            margin: 0; 
            padding: 0; 
            box-sizing: border-box; 
        }
        
        :root {
            --primary: #1976d2;
            --primary-dark: #0d47a1;
            --primary-light: #42a5f5;
            --secondary: #ff9800;
            --secondary-dark: #f57c00;
            --success: #4caf50;
            --warning: #ff9800;
            --danger: #f44336;
            --info: #2196f3;
            --surface: #ffffff;
            --background: #f5f5f5;
            --text-primary: #212121;
            --text-secondary: #757575;
            --divider: #e0e0e0;
            --shadow-1: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.24);
            --shadow-2: 0 3px 6px rgba(0,0,0,0.16), 0 3px 6px rgba(0,0,0,0.23);
            --shadow-3: 0 10px 20px rgba(0,0,0,0.19), 0 6px 6px rgba(0,0,0,0.23);
            --shadow-4: 0 14px 28px rgba(0,0,0,0.25), 0 10px 10px rgba(0,0,0,0.22);
            --shadow-5: 0 19px 38px rgba(0,0,0,0.30), 0 15px 12px rgba(0,0,0,0.22);
            --hover-bg: rgba(25, 118, 210, 0.06);
            --badge-success-fg: #1b5e20;
            --badge-success-bg: #e8f5e9;
            --badge-warning-fg: #e65100;
            --badge-warning-bg: #fff3e0;
            --badge-danger-fg: #b71c1c;
            --badge-danger-bg: #ffebee;
            --badge-info-fg: #01579b;
            --badge-info-bg: #e1f5fe;
            --ease-out-quart: cubic-bezier(0.16, 1, 0.3, 1);
        }
        
        [data-theme="dark"] {
            --primary: #42a5f5;
            --primary-dark: #1976d2;
            --primary-light: #64b5f6;
            --secondary: #ffb74d;
            --secondary-dark: #ff9800;
            --success: #66bb6a;
            --warning: #ffb74d;
            --danger: #ef5350;
            --info: #42a5f5;
            --surface: #1e1e1e;
            --background: #121212;
            --text-primary: #ffffff;
            --text-secondary: #b0b0b0;
            --divider: #333333;
            --shadow-1: 0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.4);
            --shadow-2: 0 3px 6px rgba(0,0,0,0.4), 0 3px 6px rgba(0,0,0,0.5);
            --shadow-3: 0 10px 20px rgba(0,0,0,0.5), 0 6px 6px rgba(0,0,0,0.6);
            --shadow-4: 0 14px 28px rgba(0,0,0,0.6), 0 10px 10px rgba(0,0,0,0.7);
            --shadow-5: 0 19px 38px rgba(0,0,0,0.7), 0 15px 12px rgba(0,0,0,0.8);
            --hover-bg: rgba(66, 165, 245, 0.12);
            --badge-success-fg: #81c784;
            --badge-success-bg: rgba(76, 175, 80, 0.2);
            --badge-warning-fg: #ffcc80;
            --badge-warning-bg: rgba(255, 152, 0, 0.2);
            --badge-danger-fg: #ef9a9a;
            --badge-danger-bg: rgba(244, 67, 54, 0.2);
            --badge-info-fg: #64b5f6;
            --badge-info-bg: rgba(33, 150, 243, 0.2);
        }
        
        [data-theme="dark"] .success {
            background: rgba(76, 175, 80, 0.15);
        }
        [data-theme="dark"] .rec-item .savings {
            background: rgba(76, 175, 80, 0.15);
        }
        
        body {
            font-family: 'IBM Plex Sans', 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
            line-height: 1.6;
            color: var(--text-primary);
            background: var(--background);
            min-height: 100vh;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            font-feature-settings: "ss01", "cv05";
        }

        /* Display family: Newsreader, the editorial serif. Used on the report
           title, service-section titles, and the executive summary sentence.
           PRODUCT.md: Stripe Atlas / FT Lex / Pitchbook tearsheet feel. */
        .header h1,
        .service-title,
        .summary-headline,
        .empty-state h3 {
            font-family: 'Newsreader', 'Iowan Old Style', Georgia, 'Times New Roman', serif;
            font-optical-sizing: auto;
            letter-spacing: -0.005em;
        }
        .summary-headline {
            font-style: italic;
            font-weight: 400;
        }
        .summary-headline strong {
            font-style: normal;
            font-weight: 600;
        }
        
        .container { 
            max-width: 1440px; 
            margin: 0 auto; 
            padding: 24px;
        }
        
        /* Header — Audit Trail flat treatment */
        .header {
            background: var(--surface);
            color: var(--text-primary);
            padding: 48px 32px;
            border-radius: 8px;
            margin-bottom: 24px;
            border: 1px solid var(--divider);
            position: relative;
            overflow: hidden;
        }

        .header h1 {
            font-size: clamp(1.75rem, 4vw, 2.75rem);
            font-weight: 400;
            margin-bottom: 8px;
            letter-spacing: -0.5px;
            color: var(--text-primary);
        }

        .header .subtitle {
            font-size: 1.25rem;
            font-weight: 300;
            color: var(--text-secondary);
        }

        .header-info {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-top: 32px;
        }

        .header-info-item {
            background: var(--background);
            padding: 16px;
            border-radius: 8px;
            border: 1px solid var(--divider);
            color: var(--text-primary);
        }

        .header-info-item strong {
            display: block;
            font-size: 0.75rem;
            color: var(--text-secondary);
            margin-bottom: 4px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 600;
        }
        
        /* Material Summary Cards */
        .summary {
            margin-bottom: 24px;
        }

        .summary-headline {
            font-size: clamp(1.125rem, 2vw, 1.375rem);
            font-weight: 400;
            line-height: 1.55;
            color: var(--text-primary);
            margin: 0 0 16px 0;
            max-width: 75ch;
            font-variant-numeric: tabular-nums;
        }
        .summary-headline strong {
            font-weight: 600;
            color: var(--text-primary);
        }

        .visually-hidden {
            position: absolute !important;
            width: 1px; height: 1px;
            padding: 0; margin: -1px;
            overflow: hidden; clip: rect(0, 0, 0, 0);
            white-space: nowrap; border: 0;
        }

        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 16px;
        }
        
        .summary-card {
            background: var(--surface);
            padding: 24px;
            border-radius: 8px;
            border: 1px solid var(--divider);
            transition: border-color 0.2s var(--ease-out-quart);
            position: relative;
        }

        .summary-card:hover {
            border-color: var(--primary);
        }

        .summary-card h3 {
            font-size: 0.875rem;
            font-weight: 500;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-secondary);
        }

        .summary-card .value {
            font-size: 2.5rem;
            font-weight: 400;
            color: var(--text-primary);
            line-height: 1;
            font-variant-numeric: tabular-nums;
            letter-spacing: -0.01em;
        }

        .summary-card .subtitle {
            font-size: 0.875rem;
            color: var(--text-secondary);
            margin-top: 8px;
        }
        
        /* Tabs */
        .tabs {
            background: var(--surface);
            border-radius: 8px;
            border: 1px solid var(--divider);
            overflow: hidden;
            margin-bottom: 24px;
        }
        
        .tab-buttons {
            display: flex;
            background: var(--surface);
            border-bottom: 1px solid var(--divider);
            overflow-x: auto;
            scrollbar-width: thin;
        }
        
        .tab-buttons::-webkit-scrollbar {
            height: 4px;
        }
        
        .tab-buttons::-webkit-scrollbar-track {
            background: var(--background);
        }
        
        .tab-buttons::-webkit-scrollbar-thumb {
            background: var(--text-secondary);
            border-radius: 4px;
        }
        
        .tab-buttons {
            -webkit-overflow-scrolling: touch;
            scroll-snap-type: x proximity;
            position: relative;
        }

        .tabs {
            position: relative;
        }
        .tabs::after {
            content: '';
            position: absolute;
            top: 49px;
            right: 0;
            width: 48px;
            height: calc(100% - 49px - 2px);
            background: linear-gradient(to right, transparent, var(--surface));
            pointer-events: none;
            z-index: 1;
            opacity: 1;
            transition: opacity 0.2s var(--ease-out-quart);
        }
        .tabs.scroll-at-end::after {
            opacity: 0;
        }
        
        .tab-button {
            flex: 0 0 auto;
            min-width: 160px;
            padding: 16px 24px;
            background: transparent;
            border: none;
            cursor: pointer;
            font-weight: 500;
            font-size: 0.875rem;
            transition: all 0.3s var(--ease-out-quart);
            color: var(--text-secondary);
            text-align: center;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 2px solid transparent;
            position: relative;
        }
        
        .tab-button::after {
            content: '';
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: var(--primary);
            transform: scaleX(0);
            transition: transform 0.3s var(--ease-out-quart);
        }
        
        .tab-button:hover {
            background: rgba(128,128,128,0.08);
            color: var(--text-primary);
        }
        
        .tab-button.active {
            color: var(--primary);
            font-weight: 600;
        }
        
        .tab-button.active::after {
            transform: scaleX(1);
        }
        
        /* Priority filter strip — dims non-matching rec-items across all tabs */
        .priority-filter {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
            padding: 12px 24px;
            border-bottom: 1px solid var(--divider);
            background: var(--surface);
            font-size: 0.8125rem;
        }
        .priority-filter-label {
            color: var(--text-secondary);
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-right: 4px;
        }
        .priority-filter-chip {
            padding: 4px 12px;
            border: 1px solid var(--divider);
            background: var(--surface);
            color: var(--text-secondary);
            border-radius: 16px;
            cursor: pointer;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            transition: background 0.15s, color 0.15s, border-color 0.15s;
        }
        .priority-filter-chip:hover {
            border-color: var(--text-secondary);
            color: var(--text-primary);
        }
        .priority-filter-chip.active {
            background: var(--text-primary);
            color: var(--surface);
            border-color: var(--text-primary);
        }
        .priority-filter-chip.priority-high.active {
            background: var(--badge-danger-fg);
            border-color: var(--badge-danger-fg);
            color: var(--surface);
        }
        .priority-filter-chip.priority-medium.active {
            background: var(--badge-warning-fg);
            border-color: var(--badge-warning-fg);
            color: var(--surface);
        }
        .priority-filter-chip.priority-low.active {
            background: var(--badge-success-fg);
            border-color: var(--badge-success-fg);
            color: var(--surface);
        }
        .priority-filter-chip:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
        }
        body[data-priority-filter="high"] .rec-item:not(.high-priority),
        body[data-priority-filter="medium"] .rec-item:not(.medium-priority),
        body[data-priority-filter="low"] .rec-item:not(.low-priority) {
            opacity: 0.25;
            transition: opacity 0.2s var(--ease-out-quart);
        }

        .tab-content {
            display: none;
            padding: 32px;
            min-height: 300px;
            animation: fadeIn 0.3s var(--ease-out-quart);
        }
        
        .tab-content.active { 
            display: block;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        /* Service Header */
        .service-header {
            margin-bottom: 32px;
            padding-bottom: 24px;
            border-bottom: 1px solid var(--divider);
        }
        
        .service-title {
            font-size: 2rem;
            font-weight: 400;
            color: var(--text-primary);
            margin-bottom: 24px;
            letter-spacing: -0.5px;
        }
        
        .service-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 16px;
        }
        
        .stat-card {
            background: var(--background);
            padding: 16px;
            border-radius: 8px;
            text-align: center;
            transition: background-color 0.2s var(--ease-out-quart), border-color 0.2s var(--ease-out-quart);
            border: 1px solid var(--divider);
        }

        .stat-card:hover {
            background: var(--surface);
            border-color: var(--text-secondary);
        }

        .stat-card .stat-label,
        .stat-card h4 {
            font-size: 0.8125rem;
            color: var(--text-secondary);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 500;
        }

        .stat-card .value {
            font-size: 1.75rem;
            font-weight: 400;
            color: var(--text-primary);
            font-variant-numeric: tabular-nums;
        }
        
        /* Value status colors */
        .value.success, .success {
            color: var(--success);
            font-weight: 500;
        }
        
        .value.warning, .warning {
            color: var(--warning);
            font-weight: 500;
        }
        
        .value.danger, .danger {
            color: var(--danger);
            font-weight: 500;
        }
        
        /* Source info and callout styling */
        .source-info {
            font-size: 0.875rem;
            color: var(--text-secondary);
        }
        
        .callout-margin {
            margin-top: 16px;
        }
        
        .stat-card.savings {
            background: rgba(76, 175, 80, 0.1);
            border: 1px solid rgba(76, 175, 80, 0.3);
        }
        
        .stat-card.savings .value {
            color: var(--success);
        }
        
        /* Section title for recommendations */
        .section-title {
            font-size: 1.25rem;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 2px solid var(--divider);
        }
        
        /* Savings highlight - works inside and outside rec-item */
        .savings {
            color: var(--success);
            font-weight: 500;
        }
        
        .rec-summary .savings {
            background: rgba(76, 175, 80, 0.1);
            padding: 4px 8px;
            border-radius: 4px;
        }
        
        /* Recommendation Cards — flat at rest, priority encoded by leading badge.
           Border shifts to priority hue on hover (per DESIGN.md). */
        .recommendation-list {
            margin-top: 24px;
        }

        .rec-item {
            background: var(--surface);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 16px;
            border: 1px solid var(--divider);
            transition: border-color 0.2s var(--ease-out-quart);
            position: relative;
        }

        .rec-item:hover {
            border-color: var(--text-secondary);
        }

        .rec-item.high-priority:hover { border-color: var(--danger); }
        .rec-item.medium-priority:hover { border-color: var(--warning); }
        .rec-item.low-priority:hover { border-color: var(--success); }

        .rec-item.high-priority::before,
        .rec-item.medium-priority::before,
        .rec-item.low-priority::before {
            display: inline-block;
            font-size: 0.7rem;
            font-weight: 600;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            padding: 3px 10px;
            border-radius: 16px;
            margin-bottom: 12px;
            font-variant-numeric: tabular-nums;
        }

        .rec-item.high-priority::before {
            content: "High priority";
            color: var(--badge-danger-fg);
            background: var(--badge-danger-bg);
        }

        .rec-item.medium-priority::before {
            content: "Medium priority";
            color: var(--badge-warning-fg);
            background: var(--badge-warning-bg);
        }

        .rec-item.low-priority::before {
            content: "Low priority";
            color: var(--badge-success-fg);
            background: var(--badge-success-bg);
        }

        .rec-item h4,
        .rec-item h5 {
            font-size: 1.125rem;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 12px;
        }

        /* Source-confidence typographic prefix on every rec-item title.
           The audit chain (METRIC, ML, COST HUB, AUDIT) lives in the
           typesetting itself rather than as a separate chip. PRODUCT.md:
           "the audit chain is the product." */
        .source-section[data-source] .rec-item > h4::before {
            font-family: 'IBM Plex Mono', 'SF Mono', Consolas, monospace;
            font-weight: 600;
            font-size: 0.75em;
            letter-spacing: 0.5px;
            color: var(--text-secondary);
            text-transform: uppercase;
            margin-right: 8px;
            vertical-align: 1px;
        }
        .source-section[data-source="Metric Backed"] .rec-item > h4::before {
            content: "METRIC \00b7";
            color: var(--success);
        }
        .source-section[data-source="ML Backed"] .rec-item > h4::before {
            content: "ML \00b7";
            color: var(--info);
        }
        .source-section[data-source="Cost Hub"] .rec-item > h4::before {
            content: "COST HUB \00b7";
            color: var(--warning);
        }
        .source-section[data-source="Audit Based"] .rec-item > h4::before {
            content: "AUDIT \00b7";
            color: var(--text-secondary);
        }
        /* The source-section wrapper itself is structural only; no margin
           collapse on the inner rec-item list. */
        .source-section { display: contents; }
        
        .rec-item p {
            margin-bottom: 12px;
            color: var(--text-secondary);
            line-height: 1.7;
        }
        
        .rec-item strong {
            color: var(--text-primary);
            font-weight: 500;
        }
        
        .rec-item .savings {
            color: var(--success);
            font-weight: 700;
            background: rgba(76, 175, 80, 0.1);
            padding: 8px 16px;
            border-radius: 4px;
            display: inline-block;
            margin: 8px 0;
            font-size: 1.125rem;
            letter-spacing: -0.01em;
            font-variant-numeric: tabular-nums;
        }
        
        /* Recommendation Detail Tables (Cost Hub, Anomaly, etc.) */
        .rec-table {
            width: 100%;
            border-collapse: collapse;
            margin: 8px 0 12px 0;
            font-size: 0.85rem;
        }
        .rec-table th {
            background: var(--bg-secondary);
            color: var(--text-secondary);
            font-weight: 600;
            text-align: left;
            padding: 8px 10px;
            border-bottom: 2px solid var(--border);
            white-space: nowrap;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .rec-table td {
            padding: 7px 10px;
            border-bottom: 1px solid var(--border);
            color: var(--text-primary);
            vertical-align: top;
            font-variant-numeric: tabular-nums;
        }
        .rec-table tr:last-child td {
            border-bottom: none;
        }
        .rec-table tr:hover td {
            background: var(--hover-bg, rgba(0, 0, 0, 0.04));
        }
        [data-theme="dark"] .rec-table tr:hover td {
            background: rgba(255, 255, 255, 0.06);
        }
        
        /* Material Chips/Badges */
        .badge {
            display: inline-flex;
            align-items: center;
            padding: 4px 12px;
            font-size: 0.75rem;
            font-weight: 500;
            text-align: center;
            white-space: nowrap;
            border-radius: 16px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .badge-warning {
            color: var(--badge-warning-fg);
            background-color: var(--badge-warning-bg);
        }

        .badge-info {
            color: var(--badge-info-fg);
            background-color: var(--badge-info-bg);
        }

        .source-badge {
            display: inline-block;
            margin: 8px 0;
        }

        .badge-success {
            color: var(--badge-success-fg);
            background-color: var(--badge-success-bg);
        }

        .badge-danger {
            color: var(--badge-danger-fg);
            background-color: var(--badge-danger-bg);
        }
        
        /* Tables — flat, hairline-led */
        .recommendations-table,
        .top-buckets-table table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 16px;
            background: var(--surface);
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--divider);
        }
        
        .recommendations-table th,
        .top-buckets-table th {
            background: var(--background);
            padding: 16px;
            text-align: left;
            font-weight: 500;
            font-size: 0.875rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid var(--divider);
        }
        
        .recommendations-table td,
        .top-buckets-table td {
            padding: 16px;
            border-bottom: 1px solid var(--divider);
            vertical-align: top;
            color: var(--text-primary);
            font-variant-numeric: tabular-nums;
        }
        
        .recommendations-table tr:hover,
        .top-buckets-table tr:hover {
            background-color: var(--hover-bg);
            transition: background-color 0.15s ease;
        }
        
        .recommendations-table tr:last-child td,
        .top-buckets-table tr:last-child td {
            border-bottom: none;
        }
        
        .recommendations-table code {
            background: var(--background);
            padding: 2px 8px;
            border-radius: 4px;
            font-family: 'IBM Plex Mono', 'Courier New', monospace;
            font-size: 0.875rem;
            color: var(--primary);
        }
        
        /* Status callouts — full thin borders, no side-stripes */
        .success {
            color: var(--success);
            font-weight: 500;
            background: rgba(76, 175, 80, 0.1);
            padding: 16px;
            border-radius: 8px;
            border: 1px solid var(--success);
            margin: 16px 0;
        }

        .info-box {
            background: rgba(33, 150, 243, 0.1);
            padding: 16px;
            border-radius: 8px;
            border: 1px solid var(--info);
            margin: 16px 0;
            color: var(--text-primary);
        }

        .info-note, .pricing-note {
            background: rgba(33, 150, 243, 0.1);
            border: 1px solid var(--info);
            padding: 12px 16px;
            margin: 16px 0;
            border-radius: 4px;
            font-size: 0.9em;
            color: var(--text-primary);
        }
        .info-note p, .pricing-note p {
            margin: 4px 0;
        }

        .warning-box {
            background: rgba(255, 152, 0, 0.1);
            padding: 16px;
            border-radius: 8px;
            border: 1px solid var(--warning);
            margin: 16px 0;
            color: var(--text-primary);
        }
        
        /* Sections — flat, hairline-led */
        .top-buckets-table {
            margin: 24px 0;
            background: var(--surface);
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--divider);
        }

        .bucket-sort-toggle {
            display: inline-flex;
            gap: 4px;
            margin: 12px 0;
            padding: 4px;
            background: var(--background);
            border-radius: 18px;
            border: 1px solid var(--divider);
        }
        .bucket-sort-btn {
            padding: 4px 12px;
            background: transparent;
            border: none;
            border-radius: 16px;
            cursor: pointer;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-secondary);
            transition: background 0.15s, color 0.15s;
        }
        .bucket-sort-btn:hover {
            color: var(--text-primary);
        }
        .bucket-sort-btn.active {
            background: var(--surface);
            color: var(--text-primary);
            box-shadow: 0 0 0 1px var(--divider);
        }
        .bucket-sort-btn:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
        }
        
        .top-buckets-table h4 {
            background: var(--background);
            color: var(--text-primary);
            padding: 20px 24px;
            margin: 0;
            font-size: 1.125rem;
            font-weight: 500;
            border-bottom: 1px solid var(--divider);
        }

        .affected-resources {
            margin: 24px 0;
            padding: 24px;
            background: var(--surface);
            border-radius: 8px;
            border: 1px solid var(--divider);
        }
        
        .resource-group {
            margin-bottom: 16px;
            padding: 16px;
            background: var(--background);
            border-radius: 8px;
            border: 1px solid var(--divider);
        }
        
        .resource-group h5 {
            color: var(--primary);
            margin-bottom: 8px;
            font-weight: 500;
            font-size: 1rem;
        }
        
        .group-savings {
            color: var(--success);
            font-weight: 500;
            margin-bottom: 8px;
        }
        
        .resource-list {
            margin-left: 20px;
            color: var(--text-secondary);
            max-height: 300px;
            overflow-y: auto;
        }
        
        .resource-list li {
            margin-bottom: 4px;
            line-height: 1.6;
        }
        
        /* Collapsed list overflow */
        .rec-item li.hidden-overflow,
        .resource-list li.hidden-overflow {
            display: none;
        }
        .show-more-toggle {
            display: inline-block;
            margin: 8px 0 16px;
            padding: 6px 12px;
            background: var(--background);
            border: 1px solid var(--divider);
            border-radius: 16px;
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            transition: background 0.15s, color 0.15s, border-color 0.15s;
        }
        .show-more-toggle:hover {
            border-color: var(--text-secondary);
            color: var(--text-primary);
        }
        .show-more-toggle:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
        }

        /* Copy-resource-ID button */
        .copy-id {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 22px;
            height: 22px;
            margin-right: 6px;
            padding: 0;
            border: 1px solid var(--divider);
            background: var(--surface);
            color: var(--text-secondary);
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.875rem;
            line-height: 1;
            vertical-align: middle;
            transition: background 0.15s, color 0.15s, border-color 0.15s;
        }
        .copy-id:hover {
            border-color: var(--primary);
            color: var(--primary);
        }
        .copy-id:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
        }
        .copy-id.copied {
            background: var(--badge-success-bg);
            color: var(--badge-success-fg);
            border-color: var(--badge-success-fg);
        }
        .copy-id.copied::after {
            content: " ✓";
        }

        .show-more-link {
            color: var(--primary);
            text-decoration: none;
            font-weight: 500;
            cursor: pointer;
            transition: color 0.2s var(--ease-out-quart);
        }
        
        .show-more-link:hover {
            color: var(--primary-dark);
            text-decoration: underline;
        }
        
        .show-more-link:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
            border-radius: 2px;
        }
        
        .tab-btn:focus-visible,
        .theme-toggle:focus-visible,
        button:focus-visible,
        a:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
        }
        
        .show-more-container {
            margin: 16px 0;
            text-align: center;
        }
        
        .rec-summary {
            background: rgba(255, 152, 0, 0.1);
            padding: 16px 24px;
            border-radius: 8px;
            margin-bottom: 24px;
            border: 1px solid var(--warning);
            font-size: 1rem;
            color: var(--text-primary);
        }

        /* Top-line risks row — gives executive readers 10-second confidence */
        .risks-row {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-top: 16px;
            margin-bottom: 8px;
            font-variant-numeric: tabular-nums;
        }
        .risk-pill {
            display: inline-flex;
            align-items: baseline;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 16px;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border: 1px solid var(--divider);
            color: var(--text-secondary);
            background: var(--surface);
        }
        .risk-pill strong {
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--text-primary);
        }
        .risk-pill.risk-high {
            color: var(--badge-danger-fg);
            background: var(--badge-danger-bg);
            border-color: transparent;
        }
        .risk-pill.risk-high strong { color: var(--badge-danger-fg); }
        .risk-pill.risk-medium {
            color: var(--badge-warning-fg);
            background: var(--badge-warning-bg);
            border-color: transparent;
        }
        .risk-pill.risk-medium strong { color: var(--badge-warning-fg); }
        .risk-pill.risk-low {
            color: var(--badge-success-fg);
            background: var(--badge-success-bg);
            border-color: transparent;
        }
        .risk-pill.risk-low strong { color: var(--badge-success-fg); }
        .risk-pill.risk-info {
            color: var(--badge-info-fg);
            background: var(--badge-info-bg);
            border-color: transparent;
        }
        .risk-pill.risk-info strong { color: var(--badge-info-fg); }

        /* Reconciliation footnote — surfaces the dedup math */
        .reconciliation-note {
            margin: 12px 0 0;
            font-size: 0.8125rem;
            color: var(--text-secondary);
            line-height: 1.6;
            font-variant-numeric: tabular-nums;
        }
        .reconciliation-note strong {
            color: var(--text-primary);
            font-weight: 600;
        }

        /* Investigation savings — quieter than confirmed savings */
        .savings.investigation,
        .rec-item .savings.investigation {
            background: transparent;
            color: var(--text-secondary);
            font-weight: 500;
            border-left: none;
        }
        .savings.investigation::before {
            content: "Needs data \00b7 ";
            font-weight: 600;
            color: var(--text-secondary);
        }

        /* Glossary block — defines source-confidence badges */
        .glossary {
            background: var(--surface);
            border: 1px solid var(--divider);
            border-radius: 8px;
            padding: 16px 20px;
            margin: 16px 0;
            font-size: 0.875rem;
            color: var(--text-primary);
        }
        .glossary h3 {
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-secondary);
            margin-bottom: 12px;
        }
        .glossary dl {
            display: grid;
            grid-template-columns: max-content 1fr;
            gap: 8px 16px;
            margin: 0;
        }
        .glossary dt {
            font-weight: 600;
            color: var(--text-primary);
        }
        .glossary dd {
            margin: 0;
            color: var(--text-secondary);
            line-height: 1.5;
        }
        @media (max-width: 600px) {
            .glossary dl {
                grid-template-columns: 1fr;
                gap: 4px;
            }
            .glossary dt {
                margin-top: 8px;
            }
        }

        .opportunities {
            margin-top: 24px;
        }
        
        .opportunity {
            background: rgba(76, 175, 80, 0.1);
            padding: 16px;
            margin: 12px 0;
            border-radius: 8px;
            border: 1px solid var(--success);
            transition: background-color 0.2s var(--ease-out-quart);
        }

        .opportunity:hover {
            background: rgba(76, 175, 80, 0.15);
        }
        
        /* Footer */
        .footer {
            margin-top: 48px;
            padding: 32px;
            background: var(--surface);
            border-radius: 8px;
            text-align: center;
            color: var(--text-secondary);
            border: 1px solid var(--divider);
        }

        /* Visually-hidden but available to screen readers (WCAG SC 1.1.1 fallback) */
        .visually-hidden {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            padding: 0 !important;
            margin: -1px !important;
            overflow: hidden !important;
            clip: rect(0, 0, 0, 0) !important;
            white-space: nowrap !important;
            border: 0 !important;
        }
        
        .footer p {
            margin: 8px 0;
            font-size: 0.875rem;
        }
        .footer-link {
            color: var(--primary);
            text-decoration: none;
            font-weight: 500;
        }
        .footer-link:hover {
            text-decoration: underline;
        }
        .footer-link:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
            border-radius: 2px;
        }
        
        /* Sticky savings-sorted jump-nav rail. Inline on narrow screens,
           pinned to the left margin on wide ones. Hidden in print (tabs
           become the navigation via @media print). */
        .jump-nav {
            display: none;
            background: var(--surface);
            border: 1px solid var(--divider);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 16px;
            font-size: 0.875rem;
        }
        @media (min-width: 1400px) {
            .jump-nav {
                display: block;
                position: fixed;
                top: 100px;
                left: max(16px, calc(50% - 720px - 240px));
                width: 220px;
                max-height: calc(100vh - 140px);
                overflow-y: auto;
                z-index: 40;
                margin-bottom: 0;
            }
        }
        .jump-nav__heading {
            text-transform: uppercase;
            font-size: 0.75rem;
            font-weight: 600;
            letter-spacing: 0.5px;
            color: var(--text-secondary);
            margin: 0 0 8px 0;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--divider);
        }
        .jump-nav__list {
            list-style: none;
            padding: 0;
            margin: 0;
        }
        .jump-nav__list li {
            margin: 0;
        }
        .jump-nav__list a {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 12px;
            padding: 6px 8px;
            text-decoration: none;
            color: var(--text-primary);
            border-radius: 4px;
            border: 1px solid transparent;
        }
        .jump-nav__list a:hover {
            background: var(--background);
            border-color: var(--divider);
        }
        .jump-nav__list a:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
        }
        .jump-nav__label {
            font-weight: 500;
        }
        .jump-nav__amount {
            color: var(--text-secondary);
            font-variant-numeric: tabular-nums;
            font-size: 0.8125rem;
            white-space: nowrap;
        }

        /* Responsive Design */
        @media (max-width: 960px) {
            .container { padding: 16px; }
            .header { padding: 32px 24px; }
            .header h1 { font-size: 2rem; }
            .summary-grid { grid-template-columns: 1fr; }
            .service-stats { grid-template-columns: repeat(2, 1fr); }
        }
        
        @media (max-width: 768px) {
            .summary-grid { grid-template-columns: repeat(2, 1fr); }
            .charts-container { grid-template-columns: 1fr; }
        }
        
        @media (max-width: 600px) {
            .header h1 { font-size: 1.75rem; }
            .header .subtitle { font-size: 1rem; }
            .header-info { grid-template-columns: 1fr; }
            .tab-button { min-width: 100px; font-size: 0.7rem; padding: 10px 14px; }
            .service-title { font-size: 1.5rem; }
            .service-stats { grid-template-columns: 1fr; }
            .summary-card .value { font-size: 2rem; }
            .stat-card .value { font-size: 1.5rem; }
            .rec-item { padding: 16px; }
            .top-buckets-table { overflow-x: auto; }
            .chart-wrapper { height: 250px; }
        }
        
        /* Charts Container */
        .charts-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 32px;
            margin-top: 32px;
        }
        
        .chart-section {
            background: var(--surface);
            border-radius: 8px;
            padding: 24px;
            border: 1px solid var(--divider);
        }
        
        .chart-section h3 {
            font-size: 1.125rem;
            color: var(--text-primary);
            margin-bottom: 16px;
            text-align: center;
        }
        
        .chart-wrapper {
            position: relative;
            height: 400px;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        
        .chart-wrapper canvas {
            cursor: pointer;
        }

        /* Dark Mode Toggle */
        .theme-toggle {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 1000;
            background: var(--surface);
            border: 1px solid var(--divider);
            border-radius: 50px;
            padding: 8px 16px;
            cursor: pointer;
            box-shadow: var(--shadow-2);
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            color: var(--text-primary);
        }

        .theme-toggle:hover {
            border-color: var(--text-secondary);
        }
        
        .icon {
            width: 20px;
            height: 20px;
            display: inline-block;
            vertical-align: middle;
            margin-right: 4px;
        }
        .icon-sm {
            width: 18px;
            height: 18px;
        }
        .chart-fallback {
            text-align: center;
            color: var(--text-secondary);
            font-style: italic;
            padding: 24px;
            border: 1px dashed var(--divider);
            border-radius: 8px;
            margin: 8px 0;
        }

        /* Back to Top Button */
        .back-to-top {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            width: 44px;
            height: 44px;
            border-radius: 50%;
            background: var(--primary);
            color: white;
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0;
            visibility: hidden;
            transition: opacity 0.3s, visibility 0.3s, transform 0.2s;
            box-shadow: var(--shadow-3);
            z-index: 100;
        }
        .back-to-top.visible {
            opacity: 1;
            visibility: visible;
        }
        .back-to-top:hover {
            background: var(--primary-dark);
        }

        /* Export Button */
        .export-btn {
            position: fixed;
            top: 20px;
            right: 140px;
            z-index: 1000;
            background: var(--surface);
            border: 1px solid var(--divider);
            border-radius: 50px;
            padding: 8px 16px;
            cursor: pointer;
            box-shadow: var(--shadow-2);
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            color: var(--text-primary);
        }
        .export-btn:hover {
            border-color: var(--text-secondary);
        }
        .export-btn:focus-visible {
            outline: 2px solid var(--primary);
            outline-offset: 2px;
        }

        /* Tab Chips — count or savings */
        .tab-count,
        .tab-savings {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 18px;
            height: 18px;
            padding: 0 7px;
            margin-left: 6px;
            font-size: 0.7rem;
            font-weight: 600;
            border-radius: 9px;
            background: var(--background);
            color: var(--text-secondary);
            line-height: 1;
            vertical-align: middle;
            font-variant-numeric: tabular-nums;
        }
        .tab-button.active .tab-savings {
            background: var(--badge-success-bg);
            color: var(--badge-success-fg);
        }
        [data-theme="dark"] .tab-count,
        [data-theme="dark"] .tab-savings {
            background: rgba(255,255,255,0.08);
            color: rgba(255,255,255,0.75);
        }

        /* Print Styles */
        @media print {
            body { background: white; color: black; font-family: Georgia, 'Times New Roman', serif; }
            .container { box-shadow: none; }
            .tab-buttons { display: none; }
            .priority-filter { display: none; }
            .jump-nav { display: none; }
            .theme-toggle { display: none; }
            .export-btn { display: none; }
            .back-to-top { display: none; }
            .tab-content { display: block !important; page-break-inside: avoid; }
            .rec-item { page-break-inside: avoid; }
            canvas { max-width: 100%; page-break-inside: avoid; }
            .service-section { page-break-after: auto; }
            .service-section:not(:last-child) { page-break-after: always; }
            .rec-item { page-break-inside: avoid; }
            .recommendations-table { page-break-inside: avoid; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important;
                scroll-behavior: auto !important;
            }
        }
        </style>"""

    def _get_header(self) -> str:
        """Get header section"""
        region = self.scan_results["region"]
        header = f"""<div class="header">
            <h1>AWS Cost Optimization Report</h1>
            <div class="header-info">
                <div><strong>Account ID:</strong> {html.escape(str(self.scan_results["account_id"]))}</div>
                <div><strong>Region:</strong> {html.escape(str(region))}</div>
                <div><strong>Scan Time:</strong> {html.escape(str(self.scan_results["scan_time"][:19]))}</div>
                <div><strong>Services:</strong> {self.scan_results["summary"]["total_services_scanned"]}</div>
            </div>
        </div>"""

        pricing_note = f'<div class="pricing-note"><p><svg class="icon icon-sm"><use href="#icon-dollar"/></svg> Savings estimates based on AWS Pricing API data for <strong>{region}</strong>. Some services use regional pricing estimates. Pricing may vary based on actual usage patterns and reserved capacity.</p>'
        multiplier = self.scan_results.get("pricing_multiplier")
        if multiplier is not None and multiplier > 1.0:
            pricing_note += f'<p><svg class="icon icon-sm"><use href="#icon-alert"/></svg> Regional pricing multiplier of {multiplier}x applied for this region.</p>'
        pricing_note += "</div>"

        return header + pricing_note

    def _get_summary(self) -> str:
        """Render the Audit Trail executive summary.

        One sentence carries the headline (defensible monthly savings, annual
        figure, top two services by savings with their share of total, risk
        signal count). The reconciliation footnote stays below, surfacing the
        dedup math when the per-service sum diverges from the headline. No
        four-card grid: the cards were template furniture, the sentence is
        the audit voice.
        """
        summary = self.scan_results.get("summary", {})
        total_savings = float(summary.get("total_monthly_savings", 0) or 0)

        # Per-service savings sum used by reconciliation footnote — the
        # adversarial reader mentally adds the per-tab totals; if the headline
        # differs, surface the dedup math rather than letting the discrepancy
        # register as inattention. Mirrors _get_service_content so the numbers
        # line up with each tab.
        services = self.scan_results.get("services", {})
        per_service_savings: List[Tuple[str, float]] = []
        raw_per_service_sum = 0.0
        for service_key, service_data in services.items():
            calculated = self._calculate_service_savings(service_key, service_data)
            canonical = service_data.get("total_monthly_savings", 0) or 0
            shown = float(calculated if calculated > 0 else canonical)
            raw_per_service_sum += shown
            if shown > 0:
                per_service_savings.append((service_data.get("service_name", service_key), shown))

        per_service_savings.sort(key=lambda p: -p[1])
        top_two = per_service_savings[:2]
        top_share = sum(s for _, s in top_two) / total_savings if total_savings > 0 else 0.0

        risk_counts = self._count_priorities_by_severity()
        high_count = risk_counts["high"]
        anomaly_count = risk_counts["anomalies"]

        # Compose the sentence.
        sentence_parts: List[str] = [
            f'This account can defensibly save <strong>${total_savings:,.2f}/month</strong>'
            f' (<strong>${total_savings * 12:,.0f}/year</strong>).'
        ]
        if len(top_two) >= 2 and top_share > 0:
            sentence_parts.append(
                f'<strong>{html.escape(top_two[0][0])}</strong> (${top_two[0][1]:,.0f}) and '
                f'<strong>{html.escape(top_two[1][0])}</strong> (${top_two[1][1]:,.0f}) '
                f'hold <strong>{top_share * 100:.0f}%</strong> of that.'
            )
        elif len(top_two) == 1:
            sentence_parts.append(
                f'<strong>{html.escape(top_two[0][0])}</strong> (${top_two[0][1]:,.0f}) '
                f'is the largest opportunity.'
            )

        if high_count > 0:
            severity_word = "risk is" if high_count == 1 else "risks are"
            sentence_parts.append(
                f'<strong>{high_count}</strong> HIGH-severity {severity_word} open.'
            )
        elif anomaly_count > 0:
            anomaly_word = "anomaly is" if anomaly_count == 1 else "anomalies are"
            sentence_parts.append(
                f'<strong>{anomaly_count}</strong> active cost {anomaly_word} flagged.'
            )

        sentence = " ".join(sentence_parts)

        out = (
            '<section class="summary" aria-labelledby="summary-heading">'
            '<h2 id="summary-heading" class="visually-hidden">Executive Summary</h2>'
            f'<p class="summary-headline">{sentence}</p>'
        )

        # Reconciliation footnote: only when per-service sum diverges from the
        # headline by more than $1 (avoid noise from rounding).
        dedup_delta = raw_per_service_sum - total_savings
        if dedup_delta > 1.0:
            out += (
                '<p class="reconciliation-note">'
                f"Per-tab sum: <strong>${raw_per_service_sum:,.2f}</strong> "
                f"&minus; deduplication adjustment <strong>${dedup_delta:,.2f}</strong> "
                f"(Cost Optimization Hub recommendations counted once across overlapping services) "
                f"= headline <strong>${total_savings:,.2f}</strong>."
                "</p>"
            )

        graviton_count = self._count_graviton_exclusions()
        if graviton_count > 0:
            out += (
                '<div class="info-note"><p>'
                '<svg class="icon icon-sm"><use href="#icon-clipboard"/></svg> '
                f'Note: {graviton_count} Graviton migration recommendations excluded from '
                'per-service detail. These require architecture-level review and are available '
                'via AWS Compute Optimizer.'
                '</p></div>'
            )

        out += "</section>"
        return out

    def _count_priorities_by_severity(self) -> Dict[str, int]:
        """Aggregate HIGH/MEDIUM/LOW counts + anomaly + billing-alarm risk signals.

        Returns a dict with keys ``high``, ``medium``, ``low``, ``anomalies``,
        ``billing_alarms``. Walks every service's filtered recommendations once.
        """
        counts = {"high": 0, "medium": 0, "low": 0, "anomalies": 0, "billing_alarms": 0}
        for service_key, service_data in self.scan_results.get("services", {}).items():
            filtered = self._filter_recommendations(service_data)
            for source_data in filtered.get("sources", {}).values():
                if isinstance(source_data, dict):
                    recs = source_data.get("recommendations", []) or []
                elif isinstance(source_data, list):
                    recs = source_data
                else:
                    recs = []
                for rec in recs:
                    priority = str(
                        rec.get("priority") or rec.get("Priority") or rec.get("severity") or rec.get("Severity") or ""
                    ).strip().lower()
                    if priority in ("high", "critical"):
                        counts["high"] += 1
                    elif priority in ("medium", "warning"):
                        counts["medium"] += 1
                    elif priority in ("low", "info", "informational"):
                        counts["low"] += 1
            if service_key == "cost_anomaly":
                counts["anomalies"] = service_data.get("active_anomalies_count", 0) or 0
                counts["billing_alarms"] = service_data.get("billing_alarms_count", 0) or 0
        return counts

    def _get_tabs(self) -> str:
        """Get tabs section.

        Tab order is **savings-descending** so the architect skim lands on the
        services that hold the most money first. Each tab chip shows the
        savings figure (compact dollar format) rather than a raw recommendation
        count: 'RDS  $267' beats 'RDS  8' when the question is 'where is the
        money'. Recommendation count remains in the panel header.
        """
        services = self.scan_results["services"]

        # Extract snapshots and AMIs from EBS enhanced checks
        snapshots_data = self._extract_snapshots_data(services)
        amis_data = self._extract_amis_data(services)

        # Build a flat list of (savings, key, label, rec_count, content_emitter) tuples.
        # All service tabs plus the synthetic Snapshots / AMIs aggregations. Sort
        # by savings descending so high-impact services lead the strip.
        tab_entries: List[Tuple[float, str, str, int, Callable[[], str]]] = []

        for service_key, service_data in services.items():
            filtered = self._filter_recommendations(service_data)
            rec_count = filtered.get("total_recommendations", 0)
            if rec_count == 0:
                continue
            savings = float(filtered.get("total_monthly_savings", 0) or 0)
            label = str(service_data["service_name"])
            if service_key == "ami" and amis_data["count"] > 0:
                # AMI tab is rendered via the dedicated AMIs panel.
                tab_entries.append((
                    savings,
                    service_key,
                    label,
                    rec_count,
                    lambda d=amis_data: self._get_amis_content(d),
                ))
            else:
                tab_entries.append((
                    savings,
                    service_key,
                    label,
                    rec_count,
                    lambda k=service_key, d=service_data: self._get_service_content(k, d),
                ))

        # Synthetic Snapshots tab.
        if snapshots_data["count"] > 0:
            snap_savings = float(snapshots_data.get("total_savings", 0) or 0)
            tab_entries.append((
                snap_savings,
                "snapshots",
                "Snapshots",
                snapshots_data["count"],
                lambda d=snapshots_data: self._get_snapshots_content(d),
            ))

        # Synthetic AMIs tab only if there's no underlying AMI service section.
        ami_already_present = any(
            s.get("total_recommendations", 0) > 0 for k, s in services.items() if k == "ami"
        )
        if amis_data["count"] > 0 and not ami_already_present:
            ami_savings = sum(
                float(r.get("EstimatedMonthlySavings", 0) or 0)
                for r in amis_data.get("recommendations", [])
            )
            tab_entries.append((
                ami_savings,
                "amis",
                "AMIs",
                amis_data["count"],
                lambda d=amis_data: self._get_amis_content(d),
            ))

        # Descending sort: highest savings first. Ties broken by recommendation
        # count desc then label asc so the order is deterministic.
        tab_entries.sort(key=lambda e: (-e[0], -e[3], e[2]))

        # Tab buttons
        tab_buttons = '<div class="tab-buttons" role="tablist">'
        tab_buttons += (
            '<button class="tab-button active" role="tab" aria-selected="true" '
            'aria-controls="panel-executive-summary" id="tab-executive-summary" '
            'onclick="showTab(\'executive-summary\', event)">'
            '<svg class="icon icon-sm"><use href="#icon-chart"/></svg> Executive Summary'
            "</button>"
        )

        for savings, key, label, _rec_count, _emit in tab_entries:
            chip = (
                f'<span class="tab-savings" aria-label="{savings:,.2f} dollars per month estimated savings">'
                f"{_format_savings_chip(savings)}</span>"
                if savings > 0
                else ""
            )
            tab_buttons += (
                f'<button class="tab-button" role="tab" aria-selected="false" '
                f'aria-controls="panel-{html.escape(key)}" id="tab-{html.escape(key)}" '
                f'onclick="showTab(\'{html.escape(key)}\', event)">'
                f'{html.escape(label)}{chip}</button>'
            )

        tab_buttons += "</div>"

        # Tab contents — same order as buttons.
        tab_contents = (
            '<div id="panel-executive-summary" class="tab-content active" '
            'role="tabpanel" aria-labelledby="tab-executive-summary">'
            + self._get_executive_summary_content()
            + "</div>"
        )

        for _savings, key, _label, _rec_count, emit in tab_entries:
            tab_contents += (
                f'<div id="panel-{html.escape(key)}" class="tab-content" '
                f'role="tabpanel" aria-labelledby="tab-{html.escape(key)}">'
                f"{emit()}</div>"
            )

        # Priority filter strip — dims non-matching .rec-item across all tabs.
        filter_strip = (
            '<div class="priority-filter" role="group" aria-label="Filter recommendations by priority">'
            '<span class="priority-filter-label">Filter:</span>'
            '<button type="button" class="priority-filter-chip active" data-priority-filter="all" aria-pressed="true">All</button>'
            '<button type="button" class="priority-filter-chip priority-high" data-priority-filter="high" aria-pressed="false">High priority</button>'
            '<button type="button" class="priority-filter-chip priority-medium" data-priority-filter="medium" aria-pressed="false">Medium</button>'
            '<button type="button" class="priority-filter-chip priority-low" data-priority-filter="low" aria-pressed="false">Low</button>'
            "</div>"
        )

        # Sticky savings-sorted jump-nav rail. Hidden on screens narrower than
        # the wide breakpoint and in print (tabs become the navigation there).
        # Anchor click activates the target tab via jumpToPanel and scrolls.
        rail_items = "".join(
            f'<li><a href="#panel-{html.escape(key)}" '
            f'onclick="jumpToPanel(\'{html.escape(key)}\'); event.preventDefault();">'
            f'<span class="jump-nav__label">{html.escape(label)}</span>'
            f'<span class="jump-nav__amount">{_format_savings_chip(savings) or "&mdash;"}</span>'
            "</a></li>"
            for savings, key, label, _rec_count, _emit in tab_entries
        )
        jump_nav = (
            '<aside class="jump-nav" aria-label="Service savings jump nav">'
            '<h3 class="jump-nav__heading">By savings</h3>'
            f'<ol class="jump-nav__list">{rail_items}</ol>'
            '</aside>'
        )

        return jump_nav + f'<div class="tabs">{tab_buttons}{filter_strip}{tab_contents}</div>'

    def _extract_snapshots_data(self, services: Dict[str, Any]) -> Dict[str, Any]:
        """Extract all snapshot-related recommendations from services"""
        snapshots = []
        seen_snapshot_ids = set()

        # Check EBS enhanced checks
        ebs_service = services.get("ebs", {})
        ebs_sources = ebs_service.get("sources", {})
        enhanced_checks = ebs_sources.get("enhanced_checks", {})

        for rec in enhanced_checks.get("recommendations", []):
            check_category = rec.get("CheckCategory", "")
            if "snapshot" in check_category.lower():
                # Deduplicate by SnapshotId
                snapshot_id = rec.get("SnapshotId", "N/A")
                if snapshot_id != "N/A" and snapshot_id not in seen_snapshot_ids:
                    seen_snapshot_ids.add(snapshot_id)
                    snapshots.append(rec)

        return {
            "count": len(snapshots),
            "recommendations": snapshots,
            "total_savings": sum(
                float(
                    rec.get("EstimatedSavings", "0")
                    .replace(",", "")
                    .replace("$", "")
                    .replace("/month", "")
                    .split("(")[0]
                    .strip()
                )
                for rec in snapshots
                if "EstimatedSavings" in rec
                and rec.get("EstimatedSavings", "0") != "0"
                and rec.get("EstimatedSavings", "")
                .replace(",", "")
                .replace("$", "")
                .replace("/month", "")
                .split("(")[0]
                .strip()
                .replace(".", "")
                .isdigit()
            ),
        }

    def _extract_amis_data(self, services: Dict[str, Any]) -> Dict[str, Any]:
        """Extract all AMI-related recommendations from services"""
        amis = []

        # Check dedicated AMI service section
        ami_service = services.get("ami", {})
        ami_sources = ami_service.get("sources", {})

        for source_name, source_data in ami_sources.items():
            if isinstance(source_data, dict):
                amis.extend(source_data.get("recommendations", []))
            elif isinstance(source_data, list):
                amis.extend(source_data)

        # Also check EC2 enhanced checks for backward compatibility
        ec2_service = services.get("ec2", {})
        ec2_sources = ec2_service.get("sources", {})
        enhanced_checks = ec2_sources.get("enhanced_checks", {})

        for rec in enhanced_checks.get("recommendations", []):
            check_category = rec.get("CheckCategory", "")
            if "ami" in check_category.lower():
                amis.append(rec)

        return {"count": len(amis), "recommendations": amis}

    def _get_snapshots_content(self, snapshots_data: Dict[str, Any]) -> str:
        """Generate content for Snapshots tab"""
        # Group snapshots by age range
        age_groups = {"90-180 days": [], "180-365 days": [], "1-2 years": [], "2+ years": []}

        total_savings = 0
        for rec in snapshots_data["recommendations"]:
            # Filter out invalid entries
            snapshot_id = rec.get("SnapshotId", "N/A")
            age_days = rec.get("AgeDays", 0)

            # Skip entries with missing SnapshotId or invalid age
            if snapshot_id == "N/A" or age_days < 90:
                continue

            if age_days <= 180:
                age_groups["90-180 days"].append(rec)
            elif age_days <= 365:
                age_groups["180-365 days"].append(rec)
            elif age_days <= 730:
                age_groups["1-2 years"].append(rec)
            else:
                age_groups["2+ years"].append(rec)

            # Calculate savings
            savings_str = rec.get("EstimatedSavings", "$0/month")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    # Remove currency, time unit, and any additional text like "(max estimate)"
                    clean_str = (
                        savings_str.replace(",", "").replace("$", "").replace("/month", "").split("(")[0].strip()
                    )
                    savings_val = float(clean_str)
                    total_savings += savings_val
                except (ValueError, AttributeError) as e:
                    logger.warning("Could not parse snapshot savings '%s': %s", savings_str, e)
                    # Continue without this savings amount

        # Use standard service header format
        content = '<div class="service-header">'
        content += '<h2 class="service-title">Snapshots Cost Optimization</h2>'
        content += '<div class="service-stats">'
        content += (
            f'<div class="stat-card"><div class="stat-label">Old Snapshots</div><div class="value">{snapshots_data["count"]}</div></div>'
        )
        content += f'<div class="stat-card"><div class="stat-label">Potential Monthly Savings</div><div class="value savings">${total_savings:.2f}</div></div>'
        content += "</div></div>"

        # Use standard recommendations section format
        content += '<div class="recommendation-section">'
        content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Optimization Recommendations</h3>'
        content += f'<div class="rec-summary"><strong>Total Recommendations:</strong> {snapshots_data["count"]} | '
        content += (
            f'<strong>Estimated Monthly Savings:</strong> <span class="savings">${total_savings:.2f}</span></div>'
        )

        content += '<div class="recommendation-list">'

        for age_range, snapshots in age_groups.items():
            if not snapshots:
                continue

            group_savings = 0
            total_size = 0
            for snap in snapshots:
                savings_str = snap.get("EstimatedSavings", "$0/month")
                if "$" in savings_str and "/month" in savings_str:
                    try:
                        # Remove currency, time unit, and any additional text like "(max estimate)"
                        clean_str = (
                            savings_str.replace(",", "").replace("$", "").replace("/month", "").split("(")[0].strip()
                        )
                        group_savings += float(clean_str)
                    except (ValueError, AttributeError) as e:
                        logger.warning("Could not parse snapshot savings '%s': %s", savings_str, e)
                total_size += snap.get("VolumeSize", 0)

            content += f'<div class="rec-item">'
            content += f"<h4>Snapshots aged {age_range} ({len(snapshots)} snapshots, {total_size} GB total)</h4>"
            content += (
                f"<p><strong>Recommendation:</strong> Review and delete old snapshots that are no longer needed</p>"
            )
            content += f'<p class="savings"><strong>Estimated Savings:</strong> ${group_savings:.2f}/month</p>'
            content += "<p><strong>Snapshots:</strong></p><ul>"

            for snap in snapshots:
                snapshot_id = snap.get("SnapshotId", "N/A")
                age_days = snap.get("AgeDays", 0)
                volume_size = snap.get("VolumeSize", 0)
                savings = snap.get("EstimatedSavings", "N/A")
                content += f"<li>{snapshot_id} - {age_days} days old, {volume_size} GB ({savings})</li>"

            content += "</ul></div>"

        content += "</div></div>"
        return content

    def _get_amis_content(self, amis_data: Dict[str, Any]) -> str:
        """Generate content for AMIs tab"""
        # Group AMIs by age range
        age_groups = {"90-180 days": [], "180-365 days": [], "1-2 years": [], "2+ years": []}

        # Dedupe by ImageId — the same AMI can surface from multiple sources
        # (EBS enhanced checks + the AMI service adapter). Prefer the entry with the
        # most specific EstimatedSavings string (longest non-empty form wins).
        seen: Dict[str, Dict[str, Any]] = {}
        for rec in amis_data["recommendations"]:
            image_id = rec.get("ImageId") or rec.get("ami_id") or ""
            if not image_id:
                seen[f"_unkeyed_{id(rec)}"] = rec  # unkeyed entries are preserved as-is
                continue
            existing = seen.get(image_id)
            if existing is None:
                seen[image_id] = rec
                continue
            new_sv = str(rec.get("EstimatedSavings") or "")
            old_sv = str(existing.get("EstimatedSavings") or "")
            if len(new_sv) > len(old_sv):
                seen[image_id] = rec

        deduped_recs = list(seen.values())

        for rec in deduped_recs:
            age_days = rec.get("AgeDays", 0)
            if age_days <= 180:
                age_groups["90-180 days"].append(rec)
            elif age_days <= 365:
                age_groups["180-365 days"].append(rec)
            elif age_days <= 730:
                age_groups["1-2 years"].append(rec)
            else:
                age_groups["2+ years"].append(rec)

        deduped_count = sum(len(g) for g in age_groups.values())

        # Use standard service header format
        content = '<div class="service-header">'
        content += '<h2 class="service-title">AMI Cost Optimization</h2>'
        content += '<div class="service-stats">'
        content += f'<div class="stat-card"><div class="stat-label">Old AMIs</div><div class="value">{deduped_count}</div></div>'
        content += "</div></div>"

        # Use standard recommendations section format
        content += '<div class="recommendation-section">'
        content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Optimization Recommendations</h3>'

        # Calculate total savings
        total_savings = sum(ami.get("EstimatedMonthlySavings", 0) for amis in age_groups.values() for ami in amis)

        content += f'<div class="rec-summary"><strong>Total Recommendations:</strong> {deduped_count} | '
        content += (
            f'<strong>Estimated Monthly Savings:</strong> <span class="savings">${total_savings:.2f}</span></div>'
        )

        content += '<div class="recommendation-list">'

        for age_range, amis in age_groups.items():
            if not amis:
                continue

            # Calculate savings for this age group
            group_savings = sum(ami.get("EstimatedMonthlySavings", 0) for ami in amis)

            content += f'<div class="rec-item">'
            content += f"<h4>AMIs aged {age_range} ({len(amis)} images)</h4>"
            content += f"<p><strong>Recommendation:</strong> Review and deregister unused AMIs to eliminate snapshot storage costs</p>"
            content += f'<p class="savings"><strong>Estimated Savings:</strong> ${group_savings:.2f}/month</p>'
            content += "<p><strong>AMIs:</strong></p><ul>"

            for ami in amis:
                ami_id = ami.get("ImageId", "N/A")
                ami_name = ami.get("Name", "Unnamed")
                age_days = ami.get("AgeDays", 0)
                ami_savings = ami.get("EstimatedSavings", "N/A")
                content += f"<li>{ami_id} - {ami_name} ({age_days} days old) - {ami_savings}</li>"

            content += "</ul></div>"

        content += "</div></div>"
        return content

    def _get_executive_summary_content(self) -> str:
        """Generate executive summary content with charts and key metrics"""
        services = self.scan_results["services"]
        summary = self.scan_results.get("summary", {})

        # Basic validation - check if we have recommendations
        total_recommendations = summary.get("total_recommendations", 0)
        if total_recommendations == 0:
            return """
            <div class="service-header">
                <h2 class="service-title"><svg class="icon icon-sm"><use href="#icon-chart"/></svg> Executive Summary</h2>
            </div>
            <div class="empty-state">
                <h3>No Cost Optimization Recommendations Found</h3>
                <p>Your AWS resources appear to be well-optimized. No immediate cost savings opportunities were identified.</p>
            </div>
            """

        # Stat-cards retired: the top-of-page summary sentence (_get_summary)
        # already carries Total Monthly Savings, top-services share, and risks.
        # Repeating them as cards here was the triple-stacked-summary pattern
        # PRODUCT.md asks us to leave behind. The tab opens directly into the
        # charts + legend so the FinOps audit drops straight into evidence.
        content = (
            '<div class="service-header">'
            '<h2 class="service-title">'
            '<svg class="icon icon-sm"><use href="#icon-chart"/></svg> Executive Summary'
            '</h2>'
            '</div>'
        )

        graviton_count = self._count_graviton_exclusions()
        if graviton_count > 0:
            content += f'<div class="info-note"><p><svg class="icon icon-sm"><use href="#icon-clipboard"/></svg> Note: {graviton_count} Graviton migration recommendations excluded from per-service detail. These require architecture-level review and are available via AWS Compute Optimizer.</p></div>'

        # Accessible data table fallback for the canvas charts — visually hidden
        # but read aloud by screen readers. PRODUCT.md commits to colorblind- and
        # SR-equivalent reads.
        chart_table_rows = ""
        chart_services = [
            (k, v.get("service_name", k.title()), self._calculate_service_savings(k, v))
            for k, v in services.items()
            if v.get("total_recommendations", 0) > 0
        ]
        chart_services.sort(key=lambda t: t[2], reverse=True)
        for _, name, sav in chart_services:
            chart_table_rows += f"<tr><td>{html.escape(name)}</td><td>${sav:,.2f}</td></tr>"

        content += f"""
        <div class="charts-container">
            <div class="chart-section">
                <h3 id="pie-chart-heading">Cost Savings Distribution by Service</h3>
                <div class="chart-wrapper">
                    <canvas id="savingsPieChart" width="400" height="400" role="img" aria-labelledby="pie-chart-heading" aria-describedby="chart-data-table"></canvas>
                    <noscript><p class="chart-fallback">Enable JavaScript to view the cost savings pie chart.</p></noscript>
                </div>
            </div>
            <div class="chart-section">
                <h3 id="bar-chart-heading">Top Services by Savings Potential</h3>
                <div class="chart-wrapper">
                    <canvas id="savingsBarChart" width="400" height="400" role="img" aria-labelledby="bar-chart-heading" aria-describedby="chart-data-table"></canvas>
                    <noscript><p class="chart-fallback">Enable JavaScript to view the savings bar chart.</p></noscript>
                </div>
            </div>
        </div>
        <table id="chart-data-table" class="visually-hidden" aria-label="Chart data: monthly savings by service">
            <caption>Monthly savings by service (text equivalent of the charts above).</caption>
            <thead><tr><th scope="col">Service</th><th scope="col">Monthly savings</th></tr></thead>
            <tbody>{chart_table_rows}</tbody>
        </table>
        """

        content += self._get_trends_section()
        content += self._get_glossary_section()

        return content

    def _get_glossary_section(self) -> str:
        """Render the source-confidence glossary.

        Surfaces the meaning of the four source badges so adversarial readers
        can immediately judge how much evidence backs each recommendation.
        """
        return (
            '<section class="glossary" aria-labelledby="glossary-title">'
            '<h3 id="glossary-title">Source-confidence legend</h3>'
            "<dl>"
            '<dt><span class="badge badge-success">Metric Backed</span></dt>'
            "<dd>Recommendation derived from CloudWatch metrics observed over the lookback window (typically 14&ndash;30 days). Highest confidence; defendable in review.</dd>"
            '<dt><span class="badge badge-info">ML Backed</span></dt>'
            "<dd>AWS Compute Optimizer or another AWS ML service produced the rightsizing call from utilization patterns. High confidence; subject to AWS&rsquo;s own confidence rating.</dd>"
            '<dt><span class="badge badge-warning">Cost Hub</span></dt>'
            "<dd>AWS Cost Optimization Hub recommendation. Savings already deduplicated against per-service recommendations &mdash; see headline reconciliation footnote.</dd>"
            '<dt><span class="badge badge-danger">Audit Based</span></dt>'
            "<dd>Configuration audit only (no CloudWatch metric backing). Verify operational fit before acting &mdash; the recommendation is structurally correct but lacks runtime evidence.</dd>"
            "</dl>"
            "</section>"
        )

    def _get_trends_section(self) -> str:
        trend = self.scan_results.get("trend_analysis")
        if not trend:
            return ""

        total_spend = trend.get("total_spend", 0)
        spend_change_pct = trend.get("spend_change_pct", 0)
        forecast = trend.get("forecast")
        fastest_growing = trend.get("fastest_growing", [])
        daily_spend_series = trend.get("daily_spend_series", [])
        days_back = trend.get("days_back", 90)

        stat_cards = (
            '<div class="stat-card"><div class="stat-label">90-Day Total Spend</div>'
            f'<div class="value">${total_spend:,.2f}</div></div>'
            '<div class="stat-card"><div class="stat-label">Spend Change</div>'
            f'<div class="value">{spend_change_pct:+.1f}%</div></div>'
        )
        if forecast and isinstance(forecast, (int, float)):
            stat_cards += (
                f'<div class="stat-card"><div class="stat-label">30-Day Forecast</div><div class="value">${forecast:,.2f}</div></div>'
            )

        chart_html = ""
        if daily_spend_series:
            dates = [d["date"] for d in daily_spend_series]
            amounts = [d["amount"] for d in daily_spend_series]
            dates_js = json.dumps(dates)
            amounts_js = json.dumps(amounts)
            chart_html = (
                '<div class="charts-container"><div class="chart-section">'
                f'<div class="chart-wrapper"><canvas id="spendTrendChart" width="800" height="300" role="img" aria-label="Spend trend line chart"></canvas><noscript><p class="chart-fallback">Enable JavaScript to view the spend trend chart.</p></noscript></div>'
                "</div></div>"
                "<script>(function(){"
                'var canvas=document.getElementById("spendTrendChart");'
                "if(!canvas)return;"
                'var ctx=canvas.getContext("2d");'
                'var isDark=document.documentElement.getAttribute("data-theme")==="dark";'
                'var textColor=isDark?"#e0e0e0":"#333333";'
                'var gridColor=isDark?"rgba(255,255,255,0.1)":"rgba(0,0,0,0.1)";'
                f"var dates={dates_js};"
                f"var amounts={amounts_js};"
                "new Chart(ctx,{"
                'type:"line",'
                "data:{labels:dates,datasets:[{"
                'label:"Daily Spend ($)",'
                "data:amounts,"
                'borderColor:"#42a5f5",'
                'backgroundColor:"rgba(66,165,245,0.1)",'
                "fill:true,"
                "tension:0.3,"
                "pointRadius:1,"
                "pointHoverRadius:4"
                "}]},"
                "options:{responsive:true,maintainAspectRatio:false,"
                "plugins:{legend:{labels:{color:textColor}},"
                'tooltip:{callbacks:{label:function(c){return "$"+c.parsed.y.toFixed(2);}}}}},'
                "scales:{"
                'y:{beginAtZero:false,ticks:{color:textColor,callback:function(v){return "$"+v.toFixed(0);}},grid:{color:gridColor}},'
                "x:{ticks:{color:textColor,maxTicksLimit:12,maxRotation:45},grid:{color:gridColor}}"
                "}"
                "}});})();</script>"
            )

        table_html = ""
        if fastest_growing:
            table_rows = ""
            for svc in fastest_growing[:5]:
                name = html.escape(svc.get("service", "Unknown"))
                change = svc.get("change_pct", 0)
                table_rows += f"<tr><td>{name}</td><td>{change:+.1f}%</td></tr>"
            table_html = (
                '<div class="table-responsive"><table class="data-table">'
                "<thead><tr><th>Service</th><th>Growth</th></tr></thead>"
                f"<tbody>{table_rows}</tbody></table></div>"
            )

        return (
            '<div class="chart-section">'
            f'<h3><svg class="icon icon-sm"><use href="#icon-trending"/></svg> {days_back}-Day Spend Trends</h3>'
            f'<div class="service-stats">{stat_cards}</div>'
            f"{chart_html}"
            f"{table_html}"
            "</div>"
        )

    def _filter_recommendations(self, service_data: Dict[str, Any]) -> Dict[str, Any]:
        """Filter out non-relevant recommendations like MigrateToGraviton"""
        filtered_data = copy.deepcopy(service_data)

        if "sources" in filtered_data:
            for source_name, source_data in filtered_data["sources"].items():
                if "recommendations" in source_data:
                    # Filter out MigrateToGraviton recommendations
                    original_recs = source_data["recommendations"]
                    filtered_recs = [
                        rec
                        for rec in original_recs
                        if isinstance(rec, dict) and rec.get("actionType") != "MigrateToGraviton"
                    ]
                    filtered_recs = [
                        rec for rec in filtered_recs if isinstance(rec, dict) and rec.get("finding") != "OPTIMIZED"
                    ]

                    # Update counts and savings
                    filtered_data["sources"][source_name]["recommendations"] = filtered_recs
                    filtered_data["sources"][source_name]["count"] = len(filtered_recs)

                    # Recalculate total recommendations and savings
                    if source_name == "cost_optimization_hub":
                        graviton_savings = sum(
                            rec.get("estimatedMonthlySavings", 0)
                            for rec in original_recs
                            if isinstance(rec, dict) and rec.get("actionType") == "MigrateToGraviton"
                        )
                        filtered_data["total_monthly_savings"] = max(
                            0, filtered_data.get("total_monthly_savings", 0) - graviton_savings
                        )

        # Recalculate total recommendations - only count sources with actual recommendations
        total_recs = 0
        for source in filtered_data.get("sources", {}).values():
            if isinstance(source, dict):
                # Only count if there are actual recommendations (not just a count placeholder)
                recs = source.get("recommendations", [])
                if recs and len(recs) > 0:
                    total_recs += len(recs)
                elif source.get("count", 0) > 0:
                    # Fallback to count if recommendations list is empty but count exists
                    total_recs += source.get("count", 0)
            elif isinstance(source, list):
                # New format: direct list of recommendations
                total_recs += len(source)
        filtered_data["total_recommendations"] = total_recs

        # For services without direct savings (like Compute Optimizer), preserve calculated savings
        if service_data.get("service_name") == "EC2" and all(
            "actionType" not in rec
            for source in filtered_data.get("sources", {}).values()
            for rec in (source.get("recommendations", []) if isinstance(source, dict) else source)
        ):
            # Only set to 0 if no calculated savings were provided
            if filtered_data.get("total_monthly_savings", 0) == 0:
                filtered_data["total_monthly_savings"] = 0

        return filtered_data

    def _count_graviton_exclusions(self) -> int:
        count = 0
        for service_data in self.scan_results.get("services", {}).values():
            for source_data in service_data.get("sources", {}).values():
                if isinstance(source_data, dict):
                    for rec in source_data.get("recommendations", []):
                        if isinstance(rec, dict) and rec.get("actionType") == "MigrateToGraviton":
                            count += 1
        return count

    def _get_affected_resources_list(self, service_key: str, service_data: Dict[str, Any]) -> str:
        """Get list of affected resources for each recommendation type"""
        content = ""
        sources = service_data.get("sources", {})

        resource_groups: Dict[str, list] = {}
        extractor = _RESOURCE_EXTRACTORS.get(service_key)

        for source_name, source_data in sources.items():
            if isinstance(source_data, dict):
                recommendations = source_data.get("recommendations", [])
            elif isinstance(source_data, list):
                recommendations = source_data
            else:
                recommendations = []

            for rec in recommendations:
                if extractor:
                    if service_key == "ebs":
                        extractor(rec, source_name, resource_groups)
                    else:
                        extractor(rec, resource_groups)

        if resource_groups:
            content += '<div class="affected-resources">'
            content += "<h4>Affected Resources by Recommendation Type:</h4>"

            for group_name, resources in resource_groups.items():
                if resources:
                    total_savings = sum(r["savings"] for r in resources)
                    content += f'<div class="resource-group">'
                    content += f"<h4>{group_name} ({len(resources)} resources)</h4>"
                    if total_savings > 0:
                        content += f'<p class="group-savings">Potential Monthly Savings: ${total_savings:.2f}</p>'

                    content += '<ul class="resource-list">'
                    for resource in resources:
                        content += f"<li><strong>{resource['id']}</strong> ({resource['type']})"
                        if resource["savings"] > 0:
                            content += f" - ${resource['savings']:.2f}/month"
                        content += "</li>"

                    content += "</ul></div>"

            content += "</div>"

        return content

    def _calculate_service_savings(self, service_key: str, service_data: Dict[str, Any]) -> float:
        """Calculate realistic savings for services showing $0.00"""
        if service_data.get("total_monthly_savings", 0) > 0:
            return service_data["total_monthly_savings"]

        total_savings = 0
        sources = service_data.get("sources", {})

        for source_name, source_data in sources.items():
            if isinstance(source_data, dict):
                recommendations = source_data.get("recommendations", [])
            elif isinstance(source_data, list):
                recommendations = source_data
            else:
                recommendations = []

            for rec in recommendations:
                if service_key in _FLAT_SAVINGS_SERVICES:
                    total_savings += 50
                elif service_key in _SAVINGS_KEYWORDS:
                    recommendation = rec.get("Recommendation", "").lower()
                    matched = False
                    for keyword, amount in _SAVINGS_KEYWORDS[service_key]:
                        if keyword in recommendation:
                            total_savings += amount
                            matched = True
                            break
                    if not matched:
                        if service_key in _SAVINGS_FALLBACK_TO_ESTIMATED and rec.get("estimatedMonthlySavings", 0) > 0:
                            total_savings += rec.get("estimatedMonthlySavings", 0)
                        else:
                            total_savings += _DEFAULT_SAVINGS.get(service_key, 0)

        return total_savings

    def _get_service_content(self, service_key: str, service_data: Dict[str, Any]) -> str:
        """Get content for a specific service tab"""
        canonical_savings = service_data.get("total_monthly_savings", 0)
        calculated_savings = self._calculate_service_savings(service_key, service_data)
        is_estimated = calculated_savings > 0 and (canonical_savings == 0 or calculated_savings != canonical_savings)

        if calculated_savings > 0:
            service_data = service_data.copy()
            service_data["total_monthly_savings"] = calculated_savings

        # Savings Plans removed - will be generated from another source

        # Special handler for AMI - use grouped format
        if service_key == "ami":
            sources = service_data.get("sources", {})
            all_amis = []
            for source_data in sources.values():
                if isinstance(source_data, dict):
                    all_amis.extend(source_data.get("recommendations", []))
                elif isinstance(source_data, list):
                    all_amis.extend(source_data)
            return self._get_amis_content({"count": len(all_amis), "recommendations": all_amis})

        # Filter out non-relevant recommendations
        filtered_service_data = self._filter_recommendations(service_data)

        content = f'<div class="service-header">'
        service_name_raw = str(filtered_service_data["service_name"])
        if any(token in service_name_raw.lower() for token in ("cost", "optimizer", "optimization", "anomaly", "visibility", "hub")):
            service_title = html.escape(service_name_raw)
        else:
            service_title = f"{html.escape(service_name_raw)} Cost Optimization"
        content += f'<h2 class="service-title">{service_title}</h2>'
        content += self._get_service_stats(service_key, filtered_service_data)
        content += "</div>"

        # Affected resources list (skip for services with full grouping to avoid duplication)
        if service_key not in [
            "ebs",
            "ec2",
            "rds",
            "s3",
            "dynamodb",
            "containers",
            "elasticache",
            "opensearch",
            "file_systems",
            "network",
            "monitoring",
            "additional_services",
            "lambda",
            "cloudfront",
            "api_gateway",
            "step_functions",
            "auto_scaling",
            "backup",
            "route53",
            "ami",
            "lightsail",
            "dms",
            "glue",
            "redshift",
        ]:
            content += self._get_affected_resources_list(service_key, filtered_service_data)

        # Recommendations section
        content += '<div class="recommendation-section">'
        content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Optimization Recommendations</h3>'
        savings_value = filtered_service_data.get("total_monthly_savings", 0.0)
        savings_label = "Estimated Monthly Savings" if is_estimated else "Monthly Savings"
        content += f'<div class="rec-summary"><strong>Total Recommendations:</strong> {filtered_service_data["total_recommendations"]} | '
        if savings_value > 0:
            content += f'<strong>{savings_label}:</strong> <span class="savings">${savings_value:.2f}</span></div>'
        else:
            content += f'<strong>{savings_label}:</strong> <span class="savings">$0.00</span></div>'
            content += '<div class="info-box"><p><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> These recommendations focus on best practices and cost avoidance — no direct monetary savings were calculated. Review each recommendation for potential operational improvements.</p></div>'

        # Optimization opportunities - only include if corresponding source has actual recommendations
        descs = filtered_service_data.get("optimization_descriptions")
        sources = filtered_service_data.get("sources", {})
        if descs:
            content += '<div class="opportunities">'
            for desc_key, desc in list(descs.items())[:5]:
                title = desc.get("title", "")
                description = desc.get("description", "")
                # Only show opportunity if there's corresponding source with actual recommendations
                # Map optimization desc keys to source keys
                source_key_map = {
                    "compute_optimizer": "compute_optimizer",
                    "compute_optimizer_ebs": "compute_optimizer",
                    "unattached_volumes": "unattached_volumes",
                    "gp2_migration": "gp2_migration",
                    "gp3_migration": "gp2_migration",
                    "old_snapshots": "old_snapshots",
                    "enhanced_ebs": "enhanced_ebs",
                }
                source_key = source_key_map.get(desc_key, desc_key)
                source_data = sources.get(source_key, {})
                recs = source_data.get("recommendations", []) if isinstance(source_data, dict) else []
                # Only render if source has actual recommendations
                if not recs or len(recs) == 0:
                    continue
                if not title or not description:
                    continue
                content += f"""<div class="opportunity">
                    <h4>{title}</h4>
                    <p>{description}</p>
                </div>"""
            content += "</div>"

        # Detailed recommendations
        content += self._get_detailed_recommendations(service_key, filtered_service_data)
        content += "</div>"

        return content

    def _get_service_stats(self, service_key: str, service_data: Dict[str, Any]) -> str:
        """Get service-specific statistics"""
        config = _SERVICE_STATS_CONFIG.get(service_key)
        if not config:
            return ""

        stats_html = '<div class="service-stats">'

        if "direct_key" in config:
            for label, _field in config["cards"]:
                value = service_data.get(config["direct_key"], 0)
                stats_html += f'<div class="stat-card"><div class="stat-label">{label}</div><div class="value">{value}</div></div>'
        elif "multi_source_cards" in config:
            for label, sub_key, field in config["multi_source_cards"]:
                sub_dict = service_data.get(sub_key, {})
                stats_html += (
                    f'<div class="stat-card"><div class="stat-label">{label}</div><div class="value">{sub_dict.get(field, 0)}</div></div>'
                )
        else:
            counts = service_data.get(config.get("count_key", ""), {})
            for label, field in config["cards"]:
                stats_html += (
                    f'<div class="stat-card"><div class="stat-label">{label}</div><div class="value">{counts.get(field, 0)}</div></div>'
                )

        enrichment = _STATS_ENRICHMENTS.get(service_key)
        if enrichment:
            stats_html = enrichment(stats_html, service_data, config)

        stats_html += "</div>"
        return stats_html

    def _get_detailed_recommendations(self, service_key: str, service_data: Dict[str, Any]) -> str:
        """
        Generate detailed recommendations HTML for a specific AWS service.

        This method is the core of the smart grouping system that organizes
        recommendations by category for better readability and actionability.

        Smart Grouping Strategy:
        - Groups similar recommendations together by category
        - Deduplicates findings across multiple data sources
        - Applies consistent formatting and styling
        - Calculates aggregated savings for grouped recommendations
        - Provides clear, actionable recommendations for each group

        Services with Full Grouping (11 services):
        - EC2, EBS, RDS, S3, DynamoDB, Containers
        - ElastiCache, OpenSearch, File Systems, Network, Monitoring

        Args:
            service_key (str): AWS service identifier (e.g., 'ec2', 'rds', 'elasticache')
            service_data (Dict[str, Any]): Service-specific scan results and recommendations

        Returns:
            str: HTML content with grouped recommendations and consistent styling

        Note:
            - Handles deduplication across multiple data sources
            - Applies service-specific grouping logic
            - Maintains consistent UI patterns across all services
            - Automatically hides empty groups
        """
        sources = service_data.get("sources", {})

        from reporter_phase_a import PHASE_A_DESCRIPTORS, render_grouped_by_category, render_file_systems
        from reporter_phase_b import (
            PHASE_B_HANDLERS,
            render_generic_per_rec,
            render_s3_top_tables,
            should_skip_section_header,
            should_skip_source_loop,
            should_use_handler,
            should_fallback_to_per_rec,
            source_type_label,
        )

        if service_key == "file_systems":
            return render_file_systems(sources)

        if service_key in PHASE_A_DESCRIPTORS:
            return render_grouped_by_category(service_key, sources, PHASE_A_DESCRIPTORS[service_key])

        content = '<div class="recommendation-list">'

        for source_name, source_data in sources.items():
            if isinstance(source_data, dict):
                count = source_data.get("count", 0)
                recommendations = source_data.get("recommendations", [])
            elif isinstance(source_data, list):
                count = len(source_data)
                recommendations = source_data
            else:
                count = 0
                recommendations = []

            if count > 0:
                rec_filter = _RECOMMENDATION_FILTERS.get(service_key)
                if rec_filter:
                    recommendations = rec_filter(recommendations)

                total_count = len(recommendations)
                if total_count == 0:
                    continue

                if should_use_handler(service_key, source_name):
                    handler = PHASE_B_HANDLERS[(service_key, source_name)]
                    handler_output = handler(recommendations, source_name, service_data)
                    if not handler_output:
                        # Handler filtered out all recs (e.g., all findings are "Optimized")
                        continue
                    label = source_type_label(service_key, source_name)
                    # Wrap the handler output in a source-section. CSS renders a
                    # typographic prefix on every nested rec-item h4 via the
                    # data-source attribute. The chip-style badge retires; the
                    # audit chain now lives in the rec-item title's typesetting.
                    open_wrap = (
                        f'<section class="source-section" data-source="{html.escape(label)}">'
                        if label
                        else ""
                    )
                    close_wrap = "</section>" if label else ""
                    content += open_wrap
                    if not should_skip_section_header(service_key):
                        content += f"<h4>{source_name.replace('_', ' ').title()}: {total_count} items</h4>"
                    content += handler_output
                    content += close_wrap
                    continue

                if should_fallback_to_per_rec(service_key):
                    content += render_generic_per_rec(service_key, recommendations, source_name)

        if service_key == "s3":
            content += render_s3_top_tables(service_data)

        content += "</div>"
        return content

    def _get_footer(self) -> str:
        """Get footer section.

        Embeds the raw scan-result JSON as a data: URL so the page is self-contained:
        readers can re-open the snapshot in any JSON viewer without needing the
        originating filesystem.
        """
        # Compact JSON keeps the data URL small; whitespace is not load-bearing in
        # downstream tools. AWS Cost Optimization Hub responses carry datetime
        # objects (lastRefreshTimestamp et al.); default-stringify them so the
        # encoder cannot crash on a single response shape change.
        def _serialize_aws_objects(obj: Any) -> Any:
            if isinstance(obj, datetime):
                return obj.isoformat()
            if hasattr(obj, "isoformat"):
                # Covers date, time, and any future tzaware shape.
                return obj.isoformat()
            return str(obj)

        try:
            json_blob = json.dumps(
                self.scan_results,
                separators=(",", ":"),
                default=_serialize_aws_objects,
            )
        except (TypeError, ValueError) as exc:
            # The download link is a nice-to-have; if serialization fails for
            # any reason, log it and ship the report without the link rather
            # than crashing the entire report generation.
            logger.warning("Scan-results JSON serialization failed; omitting download link: %s", exc)
            json_blob = None
        encoded = base64.b64encode(json_blob.encode("utf-8")).decode("ascii") if json_blob else ""
        region = html.escape(str(self.scan_results.get("region", "report")))
        scan_time = str(self.scan_results.get("scan_time", ""))[:10].replace(":", "-")
        filename = f"aws-cost-scan-{region}-{scan_time}.json" if scan_time else f"aws-cost-scan-{region}.json"
        download_link = (
            f'<p><a class="footer-link" href="data:application/json;base64,{encoded}" '
            f'download="{filename}">Download raw scan results (JSON)</a></p>'
            if encoded
            else ""
        )
        return f"""<div class="footer">
            <p>Generated by AWS Cost Optimization Scanner on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
            <p>Report covers {self.scan_results["summary"]["total_services_scanned"]} AWS services with {self.scan_results["summary"]["total_recommendations"]} optimization recommendations</p>
            {download_link}
        </div>"""

    def _get_savings_plans_content(self, sp_data: Dict[str, Any]) -> str:
        """Generate Savings Plans analysis content"""
        summary = sp_data.get("summary", {})
        active_plans = sp_data.get("active_plans", [])
        utilization = sp_data.get("utilization_analysis", {})
        coverage = sp_data.get("coverage_analysis", {})
        recommendations = sp_data.get("recommendations", [])
        uncovered_families = sp_data.get("uncovered_families", [])

        content = '<div class="service-header">'
        content += '<h2 class="service-title">Savings Plans Cost Optimization</h2>'
        content += '<div class="service-stats">'
        content += f'<div class="stat-card"><div class="stat-label">Active Plans</div><div class="value">{summary.get("total_active_plans", 0)}</div></div>'
        content += f'<div class="stat-card"><div class="stat-label">Total Commitment</div><div class="value">${summary.get("total_commitment", 0):.2f}/hr</div></div>'

        if utilization:
            util_pct = utilization.get("utilization_percentage", 0)
            util_status = utilization.get("status", "Unknown")
            status_class = (
                "success" if util_status == "Good" else "warning" if util_status == "Needs Attention" else "danger"
            )
            content += f'<div class="stat-card"><div class="stat-label">Utilization</div><div class="value {status_class}">{util_pct:.1f}%</div></div>'

        if coverage:
            cov_pct = coverage.get("coverage_percentage", 0)
            cov_status = coverage.get("status", "Unknown")
            status_class = "success" if cov_status == "Good" else "warning" if cov_status == "Moderate" else "danger"
            content += (
                f'<div class="stat-card"><div class="stat-label">Coverage</div><div class="value {status_class}">{cov_pct:.1f}%</div></div>'
            )

        content += "</div></div>"

        # Active Plans Section
        if active_plans:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Active Savings Plans</h3>'
            for plan in active_plans:
                plan_type = plan.get("savingsPlanType", "Unknown")
                commitment = plan.get("commitment", 0)
                region = plan.get("region", "N/A")
                family = plan.get("ec2InstanceFamily", "N/A")
                payment = plan.get("paymentOption", "N/A")

                content += '<div class="rec-item">'
                content += f"<h4>{plan_type} Savings Plan</h4>"
                content += f"<p><strong>Commitment:</strong> ${commitment:.2f}/hour</p>"
                if plan_type == "EC2Instance":
                    content += f"<p><strong>Instance Family:</strong> {family} | <strong>Region:</strong> {region}</p>"
                content += f"<p><strong>Payment Option:</strong> {payment}</p>"
                content += f"<p><strong>Term:</strong> {plan.get('start', 'N/A')} to {plan.get('end', 'N/A')}</p>"
                content += "</div>"
            content += "</div>"

        # Utilization Analysis
        if utilization:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Utilization Analysis</h3>'
            content += '<div class="rec-item">'
            content += f"<h4>Overall Utilization: {utilization.get('utilization_percentage', 0):.1f}% ({utilization.get('status', 'Unknown')})</h4>"
            content += f"<p><strong>Total Commitment:</strong> ${utilization.get('total_commitment', '0')}</p>"
            content += f"<p><strong>Used Commitment:</strong> ${utilization.get('used_commitment', '0')}</p>"
            content += f'<p><strong>Unused Commitment:</strong> <span class="danger">${utilization.get("unused_commitment", "0")}</span></p>'
            content += "</div></div>"

        # Coverage Analysis
        if coverage:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Coverage Analysis</h3>'
            content += '<div class="rec-item">'
            content += f"<h4>Coverage: {coverage.get('coverage_percentage', 0):.1f}% ({coverage.get('status', 'Unknown')})</h4>"
            content += f"<p><strong>On-Demand Cost:</strong> ${coverage.get('on_demand_cost', 0):.2f}</p>"
            content += f"<p><strong>Spend Covered by Savings Plans:</strong> ${coverage.get('spend_covered', '0')}</p>"
            content += f"<p><strong>Total Cost:</strong> ${coverage.get('total_cost', '0')}</p>"
            content += "</div></div>"

        # Recommendations
        if recommendations:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Optimization Recommendations</h3>'
            for rec in recommendations:
                severity = rec.get("severity", "Medium")
                badge_class = "danger" if severity == "High" else "warning" if severity == "Medium" else "success"

                content += f'<div class="rec-item">'
                content += f'<h4>{rec.get("type", "Recommendation")} <span class="badge badge-{badge_class}">{severity}</span></h4>'
                content += f"<p><strong>Finding:</strong> {rec.get('finding', 'N/A')}</p>"
                content += f"<p><strong>Recommendation:</strong> {rec.get('recommendation', 'N/A')}</p>"
                if "potential_monthly_savings" in rec:
                    content += (
                        f'<p class="savings"><strong>Potential Savings:</strong> {rec["potential_monthly_savings"]}</p>'
                    )
                if "potential_waste" in rec:
                    content += f'<p class="danger"><strong>Potential Waste:</strong> ${rec["potential_waste"]}</p>'
                content += "</div>"
            content += "</div>"

        # Uncovered Instance Families
        if uncovered_families:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Uncovered Instance Families</h3>'
            content += "<p>The following instance families are not covered by EC2 Instance Savings Plans:</p>"
            for family_info in uncovered_families:
                content += '<div class="rec-item">'
                content += f"<h4>{family_info.get('family', 'Unknown')} Family</h4>"
                content += f"<p><strong>Instance Count:</strong> {family_info.get('instance_count', 0)}</p>"
                content += f"<p><strong>Instance Types:</strong> {', '.join(family_info.get('instance_types', []))}</p>"
                content += f"<p><strong>Recommendation:</strong> {family_info.get('recommendation', 'N/A')}</p>"
                content += f'<p class="savings"><strong>Estimated Savings:</strong> {family_info.get("estimated_savings", "N/A")}</p>'
                content += "</div>"
            content += "</div>"

        # Cost Optimization Hub Purchase Recommendations
        cost_hub_recs = sp_data.get("cost_hub_purchase_recommendations", [])
        if cost_hub_recs:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title"><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Cost Optimization Hub - Purchase Recommendations</h3>'
            content += "<p>AWS Cost Optimization Hub has identified opportunities to purchase Savings Plans:</p>"
            for rec in cost_hub_recs:
                severity = rec.get("severity", "Medium")
                badge_class = "danger" if severity == "High" else "warning" if severity == "Medium" else "success"

                content += f'<div class="rec-item">'
                content += f'<h4>{rec.get("type", "Recommendation")} <span class="badge badge-{badge_class}">{severity}</span></h4>'
                content += f"<p><strong>Recommendation:</strong> {rec.get('recommendation', 'N/A')}</p>"
                content += f'<p class="savings"><strong>Potential Monthly Savings:</strong> {rec.get("potential_monthly_savings", "N/A")} ({rec.get("savings_percentage", "N/A")})</p>'
                content += f"<p><strong>Implementation Effort:</strong> {rec.get('implementation_effort', 'N/A')}</p>"
                content += f'<p class="source-info"><strong>Source:</strong> {rec.get("source", "N/A")}</p>'
                content += "</div>"
            content += "</div>"

        # No Savings Plans message
        if not summary.get("has_savings_plans"):
            content += '<div class="info-box">'
            content += '<h3 class="section-title">No Active Savings Plans Found</h3>'
            content += "<p>Consider purchasing Savings Plans to save up to 72% on your compute usage:</p>"
            content += "<ul>"
            content += "<li><strong>Compute Savings Plans:</strong> Most flexible, up to 66% savings on EC2, Fargate, and Lambda</li>"
            content += "<li><strong>EC2 Instance Savings Plans:</strong> Highest savings (up to 72%) for specific instance families</li>"
            content += "</ul>"

            # Show Cost Hub recommendations even without active plans
            if cost_hub_recs:
                content += '<p class="callout-margin"><strong><svg class="icon icon-sm"><use href="#icon-lightbulb"/></svg> Cost Optimization Hub has identified purchase opportunities above.</strong></p>'

            content += "</div>"

        return content

    def _get_javascript(self) -> str:
        """Get JavaScript for interactivity"""
        # Extract chart data from scan results
        services = self.scan_results["services"]
        chart_data = []

        for service_key, service_data in services.items():
            if service_data.get("total_recommendations", 0) > 0:
                chart_data.append(
                    {
                        "service": service_data.get("service_name", service_key.title()),
                        "service_key": service_key,
                        "savings": self._calculate_service_savings(service_key, service_data),
                        "recommendations": service_data.get("total_recommendations", 0),
                    }
                )

        # Sort by savings for better visualization
        chart_data.sort(key=lambda x: x["savings"], reverse=True)

        return f"""<script>
        let currentFilter = null;
        const chartData = {json.dumps(chart_data)};
        
        function formatCurrency(amount) {{
            return '$' + amount.toFixed(2).replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, ',');
        }}
        
        // Dark Mode Functions
        function toggleTheme() {{
            const html = document.documentElement;
            const currentTheme = html.getAttribute('data-theme');
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            
            html.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            document.querySelector('.theme-toggle').setAttribute('aria-pressed', newTheme === 'dark' ? 'true' : 'false');
            updateThemeToggle(newTheme);
            
            // Reinitialize charts with new colors
            setTimeout(() => {{
                initializeCharts();
            }}, 100);
        }}

        function updateThemeToggle(theme) {{
            const icon = document.getElementById('theme-icon-svg');
            const text = document.getElementById('theme-text');
            
            if (theme === 'dark') {{
                icon.setAttributeNS('http://www.w3.org/1999/xlink', 'href', '#icon-sun');
                text.textContent = 'Light';
            }} else {{
                icon.setAttributeNS('http://www.w3.org/1999/xlink', 'href', '#icon-moon');
                text.textContent = 'Dark';
            }}
        }}

        function initializeTheme() {{
            const savedTheme = localStorage.getItem('theme') || 'light';
            document.documentElement.setAttribute('data-theme', savedTheme);
            updateThemeToggle(savedTheme);
        }}
        
        function showTab(tabId, evt) {{
            const contents = document.querySelectorAll('.tab-content');
            contents.forEach(content => content.classList.remove('active'));

            const buttons = document.querySelectorAll('.tab-button');
            buttons.forEach(button => {{
                button.classList.remove('active');
                button.setAttribute('aria-selected', 'false');
                button.setAttribute('tabindex', '-1');
            }});

            const panel = document.getElementById('panel-' + tabId);
            if (panel) panel.classList.add('active');

            const tabButton = document.getElementById('tab-' + tabId);
            if (tabButton) {{
                tabButton.classList.add('active');
                tabButton.setAttribute('aria-selected', 'true');
                tabButton.setAttribute('tabindex', '0');
                if (evt && evt.type !== 'load') tabButton.focus();
            }}

            // Clear filter when switching to executive summary
            if (tabId === 'executive-summary') {{
                currentFilter = null;
                updateFilterIndicators();
            }}
        }}

        // Jump-nav rail handler: activates the target tab (via showTab) and
        // smooth-scrolls the panel into view. Used by the sticky savings rail
        // and by any anchor-style link into a service panel.
        function jumpToPanel(tabId) {{
            showTab(tabId, {{ type: 'click' }});
            const panel = document.getElementById('panel-' + tabId);
            if (panel) panel.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
        }}

        // WAI-ARIA tab keyboard pattern: roving tabindex + Left/Right/Home/End.
        function handleTabKey(evt) {{
            const target = evt.currentTarget;
            const tablist = target.closest('[role="tablist"]');
            if (!tablist) return;
            const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
            const idx = tabs.indexOf(target);
            if (idx < 0) return;

            let next = -1;
            switch (evt.key) {{
                case 'ArrowRight': next = (idx + 1) % tabs.length; break;
                case 'ArrowLeft': next = (idx - 1 + tabs.length) % tabs.length; break;
                case 'Home': next = 0; break;
                case 'End': next = tabs.length - 1; break;
                default: return;
            }}
            evt.preventDefault();
            const nextTab = tabs[next];
            const nextId = nextTab.id.replace(/^tab-/, '');
            showTab(nextId, evt);
        }}
        
        function filterByService(serviceKey) {{
            currentFilter = serviceKey;
            updateFilterIndicators();
            
            showTab(serviceKey);
            
            const btns = document.querySelectorAll('.tab-button');
            btns.forEach(b => {{
                if (b.getAttribute('onclick') && b.getAttribute('onclick').includes(serviceKey)) {{
                    b.classList.add('active');
                }}
            }});
        }}
        
        function updateFilterIndicators() {{
            const buttons = document.querySelectorAll('.tab-button');
            buttons.forEach(button => {{
                if (currentFilter && button.onclick.toString().includes(currentFilter)) {{
                    button.style.background = 'var(--primary-light)';
                }} else {{
                    button.style.background = '';
                }}
            }});
        }}
        
        let pieChart = null;
        let barChart = null;
        
        function initializeCharts() {{
            if (chartData.length === 0) return;
            try {{
            const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
            const borderColor = isDark ? '#1e1e1e' : '#fff';
            const textColor = isDark ? '#ffffff' : '#212121';
            const gridColor = isDark ? '#333333' : '#e0e0e0';
            
            Chart.defaults.font.family = "'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif";
            
            // AWS Theme Colors
            const awsColors = Array.from({{length: Math.max(chartData.length, 10)}}, (_, i) => 
                `hsl(${{(i * 360 / Math.max(chartData.length, 10)) % 360}}, 65%, 55%)`
            );
            
            // Destroy existing charts
            if (pieChart) {{
                pieChart.destroy();
                pieChart = null;
            }}
            if (barChart) {{
                barChart.destroy();
                barChart = null;
            }}
            
            // Pie Chart
            const pieCtx = document.getElementById('savingsPieChart');
            if (pieCtx) {{
                pieChart = new Chart(pieCtx, {{
                    type: 'pie',
                    data: {{
                        labels: chartData.map(d => d.service),
                        datasets: [{{
                            data: chartData.map(d => d.savings),
                            backgroundColor: awsColors.slice(0, chartData.length),
                            borderWidth: 2,
                            borderColor: borderColor
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{
                            legend: {{
                                position: 'bottom',
                                labels: {{
                                    padding: 20,
                                    usePointStyle: true,
                                    color: textColor
                                }}
                            }},
                            tooltip: {{
                                titleFont: {{ family: "'IBM Plex Sans', sans-serif" }},
                                bodyFont: {{ family: "'IBM Plex Sans', sans-serif" }},
                                titleColor: textColor,
                                bodyColor: textColor,
                                backgroundColor: isDark ? '#333333' : '#ffffff',
                                borderColor: isDark ? '#555555' : '#cccccc',
                                borderWidth: 1,
                                callbacks: {{
                                    label: function(context) {{
                                        const value = context.parsed;
                                        const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                        const percentage = ((value / total) * 100).toFixed(1);
                                        return context.label + ': $' + value.toFixed(2) + ' (' + percentage + '%)';
                                    }}
                                }}
                            }}
                        }},
                        onClick: function(event, elements) {{
                            if (elements.length > 0) {{
                                const index = elements[0].index;
                                const serviceKey = chartData[index].service_key;
                                filterByService(serviceKey);
                            }}
                        }}
                    }}
                }});
            }}
            
            // Bar Chart
            const barCtx = document.getElementById('savingsBarChart');
            if (barCtx) {{
                barChart = new Chart(barCtx, {{
                    type: 'bar',
                    data: {{
                        labels: chartData.map(d => d.service),
                        datasets: [{{
                            label: 'Monthly Savings ($)',
                            data: chartData.map(d => d.savings),
                            backgroundColor: awsColors.slice(0, chartData.length),
                            borderWidth: 1
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{
                            legend: {{
                                display: false
                            }},
                            tooltip: {{
                                titleFont: {{ family: "'IBM Plex Sans', sans-serif" }},
                                bodyFont: {{ family: "'IBM Plex Sans', sans-serif" }},
                                titleColor: textColor,
                                bodyColor: textColor,
                                backgroundColor: isDark ? '#333333' : '#ffffff',
                                borderColor: isDark ? '#555555' : '#cccccc',
                                borderWidth: 1,
                                callbacks: {{
                                    label: function(context) {{
                                        return 'Savings: $' + context.parsed.y.toFixed(2);
                                    }}
                                }}
                            }}
                        }},
                        scales: {{
                            y: {{
                                beginAtZero: true,
                                ticks: {{
                                    color: textColor,
                                    callback: function(value) {{
                                        return '$' + value.toFixed(0);
                                    }}
                                }},
                                grid: {{
                                    color: gridColor
                                }},
                                title: {{
                                    display: true,
                                    text: 'Monthly Savings ($)',
                                    color: textColor
                                }}
                            }},
                            x: {{
                                ticks: {{
                                    color: textColor,
                                    maxRotation: 45
                                }},
                                grid: {{
                                    color: gridColor
                                }},
                                title: {{
                                    display: false
                                }}
                            }}
                        }},
                        onClick: function(event, elements) {{
                            if (elements.length > 0) {{
                                const index = elements[0].index;
                                const serviceKey = chartData[index].service_key;
                                filterByService(serviceKey);
                            }}
                        }}
                    }}
                }});
            }}
            }} catch (e) {{
                console.error('Chart initialization failed:', e);
                document.querySelectorAll('.chart-fallback').forEach(function(el) {{
                    el.style.display = 'block';
                }});
            }}
        }}
        
        // Back to top button
        const backToTopBtn = document.querySelector('.back-to-top');
        if (backToTopBtn) {{
            window.addEventListener('scroll', function() {{
                if (window.scrollY > 400) {{
                    backToTopBtn.classList.add('visible');
                }} else {{
                    backToTopBtn.classList.remove('visible');
                }}
            }});
            backToTopBtn.addEventListener('click', function() {{
                window.scrollTo({{ top: 0, behavior: 'smooth' }});
            }});
        }}

        // Initialize theme and charts when page loads
        document.addEventListener('DOMContentLoaded', function() {{
            initializeTheme();
            initializeCharts();
            initializeTabKeyboardNav();
            initializeCopyButtons();
            initializePriorityFilter();
            initializeBucketSort();
            initializeShowMore();
        }});

        // S3 top-bucket sort toggle: client-side sort by cost or size.
        function initializeBucketSort() {{
            document.querySelectorAll('.bucket-sort-toggle').forEach(function(group) {{
                const buttons = group.querySelectorAll('.bucket-sort-btn');
                const table = group.parentElement.querySelector('.top-buckets-table table tbody');
                if (!table) return;
                buttons.forEach(function(btn) {{
                    btn.addEventListener('click', function() {{
                        const key = btn.getAttribute('data-bucket-sort') || 'cost';
                        buttons.forEach(function(b) {{
                            const isMatch = b === btn;
                            b.classList.toggle('active', isMatch);
                            b.setAttribute('aria-pressed', isMatch ? 'true' : 'false');
                        }});
                        const rows = Array.from(table.querySelectorAll('tr'));
                        rows.sort(function(a, b) {{
                            const av = parseFloat(a.getAttribute('data-' + key) || '0');
                            const bv = parseFloat(b.getAttribute('data-' + key) || '0');
                            return bv - av;
                        }});
                        rows.forEach(function(r) {{ table.appendChild(r); }});
                    }});
                }});
            }});
        }}

        // Collapse long resource lists inside rec-item to first 5 entries with toggle.
        function initializeShowMore() {{
            const COLLAPSE_THRESHOLD = 5;
            const resourceIdRe = /^(i|vol|snap|ami|fs|nat|vpc|eks|eni|db|cluster|sg|rtb|igw|tgw|peering)-[a-z0-9]+/i;
            document.querySelectorAll('.rec-item ul, .resource-list').forEach(function(list) {{
                const items = Array.from(list.querySelectorAll(':scope > li'));
                // Decorate resource IDs with copy buttons (best-effort, only when an
                // obvious AWS resource ID prefix sits at the start of the li text).
                items.forEach(function(li) {{
                    if (li.querySelector('.copy-id')) return;
                    const text = (li.textContent || '').trim();
                    const match = text.match(resourceIdRe);
                    if (!match) return;
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'copy-id';
                    btn.setAttribute('data-copy', match[0]);
                    btn.setAttribute('aria-label', 'Copy resource ID ' + match[0]);
                    btn.title = 'Copy ' + match[0];
                    btn.textContent = '⧉';
                    li.prepend(btn);
                }});

                if (items.length <= COLLAPSE_THRESHOLD) return;
                items.forEach(function(li, idx) {{
                    if (idx >= COLLAPSE_THRESHOLD) li.classList.add('hidden-overflow');
                }});
                const toggle = document.createElement('button');
                toggle.type = 'button';
                toggle.className = 'show-more-toggle';
                const hiddenCount = items.length - COLLAPSE_THRESHOLD;
                toggle.textContent = 'Show all ' + items.length + ' (+' + hiddenCount + ' hidden)';
                toggle.setAttribute('aria-expanded', 'false');
                toggle.addEventListener('click', function() {{
                    const expanded = toggle.getAttribute('aria-expanded') === 'true';
                    items.forEach(function(li, idx) {{
                        if (idx >= COLLAPSE_THRESHOLD) {{
                            li.classList.toggle('hidden-overflow', expanded);
                        }}
                    }});
                    toggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
                    toggle.textContent = expanded
                        ? 'Show all ' + items.length + ' (+' + hiddenCount + ' hidden)'
                        : 'Show fewer';
                }});
                list.insertAdjacentElement('afterend', toggle);
            }});

            // Re-wire any copy buttons we just injected so they fire navigator.clipboard.
            initializeCopyButtons();
        }}

        // Priority filter chips: dim non-matching rec-items across all tabs.
        function initializePriorityFilter() {{
            const chips = document.querySelectorAll('.priority-filter-chip');
            chips.forEach(function(chip) {{
                chip.addEventListener('click', function() {{
                    const value = chip.getAttribute('data-priority-filter') || 'all';
                    chips.forEach(function(c) {{
                        const isMatch = c === chip;
                        c.classList.toggle('active', isMatch);
                        c.setAttribute('aria-pressed', isMatch ? 'true' : 'false');
                    }});
                    if (value === 'all') {{
                        document.body.removeAttribute('data-priority-filter');
                    }} else {{
                        document.body.setAttribute('data-priority-filter', value);
                    }}
                }});
            }});
        }}

        // Wire WAI-ARIA tab keyboard navigation + roving tabindex.
        function initializeTabKeyboardNav() {{
            const tablist = document.querySelector('.tab-buttons[role="tablist"]');
            if (!tablist) return;
            const tabs = tablist.querySelectorAll('[role="tab"]');
            tabs.forEach(function(tab) {{
                const isActive = tab.getAttribute('aria-selected') === 'true';
                tab.setAttribute('tabindex', isActive ? '0' : '-1');
                tab.addEventListener('keydown', handleTabKey);
            }});

            // Toggle the right-edge fade gradient when the strip is scrolled to its end.
            const tabsContainer = tablist.closest('.tabs');
            if (!tabsContainer) return;
            const updateFade = function() {{
                const atEnd = tablist.scrollLeft + tablist.clientWidth >= tablist.scrollWidth - 2;
                tabsContainer.classList.toggle('scroll-at-end', atEnd);
            }};
            tablist.addEventListener('scroll', updateFade, {{ passive: true }});
            window.addEventListener('resize', updateFade);
            updateFade();
        }}

        // Wire copy-to-clipboard buttons on resource ids.
        function initializeCopyButtons() {{
            document.querySelectorAll('.copy-id').forEach(function(btn) {{
                btn.addEventListener('click', function(evt) {{
                    evt.stopPropagation();
                    const value = btn.getAttribute('data-copy');
                    if (!value || !navigator.clipboard) return;
                    navigator.clipboard.writeText(value).then(function() {{
                        const orig = btn.getAttribute('aria-label') || 'Copy';
                        btn.classList.add('copied');
                        btn.setAttribute('aria-label', 'Copied');
                        setTimeout(function() {{
                            btn.classList.remove('copied');
                            btn.setAttribute('aria-label', orig);
                        }}, 1500);
                    }});
                }});
            }});
        }}
        </script>"""


def generate_html_report_from_json(json_file: str, output_file: str | None = None) -> str:
    """Generate HTML report from JSON scan results file"""
    with open(json_file, "r") as f:
        scan_results = json.load(f)

    generator = HTMLReportGenerator(scan_results)
    return generator.generate_html_report(output_file)


if __name__ == "__main__":
    """
    Command-line interface for generating HTML reports from existing JSON files.
    
    Usage:
        python3 html_report_generator.py <json_file> [output_file]
    
    Examples:
        python3 html_report_generator.py cost_optimization_scan_us-east-1_20260117_235644.json
        python3 html_report_generator.py scan_results.json custom_report.html
    """
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 html_report_generator.py <json_file> [output_file]")
        print("")
        print("Examples:")
        print("  python3 html_report_generator.py cost_optimization_scan_us-east-1_20260117_235644.json")
        print("  python3 html_report_generator.py scan_results.json custom_report.html")
        print("")
        print("The script will:")
        print("  1. Load the existing JSON scan results")
        print("  2. Generate a professional HTML report with all groupings")
        print("  3. Save as [profile]_[region].html or custom filename")
        sys.exit(1)

    json_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        # Load existing JSON scan results
        with open(json_file, "r") as f:
            scan_results = json.load(f)

        print(f"📊 Loading scan results from: {json_file}")

        # Generate HTML report
        generator = HTMLReportGenerator(scan_results)
        generated_file = generator.generate_html_report(output_file)

        print(f"✅ HTML report generated: {generated_file}")
        print(f"🌐 Open in browser to view interactive cost optimization recommendations")

    except FileNotFoundError:
        print(f"❌ Error: JSON file '{json_file}' not found")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"❌ Error: Invalid JSON format in '{json_file}'")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error generating report: {e}")
        sys.exit(1)
