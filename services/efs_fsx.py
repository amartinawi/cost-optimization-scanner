# ruff: noqa: E501
"""EFS and FSx cost optimization checks.

Findings come in two kinds:
  * COUNTED   — a concrete, account-specific dollar saving (a real price delta or
                a measured-storage number). Carries ``EstimatedSavings`` ($),
                ``_savings`` (float), and an ``AuditBasis``.
  * ADVISORY  — a real best-practice opportunity whose dollar value cannot be
                derived from available data without access-pattern/usage evidence
                (e.g. enable dedup, intelligent-tiering, reduce backup retention).
                Carries ``Counted: False`` and NO dollar figure, so it never
                inflates the tab's savings or recommendation count.

AWS Cost Optimization Hub and Compute Optimizer do NOT cover EFS/FSx, so every
number here is derived locally from DescribeFileSystems + the live Pricing API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.scan_context import ScanContext
from services.file_systems_logic import (
    EFS_IA_TRANSITION_FRACTION,
    EFS_METRIC_WINDOW_DAYS,
    EFS_MIN_LIFECYCLE_GB,
    EFS_ONE_ZONE_MIN_GB,
    FSX_SSD_TO_HDD_MIN_GB,
    efs_idle_savings,
    efs_lifecycle_net_savings,
    efs_lifecycle_savings,
    efs_one_zone_savings,
    fsx_ssd_to_hdd_savings,
)

SMALL_EFS_SIZE_GB: float = 0.1
EXCESSIVE_BACKUP_RETENTION_DAYS: int = 30
# Only FSx for Windows File Server exposes an HDD tier with a clean, in-place
# SSD->HDD price delta we can COUNT. ONTAP has no HDD storage type (SSD +
# capacity-pool tiering only). Lustre HDD exists but only on Persistent
# deployments and at a different throughput-per-TiB tier, so the swap is not
# like-for-like — Lustre and ONTAP are surfaced as advisory, never counted.
_FSX_HDD_COUNTED_ELIGIBLE: frozenset[str] = frozenset({"WINDOWS"})


def _report_aws_error(
    ctx: ScanContext, exc: Exception, message: str, service: str, action: str | None = None
) -> None:
    """Route an AWS error to permission_issue (AccessDenied) or warn (all else).

    A throttled or denied describe/CloudWatch read must never silently become
    "no usage -> no/false saving"; it is recorded so the gap is visible.
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
            ctx.permission_issue(f"{message}: {code}", service=service, action=action)
            return
    ctx.warn(f"{message}: {exc}", service=service)


def _list_file_caches(fsx: Any) -> list[dict[str, Any]]:
    """Return all FSx file caches, following NextToken pages.

    ``describe_file_caches`` has no boto3 paginator, so a one-shot call silently
    caps the file-cache count at the first page on accounts with many caches
    (file_systems L3). The caller wraps this in its own try/except.
    """
    caches: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        resp = fsx.describe_file_caches(**({"NextToken": token} if token else {}))
        caches.extend(resp.get("FileCaches", []))
        token = resp.get("NextToken")
        if not token:
            return caches


FILE_SYSTEM_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "efs_lifecycle_policies": {
        "title": "Configure EFS Lifecycle Policies",
        "description": "Automatically move infrequently accessed files to IA storage to cut storage cost on cold data.",
        "action": "1. Enable Transition to IA after 30 days\n2. Configure Transition back to Standard on access\n3. Counted when CloudWatch access metrics prove a cold Standard set and the saving is net-positive after IA access charges; otherwise advisory.",
    },
    "efs_idle_systems": {
        "title": "Delete Idle EFS File Systems",
        "description": "Remove EFS file systems with no mount targets to eliminate 100% of their storage cost.",
        "action": "1. Verify no applications use the file system\n2. Snapshot/back up if needed\n3. Delete via console or CLI.",
    },
    "efs_one_zone_migration": {
        "title": "Migrate to EFS One Zone Storage",
        "description": "For workloads that don't require Multi-AZ resilience, One Zone storage is materially cheaper per GB.",
        "action": "1. Confirm availability requirements\n2. Recreate as One Zone and migrate with DataSync.",
    },
    "fsx_storage_optimization": {
        "title": "Optimize FSx Storage Type",
        "description": "Switch large, throughput-insensitive FSx SSD file systems to HDD storage.",
        "action": "1. Confirm performance needs allow HDD\n2. Recreate with HDD storage and migrate.",
    },
    "fsx_advisory": {
        "title": "FSx Advisory Opportunities",
        "description": "Best-practice opportunities (deduplication, intelligent-tiering, Single-AZ, backup retention) whose dollar value requires usage/backup-size evidence to quantify.",
        "action": "Review each opportunity against actual workload requirements.",
    },
}


# ── Region-correct rate helpers ──────────────────────────────────────────────


def _efs_rate(ctx: ScanContext, storage_class: str, pricing_multiplier: float) -> float:
    """Region-correct EFS $/GB-month for a storage class (fallback when no engine)."""
    if ctx.pricing_engine is not None:
        return ctx.pricing_engine.get_efs_monthly_price_per_gb(storage_class)
    from core.pricing_engine import FALLBACK_EFS_GB_MONTH, FALLBACK_EFS_GB_MONTH_BY_CLASS, _EFS_STORAGE_CLASS_LABELS

    api_class = _EFS_STORAGE_CLASS_LABELS.get(storage_class.strip().lower(), "General Purpose")
    return FALLBACK_EFS_GB_MONTH_BY_CLASS.get(api_class, FALLBACK_EFS_GB_MONTH) * pricing_multiplier


def _efs_ia_access_rate(ctx: ScanContext, pricing_multiplier: float) -> float:
    """Region-correct EFS IA per-GB data-access rate (fallback when no engine)."""
    if ctx.pricing_engine is not None:
        return ctx.pricing_engine.get_efs_ia_access_price_per_gb()
    from core.pricing_engine import FALLBACK_EFS_IA_ACCESS_GB

    return FALLBACK_EFS_IA_ACCESS_GB * pricing_multiplier


def _efs_access_signal(cloudwatch: Any, fs_id: str) -> float | None:
    """GB of data read+written for an EFS file system over the metric window.

    Sums daily ``DataReadIOBytes`` + ``DataWriteIOBytes`` (Bytes, Sum) over the
    last ``EFS_METRIC_WINDOW_DAYS`` and returns GB — the measured "hot" volume.
    Returns ``None`` when NEITHER metric has datapoints, so callers warn and keep
    the finding advisory rather than fabricating a saving. AWS errors propagate to
    the caller for permission classification.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=EFS_METRIC_WINDOW_DAYS)
    total_bytes = 0.0
    found = False
    for metric in ("DataReadIOBytes", "DataWriteIOBytes"):
        resp = cloudwatch.get_metric_statistics(
            Namespace="AWS/EFS",
            MetricName=metric,
            Dimensions=[{"Name": "FileSystemId", "Value": fs_id}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
        )
        datapoints = resp.get("Datapoints", [])
        if datapoints:
            found = True
            total_bytes += sum(d.get("Sum", 0.0) for d in datapoints)
    if not found:
        return None
    return total_bytes / (1024**3)


def _fsx_rate(ctx: ScanContext, fs_type: str, storage_type: str, deployment: str, pricing_multiplier: float) -> float:
    """Region-correct FSx $/GB-month for a (type, storage, deployment)."""
    if ctx.pricing_engine is not None:
        return ctx.pricing_engine.get_fsx_storage_price_per_gb(fs_type, storage_type, deployment)
    from core.pricing_engine import FALLBACK_FSX_GB_MONTH, FALLBACK_FSX_MULTI_AZ_GB_MONTH

    table = FALLBACK_FSX_MULTI_AZ_GB_MONTH if "MULTI" in deployment.upper() else FALLBACK_FSX_GB_MONTH
    key = (fs_type.upper(), storage_type.upper())
    rate = (
        table.get(key)
        or table.get((fs_type.upper(), "SSD"))
        or FALLBACK_FSX_GB_MONTH.get(key)
        or FALLBACK_FSX_GB_MONTH.get((fs_type.upper(), "SSD"), 0.15)
    )
    return rate * pricing_multiplier


def _fsx_deployment_option(fs: dict[str, Any]) -> str:
    """Coarse Single-AZ / Multi-AZ classification for FSx pricing lookups."""
    cfg = fs.get("WindowsConfiguration", {}) or fs.get("OntapConfiguration", {}) or fs.get("OpenZFSConfiguration", {})
    deployment = str(cfg.get("DeploymentType", "")).upper()
    return "Multi-AZ" if "MULTI" in deployment else "Single-AZ"


# ── Resource counts (unchanged behaviour) ────────────────────────────────────


def get_efs_file_system_count(ctx: ScanContext) -> dict[str, Any]:
    empty = {
        "total": 0, "available": 0, "creating": 0, "deleting": 0,
        "standard_storage": 0, "one_zone_storage": 0, "total_size_gb": 0, "unused_systems": [],
    }
    try:
        efs = ctx.client("efs")
        paginator = efs.get_paginator("describe_file_systems")
        counts: dict[str, Any] = {**empty, "unused_systems": []}
        for page in paginator.paginate():
            for fs in page["FileSystems"]:
                counts["total"] += 1
                state = fs.get("LifeCycleState", "")
                if state in ("available", "creating", "deleting"):
                    counts[state] += 1
                if fs.get("AvailabilityZoneName"):
                    counts["one_zone_storage"] += 1
                else:
                    counts["standard_storage"] += 1
                size_bytes = fs.get("SizeInBytes", {}).get("Value", 0)
                size_gb = size_bytes / (1024**3) if size_bytes else 0
                counts["total_size_gb"] += size_gb
                if size_gb < 0.1 and fs.get("NumberOfMountTargets", 0) == 0:
                    counts["unused_systems"].append(
                        {
                            "FileSystemId": fs["FileSystemId"],
                            "Name": fs.get("Name", "Unnamed"),
                            "SizeGB": round(size_gb, 3),
                            "MountTargets": fs.get("NumberOfMountTargets", 0),
                        }
                    )
        counts["total_size_gb"] = round(counts["total_size_gb"], 2)
        return counts
    except Exception as e:
        _report_aws_error(ctx, e, "Could not get EFS file system count", "efs", "elasticfilesystem:DescribeFileSystems")
        return dict(empty)


def get_fsx_file_system_count(ctx: ScanContext) -> dict[str, Any]:
    empty = {
        "total": 0, "available": 0, "creating": 0, "deleting": 0,
        "lustre": 0, "windows": 0, "ontap": 0, "openzfs": 0,
        "file_cache": 0, "total_capacity_gb": 0, "underutilized_systems": [],
    }
    try:
        fsx = ctx.client("fsx")
        fs_paginator = fsx.get_paginator("describe_file_systems")
        file_systems: list[dict[str, Any]] = []
        for page in fs_paginator.paginate():
            file_systems.extend(page.get("FileSystems", []))
        try:
            caches = _list_file_caches(fsx)
        except Exception as e:
            _report_aws_error(ctx, e, "Could not list FSx file caches", "fsx", "fsx:DescribeFileCaches")
            caches = []

        counts: dict[str, Any] = {**empty, "underutilized_systems": [], "file_cache": len(caches)}
        counts["total"] = len(file_systems) + len(caches)
        for fs in file_systems:
            state = fs.get("Lifecycle", "")
            if state == "AVAILABLE":
                counts["available"] += 1
            elif state == "CREATING":
                counts["creating"] += 1
            elif state == "DELETING":
                counts["deleting"] += 1
            fs_type = fs.get("FileSystemType", "").lower()
            if fs_type in ("lustre", "windows", "ontap", "openzfs"):
                counts[fs_type] += 1
            counts["total_capacity_gb"] += fs.get("StorageCapacity", 0)
        for cache in caches:
            counts["total_capacity_gb"] += cache.get("StorageCapacity", 0)
        return counts
    except Exception as e:
        _report_aws_error(ctx, e, "Could not get FSx file system count", "fsx", "fsx:DescribeFileSystems")
        return dict(empty)


def get_file_system_optimization_descriptions() -> dict[str, dict[str, str]]:
    return FILE_SYSTEM_OPTIMIZATION_DESCRIPTIONS


# ── EFS findings ─────────────────────────────────────────────────────────────


def get_efs_findings(
    ctx: ScanContext, pricing_multiplier: float, fast_mode: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{"counted": [...], "advisory": [...]}`` for EFS file systems.

    Counted savings use the storage-class breakdown DescribeFileSystems already
    reports (``SizeInBytes.ValueInStandard`` / ``ValueInIA``) plus the live
    per-class rate. The IA-lifecycle saving is counted ONLY when CloudWatch
    access metrics (``DataReadIOBytes`` / ``DataWriteIOBytes``) prove a cold
    Standard set and the saving is net-positive after the IA access charge;
    otherwise it stays advisory. ``fast_mode`` skips the per-FS metric reads.
    """
    counted: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    # CloudWatch evidence gates the counted lifecycle saving. Skipped in fast
    # mode (one warning) and unavailable when the client isn't provisioned.
    cloudwatch = None if fast_mode else ctx.client("cloudwatch")
    if fast_mode:
        ctx.warn(
            "EFS IA-lifecycle savings require DataReadIOBytes/DataWriteIOBytes metrics; "
            "skipped in fast mode — lifecycle opportunities shown as advisory.",
            "efs",
        )
    try:
        efs = ctx.client("efs")
        paginator = efs.get_paginator("describe_file_systems")
        region = getattr(ctx, "region", "")

        for page in paginator.paginate():
            for fs in page["FileSystems"]:
                fs_id = fs["FileSystemId"]
                # Skip transient lifecycle states (creating/updating/deleting/
                # error). A just-created FS has 0 mount targets transiently and
                # would otherwise emit a spurious idle-delete saving.
                if str(fs.get("LifeCycleState", "")).lower() not in ("available", ""):
                    continue
                name = fs.get("Name", "Unnamed")
                size = fs.get("SizeInBytes", {})
                total_gb = (size.get("Value", 0) or 0) / (1024**3)
                standard_gb = (size.get("ValueInStandard", 0) or 0) / (1024**3)
                is_one_zone = fs.get("AvailabilityZoneName") is not None
                mount_targets = fs.get("NumberOfMountTargets", 0)

                std_class = "One Zone" if is_one_zone else "Standard"
                ia_class = "One Zone-IA" if is_one_zone else "IA"
                std_rate = _efs_rate(ctx, std_class, pricing_multiplier)
                ia_rate = _efs_rate(ctx, ia_class, pricing_multiplier)

                try:
                    lifecycle = efs.describe_lifecycle_configuration(FileSystemId=fs_id).get("LifecyclePolicies", [])
                except Exception as e:
                    _report_aws_error(
                        ctx, e, f"Could not read EFS lifecycle config for {fs_id}",
                        "efs", "elasticfilesystem:DescribeLifecycleConfiguration",
                    )
                    lifecycle = []
                has_ia_policy = any(p.get("TransitionToIA") for p in lifecycle)
                has_archive_policy = any(p.get("TransitionToArchive") for p in lifecycle)

                # 1) Idle delete — no mount targets => 100% of storage cost.
                if mount_targets == 0 and total_gb > 0:
                    savings = efs_idle_savings(total_gb, std_rate)
                    counted.append(
                        {
                            "FileSystemId": fs_id, "Name": name, "SizeGB": round(total_gb, 2),
                            "StorageClass": std_class, "CheckCategory": "Idle EFS File System",
                            "Recommendation": "Delete idle file system (no mount targets)",
                            "EstimatedSavings": f"${savings:.2f}/month", "_savings": savings, "Counted": True,
                            "AuditBasis": {
                                "metric": "100% of measured storage cost", "region": region,
                                "size_gb": round(total_gb, 2), "rate_per_gb_month": round(std_rate, 6),
                                "basis": "total_gb x EFS $/GB-mo",
                            },
                        }
                    )

                # 2) Lifecycle — no IA policy and measurable Standard data.
                # COUNTED only with CloudWatch evidence: cold_gb = Standard bytes
                # NOT accessed over the window, and the saving must be net-positive
                # after the IA per-GB access charge. Otherwise ADVISORY (indicative
                # gross), mirroring the evidence-gated S3 adapter.
                elif not has_ia_policy and standard_gb >= EFS_MIN_LIFECYCLE_GB:
                    monthly_access_gb = None
                    if cloudwatch is not None:
                        try:
                            monthly_access_gb = _efs_access_signal(cloudwatch, fs_id)
                            if monthly_access_gb is None:
                                ctx.warn(
                                    f"No EFS access metrics for {fs_id}; IA-lifecycle shown as advisory "
                                    f"(no usage evidence).",
                                    "efs",
                                )
                        except Exception as e:
                            _report_aws_error(
                                ctx, e, f"Could not read EFS access metrics for {fs_id}",
                                "efs", "cloudwatch:GetMetricStatistics",
                            )
                            monthly_access_gb = None

                    if monthly_access_gb is not None:
                        ia_access_rate = _efs_ia_access_rate(ctx, pricing_multiplier)
                        est = efs_lifecycle_net_savings(
                            standard_gb, monthly_access_gb, std_rate, ia_rate, ia_access_rate
                        )
                        if est.net_savings > 0 and est.cold_gb >= EFS_MIN_LIFECYCLE_GB:
                            counted.append(
                                {
                                    "FileSystemId": fs_id, "Name": name, "SizeGB": round(total_gb, 2),
                                    "StorageClass": std_class, "HasIAPolicy": False,
                                    "CheckCategory": "EFS No Lifecycle",
                                    "Recommendation": "Enable IA lifecycle policy for infrequently accessed data",
                                    "EstimatedSavings": f"${est.net_savings:.2f}/month",
                                    "_savings": est.net_savings, "Counted": True,
                                    "AuditBasis": {
                                        "metric": f"DataReadIOBytes+DataWriteIOBytes over {EFS_METRIC_WINDOW_DAYS}d",
                                        "region": region, "standard_gb": round(standard_gb, 2),
                                        "monthly_access_gb": round(monthly_access_gb, 2),
                                        "cold_gb": round(est.cold_gb, 2),
                                        "standard_rate_per_gb_month": round(std_rate, 6),
                                        "ia_rate_per_gb_month": round(ia_rate, 6),
                                        "ia_access_rate_per_gb": round(ia_access_rate, 6),
                                        "gross_savings": round(est.gross_savings, 2),
                                        "ia_access_charge": round(est.access_charge, 2),
                                        "basis": (
                                            "cold_gb = Standard - bytes accessed in window; "
                                            "net = cold_gb x (Standard-IA) - accessed x IA-access rate"
                                        ),
                                    },
                                }
                            )
                        else:
                            advisory.append(
                                {
                                    "FileSystemId": fs_id, "Name": name, "SizeGB": round(total_gb, 2),
                                    "StorageClass": std_class, "HasIAPolicy": False,
                                    "CheckCategory": "EFS No Lifecycle", "Counted": False,
                                    "Recommendation": "Enable IA lifecycle policy for infrequently accessed data",
                                    "EstimatedSavings": (
                                        f"not cost-effective: {est.cold_gb:.0f} GB cold but net "
                                        f"${est.net_savings:.2f}/month after IA access charges "
                                        f"(over {EFS_METRIC_WINDOW_DAYS}d)"
                                    ),
                                    "AuditBasis": {
                                        "metric": f"DataReadIOBytes+DataWriteIOBytes over {EFS_METRIC_WINDOW_DAYS}d",
                                        "region": region, "standard_gb": round(standard_gb, 2),
                                        "monthly_access_gb": round(monthly_access_gb, 2),
                                        "cold_gb": round(est.cold_gb, 2),
                                        "net_savings": round(est.net_savings, 2),
                                        "basis": "net <= $0 after IA access charges; not counted",
                                    },
                                }
                            )
                    else:
                        # No evidence (fast mode, no CloudWatch client, or no
                        # datapoints): indicative GROSS only, never counted.
                        gross = efs_lifecycle_savings(standard_gb, std_rate, ia_rate)
                        if gross > 0:
                            advisory.append(
                                {
                                    "FileSystemId": fs_id, "Name": name, "SizeGB": round(total_gb, 2),
                                    "StorageClass": std_class, "HasIAPolicy": False,
                                    "CheckCategory": "EFS No Lifecycle", "Counted": False,
                                    "Recommendation": "Enable IA lifecycle policy for infrequently accessed data",
                                    "EstimatedSavings": (
                                        f"up to ~${gross:.2f}/mo-gross before IA read-access charges "
                                        f"(net depends on access patterns; enable CloudWatch metrics to quantify)"
                                    ),
                                    "AuditBasis": {
                                        "metric": "measured Standard-class bytes x (Standard-IA rate) x transition fraction",
                                        "region": region, "standard_gb": round(standard_gb, 2),
                                        "standard_rate_per_gb_month": round(std_rate, 6),
                                        "ia_rate_per_gb_month": round(ia_rate, 6),
                                        "transition_fraction": EFS_IA_TRANSITION_FRACTION,
                                        "basis": (
                                            "indicative gross only; assumes ~50% of Standard data is infrequently "
                                            "accessed and does NOT subtract the $0.01/GB IA access charge — "
                                            "not counted toward savings"
                                        ),
                                    },
                                }
                            )

                # One Zone migration — a DURABILITY tradeoff (single AZ), so it is
                # advisory rather than a counted saving even though the price delta
                # is deterministic. The estimate is shown for context.
                if not is_one_zone and total_gb >= EFS_ONE_ZONE_MIN_GB and mount_targets > 0:
                    oz_rate = _efs_rate(ctx, "One Zone", pricing_multiplier)
                    oz_savings = efs_one_zone_savings(total_gb, std_rate, oz_rate)
                    if oz_savings > 0:
                        advisory.append(
                            {
                                "FileSystemId": fs_id, "Name": name, "SizeGB": round(total_gb, 2),
                                "CheckCategory": "EFS One Zone Migration", "Counted": False,
                                "Recommendation": "Migrate to One Zone storage if Multi-AZ resilience is not required",
                                "EstimatedSavings": f"~${oz_savings:.2f}/mo-equiv if migrated (durability tradeoff)",
                            }
                        )

                # Advisory: archive (no $ without access-pattern data) & provisioned throughput.
                if not is_one_zone and not has_archive_policy and total_gb > 10:
                    advisory.append(
                        {
                            "FileSystemId": fs_id, "Name": name, "SizeGB": round(total_gb, 2),
                            "CheckCategory": "EFS Archive Storage Missing", "Counted": False,
                            "Recommendation": "Enable Archive storage class for rarely accessed data",
                            "EstimatedSavings": "Depends on cold-data share (no access-pattern data)",
                        }
                    )
                if str(fs.get("ThroughputMode", "")).lower() == "provisioned":
                    advisory.append(
                        {
                            "FileSystemId": fs_id, "Name": name, "CheckCategory": "EFS Throughput Optimization",
                            "Counted": False,
                            "Recommendation": "Switch from Provisioned to Elastic Throughput to pay for actual usage",
                            "EstimatedSavings": "Depends on provisioned-vs-used throughput (no metric data)",
                        }
                    )
    except Exception as e:
        _report_aws_error(ctx, e, "Could not analyze EFS file systems", "efs", "elasticfilesystem:DescribeFileSystems")
    return {"counted": counted, "advisory": advisory}


# ── FSx findings ─────────────────────────────────────────────────────────────


def get_fsx_findings(ctx: ScanContext, pricing_multiplier: float) -> dict[str, list[dict[str, Any]]]:
    """Return ``{"counted": [...], "advisory": [...]}`` for FSx file systems & caches."""
    counted: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    try:
        fsx = ctx.client("fsx")
        region = getattr(ctx, "region", "")
        fs_paginator = fsx.get_paginator("describe_file_systems")
        file_systems: list[dict[str, Any]] = []
        for page in fs_paginator.paginate():
            file_systems.extend(page.get("FileSystems", []))

        for fs in file_systems:
            fs_id = fs["FileSystemId"]
            fs_type = str(fs.get("FileSystemType", "")).upper()
            capacity = fs.get("StorageCapacity", 0)
            storage_type = str(fs.get("StorageType", "SSD")).upper()
            if fs.get("Lifecycle", "") not in ("AVAILABLE", ""):
                continue
            deployment = _fsx_deployment_option(fs)

            # Counted: SSD -> HDD storage swap (deterministic price delta).
            # Windows ONLY: Single-AZ ($0.130->$0.013) and Multi-AZ ($0.230->
            # $0.025) are clean, like-for-like, region-correct SKUs. ONTAP has no
            # HDD; Lustre HDD is Persistent-only at a different throughput tier
            # (handled as advisory) — neither yields a defensible counted delta.
            if storage_type == "SSD" and fs_type in _FSX_HDD_COUNTED_ELIGIBLE and capacity >= FSX_SSD_TO_HDD_MIN_GB:
                ssd_rate = _fsx_rate(ctx, fs_type, "SSD", deployment, pricing_multiplier)
                hdd_rate = _fsx_rate(ctx, fs_type, "HDD", deployment, pricing_multiplier)
                savings = fsx_ssd_to_hdd_savings(capacity, ssd_rate, hdd_rate)
                if savings > 0:
                    counted.append(
                        {
                            "FileSystemId": fs_id, "FileSystemType": fs_type, "StorageCapacity": capacity,
                            "StorageType": storage_type, "CheckCategory": "FSx Storage Type Optimization",
                            "Recommendation": f"Switch {fs_type} storage from SSD to HDD ({capacity} GB)",
                            "EstimatedSavings": f"${savings:.2f}/month", "_savings": savings, "Counted": True,
                            "AuditBasis": {
                                "metric": "SSD->HDD $/GB-mo delta", "region": region, "capacity_gb": capacity,
                                "ssd_rate_per_gb_month": round(ssd_rate, 6), "hdd_rate_per_gb_month": round(hdd_rate, 6),
                                "deployment": deployment,
                                "basis": "capacity x (SSD - HDD) rate; HDD trades throughput/latency for cost",
                            },
                        }
                    )

            # Advisory FSx opportunities (no account-specific $ without usage/backup data).
            # Lustre SSD: HDD exists only on Persistent at a different throughput
            # tier, and Scratch suits transient workloads — surfaced as advisory
            # because there is no like-for-like, deterministic price delta.
            if fs_type == "LUSTRE" and storage_type == "SSD":
                advisory.append(_fsx_advisory(
                    fs_id, fs_type, capacity, "FSx Lustre Storage Optimization",
                    "Consider Persistent HDD for throughput-insensitive data, or Scratch for transient workloads",
                ))
            if fs_type in ("LUSTRE", "OPENZFS") and storage_type not in ("INT", "INTELLIGENT_TIERING"):
                advisory.append(_fsx_advisory(fs_id, fs_type, capacity, "FSx Intelligent-Tiering",
                                               "Enable Intelligent-Tiering for automatically tiered cold data"))
            if fs_type == "WINDOWS":
                advisory.append(_fsx_advisory(fs_id, fs_type, capacity, "FSx Data Deduplication",
                                               "Enable Microsoft Data Deduplication to reduce stored bytes"))
                if "MULTI" in str(fs.get("WindowsConfiguration", {}).get("DeploymentType", "")).upper():
                    advisory.append(_fsx_advisory(fs_id, fs_type, capacity, "FSx Single-AZ Migration",
                                                   "Use Single-AZ for non-production workloads"))
            if fs_type == "ONTAP":
                # Capacity-pool tiering moves cold data from SSD (~$0.125/GB-mo)
                # to the capacity pool (~$0.0219/GB-mo). Quantifying it requires
                # per-volume cold-byte data (DescribeVolumes/SVMs), which this
                # adapter does not read — so it stays advisory (known limitation).
                advisory.append(_fsx_advisory(fs_id, fs_type, capacity, "FSx ONTAP Data Efficiency",
                                               "Enable deduplication/compression and capacity-pool tiering for cold data"))

            backup_cfg = fs.get("WindowsConfiguration", {}) or fs.get("OntapConfiguration", {}) or {}
            retention = backup_cfg.get("AutomaticBackupRetentionDays", 0)
            if retention and retention > EXCESSIVE_BACKUP_RETENTION_DAYS:
                advisory.append(
                    {
                        "FileSystemId": fs_id, "FileSystemType": fs_type, "CheckCategory": "FSx Backup Retention",
                        "Counted": False, "RetentionDays": retention,
                        "Recommendation": f"Reduce automatic backup retention from {retention} to 7-30 days",
                        "EstimatedSavings": "Depends on backup storage size (not reported by API)",
                    }
                )

        try:
            for cache in _list_file_caches(fsx):
                advisory.append(
                    {
                        "FileCacheId": cache["FileCacheId"], "FileSystemType": "FILE_CACHE",
                        "StorageCapacity": cache.get("StorageCapacity", 0), "CheckCategory": "FSx File Cache",
                        "Counted": False,
                        "Recommendation": "Review File Cache sizing, eviction, and quotas against usage",
                        "EstimatedSavings": "Depends on cache hit rate / utilization (no metric data)",
                    }
                )
        except Exception as e:
            _report_aws_error(ctx, e, "Could not list FSx file caches", "fsx", "fsx:DescribeFileCaches")
    except Exception as e:
        _report_aws_error(ctx, e, "Could not analyze FSx file systems", "fsx", "fsx:DescribeFileSystems")
    return {"counted": counted, "advisory": advisory}


def _fsx_advisory(fs_id: str, fs_type: str, capacity: int, category: str, recommendation: str) -> dict[str, Any]:
    return {
        "FileSystemId": fs_id, "FileSystemType": fs_type, "StorageCapacity": capacity,
        "CheckCategory": category, "Counted": False,
        "Recommendation": recommendation, "EstimatedSavings": "Best-practice (requires usage data to quantify)",
    }
