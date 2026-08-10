"""Athena cost optimization checks.

Extracted from CostOptimizer.get_enhanced_athena_checks() as a free function.
This module will later become AthenaModule (T-321) implementing ServiceModule.

ATH-1 — this module used to seed one rec per ENABLED workgroup carrying an
"Up to 75% scan-cost reduction" string, which the adapter turned into a COUNTED
dollar by multiplying measured scan spend by a flat 0.75. That factor had no
account-specific input and could not have one:

* Athena's API exposes no table metadata. Formats and partition keys live in the
  Glue Data Catalog (``StorageDescriptor.InputFormat``, ``SerdeInfo``,
  ``PartitionKeys``) and nothing here touches Glue, so a workgroup already 100%
  Parquet and partitioned — which has $0 of this saving available — was charged
  the same fraction as a raw-CSV lake.
* Even with Glue there is no attribution path: scanned bytes are measured per
  WORKGROUP while format lives per TABLE, and mapping one to the other means
  parsing the query SQL.
* The ratio is query-shaped, not data-shaped. AWS's own worked example
  decomposes as "3x from compression and 4x for reading only one column" — the
  4x exists only because that query touched one column of four. A ``SELECT *``
  workload gets neither leg.

So the partitioning lever counts nothing. What this module CAN do honestly is
measure what a workgroup actually scans and say so, which is why the recs are
seeded ``Counted=False`` here rather than demoted later.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

ATHENA_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "query_optimization": {
        "title": "Athena Scan Spend",
        "description": (
            "Measured per-workgroup scan spend. Partitioning and columnar formats reduce it, "
            "but the realizable fraction depends on table formats and query shape, neither of "
            "which is visible from the Athena API."
        ),
        "action": "Review the highest-spend workgroups against their table formats in Glue",
    }
}

# ATH-6 — capacity reservations do not exist in these regions, so an
# unsupported-operation error there is EVIDENCE OF ABSENCE, not lost evidence.
# Treating it as lost would classify every workgroup as provisioned-capacity and
# blank the tab in exactly the regions where per-TB billing is guaranteed.
_NO_RESERVATION_ERROR_CODES = frozenset(
    {"InvalidRequestException", "UnsupportedOperationException", "ResourceNotFoundException"}
)


def _reserved_workgroups(ctx: ScanContext, athena: Any) -> tuple[set[str], bool]:
    """Workgroups attached to a capacity reservation, and whether evidence was lost.

    Returns ``(names, evidence_lost)``. ``evidence_lost`` is True only for a
    genuine failure (AccessDenied, throttle) — never for a region that
    structurally has no capacity reservations, and never for a reservation that
    simply has no assignment configuration, which is the idle-reservation shape.
    """
    reserved: set[str] = set()
    try:
        token: str | None = None
        while True:
            params = {"NextToken": token} if token else {}
            resp = athena.list_capacity_reservations(**params)
            for reservation in resp.get("CapacityReservations", []):
                name = reservation.get("Name")
                if not name:
                    continue
                try:
                    cfg = athena.get_capacity_assignment_configuration(
                        CapacityReservationName=name
                    )["CapacityAssignmentConfiguration"]
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code in _NO_RESERVATION_ERROR_CODES:
                        continue  # reservation exists but assigns no workgroup
                    record_aws_error(
                        ctx, exc, service="athena",
                        context="athena:GetCapacityAssignmentConfiguration",
                    )
                    return reserved, True
                for assignment in cfg.get("CapacityAssignments", []):
                    reserved.update(assignment.get("WorkGroupNames", []))
            token = resp.get("NextToken")
            if not token:
                break
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _NO_RESERVATION_ERROR_CODES:
            return reserved, False  # region has no capacity reservations at all
        record_aws_error(ctx, exc, service="athena", context="athena:ListCapacityReservations")
        return reserved, True
    except Exception as exc:
        record_aws_error(ctx, exc, service="athena", context="athena:ListCapacityReservations")
        return reserved, True
    return reserved, False


def get_enhanced_athena_checks(ctx: ScanContext) -> dict[str, Any]:
    """Emit one rec per ENABLED workgroup, seeded as an advisory.

    Each rec carries what the adapter needs to measure scan spend honestly:
    the workgroup name, whether it bills per-TB or on provisioned capacity
    (ATH-2), and whether it publishes the CloudWatch metrics the measurement
    depends on (ATH-6 — ``PublishCloudWatchMetricsEnabled`` is OFF by default
    for API-created workgroups, and ``WorkGroupSummary`` does not carry it, so
    it needs a per-workgroup ``GetWorkGroup``).
    """
    checks: dict[str, list[dict[str, Any]]] = {"workgroup_optimization": []}

    try:
        athena = ctx.client("athena")
        reserved_workgroups, capacity_evidence_lost = _reserved_workgroups(ctx, athena)

        # ListWorkGroups has no botocore paginator (verified: can_paginate is
        # False), so walk NextToken by hand rather than silently taking page one.
        summaries: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params = {"NextToken": token} if token else {}
            response = athena.list_work_groups(**params)
            summaries.extend(response.get("WorkGroups", []))
            token = response.get("NextToken")
            if not token:
                break

        for wg in summaries:
            wg_name = wg.get("Name")
            state = wg.get("State", "ENABLED")
            if not wg_name or state != "ENABLED":
                continue

            rec: dict[str, Any] = {
                "WorkGroup": wg_name,
                "State": state,
                "CheckCategory": "Workgroup Scan Optimization",
                # Advisory by construction — see the module docstring.
                "Counted": False,
                "EstimatedMonthlySavings": 0.0,
            }

            try:
                cfg = athena.get_work_group(WorkGroup=wg_name)["WorkGroup"].get("Configuration", {})
            except Exception as exc:
                record_aws_error(ctx, exc, service="athena", context="athena:GetWorkGroup")
                cfg = None

            if cfg is not None:
                # Never default this to True: an unread flag must not look like
                # "metrics are published".
                rec["PublishesQueryMetrics"] = bool(cfg.get("PublishCloudWatchMetricsEnabled"))
                engine_cfg = cfg.get("EngineVersion", {}) or {}
                selected = str(engine_cfg.get("SelectedEngineVersion", "")).lower()
                effective = str(engine_cfg.get("EffectiveEngineVersion", "")).lower()
                if "pyspark" in selected or "pyspark" in effective:
                    # Spark workgroups bill DPU-hours, not scanned bytes.
                    rec["BillingModel"] = "provisioned-capacity"

            if "BillingModel" not in rec:
                if capacity_evidence_lost or wg_name in reserved_workgroups:
                    rec["BillingModel"] = "provisioned-capacity"
                else:
                    rec["BillingModel"] = "per-tb-scanned"

            checks["workgroup_optimization"].append(rec)
    except Exception as e:
        record_aws_error(ctx, e, service="athena", context="list_work_groups")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, "checks": checks}
