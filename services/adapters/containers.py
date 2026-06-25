"""Multi-source adapter for container services (ECS, EKS, ECR) optimization."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.advisor import get_ecs_compute_optimizer_recommendations
from services.containers import get_container_services_analysis, get_enhanced_container_checks
from services.containers_logic import (
    HOURS_PER_MONTH,
    dedupe_by_authority,
    fargate_task_hourly,
    normalize_resource_name,
    snap_down_fargate,
)

# Spot capacity-provider savings factor (60-70% AWS-published). Applied to the
# rightsized on-demand base when a rec is explicitly a Fargate→Spot move.
SPOT_SAVINGS_FACTOR: float = 0.70


def _co_placeholder(rec: dict[str, Any]) -> bool:
    """True for the synthetic 'enable Compute Optimizer' placeholder rec."""
    return rec.get("ResourceId") == "compute-optimizer-service"


class ContainersModule(BaseServiceModule):
    """ServiceModule adapter for container services (ECS, EKS, ECR). Fargate CPU+memory pricing."""

    key: str = "containers"
    # ECS and ECR are bundled in this adapter; accept the natural service names
    # as aliases so `--scan-only ecs` / `ecr` resolve here (EKS → eks_cost).
    cli_aliases: tuple[str, ...] = ("containers", "ecs", "ecr")
    display_name: str = "Containers"
    # The shim reads CloudWatch / Container Insights for ECS utilization; honor
    # --fast by skipping those per-resource reads (services/containers.py).
    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for container infrastructure scanning."""
        return ("ecs", "eks", "ecr", "compute-optimizer", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan container infrastructure (ECS, ECR) for cost optimization.

        EKS is owned by the dedicated ``eks_cost`` adapter; this adapter counts
        only ECS (Fargate) and ECR. Fargate rightsizing savings are computed
        from the task's real Cpu/Memory snapped to a valid Fargate combo, priced
        per architecture/OS via the live Pricing API. Compute Optimizer and Cost
        Hub savings (region-correct upstream) are deduped against the heuristics
        by normalized service name (authority CoH > CO > heuristic).

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks, cost_optimization_hub, and
            compute_optimizer SourceBlocks plus service_counts extras.
        """

        try:
            container_data = get_container_services_analysis(ctx)
        except Exception as e:
            ctx.warn(f"container services analysis failed: {e}", "containers")
            container_data = {}

        try:
            enhanced_result = get_enhanced_container_checks(ctx)
        except Exception as e:
            ctx.warn(f"enhanced checks failed: {e}", "containers")
            enhanced_result = {}
        enhanced_recs = list(enhanced_result.get("recommendations", []))

        cost_hub_recs = ctx.cost_hub_splits.get("containers", []) if hasattr(ctx, "cost_hub_splits") else []
        co_raw = get_ecs_compute_optimizer_recommendations(ctx)
        # Opt-in placeholder ($0 "enable Compute Optimizer") is an informational
        # signal, not a recommendation — surface it as a warning and drop it so
        # it never inflates the count (mirrors EC2Module).
        if any(_co_placeholder(r) for r in co_raw):
            ctx.warn(
                "AWS Compute Optimizer is not enabled — ECS Fargate task rightsizing "
                "recommendations from Compute Optimizer are unavailable (enable it for "
                "additional savings detection).",
                service="containers",
            )
        co_recs = [r for r in co_raw if not _co_placeholder(r)]

        # Cross-source dedup by normalized service name (authority CoH > CO >
        # heuristic) so one Fargate service never contributes savings twice.
        enhanced_recs = dedupe_by_authority(
            cost_hub_recs,
            co_recs,
            enhanced_recs,
            coh_key=lambda r: r.get("resourceArn") or r.get("ResourceArn") or "",
            co_key=lambda r: r.get("resource_id") or "",
            heuristic_key=_heuristic_resource_key,
        )

        # Quantify each surviving heuristic rec. A rec that quantifies to a real
        # saving is counted; one that quantifies to $0 (already-smallest task,
        # 0 running tasks, ~$0 ECR reclaim) is DROPPED — not shown as $0 noise.
        # Only recs the shim explicitly flags Advisory=True (e.g. an oversized
        # ECR repo it could not analyze) are kept as non-counted advisories.
        savings = 0.0
        kept: list[dict[str, Any]] = []
        for rec in enhanced_recs:
            rec_savings = round(self._quantify_rec(ctx, rec), 2)
            if rec_savings > 0:
                rec["EstimatedMonthlySavings"] = rec_savings
                rec["Counted"] = True
                savings += rec_savings
                kept.append(rec)
            elif rec.get("Advisory"):
                rec["EstimatedMonthlySavings"] = 0.0
                rec["Counted"] = False
                kept.append(rec)
            # else: drop the $0 finding entirely (skip-it, don't render noise)
        enhanced_recs = kept

        # Hand the ECS Fargate rightsizing total to the commitment_analysis
        # adapter (runs later) so its Fargate Savings Plan view can model the
        # commitment against the *rightsized* baseline. Only counted Fargate
        # rightsizing $ — not ECR — reduce the Fargate compute baseline.
        try:
            ctx.fargate_rightsizing_monthly = round(
                sum(
                    float(r.get("EstimatedMonthlySavings", 0) or 0)
                    for r in enhanced_recs
                    if "rightsizing" in str(r.get("CheckCategory", "")).lower()
                    and str(r.get("LaunchType", "")).upper() == "FARGATE"
                ),
                2,
            )
        except Exception:
            ctx.fargate_rightsizing_monthly = 0.0

        savings += sum(float(r.get("estimatedMonthlySavings", 0) or 0) for r in cost_hub_recs)
        savings += sum(float(r.get("estimatedMonthlySavings", 0.0) or 0.0) for r in co_recs)

        total_recs = len(enhanced_recs) + len(cost_hub_recs) + len(co_recs)

        return ServiceFindings(
            service_name="Containers",
            total_recommendations=total_recs,
            total_monthly_savings=round(savings, 2),
            sources={
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
                "cost_optimization_hub": SourceBlock(
                    count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)
                ),
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
            },
            extras={
                "service_counts": {
                    "ecs_clusters": container_data.get("ecs", {}).get("total_clusters", 0),
                    "eks_clusters": container_data.get("eks", {}).get("total_clusters", 0),
                    "ecr_repositories": container_data.get("ecr", {}).get("total_repositories", 0),
                    "ecs_services": container_data.get("ecs", {}).get("total_services", 0),
                }
            },
        )

    def _quantify_rec(self, ctx: Any, rec: dict[str, Any]) -> float:
        """Return the monthly $ saving for one heuristic rec (0 if not quantifiable).

        Only ECS Fargate rightsizing with real task config (Cpu/Memory/TaskCount
        and FARGATE launch type) is quantified. ECR lifecycle, ECS desired-count,
        and EC2-launch-type tasks have no Fargate-priced saving here and return 0
        (the caller marks them advisory).
        """
        category = (rec.get("CheckCategory") or "").lower()

        # ECR lifecycle: price the deduplicated layer storage the shim computed.
        if "ecr" in category or "lifecycle" in category:
            reclaimable = rec.get("ReclaimableBytes")
            if not reclaimable or float(reclaimable) <= 0:
                return 0.0
            try:
                gb_month_rate = ctx.pricing_engine.get_ecr_storage_gb_month()
            except Exception as e:
                ctx.warn(f"ECR storage pricing lookup failed for {rec.get('RepositoryName', '?')}: {e}", "containers")
                return 0.0
            if gb_month_rate <= 0:
                return 0.0
            gib = float(reclaimable) / (1024**3)
            saving = gib * gb_month_rate
            untagged = rec.get("UntaggedCount")
            rec["TargetAction"] = "Expire untagged images via lifecycle policy"
            rec["ReclaimableGiB"] = round(gib, 2)
            rec["Recommendation"] = (
                f"Add a lifecycle policy to expire {untagged} untagged image(s), freeing "
                f"{gib:.2f} GiB of deduplicated layers (${saving:.2f}/mo)"
            )
            rec["AuditBasis"] = {
                "reclaimable_gib": round(gib, 4),
                "rate": gb_month_rate,
                "unit": "USD/GB-month",
                "basis": "deduplicated layers referenced only by untagged images",
                "formula": "reclaimable_GiB x ECR $/GB-month",
            }
            return max(0.0, saving)

        if "rightsizing" not in category or "metric" not in category:
            return 0.0
        if str(rec.get("LaunchType", "")).upper() != "FARGATE":
            return 0.0  # EC2 launch type → cost is EC2 instances (EC2 adapter)

        try:
            cpu_units = float(rec.get("Cpu", 0))
            mem_mb = float(rec.get("Memory", 0))
            task_count = int(rec.get("TaskCount", 0))
        except (TypeError, ValueError):
            return 0.0
        if cpu_units <= 0 or mem_mb <= 0 or task_count <= 0:
            return 0.0

        cur_vcpu = cpu_units / 1024.0
        cur_mem_gb = mem_mb / 1024.0
        target = snap_down_fargate(cur_vcpu, cur_mem_gb)
        if target is None:
            return 0.0  # already smallest Fargate size
        tgt_vcpu, tgt_mem_gb = target

        arch = rec.get("Architecture", "x86")
        os = rec.get("OperatingSystem", "linux")
        try:
            vcpu_rate = ctx.pricing_engine.get_fargate_vcpu_hourly(architecture=arch, os=os)
            gb_rate = ctx.pricing_engine.get_fargate_gb_hourly(architecture=arch, os=os)
            win_os = ctx.pricing_engine.get_fargate_windows_os_hourly() if str(os).lower().startswith("win") else 0.0
        except Exception as e:
            ctx.warn(f"Fargate pricing lookup failed for {rec.get('ServiceName', '?')}: {e}", "containers")
            return 0.0
        if vcpu_rate <= 0 or gb_rate <= 0:
            return 0.0

        cur_hr = fargate_task_hourly(cur_vcpu, cur_mem_gb, vcpu_rate, gb_rate, windows_os_rate=win_os)
        tgt_hr = fargate_task_hourly(tgt_vcpu, tgt_mem_gb, vcpu_rate, gb_rate, windows_os_rate=win_os)
        monthly_saving = (cur_hr - tgt_hr) * HOURS_PER_MONTH * task_count

        # Name the concrete target so the recommendation is actionable: the
        # human-readable size, the exact task-definition values to set, and the
        # measured utilization that justifies the downsize.
        cur_label = _fmt_fargate_size(cur_vcpu, cur_mem_gb)
        tgt_label = _fmt_fargate_size(tgt_vcpu, tgt_mem_gb)
        util = ""
        if rec.get("AvgCPU") and rec.get("AvgMemory"):
            util = f" — measured CPU {rec['AvgCPU']}, Memory {rec['AvgMemory']} over 7d"
        rec["CurrentSize"] = cur_label
        rec["TargetSize"] = tgt_label
        rec["RecommendedConfig"] = {"cpu": int(tgt_vcpu * 1024), "memory": int(tgt_mem_gb * 1024)}
        rec["Recommendation"] = (
            f"Downsize Fargate task {cur_label} → {tgt_label} "
            f"(set cpu={int(tgt_vcpu * 1024)}, memory={int(tgt_mem_gb * 1024)})"
            f" across {task_count} task(s){util}"
        )
        rec["AuditBasis"] = {
            "current": f"{cur_label} x{task_count}",
            "target": tgt_label,
            "vcpu_rate": vcpu_rate,
            "gb_rate": gb_rate,
            "architecture": arch,
            "os": os,
            "launch_type": "FARGATE",
            "formula": "(current_hourly - target_hourly) x 730 x task_count",
        }
        return max(0.0, monthly_saving)


def _fmt_fargate_size(vcpu: float, mem_gb: float) -> str:
    """Human-readable Fargate task size, e.g. '0.5 vCPU / 1 GB' or '0.25 vCPU / 512 MB'."""
    vcpu_s = f"{vcpu:g}"
    mem_s = f"{int(mem_gb * 1024)} MB" if mem_gb < 1 else f"{mem_gb:g} GB"
    return f"{vcpu_s} vCPU / {mem_s}"


def _heuristic_resource_key(rec: dict[str, Any]) -> str:
    """Normalized resource name for an enhanced-check rec, for dedup."""
    for marker in ("ServiceName", "ClusterName", "TaskDefinitionArn", "RepositoryName"):
        val = rec.get(marker)
        if val:
            return normalize_resource_name(str(val))
    return ""
