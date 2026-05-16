# ruff: noqa: E501
"""EFS and FSx cost optimization checks.

Extracted from CostOptimizer EFS/FSx methods as free functions.
This module will later become EfsFsxModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

SMALL_EFS_SIZE_GB: float = 0.1
LARGE_EFS_SIZE_GB: float = 10.0
LARGE_FSX_CAPACITY_GB: int = 100
EXCESSIVE_BACKUP_RETENTION_DAYS: int = 30

EFS_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "lifecycle_policies": {
        "title": "Configure EFS Lifecycle Policies",
        "description": "Automatically move infrequently accessed files to IA (up to 94% cost savings) and Archive storage classes.",
        "action": "1. Enable Transition to IA after 30 days\n2. Enable Transition to Archive after 90 days\n3. Configure Transition back to Standard on access\n4. Estimated savings: 80-94% for infrequent data",
    },
    "unused_file_systems": {
        "title": "Delete Unused EFS File Systems",
        "description": "Remove EFS file systems with no mount targets and minimal data to eliminate unnecessary costs.",
        "action": "1. Verify no applications are using the file system\n2. Create backup if data recovery needed\n3. Delete unused file systems via console or CLI\n4. Estimated savings: 100% of file system costs",
    },
    "one_zone_migration": {
        "title": "Migrate to EFS One Zone Storage",
        "description": "For workloads that don't require Multi-AZ resilience, One Zone storage offers 47% cost savings.",
        "action": "1. Assess availability requirements\n2. Create new One Zone file system\n3. Migrate data using AWS DataSync\n4. Estimated savings: 47% vs Regional storage",
    },
    "storage_class_optimization": {
        "title": "Optimize EFS Storage Classes",
        "description": "Use appropriate storage classes based on access patterns: Standard, IA, or Archive.",
        "action": "1. Analyze file access patterns\n2. Configure lifecycle policies for automatic transitions\n3. Use EFS Intelligent-Tiering for automatic optimization\n4. Estimated savings: Up to 94% for cold data",
    },
    "throughput_optimization": {
        "title": "Optimize EFS Throughput Mode",
        "description": "Switch from Provisioned to Elastic Throughput mode to pay only for actual usage.",
        "action": "1. Monitor current throughput usage patterns\n2. Switch to Elastic Throughput mode\n3. Remove unnecessary provisioned throughput\n4. Estimated savings: 20-50% on throughput costs",
    },
}

FILE_SYSTEM_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "efs_lifecycle_policies": {
        "title": "Configure EFS Lifecycle Policies",
        "description": "Automatically move infrequently accessed files to IA (up to 94% cost savings) and Archive storage classes.",
        "action": "1. Enable Transition to IA after 30 days\n2. Enable Transition to Archive after 90 days\n3. Configure Transition back to Standard on access\n4. Estimated savings: 80-94% for infrequent data",
    },
    "efs_unused_systems": {
        "title": "Delete Unused EFS File Systems",
        "description": "Remove EFS file systems with no mount targets and minimal data to eliminate unnecessary costs.",
        "action": "1. Verify no applications are using the file system\n2. Create backup if data recovery needed\n3. Delete unused file systems via console or CLI\n4. Estimated savings: 100% of file system costs",
    },
    "efs_one_zone_migration": {
        "title": "Migrate to EFS One Zone Storage",
        "description": "For workloads that don't require Multi-AZ resilience, One Zone storage offers 47% cost savings.",
        "action": "1. Assess availability requirements\n2. Create new One Zone file system\n3. Migrate data using AWS DataSync\n4. Estimated savings: 47% vs Regional storage",
    },
    "fsx_storage_optimization": {
        "title": "Optimize FSx Storage Types",
        "description": "Choose appropriate storage types: SSD for performance, HDD for capacity, Intelligent-Tiering for automatic optimization.",
        "action": "1. Analyze performance requirements\n2. Switch to HDD for large, less critical workloads\n3. Use Intelligent-Tiering for FSx OpenZFS\n4. Estimated savings: 60-75% with HDD storage",
    },
    "fsx_capacity_rightsizing": {
        "title": "Rightsize FSx File System Capacity",
        "description": "Optimize storage capacity based on actual usage patterns and consolidate small file systems.",
        "action": "1. Monitor storage utilization metrics\n2. Consolidate small file systems\n3. Reduce over-provisioned capacity\n4. Estimated savings: 20-40% through rightsizing",
    },
    "fsx_ontap_features": {
        "title": "Enable FSx ONTAP Data Efficiency",
        "description": "Use deduplication, compression, and capacity pool tiers to reduce storage costs significantly.",
        "action": "1. Enable data deduplication and compression\n2. Configure capacity pool for cold data\n3. Use SnapMirror for efficient replication\n4. Estimated savings: 30-70% through data efficiency",
    },
    "fsx_lustre_optimization": {
        "title": "Optimize FSx Lustre Configuration",
        "description": "Use scratch file systems for temporary workloads and enable data compression.",
        "action": "1. Use scratch file systems for temporary data\n2. Enable LZ4 data compression\n3. Optimize metadata configuration\n4. Estimated savings: 40-60% for temporary workloads",
    },
    "file_cache_optimization": {
        "title": "Optimize Amazon File Cache Usage",
        "description": "Configure cache eviction policies, storage quotas, and monitor usage patterns to optimize costs.",
        "action": "1. Enable automatic cache eviction\n2. Set user and group storage quotas\n3. Monitor cache hit rates via CloudWatch\n4. Adjust capacity based on usage patterns\n5. Estimated savings: 20-40% through better utilization",
    },
}


def _estimate_efs_cost(size_gb: float, pricing_multiplier: float, is_one_zone: bool = False) -> float:
    if is_one_zone:
        standard_price = 0.16
        ia_price = 0.0133
    else:
        standard_price = 0.30
        ia_price = 0.025

    base_cost = (size_gb * 0.2 * standard_price) + (size_gb * 0.8 * ia_price)
    return base_cost * pricing_multiplier


def _estimate_fsx_cost(fs_type: str, capacity_gb: int, storage_type: str, pricing_multiplier: float) -> float:
    pricing: dict[str, dict[str, float]] = {
        "LUSTRE": {"SSD": 0.154, "HDD": 0.025},
        "WINDOWS": {"SSD": 0.13, "HDD": 0.08},
        "ONTAP": {"SSD": 0.144, "HDD": 0.05},
        "OPENZFS": {"SSD": 0.20, "INTELLIGENT_TIERING": 0.10},
    }

    fs_pricing = pricing.get(fs_type.upper(), {"SSD": 0.15})
    storage_price = fs_pricing.get(storage_type, fs_pricing.get("SSD", 0.15))

    return capacity_gb * storage_price * pricing_multiplier


def _estimate_file_cache_cost(capacity_gb: int, pricing_multiplier: float) -> float:
    cache_price_per_gb = 0.30
    return capacity_gb * cache_price_per_gb * pricing_multiplier


def _get_fsx_optimization_opportunities(fs: dict[str, Any]) -> list[str]:
    opportunities: list[str] = []
    fs_type = fs.get("FileSystemType", "").upper()
    storage_type = fs.get("StorageType", "")
    capacity = fs.get("StorageCapacity", 0)

    if fs_type in ["LUSTRE", "WINDOWS"] and storage_type == "SSD" and capacity > 500:
        opportunities.append("Consider HDD storage for large, less performance-critical workloads")

    if fs_type == "OPENZFS" and storage_type == "SSD":
        opportunities.append("Consider Intelligent-Tiering for automatic cost optimization")

    if capacity < 100:
        opportunities.append("Small file system - consider consolidation or deletion if unused")

    if fs_type == "ONTAP":
        opportunities.append("Enable data deduplication and compression for cost savings")
        opportunities.append("Use capacity pool tier for infrequently accessed data")

    if fs_type == "LUSTRE":
        opportunities.append("Consider scratch file systems for temporary workloads")
        opportunities.append("Use data compression to reduce storage requirements")

    return opportunities


def _get_file_cache_optimization_opportunities(cache: dict[str, Any]) -> list[str]:
    opportunities: list[str] = []
    capacity = cache.get("StorageCapacity", 0)

    if capacity < 2400:
        opportunities.append("Small cache size - consider consolidation or deletion if unused")

    opportunities.append("Enable automatic cache eviction to optimize storage usage")
    opportunities.append("Set storage quotas for users and groups to control costs")
    opportunities.append("Monitor cache hit rates and adjust capacity based on usage patterns")
    opportunities.append("Consider using linked data repositories to reduce cache storage needs")

    return opportunities


def get_efs_file_system_count(ctx: ScanContext) -> dict[str, Any]:
    try:
        efs = ctx.client("efs")
        paginator = efs.get_paginator("describe_file_systems")
        counts: dict[str, Any] = {
            "total": 0,
            "available": 0,
            "creating": 0,
            "deleting": 0,
            "standard_storage": 0,
            "one_zone_storage": 0,
            "total_size_gb": 0,
            "unused_systems": [],
        }

        for page in paginator.paginate():
            for fs in page["FileSystems"]:
                counts["total"] += 1
                state = fs.get("LifeCycleState", "")
                if state == "available":
                    counts["available"] += 1
                elif state == "creating":
                    counts["creating"] += 1
                elif state == "deleting":
                    counts["deleting"] += 1

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
                            "CreationTime": fs["CreationTime"].isoformat(),
                            "MountTargets": fs.get("NumberOfMountTargets", 0),
                        }
                    )

        counts["total_size_gb"] = round(counts["total_size_gb"], 2)
        return counts
    except Exception as e:
        ctx.warn(f"Could not get EFS file system count: {e}", "efs")
        return {
            "total": 0,
            "available": 0,
            "creating": 0,
            "deleting": 0,
            "standard_storage": 0,
            "one_zone_storage": 0,
            "total_size_gb": 0,
            "unused_systems": [],
        }


def get_efs_lifecycle_analysis(ctx: ScanContext, pricing_multiplier: float) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    try:
        efs = ctx.client("efs")
        paginator = efs.get_paginator("describe_file_systems")

        for page in paginator.paginate():
            for fs in page["FileSystems"]:
                fs_id = fs["FileSystemId"]

                try:
                    lifecycle_response = efs.describe_lifecycle_configuration(FileSystemId=fs_id)
                    lifecycle_policies = lifecycle_response.get("LifecyclePolicies", [])

                    availability_zone_name = fs.get("AvailabilityZoneName")
                    is_one_zone = availability_zone_name is not None

                    has_ia_policy = any(p.get("TransitionToIA") for p in lifecycle_policies)
                    has_archive_policy = (
                        any(p.get("TransitionToArchive") for p in lifecycle_policies) if not is_one_zone else False
                    )

                    size_bytes = fs.get("SizeInBytes", {}).get("Value", 0)
                    size_gb = size_bytes / (1024**3) if size_bytes else 0

                    recommendation = {
                        "FileSystemId": fs_id,
                        "Name": fs.get("Name", "Unnamed"),
                        "SizeGB": round(size_gb, 2),
                        "HasIAPolicy": has_ia_policy,
                        "HasArchivePolicy": has_archive_policy,
                        "MountTargets": fs.get("NumberOfMountTargets", 0),
                        "StorageClass": "One Zone" if fs.get("AvailabilityZoneName") else "Standard",
                        "EstimatedMonthlyCost": _estimate_efs_cost(
                            size_gb, pricing_multiplier, fs.get("AvailabilityZoneName") is not None
                        ),
                    }

                    recommendations.append(recommendation)

                except Exception as e:
                    ctx.warn(f"Could not get lifecycle config for {fs_id}: {e}", "efs")

    except Exception as e:
        ctx.warn(f"Could not analyze EFS lifecycle policies: {e}", "efs")

    return recommendations


def get_efs_optimization_descriptions() -> dict[str, dict[str, str]]:
    return EFS_OPTIMIZATION_DESCRIPTIONS


def get_fsx_file_system_count(ctx: ScanContext) -> dict[str, Any]:
    try:
        fsx = ctx.client("fsx")
        # Paginate to avoid silently dropping pages beyond the default ceiling.
        fs_paginator = fsx.get_paginator("describe_file_systems")
        _file_systems_pages: list[dict[str, Any]] = []
        for _page in fs_paginator.paginate():
            _file_systems_pages.append({"FileSystems": _page.get("FileSystems", [])})
        fs_response = {"FileSystems": [fs for p in _file_systems_pages for fs in p["FileSystems"]]}

        cache_response = fsx.describe_file_caches()

        counts: dict[str, Any] = {
            "total": len(fs_response["FileSystems"]) + len(cache_response.get("FileCaches", [])),
            "available": 0,
            "creating": 0,
            "deleting": 0,
            "lustre": 0,
            "windows": 0,
            "ontap": 0,
            "openzfs": 0,
            "file_cache": len(cache_response.get("FileCaches", [])),
            "total_capacity_gb": 0,
            "underutilized_systems": [],
        }

        for fs in fs_response["FileSystems"]:
            state = fs.get("Lifecycle", "")
            if state == "AVAILABLE":
                counts["available"] += 1
            elif state == "CREATING":
                counts["creating"] += 1
            elif state == "DELETING":
                counts["deleting"] += 1

            fs_type = fs.get("FileSystemType", "").lower()
            if fs_type == "lustre":
                counts["lustre"] += 1
            elif fs_type == "windows":
                counts["windows"] += 1
            elif fs_type == "ontap":
                counts["ontap"] += 1
            elif fs_type == "openzfs":
                counts["openzfs"] += 1

            capacity_gb = fs.get("StorageCapacity", 0)
            counts["total_capacity_gb"] += capacity_gb

            if capacity_gb > 0 and capacity_gb < 100:
                counts["underutilized_systems"].append(
                    {
                        "FileSystemId": fs["FileSystemId"],
                        "FileSystemType": fs.get("FileSystemType", "Unknown"),
                        "StorageCapacity": capacity_gb,
                        "CreationTime": fs["CreationTime"].isoformat(),
                        "Lifecycle": fs.get("Lifecycle", "Unknown"),
                    }
                )

        for cache in cache_response.get("FileCaches", []):
            state = cache.get("Lifecycle", "")
            if state == "AVAILABLE":
                counts["available"] += 1
            elif state == "CREATING":
                counts["creating"] += 1
            elif state == "DELETING":
                counts["deleting"] += 1

            capacity_gb = cache.get("StorageCapacity", 0)
            counts["total_capacity_gb"] += capacity_gb

            if capacity_gb > 0 and capacity_gb < 1200:
                counts["underutilized_systems"].append(
                    {
                        "FileCacheId": cache["FileCacheId"],
                        "FileSystemType": "FILE_CACHE",
                        "StorageCapacity": capacity_gb,
                        "CreationTime": cache["CreationTime"].isoformat(),
                        "Lifecycle": cache.get("Lifecycle", "Unknown"),
                    }
                )

        return counts
    except Exception as e:
        ctx.warn(f"Could not get FSx file system count: {e}", "fsx")
        return {
            "total": 0,
            "available": 0,
            "creating": 0,
            "deleting": 0,
            "lustre": 0,
            "windows": 0,
            "ontap": 0,
            "openzfs": 0,
            "file_cache": 0,
            "total_capacity_gb": 0,
            "underutilized_systems": [],
        }


def get_fsx_optimization_analysis(ctx: ScanContext, pricing_multiplier: float) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    try:
        fsx = ctx.client("fsx")
        # Paginate to avoid silently dropping pages beyond the default ceiling.
        fs_paginator = fsx.get_paginator("describe_file_systems")
        _file_systems_pages: list[dict[str, Any]] = []
        for _page in fs_paginator.paginate():
            _file_systems_pages.append({"FileSystems": _page.get("FileSystems", [])})
        fs_response = {"FileSystems": [fs for p in _file_systems_pages for fs in p["FileSystems"]]}

        for fs in fs_response["FileSystems"]:
            fs_id = fs["FileSystemId"]
            fs_type = fs.get("FileSystemType", "Unknown")
            capacity_gb = fs.get("StorageCapacity", 0)
            storage_type = fs.get("StorageType", "Unknown")

            recommendation = {
                "FileSystemId": fs_id,
                "FileSystemType": fs_type,
                "StorageCapacity": capacity_gb,
                "StorageType": storage_type,
                "Lifecycle": fs.get("Lifecycle", "Unknown"),
                "CreationTime": fs["CreationTime"].isoformat(),
                "EstimatedMonthlyCost": _estimate_fsx_cost(fs_type, capacity_gb, storage_type, pricing_multiplier),
                "OptimizationOpportunities": _get_fsx_optimization_opportunities(fs),
            }

            recommendations.append(recommendation)

        cache_response = fsx.describe_file_caches()

        for cache in cache_response.get("FileCaches", []):
            cache_id = cache["FileCacheId"]
            capacity_gb = cache.get("StorageCapacity", 0)

            recommendation = {
                "FileCacheId": cache_id,
                "FileSystemType": "FILE_CACHE",
                "StorageCapacity": capacity_gb,
                "StorageType": "SSD",
                "Lifecycle": cache.get("Lifecycle", "Unknown"),
                "CreationTime": cache["CreationTime"].isoformat(),
                "EstimatedMonthlyCost": _estimate_file_cache_cost(capacity_gb, pricing_multiplier),
                "OptimizationOpportunities": _get_file_cache_optimization_opportunities(cache),
            }

            recommendations.append(recommendation)

    except Exception as e:
        ctx.warn(f"Could not analyze FSx file systems: {e}", "fsx")

    return recommendations


def get_file_system_optimization_descriptions() -> dict[str, dict[str, str]]:
    return FILE_SYSTEM_OPTIMIZATION_DESCRIPTIONS


def get_enhanced_efs_fsx_checks(ctx: ScanContext, pricing_multiplier: float) -> dict[str, Any]:
    checks: dict[str, list[dict[str, Any]]] = {
        "efs_archive_storage": [],
        "efs_one_zone_migration": [],
        "efs_idle_systems": [],
        "efs_throughput_optimization": [],
        "fsx_intelligent_tiering": [],
        "fsx_storage_type_optimization": [],
        "fsx_data_deduplication": [],
        "fsx_single_az_migration": [],
        "fsx_backup_retention": [],
        "fsx_idle_systems": [],
    }

    recommendations: list[dict[str, Any]] = []

    try:
        efs = ctx.client("efs")
        paginator = efs.get_paginator("describe_file_systems")
        for page in paginator.paginate():
            for fs in page["FileSystems"]:
                fs_id = fs["FileSystemId"]
                size_bytes = fs.get("SizeInBytes", {}).get("Value", 0)
                size_gb = size_bytes / (1024**3) if size_bytes else 0

                if size_gb < SMALL_EFS_SIZE_GB:
                    continue

                try:
                    lifecycle_response = efs.describe_lifecycle_configuration(FileSystemId=fs_id)
                    lifecycle_policies = lifecycle_response.get("LifecyclePolicies", [])
                    has_archive = any(p.get("TransitionToArchive") for p in lifecycle_policies)
                    _has_ia = any(p.get("TransitionToIA") for p in lifecycle_policies)

                    is_one_zone = fs.get("AvailabilityZoneName") is not None
                    if not has_archive and size_gb > LARGE_EFS_SIZE_GB and not is_one_zone:
                        checks["efs_archive_storage"].append(
                            {
                                "FileSystemId": fs_id,
                                "Name": fs.get("Name", "Unnamed"),
                                "SizeGB": round(size_gb, 2),
                                "Recommendation": "Enable Archive storage class for rarely accessed data",
                                "EstimatedSavings": "Up to 94% for cold data",
                                "CheckCategory": "EFS Archive Storage Missing",
                            }
                        )

                    is_regional = not fs.get("AvailabilityZoneName")
                    if is_regional and size_gb > 1:
                        checks["efs_one_zone_migration"].append(
                            {
                                "FileSystemId": fs_id,
                                "Name": fs.get("Name", "Unnamed"),
                                "SizeGB": round(size_gb, 2),
                                "Recommendation": "Migrate to One Zone storage for non-critical workloads",
                                "EstimatedSavings": "47% cost reduction",
                                "CheckCategory": "EFS One Zone Migration",
                            }
                        )

                    mount_targets = fs.get("NumberOfMountTargets", 0)
                    if mount_targets == 0 or size_gb < 0.01:
                        checks["efs_idle_systems"].append(
                            {
                                "FileSystemId": fs_id,
                                "Name": fs.get("Name", "Unnamed"),
                                "SizeGB": round(size_gb, 2),
                                "MountTargets": mount_targets,
                                "Recommendation": "Delete unused file system",
                                "EstimatedSavings": f"${size_gb * 0.30:.2f}/month",
                                "CheckCategory": "Idle EFS File System",
                            }
                        )

                    throughput_mode = fs.get("ThroughputMode", "bursting")
                    if throughput_mode == "provisioned":
                        checks["efs_throughput_optimization"].append(
                            {
                                "FileSystemId": fs_id,
                                "Name": fs.get("Name", "Unnamed"),
                                "ThroughputMode": throughput_mode,
                                "Recommendation": "Switch to Elastic Throughput mode",
                                "EstimatedSavings": "20-50% on throughput costs",
                                "CheckCategory": "EFS Throughput Optimization",
                            }
                        )
                except Exception as e:
                    ctx.warn(f"Error analyzing EFS throughput: {e}", "efs")
    except Exception as e:
        ctx.warn(f"Could not analyze EFS systems: {e}", "efs")

    try:
        fsx = ctx.client("fsx")
        response = fsx.describe_file_systems()
        for fs in response.get("FileSystems", []):
            fs_id = fs.get("FileSystemId")
            fs_type = fs.get("FileSystemType")
            storage_capacity = fs.get("StorageCapacity", 0)
            lifecycle = fs.get("Lifecycle", "")

            if lifecycle != "AVAILABLE":
                continue

            if fs_type in ["LUSTRE", "OPENZFS"]:
                storage_type = fs.get("StorageType", "")
                if storage_type != "INTELLIGENT_TIERING":
                    checks["fsx_intelligent_tiering"].append(
                        {
                            "FileSystemId": fs_id,
                            "FileSystemType": fs_type,
                            "StorageCapacity": storage_capacity,
                            "Recommendation": "Enable Intelligent-Tiering for automatic cost optimization",
                            "EstimatedSavings": "Significant for infrequently accessed data",
                            "CheckCategory": "FSx Intelligent-Tiering",
                        }
                    )

            if fs_type == "WINDOWS":
                windows_config = fs.get("WindowsConfiguration", {})
                storage_type = windows_config.get("DeploymentType", "")

                if storage_capacity > LARGE_FSX_CAPACITY_GB:
                    checks["fsx_storage_type_optimization"].append(
                        {
                            "FileSystemId": fs_id,
                            "FileSystemType": fs_type,
                            "StorageCapacity": storage_capacity,
                            "Recommendation": "Consider HDD storage for general-purpose workloads",
                            "EstimatedSavings": "~85% storage cost reduction",
                            "CheckCategory": "FSx Storage Type Optimization",
                        }
                    )

                checks["fsx_data_deduplication"].append(
                    {
                        "FileSystemId": fs_id,
                        "FileSystemType": fs_type,
                        "StorageCapacity": storage_capacity,
                        "Recommendation": "Enable Microsoft Data Deduplication",
                        "EstimatedSavings": "30-80% storage capacity reduction",
                        "CheckCategory": "FSx Data Deduplication",
                    }
                )

                deployment_type = windows_config.get("DeploymentType", "")
                if deployment_type == "MULTI_AZ_1":
                    checks["fsx_single_az_migration"].append(
                        {
                            "FileSystemId": fs_id,
                            "FileSystemType": fs_type,
                            "StorageCapacity": storage_capacity,
                            "Recommendation": "Use Single-AZ for non-production workloads",
                            "EstimatedSavings": "~50% cost reduction",
                            "CheckCategory": "FSx Single-AZ Migration",
                        }
                    )

            backup_config = fs.get("WindowsConfiguration", {}) if fs_type == "WINDOWS" else {}
            automatic_backup_retention = backup_config.get("AutomaticBackupRetentionDays", 0)
            if automatic_backup_retention > EXCESSIVE_BACKUP_RETENTION_DAYS:
                checks["fsx_backup_retention"].append(
                    {
                        "FileSystemId": fs_id,
                        "FileSystemType": fs_type,
                        "RetentionDays": automatic_backup_retention,
                        "Recommendation": f"Reduce backup retention from {automatic_backup_retention} to 7-30 days",
                        "EstimatedSavings": "Reduce backup storage costs",
                        "CheckCategory": "FSx Backup Retention",
                    }
                )
    except Exception as e:
        ctx.warn(f"Could not analyze FSx systems: {e}", "fsx")

    for _category, items in checks.items():
        for item in items:
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}
