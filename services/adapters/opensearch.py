"""Live-priced adapter for OpenSearch (node-price deltas; CoH-authoritative)."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._coh_dedup import coh_key, coh_savings, is_renderable_coh_rec
from services.commitment_coverage import demote_coh_by_commitment, demote_covered_in_place
from services.opensearch import (
    LOW_CPU_THRESHOLD,
    OPENSEARCH_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_opensearch_checks,
)

# OpenSearch-managed EBS storage rates ($/GB-month, us-east-1 baseline). Region-
# scaled via pricing_multiplier at the per-rec emit site. Validated 2026-06-27
# against the AWS Pricing API (service AmazonES, productFamily "Amazon OpenSearch
# Service Volume"): ES:GP3-Storage = $0.122/GB-Mo, ES:GP2-Storage = $0.135/GB-Mo.
GP3_PRICE_PER_GB_MONTH: float = 0.122
GP2_PRICE_PER_GB_MONTH: float = 0.135

# Trailing window used to measure the billed Extended Support surcharge, matching
# the commitment-coverage headroom read (reflects the CURRENT engine versions).
_SURCHARGE_WINDOW_DAYS: int = 7

# x86/Intel OpenSearch (AmazonES) instance family -> its same-size Graviton
# (ARM) equivalent. The realizable Graviton saving is the exact per-node price
# delta, NOT a flat 20-40% / 0.25 price-performance figure (that is a
# perf-per-dollar metric, not a cost reduction — the real node-price delta is
# ~5-10%). The old flat GRAVITON_RATE=0.25 overstated it ~3-5x (live-audit H4).
# Families with no clean same-size Graviton counterpart (storage i3/i2, etc.)
# are omitted so the caller emits a $0 advisory instead of guessing a target.
_X86_TO_GRAVITON_FAMILY: dict[str, str] = {
    "m3": "m6g",
    "m4": "m6g",
    "m5": "m6g",
    "c4": "c6g",
    "c5": "c6g",
    "r3": "r6g",
    "r4": "r6g",
    "r5": "r6g",
    "t2": "t4g",
    "t3": "t4g",
}

# Standard OpenSearch instance size ladder (ascending). Used to derive the
# one-size-down downsize target for an underutilized domain (OpenSearch C3).
_SIZE_LADDER: tuple[str, ...] = (
    "micro",
    "small",
    "medium",
    "large",
    "xlarge",
    "2xlarge",
    "4xlarge",
    "8xlarge",
    "12xlarge",
    "16xlarge",
    "24xlarge",
)


def _one_size_down(instance_type: str | None) -> str | None:
    """Return the OpenSearch instance type one size smaller, or None.

    OpenSearch types are ``<family>.<size>.<suffix>`` where suffix is ``search``
    (or legacy ``elasticsearch``) -- e.g. ``r6g.xlarge.search`` ->
    ``r6g.large.search``. Returns None for the smallest rung, an unknown size, or
    an unparseable type. The target is validated by a quiet pricing lookup
    downstream, so a size that does not exist for the family simply yields no
    counted saving (fail safe).
    """
    if not instance_type:
        return None
    parts = instance_type.split(".")
    if len(parts) < 2:
        return None
    size = parts[1]
    if size not in _SIZE_LADDER:
        return None
    idx = _SIZE_LADDER.index(size)
    if idx == 0:
        return None
    parts[1] = _SIZE_LADDER[idx - 1]
    return ".".join(parts)


def _downsize_node_delta(ctx: Any, instance_type: str | None) -> tuple[float, str | None]:
    """Per-node $/month saved by downsizing one OpenSearch instance size.

    Concrete current -> one-size-down node-price delta (replaces the flat 0.30
    reduction factor -- OpenSearch C3). Returns ``(0.0, None)`` (caller emits a
    $0 advisory) when pricing is unavailable, the type cannot be downsized, or
    the delta is non-positive -- we never assert a downsize saving we cannot
    substantiate from two live prices.

    Returns:
        Tuple of (per-node monthly delta, target instance type) -- the target is
        None whenever the delta is 0.0.
    """
    if ctx.pricing_engine is None or not instance_type:
        return 0.0, None
    target = _one_size_down(instance_type)
    if target is None:
        return 0.0, None
    current = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
    smaller = ctx.pricing_engine.get_instance_monthly_price("AmazonES", target)
    if current <= 0 or smaller <= 0 or smaller >= current:
        return 0.0, None
    return current - smaller, target


def _graviton_equivalent(instance_type: str | None) -> str | None:
    """Map an x86 OpenSearch instance type to its same-size Graviton equivalent.

    ``r5.xlarge.search`` -> ``r6g.xlarge.search``. Returns ``None`` when the
    family has no known Graviton counterpart, so the caller demotes the Graviton
    rec to a $0 advisory rather than fabricating a delta against an unknown
    node price (live-audit H4).
    """
    if not instance_type:
        return None
    parts = instance_type.split(".")
    if len(parts) < 2:
        return None
    graviton_family = _X86_TO_GRAVITON_FAMILY.get(parts[0])
    if graviton_family is None:
        return None
    parts[0] = graviton_family
    return ".".join(parts)


def _domain_engine_versions(ctx: Any) -> list[tuple[str, str]]:
    """(domain, engine version) pairs, for naming who carries the surcharge.

    Context only — the surcharge dollar comes from billing, never from this list.
    Returns ``[]`` on any failure (the rec then omits the version detail).
    """
    try:
        client = ctx.client("opensearch")
        names = [d.get("DomainName", "") for d in client.list_domain_names().get("DomainNames", [])]
        names = [n for n in names if n]
        if not names:
            return []
        resp = client.describe_domains(DomainNames=names)
        return [
            (str(d.get("DomainName", "?")), str(d.get("EngineVersion", "?")))
            for d in resp.get("DomainStatusList", [])
        ]
    except Exception:  # noqa: BLE001 — cosmetic detail only
        return []


_CE_OPENSEARCH_SERVICE = "Amazon OpenSearch Service"


def _is_extended_support_usage_type(usage_type: str) -> bool:
    """True for the ``<region>-OpenSearchExtendedSupport`` billing line."""
    return "extendedsupport" in usage_type.replace("-", "").replace("_", "").lower()


def _domain_from_arn(resource_id: str) -> str:
    """``arn:aws:es:...:domain/production-bnc`` -> ``production-bnc``."""
    rid = str(resource_id or "")
    return rid.rsplit("/", 1)[-1] if "/" in rid else rid


def _extended_support_breakdown(ctx: Any) -> tuple[float, dict[str, float]]:
    """The billed OpenSearch Extended Support surcharge, and who pays it.

    Measured, never inferred: AWS bills the surcharge as its own Cost-Explorer
    usage type (``<region>-OpenSearchExtendedSupport``), so trailing-7-day spend
    on that usage type — scaled to a 30-day run rate — is the exact realizable
    saving from upgrading the offending domain's engine version. Guessing from
    engine-version numbers instead would repeat the EKS Extended-Support bug: a
    version-based guess counted a surcharge AWS was not charging.

    Attribution uses ``GetCostAndUsageWithResources``, which resolves the charge
    to a domain ARN. It requires the account to have Cost Explorer resource-level
    granularity enabled, so an empty per-domain map means "billed, but we cannot
    prove which domain" — the caller must then name no domain rather than blame
    one. Never attribute by engine version: the newest domain would be implicated
    just as readily as the oldest.

    Returns:
        ``(monthly_total, {domain: monthly_amount})``. ``(0.0, {})`` when the read
        fails or no surcharge is billed (fail closed).
    """
    try:
        ce = ctx.client("ce")
    except Exception:  # noqa: BLE001
        return 0.0, {}
    from datetime import UTC, datetime, timedelta

    end = datetime.now(UTC).date()
    start = end - timedelta(days=_SURCHARGE_WINDOW_DAYS)
    period = {"Start": start.isoformat(), "End": end.isoformat()}
    scale = 30.0 / _SURCHARGE_WINDOW_DAYS

    try:
        resp = ce.get_cost_and_usage(
            TimePeriod=period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": [_CE_OPENSEARCH_SERVICE]}},
        )
    except Exception as e:  # noqa: BLE001 — fail closed
        ctx.warn(f"Could not read OpenSearch Extended Support spend: {e}", "opensearch")
        return 0.0, {}

    total = 0.0
    usage_types: list[str] = []
    for bucket in resp.get("ResultsByTime", []) or []:
        for grp in bucket.get("Groups", []) or []:
            usage_type = str((grp.get("Keys") or [""])[0])
            if not _is_extended_support_usage_type(usage_type):
                continue
            amount = grp.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")
            try:
                total += float(amount or 0)
            except (TypeError, ValueError):
                continue
            if usage_type not in usage_types:
                usage_types.append(usage_type)

    if total <= 0 or not usage_types:
        return 0.0, {}

    per_domain: dict[str, float] = {}
    try:
        detail = ce.get_cost_and_usage_with_resources(
            TimePeriod=period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "RESOURCE_ID"}],
            Filter={
                "And": [
                    {"Dimensions": {"Key": "SERVICE", "Values": [_CE_OPENSEARCH_SERVICE]}},
                    {"Dimensions": {"Key": "USAGE_TYPE", "Values": usage_types}},
                ]
            },
        )
        for bucket in detail.get("ResultsByTime", []) or []:
            for grp in bucket.get("Groups", []) or []:
                domain = _domain_from_arn((grp.get("Keys") or [""])[0])
                amount = grp.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")
                try:
                    value = float(amount or 0)
                except (TypeError, ValueError):
                    continue
                if domain and domain.lower() != "noresourceid" and value > 0:
                    per_domain[domain] = per_domain.get(domain, 0.0) + value
    except Exception:  # noqa: BLE001 — resource granularity may be disabled
        per_domain = {}

    return total * scale, {d: v * scale for d, v in per_domain.items()}


def _graviton_node_delta(ctx: Any, instance_type: str | None) -> tuple[float, str | None]:
    """Per-node $/month saved by migrating one OpenSearch node x86 -> Graviton.

    Concrete current -> same-size Graviton node-price delta (replaces the flat
    0.25 price-performance proxy -- live-audit H4). Returns ``(0.0, None)``
    (caller emits a $0 advisory) when pricing is unavailable, the family has no
    Graviton counterpart, or the delta is non-positive -- we never assert a
    migration saving we cannot substantiate from two live prices.

    Returns:
        Tuple of (per-node monthly delta, target instance type) -- the target is
        None whenever the delta is 0.0.
    """
    if ctx.pricing_engine is None or not instance_type:
        return 0.0, None
    target = _graviton_equivalent(instance_type)
    if target is None:
        return 0.0, None
    current = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
    graviton = ctx.pricing_engine.get_instance_monthly_price("AmazonES", target)
    if current <= 0 or graviton <= 0 or graviton >= current:
        return 0.0, None
    return current - graviton, target


class OpensearchModule(BaseServiceModule):
    """ServiceModule adapter for OpenSearch. Live node-price-delta savings strategy."""

    key: str = "opensearch"
    cli_aliases: tuple[str, ...] = ("opensearch",)
    display_name: str = "OpenSearch"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for OpenSearch scanning."""
        return ("opensearch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan OpenSearch domains for cost optimization opportunities.

        Consults enhanced OpenSearch checks and Cost Optimization Hub. CoH is
        the authoritative aggregator: a domain covered by CoH suppresses that
        domain's heuristic findings (avoids double-counting). Savings calculated
        via keyword-rate heuristics matching Reserved, Graviton, and storage
        patterns.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks and (when present)
            cost_optimization_hub SourceBlocks.
        """
        result = get_enhanced_opensearch_checks(ctx)
        recs = result.get("recommendations", [])

        # Cost Optimization Hub re-surfaces OpenSearch rightsizing/idle findings
        # the orchestrator bucketed into ctx.cost_hub_splits["opensearch"]. CoH
        # is authoritative: a domain it covers suppresses that domain's heuristic
        # levers (SR-3 / OpenSearch C1).
        coh_recs = [r for r in getattr(ctx, "cost_hub_splits", {}).get("opensearch", []) if is_renderable_coh_rec(r)]
        # Suppression keys span ALL CoH recs so an RI demotion below never
        # re-enables the heuristic lever for that domain.
        coh_keys = {coh_key(r) for r in coh_recs} - {""}
        # Active-commitment demotion: a domain covered by an OpenSearch Reserved
        # Instance bills the reservation regardless of rightsizing, so its
        # on-demand CoH figure is not realizable — demote to advisory.
        coverage = getattr(ctx, "commitment_coverage", None)
        coh_counted, coh_advisory = demote_coh_by_commitment(coh_recs, coverage, "opensearch", coh_savings)
        coh_out = coh_counted + coh_advisory
        coh_total = sum(coh_savings(r) for r in coh_counted)

        # Price every rec and attach the per-rec dollar figure (the report
        # previously showed only "30-50%", with no per-domain $). Each counted
        # dollar carries a structured AuditBasis so it is defensible from the
        # report alone.
        for rec in recs:
            category = rec.get("CheckCategory", "")
            instance_type = rec.get("InstanceType")
            instance_count = rec.get("InstanceCount", 1) or 1
            value = 0.0
            audit_basis: dict[str, Any] | None = None
            if category == "Idle Domain":
                # Deleting an idle domain recovers 100% of its cost: full
                # instance monthly × count + full EBS storage (opensearch C2 —
                # previously priced $0 because the rec carried no InstanceType).
                # Priced higher than Graviton (25% instance delta) so it wins the
                # per-domain best-lever dedup below.
                instance_monthly = 0.0
                if ctx.pricing_engine is not None and instance_type:
                    instance_monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
                ebs = rec.get("EBSVolumeSize", 0) or 0
                storage_monthly = ebs * GP3_PRICE_PER_GB_MONTH * ctx.pricing_multiplier
                value = (instance_monthly * instance_count) + storage_monthly
                audit_basis = {
                    "instance_rate_monthly": round(instance_monthly, 4),
                    "instance_count": instance_count,
                    "storage_gb": ebs,
                    "gp3_rate_per_gb_month": GP3_PRICE_PER_GB_MONTH,
                    "region_multiplier": round(ctx.pricing_multiplier, 4),
                    "formula": "instance_rate x count + storage_gb x gp3_rate x region_multiplier",
                }
            elif "storage" in category.lower():
                # gp2 -> gp3 migration delta (OpenSearch H3): the realizable
                # saving is the per-GB price *difference*, not a flat fraction of
                # the gp3 base. Both rates are OpenSearch-managed EBS, region-
                # scaled once.
                ebs = rec.get("EBSVolumeSize", 0) or 0
                delta_rate = GP2_PRICE_PER_GB_MONTH - GP3_PRICE_PER_GB_MONTH
                value = ebs * delta_rate * ctx.pricing_multiplier
                audit_basis = {
                    "storage_gb": ebs,
                    "gp2_rate_per_gb_month": GP2_PRICE_PER_GB_MONTH,
                    "gp3_rate_per_gb_month": GP3_PRICE_PER_GB_MONTH,
                    "delta_rate_per_gb_month": round(delta_rate, 4),
                    "region_multiplier": round(ctx.pricing_multiplier, 4),
                    "formula": "storage_gb x (gp2_rate - gp3_rate) x region_multiplier",
                }
            elif category == "Underutilized Domain":
                # Concrete current -> one-size-down node-price delta (OpenSearch
                # C3): replaces the flat 0.30 reduction factor. CloudWatch-gated
                # upstream (avg CPU < LOW_CPU_THRESHOLD). Abstains to a $0
                # advisory when the downsize target cannot be priced (fail safe).
                per_node_delta, target = _downsize_node_delta(ctx, instance_type)
                value = per_node_delta * instance_count
                if value > 0:
                    audit_basis = {
                        "current_type": instance_type,
                        "target_type": target,
                        "per_node_delta_monthly": round(per_node_delta, 4),
                        "instance_count": instance_count,
                        "metric": f"CloudWatch AWS/ES CPUUtilization avg < {LOW_CPU_THRESHOLD}% over 14d",
                        "formula": "(current_node_monthly - one_size_down_node_monthly) x count",
                    }
            elif category == "Graviton Migration":
                # Concrete current -> same-size Graviton node-price delta
                # (live-audit H4): replaces the flat 0.25 price-performance
                # proxy, which overstated the real ~5-10% node delta ~3-5x.
                # Abstains to a $0 advisory when no Graviton counterpart prices.
                per_node_delta, target = _graviton_node_delta(ctx, instance_type)
                value = per_node_delta * instance_count
                if value > 0:
                    audit_basis = {
                        "current_type": instance_type,
                        "target_type": target,
                        "per_node_delta_monthly": round(per_node_delta, 4),
                        "instance_count": instance_count,
                        "formula": "(current_node_monthly - same_size_graviton_node_monthly) x count",
                    }
            rec["EstimatedMonthlySavings"] = round(value, 2)
            if audit_basis is not None:
                rec["AuditBasis"] = audit_basis
            if "Reserved" in category:
                rec["Counted"] = False  # commitment lever — advisory
            elif value <= 0 and category in ("Underutilized Domain", "Graviton Migration"):
                # Could not quantify a concrete delta → explicit $0 advisory
                # (shown, not counted) — never a silent drop (OpenSearch C3).
                rec["Counted"] = False
                rec["EstimatedSavings"] = (
                    "$0.00/month — advisory: no concrete price delta available "
                    "(downsize target / instance rate not priceable)"
                )
            # CoH already covers this domain → demote the heuristic lever so the
            # same saving is not counted twice.
            if rec.get("DomainName", "") in coh_keys:
                rec["Counted"] = False
            # Idle-domain DELETE is irreversible: only count it when request-level
            # activity (SearchRate + IndexingRate ~ 0) corroborates the domain is
            # truly unused. A CPU-only "idle" verdict renders the potential saving
            # as a $0 advisory, never a counted delete (opensearch idle-domain
            # safety gate — a 4.7% CPU prod search cluster is not safely deletable).
            if category == "Idle Domain" and not rec.get("IdleCorroborated", False):
                rec["Counted"] = False
                rec["Recommendation"] = rec.get(
                    "Recommendation", "Verify the domain is unused before deleting"
                )

        # Dedupe instance-axis levers (Graviton vs downsize) per domain — they are
        # alternatives on the same nodes. Storage is a separate axis (kept). A rec
        # that prices to $0 (e.g. underutilized with no InstanceType) is advisory.
        best_instance: dict[str, dict[str, Any]] = {}
        for rec in recs:
            if rec.get("Counted") is False or "storage" in rec.get("CheckCategory", "").lower():
                continue
            dom = rec.get("DomainName", "")
            cur = best_instance.get(dom)
            if cur is None or rec["EstimatedMonthlySavings"] > cur["EstimatedMonthlySavings"]:
                best_instance[dom] = rec

        # Domains being counted as an idle DELETE: their separate gp2→gp3 storage
        # rec is mutually exclusive (you cannot migrate storage on a deleted
        # domain) — and the idle saving already includes that storage at the gp3
        # rate — so the storage rec must not also be counted (opensearch
        # idle-vs-storage double-count). Built from non-demoted idle recs.
        idle_deleted_domains = {
            rec.get("DomainName", "")
            for rec in recs
            if rec.get("CheckCategory") == "Idle Domain"
            and rec.get("Counted") is not False
            and rec.get("EstimatedMonthlySavings", 0) > 0
        }

        best_ids = {id(r) for r in best_instance.values()}
        savings = 0.0
        for rec in recs:
            if rec.get("Counted") is False:
                continue
            is_storage = "storage" in rec.get("CheckCategory", "").lower()
            if is_storage and rec.get("DomainName", "") in idle_deleted_domains:
                rec["Counted"] = False  # superseded by the idle-domain delete
                continue
            keep = (is_storage or id(rec) in best_ids) and rec["EstimatedMonthlySavings"] > 0
            if keep:
                rec["Counted"] = True
                savings += rec["EstimatedMonthlySavings"]
            else:
                rec["Counted"] = False

        # Active-commitment gate for the locally-derived levers (demote_coh_by_commitment
        # above only sees CoH recs). Downsizing a domain whose exact instance type carries
        # an OpenSearch Reserved Instance strands that reservation, so the on-demand figure
        # is realizable only up to that type's uncovered on-demand spend. Storage-tier recs
        # carry no InstanceType and are never RI-covered, so they pass through untouched.
        savings -= demote_covered_in_place(recs, coverage, "opensearch", lambda r: r.get("InstanceType") or "")

        savings += coh_total

        # counted == rendered: single-source the per-rec EstimatedSavings STRING
        # from the finalized counted dollar so the card the reporter renders (it
        # reads EstimatedSavings) matches the number summed into the headline.
        # The qualitative upstream "30-50%" price-performance wording is dropped
        # from the savings slot — it is not a $ figure. Mirrors elasticache.py;
        # opensearch.py previously lacked this loop, leaving a counted domain
        # rendering "30-50%" while $689.12 was credited (counted!=rendered fix).
        # Advisory recs render an honest $0 line, preserving any specific
        # no-delta reason already set above.
        for rec in recs:
            if rec.get("Counted") is False:
                # Zero the numeric so the advisory's field matches its $0 string
                # (counted == rendered at the field level): a consumer summing
                # EstimatedMonthlySavings must not pick up a demoted domain's
                # pre-demotion value. Preserve it as PotentialMonthlySavings.
                # Mirrors elasticache.py's advisory-zeroing.
                emv = rec.get("EstimatedMonthlySavings", 0.0)
                if emv:
                    rec["PotentialMonthlySavings"] = emv
                    rec["EstimatedMonthlySavings"] = 0.0
                if not str(rec.get("EstimatedSavings", "")).startswith("$0.00"):
                    rec["EstimatedSavings"] = "$0.00/month — advisory (not counted toward total)"
            else:
                rec["EstimatedSavings"] = f"${rec.get('EstimatedMonthlySavings', 0.0):.2f}/month"

        # Extended Support surcharge — a real, separately-billed recurring charge
        # (CE usage type `*-OpenSearchExtendedSupport`) that no other lever covers.
        # Kept OUT of `recs` so the per-domain "best lever" dedup cannot suppress
        # it: it is additive to any downsize/Graviton saving on the same domain,
        # not an alternative remediation.
        surcharge_recs: list[dict[str, Any]] = []
        surcharge, per_domain = _extended_support_breakdown(ctx)
        if surcharge > 0:
            engine_of = dict(_domain_engine_versions(ctx))
            basis = {
                "metric": f"Cost Explorer usage type *-OpenSearchExtendedSupport, trailing {_SURCHARGE_WINDOW_DAYS}d",
                "formula": f"billed surcharge / {_SURCHARGE_WINDOW_DAYS}d x 30",
                "evidence": "measured from actual billing, not inferred from engine-version numbers",
            }
            if per_domain:
                # Attributed: charge exactly the domain(s) AWS bills. Never implicate
                # a domain whose engine version merely looks old.
                for domain, amount in sorted(per_domain.items(), key=lambda kv: -kv[1]):
                    version = engine_of.get(domain, "?")
                    surcharge_recs.append(
                        {
                            "DomainName": domain,
                            "EngineVersion": version,
                            "CheckCategory": "OpenSearch Extended Support",
                            "Recommendation": f"Upgrade {domain} off Extended Support (currently {version})",
                            "EstimatedMonthlySavings": round(amount, 2),
                            "EstimatedSavings": f"${amount:.2f}/month",
                            "Counted": True,
                            "Severity": "HIGH",
                            "AuditBasis": {**basis, "attribution": "CE resource-level (GetCostAndUsageWithResources)"},
                            "Reason": (
                                f"AWS bills ~${amount:.2f}/mo of OpenSearch Extended Support against domain "
                                f"'{domain}' (engine {version}). Upgrading it to a standard-support engine "
                                f"version removes the surcharge."
                            ),
                        }
                    )
            else:
                # Billed, but resource-level granularity is off, so we cannot prove
                # which domain pays. Name none — list the versions as context only.
                engines = ", ".join(f"{n}={v}" for n, v in sorted(engine_of.items())) or "unknown"
                surcharge_recs.append(
                    {
                        "DomainName": "(unattributed — enable Cost Explorer resource-level granularity)",
                        "CheckCategory": "OpenSearch Extended Support",
                        "Recommendation": "Upgrade the domain running an end-of-standard-support engine version",
                        "EstimatedMonthlySavings": round(surcharge, 2),
                        "EstimatedSavings": f"${surcharge:.2f}/month",
                        "Counted": True,
                        "Severity": "HIGH",
                        "AuditBasis": {
                            **basis,
                            "attribution": "unavailable — CE resource-level granularity disabled",
                            "engine_versions": engines,
                        },
                        "Reason": (
                            f"AWS is billing an OpenSearch Extended Support surcharge of ~${surcharge:.2f}/mo. "
                            f"Billing does not attribute it to a domain at this granularity, so no single domain "
                            f"is named here. Engine versions in this region: {engines}."
                        ),
                    }
                )
            savings += surcharge

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}
        if coh_out:
            sources["cost_optimization_hub"] = SourceBlock(count=len(coh_out), recommendations=tuple(coh_out))
        if surcharge_recs:
            sources["extended_support"] = SourceBlock(count=len(surcharge_recs), recommendations=tuple(surcharge_recs))

        return ServiceFindings(
            service_name="OpenSearch",
            total_recommendations=len(recs) + len(coh_counted) + len(surcharge_recs),
            total_monthly_savings=round(savings, 2),
            sources=sources,
            optimization_descriptions=OPENSEARCH_OPTIMIZATION_DESCRIPTIONS,
        )
