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


def _compute_ec2_savings(ctx: ScanContext, instance_type: str, category: str) -> float:
    """Return estimated monthly $ savings for an instance under a named check.

    Looks up the on-demand Linux hourly price via PricingEngine, converts to
    monthly (730 hours), and multiplies by the category's reduction factor.
    Returns 0.0 when the category is unknown, pricing is unavailable, or any
    lookup fails — never crashes the scan.
    """
    factor = EC2_SAVINGS_FACTORS.get(category, 0.0)
    if factor <= 0.0 or not ctx.pricing_engine or not instance_type:
        return 0.0
    try:
        hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type, "Linux")
    except Exception:
        return 0.0
    return hourly * _HOURS_PER_MONTH * factor


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

    When ``fast_mode`` is True, skips all CloudWatch metric queries and the
    Auto Scaling membership probe — emitting only the cheap structural checks
    (previous-generation t2 family, dedicated tenancy, stopped instances).
    This matches the public ``--fast`` contract documented in README.
    """
    ec2 = ctx.client("ec2")
    cloudwatch = ctx.client("cloudwatch", ctx.region) if not fast_mode else None
    autoscaling = ctx.client("autoscaling", ctx.region) if not fast_mode else None
    checks: dict[str, list[dict[str, Any]]] = {
        "idle_instances": [],
        "rightsizing_opportunities": [],
        "previous_generation": [],
        "auto_scaling_missing": [],
        "stopped_instances": [],
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

                    if state == "running":
                        try:
                            end_time = datetime.now(UTC)
                            start_time = end_time - timedelta(days=7)

                            if cloudwatch is None:
                                raise RuntimeError("fast_mode: CloudWatch skipped")

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
                                    idle_savings = _compute_ec2_savings(ctx, instance_type, "Idle Instances")
                                    checks["idle_instances"].append(
                                        {
                                            "InstanceId": instance_id,
                                            "InstanceType": instance_type,
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
                                            "CheckCategory": "Idle Instances",
                                        }
                                    )
                                elif avg_cpu < 20 and max_cpu < 40:
                                    rs_savings = _compute_ec2_savings(
                                        ctx, instance_type, "Rightsizing Opportunities"
                                    )
                                    checks["rightsizing_opportunities"].append(
                                        {
                                            "InstanceId": instance_id,
                                            "InstanceType": instance_type,
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
                                            "CheckCategory": ("Rightsizing Opportunities"),
                                        }
                                    )

                        except Exception:
                            if cloudwatch is None:
                                # fast_mode intentionally skips CloudWatch — don't emit a noisy
                                # "enable monitoring" rec for every running instance.
                                pass
                            # else: no CloudWatch metrics — emit no finding (cost-only scope:
                            # "enable monitoring" nudges produce no quantified savings).

                        try:
                            if autoscaling is None:
                                raise RuntimeError("fast_mode: AutoScaling probe skipped")
                            asg_response = autoscaling.describe_auto_scaling_instances(InstanceIds=[instance_id])
                            if not asg_response.get("AutoScalingInstances"):
                                instance_name = ""
                                for tag in instance.get("Tags", []):
                                    if tag["Key"] == "Name":
                                        instance_name = tag["Value"]
                                        break

                                # Auto Scaling Missing finding removed: $0/month, primarily a resilience
                                # nudge ("improves availability") not a quantified cost recommendation.
                                pass
                        except ClientError as ec:
                            code = ec.response.get("Error", {}).get("Code", "")
                            if code in ("UnauthorizedOperation", "AccessDenied"):
                                ctx.permission_issue(
                                    f"describe_auto_scaling_instances denied: {code}",
                                    service="ec2",
                                    action="autoscaling:DescribeAutoScalingInstances",
                                )
                            elif code:
                                logger.debug(
                                    "ASG probe failed for %s: %s",
                                    instance_id,
                                    code,
                                )
                        except Exception as e:
                            logger.debug("ASG probe failed for %s: %s", instance_id, e)

                        if instance_type.startswith("t2."):
                            prevgen_savings = _compute_ec2_savings(
                                ctx, instance_type, "Previous Generation Migration"
                            )
                            checks["previous_generation"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "Recommendation": (
                                        f"Consider migrating to"
                                        f" {instance_type.replace('t2.', 't3.')}"
                                        " for better cost efficiency"
                                    ),
                                    "EstimatedSavings": f"${prevgen_savings:.2f}/month after t2→t3 migration",
                                    "CheckCategory": ("Previous Generation Migration"),
                                }
                            )

                        if instance.get("Placement", {}).get("Tenancy") == "dedicated":
                            dedicated_savings = _compute_ec2_savings(ctx, instance_type, "Dedicated Hosts")
                            checks["dedicated_hosts"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "Tenancy": "dedicated",
                                    "Recommendation": ("Review dedicated tenancy necessity"),
                                    "EstimatedSavings": f"${dedicated_savings:.2f}/month with shared tenancy",
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

                                if credit_datapoints and cpu_datapoints:
                                    _avg_credits = sum(dp["Average"] for dp in credit_datapoints) / len(
                                        credit_datapoints
                                    )
                                    min_credits = min(dp["Minimum"] for dp in credit_datapoints)
                                    avg_cpu = sum(dp["Average"] for dp in cpu_datapoints) / len(cpu_datapoints)
                                    max_cpu = max(dp["Maximum"] for dp in cpu_datapoints)

                                    if min_credits < 10 and avg_cpu > 40:
                                        pass
                                    elif min_credits < 10:
                                        recommendation = (
                                            f"CloudWatch shows credit exhaustion"
                                            f" (min: {min_credits:.1f})"
                                            f" despite low CPU"
                                            f" (avg: {avg_cpu:.1f}%)"
                                            " - consider smaller fixed"
                                            " instance"
                                        )
                                        burst_savings = _compute_ec2_savings(
                                            ctx, instance_type, "Burstable Instance Optimization"
                                        )

                                        checks["burstable_credits"].append(
                                            {
                                                "InstanceId": instance_id,
                                                "InstanceType": instance_type,
                                                "Recommendation": recommendation,
                                                "CheckCategory": ("Burstable Instance Optimization"),
                                                "EstimatedSavings": (
                                                    f"${burst_savings:.2f}/month"
                                                    " with smaller fixed instance"
                                                ),
                                                "ActionRequired": (
                                                    "Enable detailed CloudWatch monitoring if not already enabled"
                                                ),
                                            }
                                        )

                                    elif avg_cpu > 40:
                                        pass
                                else:
                                    recommendation = (
                                        "Enable detailed CloudWatch monitoring for accurate burstable instance analysis"
                                    )

                                    checks["burstable_credits"].append(
                                        {
                                            "InstanceId": instance_id,
                                            "InstanceType": instance_type,
                                            "Recommendation": recommendation,
                                            "CheckCategory": ("Burstable Instance Optimization"),
                                            "EstimatedSavings": (
                                                "$0.00/month - enable"
                                                " CloudWatch monitoring to quantify"
                                            ),
                                            "ActionRequired": (
                                                "Enable detailed CloudWatch monitoring if not already enabled"
                                            ),
                                        }
                                    )

                            except Exception:
                                recommendation = (
                                    "Enable detailed CloudWatch monitoring to"
                                    " analyze CPU credit usage and determine"
                                    " optimal instance type"
                                )

                                # Burstable Instance Optimization without metrics: removed
                                # ($0/month, "enable monitoring" nudge — no quantified savings).
                                pass

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
                        checks["oversized_instances"].append(
                            {
                                "AutoScalingGroupName": asg_name,
                                "InstanceType": instance_type,
                                "Recommendation": ("Large instance type in ASG - verify rightsizing"),
                                "EstimatedSavings": (
                                    f"${_compute_ec2_savings(ctx, instance_type, 'Rightsizing Opportunities'):.2f}"
                                    "/month per node if rightsized"
                                ),
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
    used by the Oversized Root Volumes check is skipped. Name-pattern checks
    (cron/batch/monitoring/instance-store) still run because they read only the
    fields already returned by ``describe_instances``.
    """
    ec2 = ctx.client("ec2")
    checks: dict[str, list[dict[str, Any]]] = {
        "no_network_traffic": [],
        "cron_job_instances": [],
        "batch_job_instances": [],
        "monitoring_only_instances": [],
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

                    name = tags.get("Name", instance_id)

                    if any(
                        keyword in name.lower()
                        for keyword in [
                            "cron",
                            "batch",
                            "job",
                            "scheduler",
                        ]
                    ):
                        cron_savings = _compute_ec2_savings(ctx, instance_type, "Cron Job Instances")
                        checks["cron_job_instances"].append(
                            {
                                "InstanceId": instance_id,
                                "InstanceType": instance_type,
                                "Name": name,
                                "Recommendation": ("Consider Lambda, EventBridge, or Batch for cron jobs"),
                                "EstimatedSavings": f"${cron_savings:.2f}/month with serverless equivalent",
                                "CheckCategory": "Cron Job Instances",
                            }
                        )

                    # Monitoring-only instances finding removed: $0/month, "quantify after
                    # replacement design" is a migration nudge, not a quantified saving.

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
                                                ),
                                                "EstimatedSavings": (
                                                    f"${root_savings:.2f}"
                                                    "/month if reduced to 20GB"
                                                    " + separate data volume"
                                                ),
                                                "CheckCategory": ("Oversized Root Volumes"),
                                            }
                                        )

                                except Exception as e:
                                    ctx.warn(f"Could not get volume details for {volume_id}: {e}", service="ec2")

                    if any(
                        family in instance_type
                        for family in [
                            "m5d",
                            "c5d",
                            "r5d",
                            "i3",
                            "i4i",
                        ]
                    ) and not any(
                        keyword in name.lower()
                        for keyword in [
                            "database",
                            "cache",
                            "storage",
                            "data",
                            "analytics",
                        ]
                    ):
                        store_savings = _compute_ec2_savings(
                            ctx, instance_type, "Underutilized Instance Store"
                        )
                        checks["underutilized_instance_store"].append(
                            {
                                "InstanceId": instance_id,
                                "InstanceType": instance_type,
                                "Name": name,
                                "Recommendation": (
                                    "Instance has local storage but"
                                    " workload may not require it -"
                                    " consider non-storage optimized"
                                    " type"
                                ),
                                "EstimatedSavings": f"${store_savings:.2f}/month with non-storage equivalent",
                                "CheckCategory": ("Underutilized Instance Store"),
                            }
                        )

                    if (
                        any(
                            keyword in name.lower()
                            for keyword in [
                                "batch",
                                "job",
                                "worker",
                                "process",
                            ]
                        )
                        and "web" not in name.lower()
                    ):
                        batch_savings = _compute_ec2_savings(ctx, instance_type, "Batch Job Instances")
                        checks["batch_job_instances"].append(
                            {
                                "InstanceId": instance_id,
                                "InstanceType": instance_type,
                                "Name": name,
                                "Recommendation": ("Consider AWS Batch with Spot instances for batch workloads"),
                                "EstimatedSavings": f"${batch_savings:.2f}/month with Spot in Batch",
                                "CheckCategory": "Batch Job Instances",
                            }
                        )

    except Exception as e:
        ctx.warn(f"Could not perform Advanced EC2 checks: {e}", service="ec2")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        for item in items:
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}
