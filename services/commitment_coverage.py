"""Active commitment (Savings Plan / Reserved Instance) coverage detection.

Rightsizing, Graviton-migration, and idle recommendations from AWS Cost
Optimization Hub / Compute Optimizer are computed on an **on-demand ("before
discounts") basis**. When the account already holds a Savings Plan or Reserved
Instance covering the resource, that on-demand figure is not the realizable
saving:

* An **EC2-Instance Savings Plan** is *family-locked* to one instance family in
  one region. Migrating a covered instance to a different family (e.g. m5 ->
  r6g Graviton) moves it out of coverage: the new instance bills at full
  on-demand while the family-locked commitment keeps billing until it expires
  (stranded spend). The net effect can be **zero or cost-negative**, never the
  reported on-demand delta.
* A same-family downsize only saves if the freed commitment is reabsorbed by
  other on-demand usage in that family; otherwise it strands too.
* **Reserved Instances / Nodes** (RDS, ElastiCache, Redshift, OpenSearch) create
  the identical trap for those services' rightsizing recommendations.

Because per-instance realizability cannot be asserted without modelling the
whole commitment fill, the honest treatment is to **demote** a
commitment-covered rec to advisory (``Counted = False``): it still renders with
its indicative on-demand figure, but never inflates the counted headline. This
mirrors the project's existing advisory-demotion convention (EFS lifecycle,
non-prod scheduling).

This module holds the coverage model (``CommitmentCoverage``), the family
extractor, the pure demotion split (``split_by_commitment``), and the live
fetch (``fetch_commitment_coverage``). The pure parts carry no boto3 dependency
so the matching/demotion arithmetic is unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

# Services whose rightsizing recs this module can demote. Used by the
# orchestrator to decide whether a coverage prefetch is worth its API calls.
COMMITMENT_SENSITIVE_SERVICES: frozenset[str] = frozenset(
    {"ec2", "lambda", "rds", "elasticache", "redshift", "opensearch"}
)


def instance_family(instance_type: str) -> str:
    """Return the family token of an instance/node type, for commitment matching.

    Strips service-specific prefixes/suffixes and keeps everything before the
    first size dot:

    * EC2 ``m5.xlarge`` -> ``m5``; ``m7i-flex.large`` -> ``m7i-flex``
    * RDS ``db.r5.large`` -> ``r5``
    * ElastiCache ``cache.r6g.large`` -> ``r6g``
    * OpenSearch ``r6g.large.search`` -> ``r6g``
    * Redshift ``ra3.xlplus`` -> ``ra3``

    Matching at family granularity is intentionally *conservative*: it may
    demote a rec whose exact size/engine a commitment does not cover, but it
    never counts a saving a commitment silently absorbs. Over-demotion costs a
    real-but-unclaimed saving; under-demotion reports a phantom one — the former
    is the safe direction for a cost-fidelity scanner.
    """
    t = (instance_type or "").strip().lower()
    if not t:
        return ""
    # Drop leading service prefix (db., cache.).
    if t.startswith("db."):
        t = t[3:]
    elif t.startswith("cache."):
        t = t[6:]
    # Drop trailing OpenSearch ".search" / ".elasticsearch" marker.
    for suffix in (".search", ".elasticsearch"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t.split(".")[0]


@dataclass(frozen=True)
class CommitmentCoverage:
    """Immutable snapshot of the account's active commitments in one region.

    Attributes:
        region: Scan region the coverage was resolved for.
        ec2_sp_families: EC2-Instance Savings Plan families active *in region*.
        has_compute_sp: Whether any active Compute Savings Plan exists (region-
            flexible; covers every EC2 family plus Lambda and Fargate).
        rds_ri_families: RDS Reserved DB Instance families (in region).
        elasticache_ri_families: ElastiCache Reserved Cache Node families.
        redshift_ri_families: Redshift Reserved Node families.
        opensearch_ri_families: OpenSearch Reserved Instance families.
        sp_utilization_pct: Last-30d Savings Plan utilization %, if resolved.
        sp_unused_monthly: Last-30d unused SP commitment ($/mo), if resolved.
    """

    region: str = ""
    ec2_sp_families: frozenset[str] = field(default_factory=frozenset)
    has_compute_sp: bool = False
    rds_ri_families: frozenset[str] = field(default_factory=frozenset)
    elasticache_ri_families: frozenset[str] = field(default_factory=frozenset)
    redshift_ri_families: frozenset[str] = field(default_factory=frozenset)
    opensearch_ri_families: frozenset[str] = field(default_factory=frozenset)
    sp_utilization_pct: float | None = None
    sp_unused_monthly: float | None = None

    # --- coverage predicates (per service) --------------------------------
    def covers_ec2(self, instance_type: str) -> bool:
        """True if an EC2 SP (family-locked or Compute) covers this instance."""
        return self.has_compute_sp or instance_family(instance_type) in self.ec2_sp_families

    def covers_lambda(self) -> bool:
        """True if a Compute SP covers Lambda usage (EC2-Instance SPs do not)."""
        return self.has_compute_sp

    def covers_rds(self, instance_class: str) -> bool:
        """True if an RDS Reserved DB Instance covers this instance's family."""
        return instance_family(instance_class) in self.rds_ri_families

    def covers_elasticache(self, node_type: str) -> bool:
        """True if a Reserved Cache Node covers this node's family."""
        return instance_family(node_type) in self.elasticache_ri_families

    def covers_redshift(self, node_type: str) -> bool:
        """True if a Reserved Node covers this node's family."""
        return instance_family(node_type) in self.redshift_ri_families

    def covers_opensearch(self, instance_type: str) -> bool:
        """True if a Reserved Instance covers this domain node's family."""
        return instance_family(instance_type) in self.opensearch_ri_families

    @property
    def has_any_commitment(self) -> bool:
        """True if any commitment was detected in this region."""
        return bool(
            self.has_compute_sp
            or self.ec2_sp_families
            or self.rds_ri_families
            or self.elasticache_ri_families
            or self.redshift_ri_families
            or self.opensearch_ri_families
        )

    def covers(self, service: str, resource_type: str) -> bool:
        """Dispatch coverage check by service key (for the DB/cache adapters)."""
        return {
            "ec2": self.covers_ec2,
            "rds": self.covers_rds,
            "elasticache": self.covers_elasticache,
            "redshift": self.covers_redshift,
            "opensearch": self.covers_opensearch,
        }.get(service, lambda _t: False)(resource_type)

    def ri_note(self, service: str, resource_type: str, gross: float) -> str:
        """Human-readable reason a Reserved-Instance-covered rec was demoted."""
        fam = instance_family(resource_type)
        label = {
            "rds": "RDS Reserved DB Instance",
            "elasticache": "ElastiCache Reserved Cache Node",
            "redshift": "Redshift Reserved Node",
            "opensearch": "OpenSearch Reserved Instance",
        }.get(service, "Reserved Instance")
        return (
            f"Covered by an active {fam} {label} in {self.region}. The ${gross:,.2f}/mo figure is "
            f"on-demand basis; the reservation is fixed spend that continues after rightsizing, so "
            f"the realizable saving requires the freed reservation to be reused or to expire — not counted."
        )

    def ec2_note(self, instance_type: str, gross: float) -> str:
        """Human-readable reason an EC2 rec was demoted to advisory."""
        fam = instance_family(instance_type)
        if self.has_compute_sp and fam not in self.ec2_sp_families:
            basis = "an active Compute Savings Plan"
        else:
            basis = f"an active {fam} EC2-Instance Savings Plan in {self.region}"
        util = ""
        if self.sp_utilization_pct is not None:
            util = f" (SP utilization {self.sp_utilization_pct:.0f}%)"
        return (
            f"Covered by {basis}{util}. The ${gross:,.2f}/mo figure is on-demand basis; "
            f"the realizable saving requires the freed commitment to be reabsorbed by other "
            f"in-family usage or the plan to expire, so it is not counted."
        )


# A single frozen instance reused when no commitment data could be resolved
# (fail-safe: adapters treat it as "nothing covered" and count as before).
EMPTY_COVERAGE = CommitmentCoverage()

# Instance/node type keys AWS uses inside a CoH rec's nested configuration,
# across EC2, RDS, ElastiCache, Redshift, and OpenSearch.
_COH_TYPE_KEYS: tuple[str, ...] = (
    "dbInstanceClass",
    "cacheNodeType",
    "nodeType",
    "instanceType",
    "type",
)


def coh_resource_type(rec: dict[str, Any]) -> str:
    """Extract the current instance/node type from any CoH recommendation.

    CoH nests the type as ``currentResourceDetails.<resourceWrapper>.
    configuration.instance.<typeKey>`` (verified for EC2 ``type`` and RDS
    ``dbInstanceClass``); ElastiCache / Redshift / OpenSearch follow the same
    shape with their own type key. Returns ``""`` when no type is found, which
    ``instance_family`` maps to a non-matching family (safe: no demotion).
    """
    crd = rec.get("currentResourceDetails") or {}
    for wrapper in crd.values():
        if not isinstance(wrapper, dict):
            continue
        cfg = wrapper.get("configuration", {}) or {}
        inst = cfg.get("instance", {}) or {}
        for container in (inst, cfg):
            for key in _COH_TYPE_KEYS:
                val = container.get(key)
                if val:
                    return str(val)
    return ""


def demote_coh_by_commitment(
    coh_recs: list[dict[str, Any]],
    coverage: "CommitmentCoverage | None",
    service: str,
    gross_of: Callable[[dict[str, Any]], float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a data-store adapter's CoH recs into (counted, advisory) by RI cover.

    Convenience wrapper over ``split_by_commitment`` for the RDS / ElastiCache /
    Redshift / OpenSearch adapters, which each own a single counted CoH source.
    Returns all recs as counted when coverage is absent/empty (no behaviour
    change for accounts without reservations).
    """
    if coverage is None or not coverage.has_any_commitment:
        return list(coh_recs), []
    return split_by_commitment(
        coh_recs,
        is_covered=lambda r: coverage.covers(service, coh_resource_type(r)),
        gross_of=gross_of,
        note_of=lambda r, g: coverage.ri_note(service, coh_resource_type(r), g),
    )


def split_by_commitment(
    recs: list[dict[str, Any]],
    *,
    is_covered: Callable[[dict[str, Any]], bool],
    gross_of: Callable[[dict[str, Any]], float],
    note_of: Callable[[dict[str, Any], float], str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split recs into (counted, advisory) by active-commitment coverage.

    Pure function (no boto3, no mutation). Covered recs are returned as new
    dicts carrying ``Counted = False`` plus ``AdvisoryEstimate`` (the original
    gross) and ``CommitmentCoverageNote`` (the reason), so the reporter's
    existing ``Counted is False`` convention demotes them from every counted
    total while still rendering them. Uncovered recs pass through unchanged.

    Args:
        recs: Source recommendations (any shape).
        is_covered: Predicate — True if the rec's resource is commitment-covered.
        gross_of: Extracts the rec's on-demand gross saving (for the note/estimate).
        note_of: Builds the demotion note from (rec, gross).

    Returns:
        (counted, advisory) — two disjoint lists; advisory items are copies.
    """
    counted: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    for rec in recs:
        if is_covered(rec):
            gross = gross_of(rec)
            advisory.append(
                {
                    **rec,
                    "Counted": False,
                    "AdvisoryEstimate": gross,
                    "CommitmentCoverageNote": note_of(rec, gross),
                }
            )
        else:
            counted.append(rec)
    return counted, advisory


# ------------------------------------------------------------------------
# Live fetch
# ------------------------------------------------------------------------
def _is_permission_error(exc: Exception) -> bool:
    """True for IAM access-denied style errors (vs transient/service errors)."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in (
            "AccessDenied",
            "AccessDeniedException",
            "UnauthorizedOperation",
            "AuthorizationError",
        )
    return False


def _report(ctx: Any, exc: Exception, message: str, action: str) -> None:
    """Route a fetch error to permission_issue (IAM) or warn (everything else)."""
    if _is_permission_error(exc):
        ctx.permission_issue(f"{message}: {exc}", "commitment_coverage", action)
    else:
        ctx.warn(f"{message}: {exc}", service="commitment_coverage")


def _fetch_savings_plans(ctx: Any) -> tuple[frozenset[str], bool]:
    """Return (region-matched EC2-Instance SP families, any Compute SP active).

    EC2-Instance SPs are region-locked, so only families whose ``region``
    matches the scan region are applied. Compute SPs are region-flexible, so a
    Compute SP anywhere covers the scan region.
    """
    families: set[str] = set()
    has_compute = False
    try:
        client = ctx.client("savingsplans")
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"states": ["active"]}
            if token:
                kwargs["nextToken"] = token
            resp = client.describe_savings_plans(**kwargs)
            for sp in resp.get("savingsPlans", []):
                sp_type = sp.get("savingsPlanType", "")
                if sp_type == "Compute":
                    has_compute = True
                elif sp_type == "EC2Instance":
                    if sp.get("region", "") == ctx.region:
                        fam = (sp.get("ec2InstanceFamily", "") or "").lower()
                        if fam:
                            families.add(fam)
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 — fail-safe: no demotion on read failure
        _report(ctx, exc, "Could not read Savings Plans (rightsizing recs not SP-adjusted)", "savingsplans:DescribeSavingsPlans")
    return frozenset(families), has_compute


def _fetch_sp_utilization(ctx: Any) -> tuple[float | None, float | None]:
    """Return (last-30d SP utilization %, unused commitment $/mo), best-effort."""
    try:
        from datetime import UTC, datetime, timedelta

        end = datetime.now(UTC).date()
        start = end - timedelta(days=30)
        client = ctx.client("ce")
        resp = client.get_savings_plans_utilization(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
        )
        util = resp.get("Total", {}).get("Utilization", {})
        pct = util.get("UtilizationPercentage")
        unused = util.get("UnusedCommitment")
        return (
            float(pct) if pct is not None else None,
            float(unused) if unused is not None else None,
        )
    except Exception:  # noqa: BLE001 — utilization is contextual only; never fatal
        return None, None


def _fetch_ri_families(ctx: Any, service: str) -> frozenset[str]:
    """Return active Reserved-Instance/Node families for a data-store service."""
    fams: set[str] = set()
    try:
        if service == "rds":
            client = ctx.client("rds")
            for page in client.get_paginator("describe_reserved_db_instances").paginate():
                for ri in page.get("ReservedDBInstances", []):
                    if ri.get("State") == "active":
                        fams.add(instance_family(ri.get("DBInstanceClass", "")))
        elif service == "elasticache":
            client = ctx.client("elasticache")
            for page in client.get_paginator("describe_reserved_cache_nodes").paginate():
                for ri in page.get("ReservedCacheNodes", []):
                    if ri.get("State") == "active":
                        fams.add(instance_family(ri.get("CacheNodeType", "")))
        elif service == "redshift":
            client = ctx.client("redshift")
            for page in client.get_paginator("describe_reserved_nodes").paginate():
                for ri in page.get("ReservedNodes", []):
                    if ri.get("State") == "active":
                        fams.add(instance_family(ri.get("NodeType", "")))
        elif service == "opensearch":
            client = ctx.client("opensearch")
            resp = client.describe_reserved_instances()
            for ri in resp.get("ReservedInstances", []):
                if ri.get("State", "").lower() in ("active", "payment-pending"):
                    fams.add(instance_family(ri.get("InstanceType", "")))
    except Exception as exc:  # noqa: BLE001 — fail-safe per service
        _report(ctx, exc, f"Could not read {service} Reserved Instances (rightsizing recs not RI-adjusted)", f"{service}:DescribeReserved")
    fams.discard("")
    return frozenset(fams)


def fetch_commitment_coverage(ctx: Any, selected: set[str]) -> CommitmentCoverage:
    """Resolve the account's active commitments for the scan region.

    Only queries the APIs a selected service actually needs: Savings Plans when
    EC2 or Lambda is scanned; each Reserved-Instance API only when its service
    is scanned. Every source is individually error-isolated — a failure leaves
    that dimension empty (no demotion, counted as before) and surfaces a
    warning/permission issue, so the scan never crashes and degradation is
    visible.
    """
    want_sp = bool(selected & {"ec2", "lambda"})
    ec2_fams: frozenset[str] = frozenset()
    has_compute = False
    util_pct = unused = None
    if want_sp:
        ec2_fams, has_compute = _fetch_savings_plans(ctx)
        if ec2_fams or has_compute:
            util_pct, unused = _fetch_sp_utilization(ctx)

    return CommitmentCoverage(
        region=ctx.region,
        ec2_sp_families=ec2_fams,
        has_compute_sp=has_compute,
        rds_ri_families=_fetch_ri_families(ctx, "rds") if "rds" in selected else frozenset(),
        elasticache_ri_families=_fetch_ri_families(ctx, "elasticache") if "elasticache" in selected else frozenset(),
        redshift_ri_families=_fetch_ri_families(ctx, "redshift") if "redshift" in selected else frozenset(),
        opensearch_ri_families=_fetch_ri_families(ctx, "opensearch") if "opensearch" in selected else frozenset(),
        sp_utilization_pct=util_pct,
        sp_unused_monthly=unused,
    )
