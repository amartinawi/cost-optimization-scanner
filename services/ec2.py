"""EC2 compute optimization checks.

Extracted from CostOptimizer EC2-related methods as free functions.
This module will later become Ec2Module (T-316) implementing ServiceModule.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.scan_context import ScanContext

logger = logging.getLogger(__name__)

# Per-check reduction factors used to convert an instance's on-demand monthly
# cost into an estimated saving. Calibrated against typical AWS rightsizing
# guidance — kept as a single table so the audit trail is one diff away.
EC2_SAVINGS_FACTORS: dict[str, float] = {
    "Idle Instances": 1.0,
    "Rightsizing Opportunities": 0.40,
    "Burstable Instance Optimization": 0.30,
    "Previous Generation Migration": 0.10,
    "Cron Job Instances": 0.85,
    "Batch Job Instances": 0.75,
    "Underutilized Instance Store": 0.15,
    "Dedicated Hosts": 0.30,
}

_HOURS_PER_MONTH: int = 730

# Maps the EC2 DescribeInstances ``PlatformDetails`` string to the AWS Pricing
# API ``operatingSystem`` attribute value. Pricing for non-Linux platforms can
# be several multiples of Linux (e.g. Windows ~2x), so savings must be priced
# against the instance's real OS rather than assuming Linux.
_PLATFORM_TO_PRICING_OS: dict[str, str] = {
    "Linux/UNIX": "Linux",
    "Red Hat Enterprise Linux": "RHEL",
    "Red Hat Enterprise Linux with HA": "RHEL",
    "SUSE Linux": "SUSE",
    "Windows": "Windows",
    "Windows BYOL": "Windows",
}

# Previous-generation instance family prefixes mapped to a current-generation
# migration target. The 0.10 "Previous Generation Migration" factor reflects the
# typical ~10% price drop per generation step (validated for t2->t3, m4->m5,
# c4->c5, r4->r5). Detection is no longer limited to the t2 family.
_PREVIOUS_GEN_TARGETS: dict[str, str] = {
    "t1.": "t3.",
    "t2.": "t3.",
    "m1.": "m6i.",
    "m2.": "r6i.",
    "m3.": "m6i.",
    "m4.": "m6i.",
    "c1.": "c6i.",
    "c3.": "c6i.",
    "c4.": "c6i.",
    "cc2.": "c6i.",
    "r3.": "r6i.",
    "r4.": "r6i.",
    "i2.": "i4i.",
    "hi1.": "i4i.",
    "hs1.": "d3.",
    "g2.": "g5.",
    "cr1.": "r6i.",
}

# Instance families that ship local NVMe/SSD instance store. The check flags
# these when the workload name does not suggest the local storage is needed.
# Matched on the family token (text before the first dot), not a substring, so
# "i3" no longer accidentally matches "i3en".
_INSTANCE_STORE_FAMILIES: frozenset[str] = frozenset(
    {
        "m5d", "m5ad", "m5dn", "m6gd", "m6id", "m6idn", "m7gd",
        "c5d", "c5ad", "c6gd", "c6id", "c7gd",
        "r5d", "r5ad", "r5dn", "r6gd", "r6id", "r6idn", "r7gd",
        "i3", "i3en", "i4i", "i4g", "im4gn", "is4gen",
        "d2", "d3", "d3en", "h1", "z1d",
        "x1", "x1e", "x2idn", "x2iedn", "x2gd",
    }
)


def _instance_pricing_os(instance: dict[str, Any]) -> str:
    """Map an instance's PlatformDetails to a Pricing API operatingSystem value.

    Falls back to "Linux" for unknown or absent platform strings so pricing
    never regresses below the previous Linux-only behaviour.
    """
    platform = instance.get("PlatformDetails", "") or ""
    if platform in _PLATFORM_TO_PRICING_OS:
        return _PLATFORM_TO_PRICING_OS[platform]
    if platform.startswith("Windows"):
        return "Windows"
    if "Red Hat" in platform or "RHEL" in platform:
        return "RHEL"
    if "SUSE" in platform:
        return "SUSE"
    return "Linux"


def _is_spot_instance(instance: dict[str, Any]) -> bool:
    """True when the instance is a Spot instance.

    Spot is already deeply discounted, so applying on-demand-priced savings
    factors would materially overstate recoverable cost. Callers skip Spot
    instances from the on-demand-priced heuristic checks.
    """
    return instance.get("InstanceLifecycle") == "spot"


def _compute_ec2_savings(
    ctx: ScanContext,
    instance_type: str,
    category: str,
    os_name: str = "Linux",
) -> tuple[float, str]:
    """Return ``(monthly_savings, pricing_basis)`` for an instance under a check.

    Looks up the on-demand hourly price for the instance's real operating
    system via PricingEngine, converts to monthly (730 hours), and multiplies
    by the category's reduction factor. If the OS-specific lookup yields no
    price it falls back to Linux pricing rather than $0.

    ``pricing_basis`` is a human-readable audit string showing exactly how the
    number was derived, e.g. ``"$0.2330/hr Windows on-demand x 730h x 40%"``,
    so every emitted savings figure is defensible from the report alone.
    Returns ``(0.0, "")`` when the category is unknown, pricing is unavailable,
    or any lookup fails — never crashes the scan.
    """
    factor = EC2_SAVINGS_FACTORS.get(category, 0.0)
    if factor <= 0.0 or not ctx.pricing_engine or not instance_type:
        return 0.0, ""
    try:
        priced_os = os_name
        hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type, os_name)
        if hourly <= 0.0 and os_name != "Linux":
            # OS-specific price unavailable — fall back to Linux so a Windows/
            # RHEL/SUSE instance still produces a (conservative) estimate.
            hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type, "Linux")
            priced_os = "Linux"
    except Exception:
        return 0.0, ""
    savings = hourly * _HOURS_PER_MONTH * factor
    if savings <= 0.0:
        return 0.0, ""
    basis = f"${hourly:.4f}/hr {priced_os} on-demand x {_HOURS_PER_MONTH}h x {factor:.0%}"
    return savings, basis


def get_ec2_instance_count(ctx: ScanContext) -> int:
    """Get total EC2 instance count in region.

    Uses pagination to support accounts with unlimited instances.
    Counts all instances across all reservations regardless of state.

    Returns:
        Total number of EC2 instances in the region.
        Returns 0 on errors (with warning messages).
    """
    logger.debug("EC2 module active")
    ec2 = ctx.client("ec2")
    try:
        paginator = ec2.get_paginator("describe_instances")
        count = 0
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                count += len(reservation["Instances"])
        return count
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("UnauthorizedOperation", "AccessDenied"):
            ctx.permission_issue(
                f"Missing IAM permission for describe_instances: {error_code}",
                service="ec2",
                action="ec2:DescribeInstances",
            )
        elif error_code == "RequestLimitExceeded":
            ctx.warn("Rate limit exceeded for describe_instances (retries exhausted)", service="ec2")
        else:
            ctx.warn(f"Could not get EC2 instance count: {e}", service="ec2")
        return 0
    except Exception as e:
        ctx.warn(f"Unexpected error getting EC2 instance count: {e}", service="ec2")
        return 0


def _is_eks_nodegroup_asg(asg_name: str, tags: dict[str, str]) -> bool:
    """Determine if an ASG is an EKS node group.

    EKS node groups have specific naming patterns and tags:
    - Names often contain 'eks-', 'nodegroup', or cluster names
    - Tags include kubernetes.io/cluster/<cluster-name>
    - Tags include eks:nodegroup-name
    """
    try:
        eks_patterns = ["eks-", "nodegroup", "node-group", "ng-"]

        if any(pattern in asg_name.lower() for pattern in eks_patterns):
            return True

        for key in tags:
            key_lower = key.lower()

            if key_lower.startswith("kubernetes.io/cluster/"):
                return True
            if key_lower == "eks:nodegroup-name":
                return True
            if key_lower == "eks:cluster-name":
                return True
            if "nodegroup" in key_lower:
                return True

        return False

    except Exception as e:
        logger.debug("Error checking EKS nodegroup for %s: %s", asg_name, e)
        return False


def _is_eks_managed_instance(tags: dict[str, str]) -> bool:
    """Determine if an EC2 instance is managed by EKS.

    EKS-managed instances have tags:
    - kubernetes.io/cluster/<cluster-name>
    - eks:cluster-name
    - eks:nodegroup-name
    """
    for key in tags:
        key_lower = key.lower()
        if key_lower.startswith("kubernetes.io/cluster/"):
            return True
        if key_lower == "eks:cluster-name":
            return True
        if key_lower == "eks:nodegroup-name":
            return True
    return False


def get_enhanced_ec2_checks(
    ctx: ScanContext,
    pricing_multiplier: float,
    fast_mode: bool = False,
) -> dict[str, Any]:
    """Get enhanced EC2 cost optimization checks.

    When ``fast_mode`` is True, skips all CloudWatch metric queries — emitting
    only the cheap structural checks (previous-generation families, dedicated
    tenancy). This matches the public ``--fast`` contract documented in README.

    Spot instances are excluded from every check: their cost is already deeply
    discounted, so on-demand-priced savings factors would overstate recovery.
    """
    ec2 = ctx.client("ec2")
    cloudwatch = ctx.client("cloudwatch", ctx.region) if not fast_mode else None
    checks: dict[str, list[dict[str, Any]]] = {
        "idle_instances": [],
        "rightsizing_opportunities": [],
        "previous_generation": [],
        "dedicated_hosts": [],
        "burstable_credits": [],
    }

    try:
        paginator = ec2.get_paginator("describe_instances")

        for page in paginator.paginate():
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    instance_id = instance["InstanceId"]
                    instance_type = instance["InstanceType"]
                    state = instance["State"]["Name"]
                    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

                    if _is_eks_managed_instance(tags):
                        continue

                    # Spot instances are already discounted; on-demand-priced
                    # savings factors would overstate recoverable cost.
                    if _is_spot_instance(instance):
                        continue

                    os_name = _instance_pricing_os(instance)

                    if state == "running":
                        if cloudwatch is not None:
                            try:
                                end_time = datetime.now(UTC)
                                start_time = end_time - timedelta(days=7)

                                cpu_response = cloudwatch.get_metric_statistics(
                                    Namespace="AWS/EC2",
                                    MetricName="CPUUtilization",
                                    Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average", "Maximum"],
                                )

                                if cpu_response.get("Datapoints"):
                                    avg_cpu = sum(dp["Average"] for dp in cpu_response["Datapoints"]) / len(
                                        cpu_response["Datapoints"]
                                    )
                                    max_cpu = max(dp["Maximum"] for dp in cpu_response["Datapoints"])

                                    if avg_cpu < 5 and max_cpu < 10:
                                        idle_savings, idle_basis = _compute_ec2_savings(
                                            ctx, instance_type, "Idle Instances", os_name
                                        )
                                        if idle_savings > 0:
                                            checks["idle_instances"].append(
                                                {
                                                    "InstanceId": instance_id,
                                                    "InstanceType": instance_type,
                                                    "OS": os_name,
                                                    "AvgCPU": f"{avg_cpu:.1f}%",
                                                    "MaxCPU": f"{max_cpu:.1f}%",
                                                    "Recommendation": (
                                                        f"Instance shows very low utilization"
                                                        f" (avg: {avg_cpu:.1f}%,"
                                                        f" max: {max_cpu:.1f}%)"
                                                        " - consider terminating or"
                                                        " downsizing"
                                                    ),
                                                    "EstimatedSavings": f"${idle_savings:.2f}/month if terminated",
                                                    "PricingBasis": idle_basis,
                                                    "CheckCategory": "Idle Instances",
                                                }
                                            )
                                    elif avg_cpu < 20 and max_cpu < 40:
                                        rs_savings, rs_basis = _compute_ec2_savings(
                                            ctx, instance_type, "Rightsizing Opportunities", os_name
                                        )
                                        if rs_savings > 0:
                                            checks["rightsizing_opportunities"].append(
                                                {
                                                    "InstanceId": instance_id,
                                                    "InstanceType": instance_type,
                                                    "OS": os_name,
                                                    "AvgCPU": f"{avg_cpu:.1f}%",
                                                    "MaxCPU": f"{max_cpu:.1f}%",
                                                    "Recommendation": (
                                                        f"Low utilization"
                                                        f" (avg: {avg_cpu:.1f}%,"
                                                        f" max: {max_cpu:.1f}%)"
                                                        " - consider smaller instance"
                                                        " type"
                                                    ),
                                                    "EstimatedSavings": f"${rs_savings:.2f}/month if rightsized",
                                                    "PricingBasis": rs_basis,
                                                    "CheckCategory": ("Rightsizing Opportunities"),
                                                }
                                            )

                            except ClientError as ce:
                                code = ce.response.get("Error", {}).get("Code", "")
                                if code in ("UnauthorizedOperation", "AccessDenied"):
                                    ctx.permission_issue(
                                        f"get_metric_statistics denied: {code}",
                                        service="ec2",
                                        action="cloudwatch:GetMetricStatistics",
                                    )
                                else:
                                    # Throttling / transient errors must not silently drop the
                                    # instance from idle/rightsizing detection without a trace.
                                    ctx.warn(
                                        f"CloudWatch CPU lookup failed for {instance_id} ({code or 'unknown'})"
                                        " - idle/rightsizing check skipped",
                                        service="ec2",
                                    )
                            except Exception as cw_err:
                                ctx.warn(
                                    f"CloudWatch CPU lookup error for {instance_id}: {cw_err}"
                                    " - idle/rightsizing check skipped",
                                    service="ec2",
                                )

                        prevgen_prefix = next(
                            (p for p in _PREVIOUS_GEN_TARGETS if instance_type.startswith(p)),
                            None,
                        )
                        if prevgen_prefix:
                            target_family = _PREVIOUS_GEN_TARGETS[prevgen_prefix]
                            recommended_type = target_family + instance_type[len(prevgen_prefix):]
                            prevgen_savings, prevgen_basis = _compute_ec2_savings(
                                ctx, instance_type, "Previous Generation Migration", os_name
                            )
                            if prevgen_savings > 0:
                                checks["previous_generation"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "InstanceType": instance_type,
                                        "OS": os_name,
                                        "Recommendation": (
                                            f"Consider migrating to {recommended_type}"
                                            " for better price/performance"
                                        ),
                                        "EstimatedSavings": (
                                            f"${prevgen_savings:.2f}/month after"
                                            f" {instance_type}→{recommended_type} migration"
                                        ),
                                        "PricingBasis": prevgen_basis,
                                        "CheckCategory": ("Previous Generation Migration"),
                                    }
                                )

                        if instance.get("Placement", {}).get("Tenancy") == "dedicated":
                            dedicated_savings, dedicated_basis = _compute_ec2_savings(
                                ctx, instance_type, "Dedicated Hosts", os_name
                            )
                            if dedicated_savings > 0:
                                checks["dedicated_hosts"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "InstanceType": instance_type,
                                        "OS": os_name,
                                        "Tenancy": "dedicated",
                                        "Recommendation": ("Review dedicated tenancy necessity"),
                                        "EstimatedSavings": f"${dedicated_savings:.2f}/month with shared tenancy",
                                        "PricingBasis": dedicated_basis,
                                        "CheckCategory": "Dedicated Hosts",
                                    }
                                )

                        if instance_type.startswith(("t2.", "t3.", "t4g.")) and cloudwatch is not None:
                            try:
                                end_time = datetime.now(UTC)
                                start_time = end_time - timedelta(days=7)

                                # CPUCreditBalance publishes at 5-min frequency only — use Period=300
                                # with Minimum statistic to surface short credit droughts that hourly
                                # averaging would smooth over.
                                credit_response = cloudwatch.get_metric_statistics(
                                    Namespace="AWS/EC2",
                                    MetricName="CPUCreditBalance",
                                    Dimensions=[
                                        {
                                            "Name": "InstanceId",
                                            "Value": instance_id,
                                        }
                                    ],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=300,
                                    Statistics=["Average", "Minimum"],
                                )

                                cpu_response = cloudwatch.get_metric_statistics(
                                    Namespace="AWS/EC2",
                                    MetricName="CPUUtilization",
                                    Dimensions=[
                                        {
                                            "Name": "InstanceId",
                                            "Value": instance_id,
                                        }
                                    ],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average", "Maximum"],
                                )

                                credit_datapoints = credit_response.get("Datapoints", [])
                                cpu_datapoints = cpu_response.get("Datapoints", [])

                                # Only the "credit exhaustion despite low CPU" case yields a
                                # quantified saving (move to a smaller fixed instance). All other
                                # branches — healthy credits, genuinely busy, or no datapoints —
                                # produce no cost-quantified finding, so nothing is emitted.
                                if credit_datapoints and cpu_datapoints:
                                    min_credits = min(dp["Minimum"] for dp in credit_datapoints)
                                    avg_cpu = sum(dp["Average"] for dp in cpu_datapoints) / len(cpu_datapoints)

                                    if min_credits < 10 and avg_cpu <= 40:
                                        recommendation = (
                                            f"CloudWatch shows credit exhaustion"
                                            f" (min: {min_credits:.1f})"
                                            f" despite low CPU"
                                            f" (avg: {avg_cpu:.1f}%)"
                                            " - consider smaller fixed"
                                            " instance"
                                        )
                                        burst_savings, burst_basis = _compute_ec2_savings(
                                            ctx, instance_type, "Burstable Instance Optimization", os_name
                                        )
                                        if burst_savings > 0:
                                            checks["burstable_credits"].append(
                                                {
                                                    "InstanceId": instance_id,
                                                    "InstanceType": instance_type,
                                                    "OS": os_name,
                                                    "Recommendation": recommendation,
                                                    "CheckCategory": ("Burstable Instance Optimization"),
                                                    "EstimatedSavings": (
                                                        f"${burst_savings:.2f}/month"
                                                        " with smaller fixed instance"
                                                    ),
                                                    "PricingBasis": burst_basis,
                                                }
                                            )

                            except ClientError as ce:
                                code = ce.response.get("Error", {}).get("Code", "")
                                if code in ("UnauthorizedOperation", "AccessDenied"):
                                    ctx.permission_issue(
                                        f"get_metric_statistics (CPUCreditBalance) denied: {code}",
                                        service="ec2",
                                        action="cloudwatch:GetMetricStatistics",
                                    )
                                else:
                                    logger.debug("Burstable credit lookup failed for %s: %s", instance_id, code)
                            except Exception as burst_err:
                                logger.debug("Burstable credit lookup error for %s: %s", instance_id, burst_err)

                    # Stopped Instances finding removed: $0/month, already-stopped instances
                    # incur no compute cost; attached-EBS savings are surfaced by the EBS adapter.

    except Exception as e:
        ctx.warn(f"Could not perform enhanced EC2 checks: {e}", service="ec2")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        for item in items:
            item["CheckCategory"] = item.get(
                "CheckCategory",
                _category.replace("_", " ").title(),
            )
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}


def get_compute_optimizer_recommendations(
    ctx: ScanContext,
) -> list[dict[str, Any]]:
    """Get EC2 recommendations from Compute Optimizer.

    Delegates to services.advisor — the canonical location for all
    advisory-service (Cost Hub / Compute Optimizer) functions.
    """
    from services.advisor import get_ec2_compute_optimizer_recommendations

    return get_ec2_compute_optimizer_recommendations(ctx)


def get_auto_scaling_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 5: Auto Scaling Groups optimization checks."""
    ec2 = ctx.client("ec2")
    autoscaling = ctx.client("autoscaling")
    checks: dict[str, list[dict[str, Any]]] = {
        "static_asgs": [],
        "never_scaling_asgs": [],
        "nonprod_24x7_asgs": [],
        "oversized_instances": [],
        "missing_scale_in_policies": [],
    }

    try:
        asg_response = autoscaling.describe_auto_scaling_groups()
        asgs = asg_response.get("AutoScalingGroups", [])

        for asg in asgs:
            asg_name = asg.get("AutoScalingGroupName")
            min_size = asg.get("MinSize", 0)
            max_size = asg.get("MaxSize", 0)
            desired_capacity = asg.get("DesiredCapacity", 0)

            tags = {tag["Key"]: tag["Value"] for tag in asg.get("Tags", [])}
            environment = tags.get("Environment", "").lower()

            # Static ASGs / EKS static node groups: removed (each emitted $0/month with
            # "quantify after sizing" — best-practice nudges, not cost-quantified findings).
            # Non-Prod 24/7 ASGs: removed (also $0/month).

            launch_template = asg.get("LaunchTemplate")
            _launch_config = asg.get("LaunchConfigurationName")

            if launch_template:
                try:
                    lt_response = ec2.describe_launch_template_versions(
                        LaunchTemplateId=launch_template["LaunchTemplateId"],
                        Versions=[launch_template.get("Version", "$Latest")],
                    )
                    lt_data = lt_response["LaunchTemplateVersions"][0]["LaunchTemplateData"]
                    instance_type = lt_data.get("InstanceType")

                    if instance_type and any(size in instance_type for size in ["xlarge", "2xlarge", "4xlarge"]):
                        asg_node_savings, asg_node_basis = _compute_ec2_savings(
                            ctx, instance_type, "Rightsizing Opportunities"
                        )
                        checks["oversized_instances"].append(
                            {
                                "AutoScalingGroupName": asg_name,
                                "InstanceType": instance_type,
                                "Recommendation": ("Large instance type in ASG - verify rightsizing"),
                                "EstimatedSavings": (f"${asg_node_savings:.2f}/month per node if rightsized"),
                                "PricingBasis": asg_node_basis,
                                "CheckCategory": "Oversized ASG Instances",
                            }
                        )

                except Exception as e:
                    ctx.warn(f"Could not analyze launch template for {asg_name}: {e}", service="ec2")

            try:
                policies_response = autoscaling.describe_policies(AutoScalingGroupName=asg_name)
                policies = policies_response.get("ScalingPolicies", [])

                scale_out_policies = [p for p in policies if p.get("ScalingAdjustment", 0) > 0]
                scale_in_policies = [p for p in policies if p.get("ScalingAdjustment", 0) < 0]

                # Missing Scale-In Policies finding removed: $0/month, "prevents future cost
                # spikes" is risk-mitigation, not realized savings.
                _ = scale_in_policies

            except Exception as e:
                ctx.warn(f"Could not get Auto Scaling policies for {asg_name}: {e}", service="ec2")

    except Exception as e:
        ctx.warn(f"Could not perform Auto Scaling checks: {e}", service="ec2")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        for item in items:
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}


def get_advanced_ec2_checks(
    ctx: ScanContext,
    pricing_multiplier: float,
    fast_mode: bool,
) -> dict[str, Any]:
    """Category 6: EC2 Advanced optimization checks.

    When ``fast_mode`` is True, the per-instance ``describe_volumes`` enrichment
    used by the Oversized Root Volumes check is skipped. The name-pattern and
    instance-store checks still run because they read only the fields already
    returned by ``describe_instances``. Cron and Batch are mutually exclusive
    (an instance matches at most one), and Spot instances are excluded.
    """
    ec2 = ctx.client("ec2")
    checks: dict[str, list[dict[str, Any]]] = {
        "cron_job_instances": [],
        "batch_job_instances": [],
        "underutilized_instance_store": [],
        "oversized_root_volumes": [],
    }

    try:
        paginator = ec2.get_paginator("describe_instances")

        for page in paginator.paginate():
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    instance_id = instance["InstanceId"]
                    instance_type = instance.get("InstanceType", "unknown")
                    state = instance.get("State", {}).get("Name", "unknown")

                    if state != "running":
                        continue

                    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

                    if _is_eks_managed_instance(tags):
                        continue

                    # Spot is already discounted — exclude from on-demand-priced heuristics.
                    if _is_spot_instance(instance):
                        continue

                    os_name = _instance_pricing_os(instance)
                    name = tags.get("Name", instance_id)
                    name_l = name.lower()

                    # Classify each instance into at most ONE name-pattern bucket so a
                    # single instance can never produce both a Cron and a Batch finding
                    # (which previously double-counted savings for names like "batch-job").
                    is_cron = any(k in name_l for k in ("cron", "scheduler"))
                    is_batch = (
                        not is_cron
                        and "web" not in name_l
                        and any(k in name_l for k in ("batch", "job", "worker", "process"))
                    )

                    if is_cron:
                        cron_savings, cron_basis = _compute_ec2_savings(
                            ctx, instance_type, "Cron Job Instances", os_name
                        )
                        if cron_savings > 0:
                            checks["cron_job_instances"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "OS": os_name,
                                    "Name": name,
                                    "Recommendation": ("Consider Lambda, EventBridge, or Batch for cron jobs"),
                                    "EstimatedSavings": f"${cron_savings:.2f}/month with serverless equivalent",
                                    "PricingBasis": cron_basis,
                                    "CheckCategory": "Cron Job Instances",
                                }
                            )
                    elif is_batch:
                        batch_savings, batch_basis = _compute_ec2_savings(
                            ctx, instance_type, "Batch Job Instances", os_name
                        )
                        if batch_savings > 0:
                            checks["batch_job_instances"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "OS": os_name,
                                    "Name": name,
                                    "Recommendation": ("Consider AWS Batch with Spot instances for batch workloads"),
                                    "EstimatedSavings": f"${batch_savings:.2f}/month with Spot in Batch",
                                    "PricingBasis": batch_basis,
                                    "CheckCategory": "Batch Job Instances",
                                }
                            )

                    if fast_mode:
                        # Skip the per-instance describe_volumes enrichment;
                        # name-pattern checks above still run.
                        continue

                    for bdm in instance.get("BlockDeviceMappings", []):
                        if bdm.get("DeviceName") in [
                            "/dev/sda1",
                            "/dev/xvda",
                        ]:
                            ebs = bdm.get("Ebs", {})
                            volume_id = ebs.get("VolumeId")

                            if volume_id:
                                try:
                                    volume_response = ec2.describe_volumes(VolumeIds=[volume_id])
                                    volume = volume_response["Volumes"][0]
                                    size_gb = volume.get("Size", 0)

                                    if size_gb > 100:
                                        volume_type = volume.get("VolumeType", "gp2")
                                        gb_rate = (
                                            ctx.pricing_engine.get_ebs_monthly_price_per_gb(volume_type)
                                            if ctx.pricing_engine
                                            else 0.10 * ctx.pricing_multiplier
                                        )
                                        root_savings = max(size_gb - 20, 0) * gb_rate
                                        if root_savings > 0:
                                            checks["oversized_root_volumes"].append(
                                                {
                                                    "InstanceId": instance_id,
                                                    "InstanceType": instance_type,
                                                    "RootVolumeSize": f"{size_gb}GB",
                                                    "VolumeId": volume_id,
                                                    "VolumeType": volume_type,
                                                    "Recommendation": (
                                                        f"Root volume ({size_gb}GB)"
                                                        " may be oversized -"
                                                        " consider reducing or using"
                                                        " separate data volumes"
                                                        " (verify actual disk usage first)"
                                                    ),
                                                    "EstimatedSavings": (
                                                        f"${root_savings:.2f}"
                                                        "/month if reduced to 20GB"
                                                        " + separate data volume"
                                                    ),
                                                    "PricingBasis": (
                                                        f"${gb_rate:.4f}/GB-mo {volume_type} x ({size_gb}-20)GB"
                                                        " (size-based estimate, not usage-verified)"
                                                    ),
                                                    "CheckCategory": ("Oversized Root Volumes"),
                                                }
                                            )

                                except Exception as e:
                                    ctx.warn(f"Could not get volume details for {volume_id}: {e}", service="ec2")

                    family_token = instance_type.split(".")[0]
                    if family_token in _INSTANCE_STORE_FAMILIES and not any(
                        keyword in name_l
                        for keyword in (
                            "database",
                            "cache",
                            "storage",
                            "data",
                            "analytics",
                        )
                    ):
                        store_savings, store_basis = _compute_ec2_savings(
                            ctx, instance_type, "Underutilized Instance Store", os_name
                        )
                        if store_savings > 0:
                            checks["underutilized_instance_store"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "OS": os_name,
                                    "Name": name,
                                    "Recommendation": (
                                        "Instance has local storage but"
                                        " workload may not require it -"
                                        " consider non-storage optimized"
                                        " type"
                                    ),
                                    "EstimatedSavings": f"${store_savings:.2f}/month with non-storage equivalent",
                                    "PricingBasis": store_basis,
                                    "CheckCategory": ("Underutilized Instance Store"),
                                }
                            )

    except Exception as e:
        ctx.warn(f"Could not perform Advanced EC2 checks: {e}", service="ec2")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        for item in items:
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}
