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

from typing import Any

from core.scan_context import ScanContext
from services.file_systems_logic import (
    EFS_IA_TRANSITION_FRACTION,
    EFS_MIN_LIFECYCLE_GB,
    EFS_ONE_ZONE_MIN_GB,
    FSX_SSD_TO_HDD_MIN_GB,
    efs_idle_savings,
    efs_lifecycle_savings,
    efs_one_zone_savings,
    fsx_ssd_to_hdd_savings,
)

SMALL_EFS_SIZE_GB: float = 0.1
EXCESSIVE_BACKUP_RETENTION_DAYS: int = 30
# FSx file-system types that support an HDD storage tier (OpenZFS does not).
_FSX_HDD_ELIGIBLE: frozenset[str] = frozenset({"WINDOWS", "LUSTRE", "ONTAP"})

FILE_SYSTEM_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "efs_lifecycle_policies": {
        "title": "Configure EFS Lifecycle Policies",
        "description": "Automatically move infrequently accessed files to IA storage to cut storage cost on cold data.",
        "action": "1. Enable Transition to IA after 30 days\n2. Configure Transition back to Standard on access\n3. Savings shown are based on measured Standard-class bytes.",
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


def _fsx_rate(ctx: ScanContext, fs_type: str, storage_type: str, deployment: str, pricing_multiplier: float) -> float:
    """Region-correct FSx $/GB-month for a (type, storage, deployment)."""
    if ctx.pricing_engine is not None:
        return ctx.pricing_engine.get_fsx_storage_price_per_gb(fs_type, storage_type, deployment)
    from core.pricing_engine import FALLBACK_FSX_GB_MONTH

    key = (fs_type.upper(), storage_type.upper())
    rate = FALLBACK_FSX_GB_MONTH.get(key) or FALLBACK_FSX_GB_MONTH.get((fs_type.upper(), "SSD"), 0.15)
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
        ctx.warn(f"Could not get EFS file system count: {e}", "efs")
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
            caches = fsx.describe_file_caches().get("FileCaches", [])
        except Exception:
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
        ctx.warn(f"Could not get FSx file system count: {e}", "fsx")
        return dict(empty)


def get_file_system_optimization_descriptions() -> dict[str, dict[str, str]]:
    return FILE_SYSTEM_OPTIMIZATION_DESCRIPTIONS


# ── EFS findings ─────────────────────────────────────────────────────────────


def get_efs_findings(ctx: ScanContext, pricing_multiplier: float) -> dict[str, list[dict[str, Any]]]:
    """Return ``{"counted": [...], "advisory": [...]}`` for EFS file systems.

    Counted savings use the storage-class breakdown DescribeFileSystems already
    reports (``SizeInBytes.ValueInStandard`` / ``ValueInIA``) plus the live
    per-class rate, so every dollar is anchored to measured data.
    """
    counted: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    try:
        efs = ctx.client("efs")
        paginator = efs.get_paginator("describe_file_systems")
        region = getattr(ctx, "region", "")

        for page in paginator.paginate():
            for fs in page["FileSystems"]:
                fs_id = fs["FileSystemId"]
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
                    ctx.warn(f"Could not read EFS lifecycle config for {fs_id}: {e}", "efs")
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
                elif not has_ia_policy and standard_gb >= EFS_MIN_LIFECYCLE_GB:
                    savings = efs_lifecycle_savings(standard_gb, std_rate, ia_rate)
                    counted.append(
                        {
                            "FileSystemId": fs_id, "Name": name, "SizeGB": round(total_gb, 2),
                            "StorageClass": std_class, "HasIAPolicy": False,
                            "CheckCategory": "EFS No Lifecycle",
                            "Recommendation": "Enable IA lifecycle policy for infrequently accessed data",
                            "EstimatedSavings": f"${savings:.2f}/month", "_savings": savings, "Counted": True,
                            "AuditBasis": {
                                "metric": "measured Standard-class bytes x (Standard-IA rate) x transition fraction",
                                "region": region, "standard_gb": round(standard_gb, 2),
                                "standard_rate_per_gb_month": round(std_rate, 6),
                                "ia_rate_per_gb_month": round(ia_rate, 6),
                                "transition_fraction": EFS_IA_TRANSITION_FRACTION,
                                "basis": "assumes ~50% of Standard data is infrequently accessed; actual depends on access patterns",
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
                                "EstimatedSavings": f"~${oz_savings:.2f}/month if migrated (durability tradeoff)",
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
        ctx.warn(f"Could not analyze EFS file systems: {e}", "efs")
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
            if storage_type == "SSD" and fs_type in _FSX_HDD_ELIGIBLE and capacity >= FSX_SSD_TO_HDD_MIN_GB:
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
                advisory.append(_fsx_advisory(fs_id, fs_type, capacity, "FSx ONTAP Data Efficiency",
                                               "Enable deduplication/compression and capacity-pool tiering"))

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
            for cache in fsx.describe_file_caches().get("FileCaches", []):
                advisory.append(
                    {
                        "FileCacheId": cache["FileCacheId"], "FileSystemType": "FILE_CACHE",
                        "StorageCapacity": cache.get("StorageCapacity", 0), "CheckCategory": "FSx File Cache",
                        "Counted": False,
                        "Recommendation": "Review File Cache sizing, eviction, and quotas against usage",
                        "EstimatedSavings": "Depends on cache hit rate / utilization (no metric data)",
                    }
                )
        except Exception:
            pass
    except Exception as e:
        ctx.warn(f"Could not analyze FSx file systems: {e}", "fsx")
    return {"counted": counted, "advisory": advisory}


def _fsx_advisory(fs_id: str, fs_type: str, capacity: int, category: str, recommendation: str) -> dict[str, Any]:
    return {
        "FileSystemId": fs_id, "FileSystemType": fs_type, "StorageCapacity": capacity,
        "CheckCategory": category, "Counted": False,
        "Recommendation": recommendation, "EstimatedSavings": "Best-practice (requires usage data to quantify)",
    }
