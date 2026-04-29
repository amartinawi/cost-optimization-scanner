"""AWS Backup cost optimization checks.

Extracted from CostOptimizer.get_backup_checks() as a free function.
This module will later become BackupModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/backup.py] Backup module active")

BACKUP_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "excessive_retention": {
        "title": "Optimize Backup Retention Periods",
        "description": "Retention periods exceeding compliance needs increase storage costs.",
        "action": "Reduce retention to the minimum required by compliance",
    },
    "daily_static_data": {
        "title": "Review Daily Backup Frequency",
        "description": "Daily backups may be excessive for static or infrequently changing data.",
        "action": "Consider weekly or monthly schedules for static data",
    },
}


def get_backup_checks(ctx: ScanContext) -> dict[str, Any]:
    """AWS Backup optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "backup_unused_resources": [],
        "multiple_backup_plans": [],
        "excessive_retention": [],
        "unnecessary_cross_region": [],
        "daily_static_data": [],
        "ephemeral_backups": [],
    }

    try:
        backup = ctx.client("backup")

        paginator = backup.get_paginator("list_backup_plans")
        backup_plans: list[dict[str, Any]] = []
        for page in paginator.paginate():
            backup_plans.extend(page.get("BackupPlansList", []))

        for plan in backup_plans:
            plan_id = plan.get("BackupPlanId")
            plan_name = plan.get("BackupPlanName")

            try:
                plan_response = backup.get_backup_plan(BackupPlanId=plan_id)
                plan_details = plan_response.get("BackupPlan", {})
                rules = plan_details.get("Rules", [])

                for rule in rules:
                    rule_name = rule.get("RuleName")
                    schedule = rule.get("ScheduleExpression", "")
                    lifecycle = rule.get("Lifecycle", {})

                    delete_after_days = lifecycle.get("DeleteAfterDays")
                    if delete_after_days and delete_after_days > 2555:
                        checks["excessive_retention"].append(
                            {
                                "BackupPlanName": plan_name,
                                "RuleName": rule_name,
                                "RetentionDays": delete_after_days,
                                "Recommendation": (
                                    f"Retention period ({delete_after_days} days) may exceed compliance needs"
                                ),
                                "EstimatedSavings": "Reduce retention to lower storage costs",
                                "CheckCategory": "Excessive Backup Retention",
                            }
                        )

                    if "daily" in schedule.lower() or "cron(0 " in schedule:
                        checks["daily_static_data"].append(
                            {
                                "BackupPlanName": plan_name,
                                "RuleName": rule_name,
                                "Schedule": schedule,
                                "Recommendation": "Daily backups - verify if needed for static/infrequent data",
                                "EstimatedSavings": "Weekly/monthly backups can reduce costs by 70-85%",
                                "CheckCategory": "Daily Backup Frequency",
                            }
                        )

                    copy_actions = rule.get("CopyActions", [])
                    if copy_actions:
                        for copy_action in copy_actions:
                            dest_vault_arn = copy_action.get("DestinationBackupVaultArn", "")
                            if dest_vault_arn and ctx.region not in dest_vault_arn:
                                checks["unnecessary_cross_region"].append(
                                    {
                                        "BackupPlanName": plan_name,
                                        "RuleName": rule_name,
                                        "DestinationVault": dest_vault_arn,
                                        "Recommendation": "Cross-region backup copy - verify business need",
                                        "EstimatedSavings": "Remove if not required for DR",
                                        "CheckCategory": "Cross-Region Backup Copies",
                                    }
                                )

                paginator = backup.get_paginator("list_backup_selections")
                selections: list[dict[str, Any]] = []
                for page in paginator.paginate(BackupPlanId=plan_id):
                    selections.extend(page.get("BackupSelectionsList", []))

                for selection in selections:
                    selection_id = selection.get("SelectionId")
                    selection_name = selection.get("SelectionName")

                    try:
                        selection_response = backup.get_backup_selection(BackupPlanId=plan_id, SelectionId=selection_id)
                        selection_details = selection_response.get("BackupSelection", {})
                        resources = selection_details.get("Resources", [])

                        for resource_arn in resources:
                            if any(env in resource_arn.lower() for env in ["dev", "test", "staging"]):
                                checks["ephemeral_backups"].append(
                                    {
                                        "BackupPlanName": plan_name,
                                        "SelectionName": selection_name,
                                        "ResourceArn": resource_arn,
                                        "Recommendation": "Backing up dev/test resources - often unnecessary",
                                        "EstimatedSavings": "Remove ephemeral resource backups",
                                        "CheckCategory": "Ephemeral Resource Backups",
                                    }
                                )

                    except Exception as e:
                        ctx.warn(f"Could not analyze backup selection {selection_name}: {e}", "backup")

            except Exception as e:
                ctx.warn(f"Could not analyze backup plan {plan_name}: {e}", "backup")

        if len(backup_plans) > 3:
            checks["multiple_backup_plans"].append(
                {
                    "BackupPlanCount": len(backup_plans),
                    "PlanNames": [p.get("BackupPlanName") for p in backup_plans],
                    "Recommendation": "Multiple backup plans - check for overlapping coverage",
                    "EstimatedSavings": "Consolidate plans to avoid duplicate backups",
                    "CheckCategory": "Multiple Backup Plans",
                }
            )

    except Exception as e:
        ctx.warn(f"Could not perform AWS Backup checks: {e}", "backup")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}
