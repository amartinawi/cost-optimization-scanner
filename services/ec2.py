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
    # Previous Generation / Rightsizing / Burstable now use EXACT current->target
    # price deltas (see _compute_ec2_savings target_type mode); their factors here
    # are retained only as a fallback and are not used on the delta path.
    "Rightsizing Opportunities": 0.40,
    "Burstable Instance Optimization": 0.30,
    "Previous Generation Migration": 0.10,
    "Cron Job Instances": 0.85,
    "Batch Job Instances": 0.75,
    "Underutilized Instance Store": 0.15,
    "Dedicated Hosts": 0.30,
    # Non-prod scheduling: stop instances outside business hours. Off-fraction
    # assumes ~12h/weekday uptime (≈264 of 730 monthly hours) ⇒ ~64% recoverable.
    "Non-Prod Scheduling": 0.64,
}

# Tag values that mark an instance as non-production (safe to stop off-hours).
_NONPROD_ENV_VALUES: frozenset[str] = frozenset(
    {"dev", "development", "test", "testing", "staging", "stage", "qa", "uat", "sandbox", "sbx", "demo"}
)
# Tag keys/values that explicitly opt an instance into Spot (interruptible).
_SPOT_ELIGIBLE_TAG_KEYS: frozenset[str] = frozenset({"spot-eligible", "spoteligible", "interruptible", "spot"})


def _is_nonprod(tags: dict[str, str]) -> bool:
    """True when an Environment/Env/Stage tag marks the instance as non-production."""
    for key in ("Environment", "environment", "Env", "env", "Stage", "stage"):
        if tags.get(key, "").strip().lower() in _NONPROD_ENV_VALUES:
            return True
    return False


def _is_spot_eligible(tags: dict[str, str]) -> bool:
    """True only when a tag explicitly marks the workload as interruptible.

    Spot is never recommended implicitly — running on Spot risks interruption, so
    we require an explicit operator signal rather than guessing from the workload.
    """
    for key, value in tags.items():
        kl = key.strip().lower()
        if kl in _SPOT_ELIGIBLE_TAG_KEYS and value.strip().lower() in ("true", "yes", "1", ""):
            return True
        if kl == "workload" and value.strip().lower() in ("batch", "spot", "interruptible"):
            return True
    return False

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
# Refresh against https://aws.amazon.com/ec2/instance-types/ when new families ship.
_INSTANCE_STORE_FAMILIES: frozenset[str] = frozenset(
    {
        "m5d", "m5ad", "m5dn", "m6gd", "m6id", "m6idn", "m7gd",
        "c5d", "c5ad", "c6gd", "c6id", "c7gd",
        "r5d", "r5ad", "r5dn", "r6gd", "r6id", "r6idn", "r7gd",
        "i3", "i3en", "i4i", "i4g", "im4gn", "is4gen", "i7ie", "i8g",
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


def _instance_license_model(instance: dict[str, Any]) -> str:
    """Return the AWS Pricing licenseModel implied by the instance's platform.

    DescribeInstances reports ``PlatformDetails`` of "Windows BYOL" (and similar
    BYOL variants) for bring-your-own-license instances, which are billed at the
    lower base-compute rate. Everything else uses the on-demand list price.
    """
    platform = instance.get("PlatformDetails", "") or ""
    return "Bring your own license" if "BYOL" in platform else "No License required"


# Idle/rightsizing corroboration thresholds. NetworkIn/NetworkOut (AWS/EC2,
# bytes) are always available without an agent and rule out "CPU-idle but
# network-busy" false positives. Memory (CWAgent mem_used_percent) is only
# present when the CloudWatch agent is installed; when present it prevents
# recommending a downsize of a memory-bound instance.
_NETWORK_IDLE_BYTES_PER_HOUR: float = 5 * 1024 * 1024  # ~5 MB/hr ⇒ effectively idle
_MEMORY_PRESSURE_PCT: float = 80.0

# EC2 instance size ladder (small → large). Used to derive the "one size smaller"
# rightsizing/burstable target so savings are the *actual* price delta between
# the current and target size rather than an arbitrary reduction factor.
#
# Only sizes that exist across the vast majority of families are listed: the
# uncommon rungs (3xlarge, 6xlarge, 9xlarge, 18xlarge — present on only a few
# families) are deliberately omitted so the derived target is a real size for
# most families (e.g. r5.4xlarge -> r5.2xlarge, not the non-existent r5.3xlarge).
# A derived target that still doesn't exist for the family prices to 0 (quietly)
# and the caller emits no finding.
_SIZE_LADDER: tuple[str, ...] = (
    "nano", "micro", "small", "medium", "large", "xlarge",
    "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge", "32xlarge", "48xlarge",
)


def _one_size_down(instance_type: str) -> str | None:
    """Return the next-smaller size in the same family, or None.

    e.g. ``m5.xlarge`` -> ``m5.large``, ``r5.4xlarge`` -> ``r5.2xlarge``. Returns
    None for the smallest rung or an unparseable type. The target is validated by
    a (quiet) pricing lookup downstream, so a size that doesn't exist for the
    family simply yields no finding.
    """
    if "." not in instance_type:
        return None
    family, size = instance_type.split(".", 1)
    if size not in _SIZE_LADDER:
        return None
    idx = _SIZE_LADDER.index(size)
    if idx == 0:
        return None
    return f"{family}.{_SIZE_LADDER[idx - 1]}"


def _classify_utilization(
    avg_cpu: float,
    max_cpu: float,
    net_bytes_per_hour: float | None = None,
    mem_pct: float | None = None,
) -> str | None:
    """Classify an instance as ``"idle"``, ``"rightsize"``, or ``None``.

    Pure decision function (no AWS calls) so the thresholds are unit-testable.

    - ``idle``: very low CPU AND not clearly network-active. Network is only
      used to *suppress* a false idle, never to invent one — when network data
      is unavailable the CPU-only verdict stands (no regression).
    - ``rightsize``: low CPU AND not memory-bound. Memory likewise only
      suppresses; absent memory data leaves prior behaviour unchanged.
    """
    network_active = net_bytes_per_hour is not None and net_bytes_per_hour > _NETWORK_IDLE_BYTES_PER_HOUR
    memory_bound = mem_pct is not None and mem_pct > _MEMORY_PRESSURE_PCT
    if avg_cpu < 5 and max_cpu < 10 and not network_active:
        return "idle"
    if avg_cpu < 20 and max_cpu < 40 and not memory_bound:
        return "rightsize"
    return None


def _network_bytes_per_hour(cloudwatch: Any, instance_id: str, start_time: Any, end_time: Any) -> float | None:
    """Average NetworkIn+NetworkOut bytes per hour over the window, or None.

    NetworkIn/NetworkOut (AWS/EC2, bytes) are published without an agent. Uses
    the Sum statistic over hourly periods to get true bytes/hour. Returns None on
    any error or when no datapoints exist, so the caller treats the signal as
    unknown and the CPU-only verdict stands.
    """
    try:
        total_per_hour = 0.0
        seen = False
        for metric in ("NetworkIn", "NetworkOut"):
            resp = cloudwatch.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName=metric,
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=3600,
                Statistics=["Sum"],
            )
            dps = resp.get("Datapoints", [])
            if dps:
                seen = True
                total_per_hour += sum(dp["Sum"] for dp in dps) / len(dps)
        return total_per_hour if seen else None
    except Exception:
        return None


def _discover_mem_dimension_sets(cloudwatch: Any, instance_id: str) -> list[list[dict[str, str]]]:
    """Discover the full CWAgent ``mem_used_percent`` dimension sets for an instance.

    The CloudWatch agent often publishes memory under more dimensions than just
    InstanceId (ImageId, InstanceType, AutoScalingGroupName, …), and
    get_metric_statistics requires an exact dimension match. list_metrics with a
    partial InstanceId filter returns every matching metric's full dimension set.
    Always includes the InstanceId-only set as a fallback.
    """
    sets: list[list[dict[str, str]]] = []
    try:
        resp = cloudwatch.list_metrics(
            Namespace="CWAgent",
            MetricName="mem_used_percent",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        )
        for metric in resp.get("Metrics", []):
            dims = [{"Name": d["Name"], "Value": d["Value"]} for d in metric.get("Dimensions", [])]
            if dims and dims not in sets:
                sets.append(dims)
    except Exception:
        pass
    instance_only = [{"Name": "InstanceId", "Value": instance_id}]
    if instance_only not in sets:
        sets.append(instance_only)
    return sets


def _memory_used_percent(cloudwatch: Any, instance_id: str, start_time: Any, end_time: Any) -> float | None:
    """Average CWAgent ``mem_used_percent`` over the window, or None.

    Requires the CloudWatch agent (namespace ``CWAgent``). Discovers the agent's
    actual dimension set via list_metrics (the agent rarely publishes by
    InstanceId alone) and queries each until one returns data. Yields None when
    the agent is absent (treated as unknown — no effect on the verdict). A
    best-effort precision signal, never required.
    """
    try:
        for dim_set in _discover_mem_dimension_sets(cloudwatch, instance_id):
            resp = cloudwatch.get_metric_statistics(
                Namespace="CWAgent",
                MetricName="mem_used_percent",
                Dimensions=dim_set,
                StartTime=start_time,
                EndTime=end_time,
                Period=3600,
                Statistics=["Average"],
            )
            dps = resp.get("Datapoints", [])
            if dps:
                return sum(dp["Average"] for dp in dps) / len(dps)
        return None
    except Exception:
        return None


def _ec2_hourly(
    ctx: ScanContext, instance_type: str, os_name: str, license_model: str, quiet: bool = False
) -> tuple[float, str]:
    """Hourly price for (type, os, license) with Linux fallback. Returns (price, priced_os).

    ``quiet=True`` is used for speculative target lookups (candidate rightsizing
    types that may not exist for the family) so an expected miss does not emit a
    pricing-fallback warning.
    """
    if not ctx.pricing_engine:
        return 0.0, os_name
    priced_os = os_name
    hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type, os_name, license_model, quiet=quiet)
    if hourly <= 0.0 and os_name != "Linux":
        if not quiet:
            ctx.warn(
                f"EC2 pricing: {instance_type}/{os_name} SKU unavailable — using Linux lower-bound rate",
                service="ec2",
            )
        hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_type, "Linux", quiet=quiet)
        # Mark the degraded estimate so every downstream PricingBasis string makes
        # the Linux lower-bound fallback visible (the dollar value is unchanged).
        priced_os = "Linux (lower bound)"
    return hourly, priced_os


_PRICING_OS_TO_SPOT_PRODUCT: dict[str, str] = {
    "Linux": "Linux/UNIX",
    "Windows": "Windows",
    "RHEL": "Red Hat Enterprise Linux",
    "SUSE": "SUSE Linux",
}


def _spot_hourly(ctx: ScanContext, instance_type: str, os_name: str) -> float | None:
    """Latest Spot price/hr for the instance type, or None.

    Spot prices live in the EC2 ``describe_spot_price_history`` API (not the
    Pricing API). Returns None on any error or when no history exists so the
    caller emits no Spot finding.
    """
    product = _PRICING_OS_TO_SPOT_PRODUCT.get(os_name, "Linux/UNIX")
    try:
        ec2 = ctx.client("ec2")
        resp = ec2.describe_spot_price_history(
            InstanceTypes=[instance_type],
            ProductDescriptions=[product],
            MaxResults=1,
        )
        history = resp.get("SpotPriceHistory", [])
        if history:
            return float(history[0]["SpotPrice"])
    except Exception:
        return None
    return None


def _compute_ec2_savings(
    ctx: ScanContext,
    instance_type: str,
    category: str,
    os_name: str = "Linux",
    license_model: str = "No License required",
    target_type: str | None = None,
) -> tuple[float, str]:
    """Return ``(monthly_savings, pricing_basis)`` for an instance under a check.

    Two modes:

    * **Exact price-delta** (when ``target_type`` is given): savings are the real
      ``(current_price − target_price) × 730`` between the current instance and a
      concrete recommended type (e.g. previous-gen migration target, one size
      smaller). This is exact, not a reduction-factor estimate. If the target is
      not cheaper or has no price, no finding is emitted.
    * **Factor** (no ``target_type``): for actions with no single instance target
      (terminate = full cost; move-to-serverless), savings are
      ``current_price × 730 × EC2_SAVINGS_FACTORS[category]``.

    Both price by the instance's real OS and license model (Linux fallback), and
    return a human-readable ``pricing_basis`` so every figure is defensible from
    the report alone. Returns ``(0.0, "")`` on any unavailable/failed lookup —
    never crashes the scan.
    """
    if not ctx.pricing_engine or not instance_type:
        return 0.0, ""
    license_note = " BYOL" if license_model == "Bring your own license" else ""
    try:
        hourly, priced_os = _ec2_hourly(ctx, instance_type, os_name, license_model)
        if hourly <= 0.0:
            return 0.0, ""

        if target_type:
            target_hourly, _ = _ec2_hourly(ctx, target_type, os_name, license_model, quiet=True)
            if target_hourly <= 0.0 or target_hourly >= hourly:
                # Target unpriced or not actually cheaper — emit nothing rather
                # than fabricate a saving.
                return 0.0, ""
            savings = (hourly - target_hourly) * _HOURS_PER_MONTH
            basis = (
                f"${hourly:.4f}->${target_hourly:.4f}/hr {priced_os}{license_note} on-demand"
                f" ({instance_type}->{target_type}) x {_HOURS_PER_MONTH}h"
            )
            return savings, basis

        factor = EC2_SAVINGS_FACTORS.get(category, 0.0)
        if factor <= 0.0:
            return 0.0, ""
        savings = hourly * _HOURS_PER_MONTH * factor
        if savings <= 0.0:
            return 0.0, ""
        basis = f"${hourly:.4f}/hr {priced_os}{license_note} on-demand x {_HOURS_PER_MONTH}h x {factor:.0%}"
        return savings, basis
    except Exception:
        return 0.0, ""


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
                    license_model = _instance_license_model(instance)

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

                                    # Corroborating signals (only ever *suppress* a finding, never
                                    # invent one): network rules out CPU-idle-but-busy instances;
                                    # memory (CW agent, often absent) protects memory-bound ones.
                                    net_per_hr = _network_bytes_per_hour(
                                        cloudwatch, instance_id, start_time, end_time
                                    )
                                    mem_pct = _memory_used_percent(
                                        cloudwatch, instance_id, start_time, end_time
                                    )
                                    verdict = _classify_utilization(avg_cpu, max_cpu, net_per_hr, mem_pct)

                                    evidence = {
                                        "OS": os_name,
                                        "AvgCPU": f"{avg_cpu:.1f}%",
                                        "MaxCPU": f"{max_cpu:.1f}%",
                                    }
                                    if net_per_hr is not None:
                                        evidence["NetworkIO"] = f"{net_per_hr / (1024 * 1024):.1f} MB/hr"
                                    if mem_pct is not None:
                                        evidence["AvgMemory"] = f"{mem_pct:.1f}%"

                                    if verdict == "idle":
                                        idle_savings, idle_basis = _compute_ec2_savings(
                                            ctx, instance_type, "Idle Instances", os_name, license_model
                                        )
                                        if idle_savings > 0:
                                            checks["idle_instances"].append(
                                                {
                                                    "InstanceId": instance_id,
                                                    "InstanceType": instance_type,
                                                    **evidence,
                                                    "Recommendation": (
                                                        f"Instance shows very low utilization"
                                                        f" (avg CPU: {avg_cpu:.1f}%,"
                                                        f" max: {max_cpu:.1f}%)"
                                                        " - consider terminating or"
                                                        " downsizing"
                                                    ),
                                                    "EstimatedSavings": f"${idle_savings:.2f}/month if terminated",
                                                    "PricingBasis": idle_basis,
                                                    "CheckCategory": "Idle Instances",
                                                }
                                            )
                                    elif verdict == "rightsize":
                                        rs_target = _one_size_down(instance_type)
                                        rs_savings, rs_basis = (
                                            _compute_ec2_savings(
                                                ctx, instance_type, "Rightsizing Opportunities",
                                                os_name, license_model, target_type=rs_target,
                                            )
                                            if rs_target
                                            else (0.0, "")
                                        )
                                        if rs_savings > 0:
                                            checks["rightsizing_opportunities"].append(
                                                {
                                                    "InstanceId": instance_id,
                                                    "InstanceType": instance_type,
                                                    **evidence,
                                                    "Recommendation": (
                                                        f"Low utilization"
                                                        f" (avg CPU: {avg_cpu:.1f}%,"
                                                        f" max: {max_cpu:.1f}%)"
                                                        f" - consider downsizing to {rs_target}"
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
                                ctx, instance_type, "Previous Generation Migration", os_name, license_model,
                                target_type=recommended_type,
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
                                ctx, instance_type, "Dedicated Hosts", os_name, license_model
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

                                    burst_target = _one_size_down(instance_type)
                                    if min_credits < 10 and avg_cpu <= 40 and burst_target:
                                        recommendation = (
                                            f"CloudWatch shows credit exhaustion"
                                            f" (min: {min_credits:.1f})"
                                            f" despite low CPU"
                                            f" (avg: {avg_cpu:.1f}%)"
                                            f" - consider smaller fixed instance ({burst_target})"
                                        )
                                        burst_savings, burst_basis = _compute_ec2_savings(
                                            ctx, instance_type, "Burstable Instance Optimization",
                                            os_name, license_model, target_type=burst_target,
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
        asgs: list[dict[str, Any]] = []
        asg_paginator = autoscaling.get_paginator("describe_auto_scaling_groups")
        for page in asg_paginator.paginate():
            asgs.extend(page.get("AutoScalingGroups", []))

        for asg in asgs:
            asg_name = asg.get("AutoScalingGroupName")
            min_size = asg.get("MinSize", 0)
            max_size = asg.get("MaxSize", 0)
            desired_capacity = asg.get("DesiredCapacity", 0)

            tags = {tag["Key"]: tag["Value"] for tag in asg.get("Tags", [])}

            # EKS node-group ASGs are sized by the cluster's node-group config and
            # surface under the EKS tab, so skip them here to avoid redundant
            # advisory cards (ec2 L3).
            if _is_eks_nodegroup_asg(asg_name, tags):
                continue

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

                    # Spot-backed ASGs already run at the discounted Spot rate, so a
                    # rightsizing-delta saving against on-demand pricing would be
                    # misleading — skip them (ec2 L3).
                    market_type = lt_data.get("InstanceMarketOptions", {}).get("MarketType", "")
                    if str(market_type).lower() == "spot":
                        continue

                    # A scaled-to-zero ASG runs no instances, so there are no
                    # per-node dollars to save (mirror the EKS 0-node fix).
                    if (
                        instance_type
                        and desired_capacity > 0
                        and any(size in instance_type for size in ["xlarge", "2xlarge", "4xlarge"])
                    ):
                        asg_node_savings, asg_node_basis = _compute_ec2_savings(
                            ctx, instance_type, "Rightsizing Opportunities"
                        )
                        # No defensible per-node dollar (pricing unavailable) -> emit
                        # no $0.00 advisory card (ec2 L3).
                        if asg_node_savings > 0:
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


def _tag_heuristic_savings(
    savings: float,
    counted_suffix: str,
    corroborated: bool,
) -> tuple[str, dict[str, Any]]:
    """Build the ``EstimatedSavings`` string + extra rec fields for a tag heuristic.

    ec2 H2 — the cron / batch / instance-store / non-prod findings are inferred
    from Name/Environment tags alone and carry no usage evidence. Their
    blanket-factor dollar (``EC2_SAVINGS_FACTORS``) is only counted when the same
    instance is corroborated by the CloudWatch idle/low-CPU signal the rightsizing
    checks already gather. Otherwise the rec is a ``$0.00`` advisory
    (``Counted=False``) that still renders as a visible architectural nudge — the
    speculative figure is preserved in ``AdvisoryEstimate`` — but is never summed
    into the EC2 headline. This avoids both fabricating dollars on well-utilized
    (or unmeasured) instances and double-counting against the idle/rightsizing
    lever that already owns a corroborated instance.

    Args:
        savings: The factor-based monthly saving (``$/mo``) if the lever applied.
        counted_suffix: Trailing clause for the counted ``EstimatedSavings`` text.
        corroborated: Whether the instance shows measured low utilization.

    Returns:
        ``(estimated_savings_string, extra_rec_fields)`` — ``extra_rec_fields`` is
        empty when counted, or ``{"Counted": False, "AdvisoryEstimate": ...}``
        when advisory.
    """
    if corroborated:
        return f"${savings:.2f}/month {counted_suffix}", {}
    return (
        "$0.00/month — advisory: tag-based heuristic, no CloudWatch utilization evidence",
        {"Counted": False, "AdvisoryEstimate": round(savings, 2)},
    )


def get_advanced_ec2_checks(
    ctx: ScanContext,
    pricing_multiplier: float,
    fast_mode: bool,
    corroborated_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Category 6: EC2 Advanced optimization checks.

    All checks read only fields returned by ``describe_instances`` (instance
    type and name tags), so they run identically in ``fast_mode``. Cron and
    Batch are mutually exclusive (an instance matches at most one), and Spot
    instances are excluded.

    ec2 H2 — the four tag-based levers (cron, batch, instance-store, non-prod)
    only emit a **counted** dollar when their instance id is in
    ``corroborated_ids`` (the set the adapter derives from the CloudWatch
    idle/low-CPU rightsizing findings). Without that evidence each is demoted to
    a ``$0.00`` ``Counted=False`` advisory that still renders. Spot migration is
    unaffected — its saving is the live on-demand minus Spot price, not a factor.

    Volume sizing is intentionally NOT handled here: it is owned by the EBS
    adapter (AWS Compute Optimizer volume rightsizing + gp2→gp3), so emitting a
    root-volume resize from EC2 would double-count the same dollars across the
    EC2 and EBS tabs and lacked disk-utilization evidence.

    Args:
        ctx: ScanContext with clients and pricing data.
        pricing_multiplier: Regional pricing multiplier (fallback paths only).
        fast_mode: When True the adapter passes an empty ``corroborated_ids`` (no
            CloudWatch evidence is gathered), so every tag lever is advisory.
        corroborated_ids: Instance ids with measured low utilization, used to gate
            the four tag levers from advisory to counted.
    """
    ec2 = ctx.client("ec2")
    checks: dict[str, list[dict[str, Any]]] = {
        "cron_job_instances": [],
        "batch_job_instances": [],
        "underutilized_instance_store": [],
        "nonprod_scheduling": [],
        "spot_migration": [],
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
                    license_model = _instance_license_model(instance)
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
                            ctx, instance_type, "Cron Job Instances", os_name, license_model
                        )
                        if cron_savings > 0:
                            est, extra = _tag_heuristic_savings(
                                cron_savings, "with serverless equivalent", instance_id in corroborated_ids
                            )
                            checks["cron_job_instances"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "OS": os_name,
                                    "Name": name,
                                    "Recommendation": ("Consider Lambda, EventBridge, or Batch for cron jobs"),
                                    "EstimatedSavings": est,
                                    "PricingBasis": cron_basis,
                                    "CheckCategory": "Cron Job Instances",
                                    **extra,
                                }
                            )
                    elif is_batch:
                        batch_savings, batch_basis = _compute_ec2_savings(
                            ctx, instance_type, "Batch Job Instances", os_name, license_model
                        )
                        if batch_savings > 0:
                            est, extra = _tag_heuristic_savings(
                                batch_savings, "with Spot in Batch", instance_id in corroborated_ids
                            )
                            checks["batch_job_instances"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "OS": os_name,
                                    "Name": name,
                                    "Recommendation": ("Consider AWS Batch with Spot instances for batch workloads"),
                                    "EstimatedSavings": est,
                                    "PricingBasis": batch_basis,
                                    "CheckCategory": "Batch Job Instances",
                                    **extra,
                                }
                            )

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
                            ctx, instance_type, "Underutilized Instance Store", os_name, license_model
                        )
                        if store_savings > 0:
                            est, extra = _tag_heuristic_savings(
                                store_savings, "with non-storage equivalent", instance_id in corroborated_ids
                            )
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
                                    "EstimatedSavings": est,
                                    "PricingBasis": store_basis,
                                    "CheckCategory": ("Underutilized Instance Store"),
                                    **extra,
                                }
                            )

                    # Non-prod scheduling: stop tagged non-production instances
                    # outside business hours (≈64% recoverable). Quantified from
                    # the instance's real monthly cost × documented off-fraction.
                    if _is_nonprod(tags):
                        sched_savings, sched_basis = _compute_ec2_savings(
                            ctx, instance_type, "Non-Prod Scheduling", os_name, license_model
                        )
                        if sched_savings > 0:
                            est, extra = _tag_heuristic_savings(
                                sched_savings, "with an off-hours schedule", instance_id in corroborated_ids
                            )
                            checks["nonprod_scheduling"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "OS": os_name,
                                    "Name": name,
                                    "Environment": tags.get("Environment", tags.get("environment", "")),
                                    "Recommendation": (
                                        "Non-production instance runs 24/7 - schedule stop/start"
                                        " outside business hours (e.g. via Instance Scheduler)"
                                    ),
                                    "EstimatedSavings": est,
                                    "PricingBasis": sched_basis,
                                    "CheckCategory": "Non-Prod Scheduling",
                                    **extra,
                                }
                            )

                    # Spot migration: only when an explicit tag marks the workload
                    # interruptible. Savings = live on-demand minus Spot price.
                    if _is_spot_eligible(tags):
                        on_demand, priced_os = _ec2_hourly(ctx, instance_type, os_name, license_model)
                        spot = _spot_hourly(ctx, instance_type, os_name)
                        if on_demand > 0 and spot and spot < on_demand:
                            spot_savings = (on_demand - spot) * _HOURS_PER_MONTH
                            checks["spot_migration"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "OS": priced_os,
                                    "Name": name,
                                    "Recommendation": (
                                        "Tagged interruptible - run on Spot capacity"
                                        " (use a mixed-instances ASG / capacity-optimized allocation)"
                                    ),
                                    "EstimatedSavings": f"${spot_savings:.2f}/month on Spot vs on-demand",
                                    "PricingBasis": (
                                        f"${on_demand:.4f}->${spot:.4f}/hr (on-demand->spot) x {_HOURS_PER_MONTH}h"
                                    ),
                                    "CheckCategory": "Spot Migration",
                                }
                            )

    except Exception as e:
        ctx.warn(f"Could not perform Advanced EC2 checks: {e}", service="ec2")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        for item in items:
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}
