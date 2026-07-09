"""Active commitment (Savings Plan / Reserved Instance) coverage detection.

Rightsizing, Graviton-migration, and idle recommendations from AWS Cost
Optimization Hub / Compute Optimizer are computed on an **on-demand ("before
discounts") basis** (`estimatedMonthlyCost` == the on-demand monthly cost).
When the account already holds a Savings Plan or Reserved Instance covering the
resource, that on-demand figure is not the realizable saving:

* An **EC2-Instance / SageMaker Savings Plan** and every **Reserved Instance /
  Node** (EC2 classic, RDS, ElastiCache, Redshift, OpenSearch, DynamoDB reserved
  capacity) is a *fixed pre-paid commitment over a coverage matrix*. Migrating a
  covered resource out of that matrix (e.g. an m5 EC2-Instance SP + m5->r6g
  Graviton migration) strands the commitment until it expires while the new
  resource bills full on-demand — net zero or **cost-negative**.
* A **Compute Savings Plan** covers EC2 (any family), Lambda, and Fargate
  (ECS/EKS) — but NOT RDS/ElastiCache/Redshift/OpenSearch/SageMaker.

Two-layer, aggregate-safe treatment:

1. **Membership demotion** — a rec whose resource family/type sits in an active
   commitment is a *candidate* for demotion to advisory (`Counted = False`).
   This never overstates savings (safe direction), but on its own over-demotes
   (a family-locked RI covering only one engine/size still flags the whole
   family).
2. **CE headroom cap** — Cost Explorer reports the *uncovered on-demand $* per
   `(service, family)` (`GetReservationCoverage` / `GetSavingsPlansCoverage`).
   Candidate recs are counted greedily up to that ceiling and only the remainder
   demoted, so a genuinely realizable saving (the on-demand overflow — including
   the uncovered engine/size a family-level RI misses) survives, while the total
   counted for a family can never exceed its real uncovered on-demand spend.

When the CE ceiling can't be resolved the layer-1 default (demote all
candidates) applies — the tightest, safe fallback. When no commitment is
detected at all, nothing is demoted (accounts without reservations are
unaffected).

The pure parts (model, matchers, split) carry no boto3 dependency and are
unit-testable in isolation; ``fetch_commitment_coverage`` performs the live
reads, each individually error-isolated.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

# Services whose rightsizing recs this module can demote. Used by the
# orchestrator to decide whether a coverage prefetch is worth its API calls.
COMMITMENT_SENSITIVE_SERVICES: frozenset[str] = frozenset(
    {"ec2", "lambda", "rds", "aurora", "elasticache", "redshift", "opensearch", "sagemaker", "dynamodb", "containers"}
)

# Aurora instances draw on the same Reserved DB Instance pool as RDS.
_RDS_RI_SERVICES: frozenset[str] = frozenset({"rds", "aurora"})

# Services whose Reserved Instances/Nodes are matched at EXACT type granularity
# (their reservations are NOT size-flexible): a Redshift Reserved Node / OpenSearch
# Reserved Instance covers only the purchased node/instance type, not the family.
_EXACT_TYPE_SERVICES: frozenset[str] = frozenset({"redshift", "opensearch"})


def instance_family(instance_type: str) -> str:
    """Return the family token of an instance/node type, for commitment matching.

    Strips service prefixes/suffixes and keeps everything before the first size
    dot: EC2 ``m5.xlarge`` -> ``m5``; ``m7i-flex.large`` -> ``m7i-flex``; RDS
    ``db.r5.large`` -> ``r5``; ElastiCache ``cache.r6g.large`` -> ``r6g``;
    OpenSearch ``r6g.large.search`` -> ``r6g``; Redshift ``ra3.xlplus`` -> ``ra3``.
    """
    t = normalize_type(instance_type)
    return t.split(".")[0] if t else ""


def normalize_type(instance_type: str) -> str:
    """Normalize an instance/node type for exact matching (prefixes/suffixes off).

    ``db.r5.large`` -> ``r5.large``; ``cache.r6g.large`` -> ``r6g.large``;
    ``r6g.large.search`` -> ``r6g.large``. Lower-cased, whitespace-trimmed.
    """
    t = (instance_type or "").strip().lower()
    if not t:
        return ""
    if t.startswith("db."):
        t = t[3:]
    elif t.startswith("cache."):
        t = t[6:]
    for suffix in (".search", ".elasticsearch"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t


def normalize_engine(engine: str) -> str:
    """Normalize an RDS engine / RI ProductDescription for coverage matching.

    A Reserved DB Instance covers only its own engine, so ``aurora-mysql`` never
    covers ``mysql``. Strips licence suffixes (``oracle-se2(byol)`` ->
    ``oracle-se2``) and reconciles the two spellings AWS uses for the same
    engine: instances report ``postgres`` where RIs report ``postgresql``, and
    bare ``aurora`` is the legacy name for ``aurora-mysql``.
    """
    e = (engine or "").strip().lower()
    if not e:
        return ""
    e = e.split("(")[0].strip()
    return {"postgres": "postgresql", "aurora": "aurora-mysql"}.get(e, e)


def _match_key(service: str, resource_type: str) -> str:
    """The membership key for a service: exact type for OS/Redshift, else family."""
    if service in _EXACT_TYPE_SERVICES:
        return normalize_type(resource_type)
    return instance_family(resource_type)


@dataclass(frozen=True)
class CommitmentCoverage:
    """Immutable snapshot of the account's active commitments in one region.

    Membership sets hold, per service, the families (or exact types for
    non-size-flexible reservations) that carry an active commitment.
    ``uncovered_on_demand`` maps ``"{service}:{key}"`` to the Cost-Explorer
    uncovered on-demand $/mo for that family — the realizable ceiling.
    """

    region: str = ""
    # EC2 — Savings Plans + classic Reserved Instances.
    ec2_sp_families: frozenset[str] = field(default_factory=frozenset)
    ec2_ri_families: frozenset[str] = field(default_factory=frozenset)
    ec2_ri_types: frozenset[str] = field(default_factory=frozenset)
    has_compute_sp: bool = False
    has_sagemaker_sp: bool = False
    # Data-store / cache Reserved Instances (family-flexible unless noted).
    # RDS/Aurora share one pool; RIs are engine-scoped, so the (family, engine)
    # pairs are authoritative and ``rds_ri_families`` is the engine-agnostic
    # fallback used when a rec does not carry its engine.
    rds_ri_families: frozenset[str] = field(default_factory=frozenset)
    rds_ri_engine_families: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    elasticache_ri_families: frozenset[str] = field(default_factory=frozenset)
    # Non-size-flexible: exact node/instance types.
    redshift_ri_types: frozenset[str] = field(default_factory=frozenset)
    opensearch_ri_types: frozenset[str] = field(default_factory=frozenset)
    # DynamoDB reserved capacity (RCU/WCU units, not instances) — presence only.
    dynamodb_reserved: bool = False
    # CE headroom: "{service}:{key}" -> uncovered on-demand $/mo (realizable cap).
    uncovered_on_demand: Mapping[str, float] = field(default_factory=dict)
    sp_utilization_pct: float | None = None
    sp_unused_monthly: float | None = None

    # --- coverage predicates (per service) --------------------------------
    def covers_ec2(self, instance_type: str) -> bool:
        """True if an EC2 SP / RI or Compute SP covers this instance."""
        fam = instance_family(instance_type)
        return (
            self.has_compute_sp
            or fam in self.ec2_sp_families
            or fam in self.ec2_ri_families
            or normalize_type(instance_type) in self.ec2_ri_types
        )

    def covers_lambda(self) -> bool:
        """True if a Compute SP covers Lambda usage (EC2-Instance SPs do not)."""
        return self.has_compute_sp

    def covers_containers(self) -> bool:
        """True if a Compute SP covers Fargate (ECS/EKS) usage."""
        return self.has_compute_sp

    def covers_sagemaker(self) -> bool:
        """True if a SageMaker SP covers SageMaker usage (Compute SP does not)."""
        return self.has_sagemaker_sp

    def covers_dynamodb(self) -> bool:
        """True if the account holds DynamoDB reserved capacity."""
        return self.dynamodb_reserved

    def covers_rds(self, instance_class: str, engine: str = "") -> bool:
        """True if a Reserved DB Instance covers this instance's family + engine.

        RDS/Aurora RIs are size-flexible within a family but **engine-scoped** —
        an ``aurora-mysql`` reservation does not cover a ``mysql`` instance. When
        ``engine`` is given and engine-tagged RIs were resolved, both must match;
        otherwise falls back to the engine-agnostic family check.
        """
        fam = instance_family(instance_class)
        if engine and self.rds_ri_engine_families:
            return (fam, normalize_engine(engine)) in self.rds_ri_engine_families
        return fam in self.rds_ri_families

    def covers_aurora(self, instance_class: str, engine: str = "") -> bool:
        """True if a Reserved DB Instance covers this Aurora instance (same pool)."""
        return self.covers_rds(instance_class, engine)

    def covers_elasticache(self, node_type: str) -> bool:
        """True if a Reserved Cache Node covers this node's family."""
        return instance_family(node_type) in self.elasticache_ri_families

    def covers_redshift(self, node_type: str) -> bool:
        """True if a Reserved Node covers this EXACT node type (not size-flexible)."""
        return normalize_type(node_type) in self.redshift_ri_types

    def covers_opensearch(self, instance_type: str) -> bool:
        """True if a Reserved Instance covers this EXACT type (not size-flexible)."""
        return normalize_type(instance_type) in self.opensearch_ri_types

    def covers(self, service: str, resource_type: str, engine: str = "") -> bool:
        """Dispatch coverage check by service key (for the data-store adapters)."""
        if service in _RDS_RI_SERVICES:
            return self.covers_rds(resource_type, engine)
        return {
            "ec2": self.covers_ec2,
            "elasticache": self.covers_elasticache,
            "redshift": self.covers_redshift,
            "opensearch": self.covers_opensearch,
        }.get(service, lambda _t: False)(resource_type)

    def realizable_ceiling(self, service: str, resource_type: str) -> float | None:
        """Uncovered on-demand $/mo for this rec's EXACT instance type — the cap.

        Keyed by exact type, not family: on-demand overflow concentrates in
        individual sizes, so a family-aggregate ceiling would let a rec against a
        fully-covered size spend a sibling size's headroom.

        ``None`` when Cost Explorer coverage could not be resolved, or when the
        type carries no on-demand spend — both demote the candidate (safe).
        """
        if not self.uncovered_on_demand:
            return None
        return self.uncovered_on_demand.get(f"{service}:{normalize_type(resource_type)}")

    @property
    def has_any_commitment(self) -> bool:
        """True if any commitment was detected in this region."""
        return bool(
            self.has_compute_sp
            or self.has_sagemaker_sp
            or self.dynamodb_reserved
            or self.ec2_sp_families
            or self.ec2_ri_families
            or self.ec2_ri_types
            or self.rds_ri_families
            or self.elasticache_ri_families
            or self.redshift_ri_types
            or self.opensearch_ri_types
        )

    def ri_note(self, service: str, resource_type: str, gross: float) -> str:
        """Human-readable reason a Reserved-Instance-covered rec was demoted."""
        key = _match_key(service, resource_type)
        label = {
            "rds": "RDS Reserved DB Instance",
            "aurora": "RDS Reserved DB Instance",
            "elasticache": "ElastiCache Reserved Cache Node",
            "redshift": "Redshift Reserved Node",
            "opensearch": "OpenSearch Reserved Instance",
        }.get(service, "Reserved Instance")
        return (
            f"Covered by an active {key} {label} in {self.region}. The ${gross:,.2f}/mo figure is "
            f"on-demand basis; the reservation is fixed spend that continues after rightsizing, so "
            f"the realizable saving requires the freed reservation to be reused or to expire — not counted."
        )

    def ec2_note(self, instance_type: str, gross: float) -> str:
        """Human-readable reason an EC2 rec was demoted to advisory."""
        fam = instance_family(instance_type)
        if self.has_compute_sp and fam not in self.ec2_sp_families and fam not in self.ec2_ri_families:
            basis = "an active Compute Savings Plan"
        elif fam in self.ec2_ri_families or normalize_type(instance_type) in self.ec2_ri_types:
            basis = f"an active {fam} EC2 Reserved Instance in {self.region}"
        else:
            basis = f"an active {fam} EC2-Instance Savings Plan in {self.region}"
        util = f" (SP utilization {self.sp_utilization_pct:.0f}%)" if self.sp_utilization_pct is not None else ""
        return (
            f"Covered by {basis}{util}. The ${gross:,.2f}/mo figure is on-demand basis; "
            f"the realizable saving requires the freed commitment to be reabsorbed by other "
            f"in-family usage or the plan to expire, so it is not counted."
        )

    def plan_note(self, kind: str, gross: float) -> str:
        """Demotion note for a Compute/SageMaker SP-covered non-EC2 rec."""
        return (
            f"Covered by an active {kind}; the on-demand ${gross:,.2f}/mo is not realizable "
            f"while the commitment bills regardless of rightsizing — not counted."
        )


# Fail-safe empty coverage: adapters treat it as "nothing covered".
EMPTY_COVERAGE = CommitmentCoverage()

# Instance/node type keys AWS nests inside a CoH rec's configuration.
_COH_TYPE_KEYS: tuple[str, ...] = ("dbInstanceClass", "cacheNodeType", "nodeType", "instanceType", "type")


def coh_resource_type(rec: dict[str, Any]) -> str:
    """Extract the current instance/node type from any CoH recommendation.

    CoH nests the type as ``currentResourceDetails.<wrapper>.configuration.
    instance.<typeKey>`` (verified for EC2 ``type`` and RDS ``dbInstanceClass``).
    Returns ``""`` when absent (maps to a non-matching key — safe: no demotion).
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


def split_by_commitment(
    recs: list[dict[str, Any]],
    *,
    is_covered: Callable[[dict[str, Any]], bool],
    gross_of: Callable[[dict[str, Any]], float],
    note_of: Callable[[dict[str, Any], float], str],
    ceiling_of: Callable[[dict[str, Any]], float | None] | None = None,
    key_of: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split recs into (counted, advisory) by active-commitment coverage.

    Pure (no boto3, no mutation). Uncovered recs pass through counted. Covered
    recs are *candidates*: with no ``ceiling_of`` every candidate is demoted
    (layer-1, safe default); with a ceiling, candidates sharing a ``key_of``
    family key are counted greedily (highest gross first) up to that family's
    uncovered-on-demand ceiling and only the remainder demoted (layer-2 headroom
    cap). Demoted recs are new dicts carrying ``Counted = False`` +
    ``AdvisoryEstimate`` (gross) + ``CommitmentCoverageNote``.

    The greedy budget is keyed by ``key_of`` (the family), NOT by the ceiling
    value, so two distinct families that happen to share an equal uncovered-$
    figure keep independent budgets.
    """
    counted: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for rec in recs:
        (candidates if is_covered(rec) else counted).append(rec)

    if ceiling_of is None:
        # Layer-1: demote every candidate.
        advisory = [_demote(rec, gross_of(rec), note_of) for rec in candidates]
        return counted, advisory

    # Layer-2: greedy fill up to each family's uncovered-on-demand ceiling.
    _key = key_of or (lambda _r: "")
    budgets: dict[str, float] = {}
    advisory: list[dict[str, Any]] = []
    for rec in sorted(candidates, key=lambda r: gross_of(r), reverse=True):
        ceiling = ceiling_of(rec)
        gross = gross_of(rec)
        if ceiling is None or ceiling <= 0:
            advisory.append(_demote(rec, gross, note_of))
            continue
        fam = _key(rec)
        remaining = budgets.get(fam, ceiling)
        if gross <= remaining:
            budgets[fam] = remaining - gross
            counted.append(rec)
        else:
            budgets[fam] = remaining
            advisory.append(_demote(rec, gross, note_of))
    return counted, advisory


def _demote(rec: dict[str, Any], gross: float, note_of: Callable[[dict[str, Any], float], str]) -> dict[str, Any]:
    """Return a Counted=False copy of ``rec`` annotated with the demotion reason."""
    return {**rec, "Counted": False, "AdvisoryEstimate": gross, "CommitmentCoverageNote": note_of(rec, gross)}


def rec_gross(rec: dict[str, Any]) -> float:
    """Best-effort on-demand gross saving of a rec across the field-name variants."""
    for key in ("EstimatedMonthlySavings", "estimatedMonthlySavings", "monthly_savings"):
        val = rec.get(key)
        if val:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    raw = str(rec.get("EstimatedSavings", "") or "")
    digits = "".join(c for c in raw if c.isdigit() or c == ".")
    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return 0.0


def demote_recs_in_place(
    recs: list[dict[str, Any]],
    note: Callable[[float], str],
    *,
    only: Callable[[dict[str, Any]], bool] | None = None,
) -> float:
    """Mark each counted rec advisory in place (whole-service commitment gate).

    For services whose commitment coverage is all-or-nothing (Compute SP over
    Fargate/Lambda, SageMaker SP over SageMaker, DynamoDB reserved capacity) —
    no per-family ceiling applies, so every counted rec that ``only`` admits is
    demoted. Follows these adapters' existing in-place-mutation convention.
    Returns the total gross removed from the counted headline.
    """
    removed = 0.0
    for rec in recs:
        if rec.get("Counted") is False:
            continue
        if only is not None and not only(rec):
            continue
        gross = rec_gross(rec)
        rec["Counted"] = False
        rec["AdvisoryEstimate"] = gross
        rec["CommitmentCoverageNote"] = note(gross)
        removed += gross
    return removed


def demote_covered_in_place(
    recs: list[dict[str, Any]],
    coverage: "CommitmentCoverage | None",
    service: str,
    type_of: Callable[[dict[str, Any]], str],
    *,
    engine_of: Callable[[dict[str, Any]], str] | None = None,
    zero_keys: tuple[str, ...] = (),
) -> float:
    """Demote commitment-covered *locally-derived* recs, capped by CE headroom.

    The CoH/Compute-Optimizer path (``demote_coh_by_commitment``) does not see an
    adapter's own ``enhanced_checks`` levers, yet a downsizing rec against an
    RI-covered node is exactly as unrealizable: the reservation bills regardless.
    This applies the same two-layer treatment to those recs, in place (matching
    those adapters' mutation convention).

    Candidates on one **exact instance type** are counted greedily (highest gross
    first) up to that type's uncovered on-demand ceiling — genuine overflow
    survives — and the remainder demote. A type with no headroom demotes wholly.
    No coverage / no commitment / no gross -> no-op.

    ``zero_keys`` names numeric savings fields to zero on a demoted rec, for
    adapters that total their headline straight off a rec field (the gross is
    preserved in ``AdvisoryEstimate``), keeping counted == rendered.

    Returns the total gross removed from the counted headline.
    """
    if coverage is None or not coverage.has_any_commitment:
        return 0.0
    candidates = [
        rec
        for rec in recs
        if rec.get("Counted") is not False
        and rec_gross(rec) > 0
        and coverage.covers(service, type_of(rec), engine_of(rec) if engine_of else "")
    ]
    budgets: dict[str, float] = {}
    removed = 0.0
    for rec in sorted(candidates, key=rec_gross, reverse=True):
        rtype = type_of(rec)
        gross = rec_gross(rec)
        key = normalize_type(rtype)
        if key not in budgets:
            ceiling = coverage.realizable_ceiling(service, rtype)
            budgets[key] = ceiling if ceiling and ceiling > 0 else 0.0
        if gross <= budgets[key]:
            budgets[key] -= gross  # realizable on-demand overflow — stays counted
            continue
        rec["Counted"] = False
        rec["AdvisoryEstimate"] = gross
        rec["CommitmentCoverageNote"] = coverage.ri_note(service, rtype, gross)
        for zkey in zero_keys:
            if zkey in rec:
                rec[zkey] = 0.0
        removed += gross
    return removed


def demote_coh_by_commitment(
    coh_recs: list[dict[str, Any]],
    coverage: "CommitmentCoverage | None",
    service: str,
    gross_of: Callable[[dict[str, Any]], float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a data-store adapter's CoH recs into (counted, advisory) by RI cover.

    Applies the CE headroom cap when ``coverage`` carries per-family uncovered
    on-demand data; otherwise demotes every covered candidate (safe default).
    No-op (all counted) when coverage is absent/empty.
    """
    if coverage is None or not coverage.has_any_commitment:
        return list(coh_recs), []
    has_ceiling = bool(coverage.uncovered_on_demand)
    return split_by_commitment(
        coh_recs,
        is_covered=lambda r: coverage.covers(service, coh_resource_type(r)),
        gross_of=gross_of,
        note_of=lambda r, g: coverage.ri_note(service, coh_resource_type(r), g),
        ceiling_of=(lambda r: coverage.realizable_ceiling(service, coh_resource_type(r))) if has_ceiling else None,
        # Budget key must match the ceiling key (exact type), so sibling sizes
        # never share a headroom budget.
        key_of=lambda r: normalize_type(coh_resource_type(r)),
    )


# ------------------------------------------------------------------------
# Live fetch
# ------------------------------------------------------------------------
def _is_permission_error(exc: Exception) -> bool:
    """True for IAM access-denied style errors (vs transient/service errors)."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthorizationError")
    return False


def _report(ctx: Any, exc: Exception, message: str, action: str) -> None:
    """Route a fetch error to permission_issue (IAM) or warn (everything else)."""
    if _is_permission_error(exc):
        ctx.permission_issue(f"{message}: {exc}", "commitment_coverage", action)
    else:
        ctx.warn(f"{message}: {exc}", service="commitment_coverage")


def _fetch_savings_plans(ctx: Any) -> tuple[frozenset[str], bool, bool]:
    """Return (region EC2-Instance SP families, any Compute SP, any SageMaker SP).

    EC2-Instance SPs are region-locked (filter on ``region``); Compute and
    SageMaker SPs are region-flexible (a plan anywhere covers the scan region).
    """
    families: set[str] = set()
    has_compute = has_sagemaker = False
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
                elif sp_type == "SageMaker":
                    has_sagemaker = True
                elif sp_type == "EC2Instance" and sp.get("region", "") == ctx.region:
                    fam = (sp.get("ec2InstanceFamily", "") or "").lower()
                    if fam:
                        families.add(fam)
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 — fail-safe: no demotion on read failure
        _report(ctx, exc, "Could not read Savings Plans (rightsizing recs not SP-adjusted)", "savingsplans:DescribeSavingsPlans")
    return frozenset(families), has_compute, has_sagemaker


def _fetch_ec2_reserved(ctx: Any) -> tuple[frozenset[str], frozenset[str]]:
    """Return classic EC2 Reserved Instances as (regional families, zonal types).

    A regional (size-flexible) RI covers a whole family; a zonal RI covers only
    its exact instance type. Both are region-scoped by the client.
    """
    families: set[str] = set()
    types: set[str] = set()
    try:
        client = ctx.client("ec2")
        resp = client.describe_reserved_instances(Filters=[{"Name": "state", "Values": ["active"]}])
        for ri in resp.get("ReservedInstances", []):
            itype = ri.get("InstanceType", "")
            if not itype:
                continue
            if ri.get("Scope") == "Region":
                families.add(instance_family(itype))
            else:
                types.add(normalize_type(itype))
    except Exception as exc:  # noqa: BLE001
        _report(ctx, exc, "Could not read EC2 Reserved Instances", "ec2:DescribeReservedInstances")
    return frozenset(families), frozenset(types)


def _fetch_rds_reserved(ctx: Any) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """Active Reserved DB Instances as (families, (family, engine) pairs).

    RDS/Aurora RIs are size-flexible within a family but engine-scoped, so the
    engine-tagged pairs are what coverage matching should use.
    """
    families: set[str] = set()
    engine_families: set[tuple[str, str]] = set()
    try:
        for page in ctx.client("rds").get_paginator("describe_reserved_db_instances").paginate():
            for ri in page.get("ReservedDBInstances", []):
                if ri.get("State") != "active":
                    continue
                fam = instance_family(ri.get("DBInstanceClass", ""))
                if not fam:
                    continue
                families.add(fam)
                engine = normalize_engine(ri.get("ProductDescription", ""))
                if engine:
                    engine_families.add((fam, engine))
    except Exception as exc:  # noqa: BLE001 — fail-safe
        _report(ctx, exc, "Could not read rds Reserved Instances (recs not RI-adjusted)", "rds:DescribeReserved")
    return frozenset(families), frozenset(engine_families)


def _fetch_ri_families(ctx: Any, service: str) -> frozenset[str]:
    """Active Reserved-Instance families (elasticache) or exact types (redshift/opensearch)."""
    keys: set[str] = set()
    exact = service in _EXACT_TYPE_SERVICES
    add = normalize_type if exact else instance_family
    try:
        if service == "elasticache":
            for page in ctx.client("elasticache").get_paginator("describe_reserved_cache_nodes").paginate():
                for ri in page.get("ReservedCacheNodes", []):
                    if ri.get("State") == "active":
                        keys.add(add(ri.get("CacheNodeType", "")))
        elif service == "redshift":
            for page in ctx.client("redshift").get_paginator("describe_reserved_nodes").paginate():
                for ri in page.get("ReservedNodes", []):
                    if ri.get("State") == "active":
                        keys.add(add(ri.get("NodeType", "")))
        elif service == "opensearch":
            resp = ctx.client("opensearch").describe_reserved_instances()
            for ri in resp.get("ReservedInstances", []):
                if ri.get("State", "").lower() in ("active", "payment-pending"):
                    keys.add(add(ri.get("InstanceType", "")))
    except Exception as exc:  # noqa: BLE001 — fail-safe per service
        _report(ctx, exc, f"Could not read {service} Reserved Instances (recs not RI-adjusted)", f"{service}:DescribeReserved")
    keys.discard("")
    return frozenset(keys)


def _fetch_sp_utilization(ctx: Any) -> tuple[float | None, float | None]:
    """Return (last-30d SP utilization %, unused commitment $/mo), best-effort."""
    try:
        start, end = _last_30d(ctx)
        resp = ctx.client("ce").get_savings_plans_utilization(
            TimePeriod={"Start": start, "End": end}, Granularity="MONTHLY"
        )
        util = resp.get("Total", {}).get("Utilization", {})
        pct = util.get("UtilizationPercentage")
        unused = util.get("UnusedCommitment")
        return (float(pct) if pct is not None else None, float(unused) if unused is not None else None)
    except Exception:  # noqa: BLE001 — contextual only; never fatal
        return None, None


def _fetch_dynamodb_reserved(ctx: Any) -> bool:
    """True if the account has DynamoDB reserved capacity (via CE utilization)."""
    try:
        start, end = _last_30d(ctx)
        resp = ctx.client("ce").get_reservation_utilization(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon DynamoDB"]}},
        )
        total = resp.get("Total", {})
        purchased = total.get("PurchasedHours") or total.get("TotalActualUnits") or "0"
        return float(purchased or 0) > 0
    except Exception:  # noqa: BLE001 — best-effort; absence -> not reserved
        return False


# Cost Explorer ``SERVICE`` dimension values for the on-demand headroom read.
# Aurora bills under the RDS service dimension.
_CE_SERVICE_DIM: dict[str, str] = {
    "ec2": "Amazon Elastic Compute Cloud - Compute",
    "rds": "Amazon Relational Database Service",
    "aurora": "Amazon Relational Database Service",
    "elasticache": "Amazon ElastiCache",
    "redshift": "Amazon Redshift",
    "opensearch": "Amazon OpenSearch Service",
}

# Trailing window for the headroom read. Deliberately short: it must reflect the
# CURRENT commitment posture. A 30-day window spanning a mid-window RI purchase
# reports on-demand spend that the now-active reservation already absorbs, which
# would inflate the ceiling and let a phantom saving through.
_HEADROOM_DAYS = 7

# Hard page cap for the headroom read (7 days x a handful of instance types fits
# in one page; the cap only exists so a pathological token can never spin).
_MAX_CE_PAGES = 20


def _fetch_uncovered_on_demand(ctx: Any, selected: set[str]) -> dict[str, float]:
    """Per-(service, exact instance type) uncovered on-demand $/mo — the ceiling.

    Read from ``GetCostAndUsage`` filtered to ``PURCHASE_TYPE="On Demand
    Instances"`` (SP- and RI-covered usage bills under other purchase types) and
    grouped by ``INSTANCE_TYPE``, over the trailing ``_HEADROOM_DAYS`` scaled to
    a 30-day run rate.

    Keyed by **exact instance type**, never family: on-demand overflow
    concentrates in individual sizes (a family-level ceiling would let a rec
    against a fully-covered size spend a sibling size's headroom). Types absent
    from the result carry no headroom, so their recs demote.

    ``GetReservationCoverage`` is deliberately not used: its
    ``Coverage.CoverageCost.OnDemandCost`` is ``null`` for RDS/ElastiCache/
    OpenSearch, and it rejects an ``INSTANCE_TYPE_FAMILY`` groupBy. A failed read
    omits that service, so the caller falls back to demote-all for it (safe).
    """
    out: dict[str, float] = {}
    try:
        ce = ctx.client("ce")
    except Exception:  # noqa: BLE001
        return out
    start, end = _trailing_window(ctx, _HEADROOM_DAYS)

    for svc in sorted(selected & set(_CE_SERVICE_DIM)):
        totals: dict[str, float] = {}
        try:
            token: str | None = None
            # Bounded: a page cap plus a strict string-token check means a service
            # that echoes the same token (or a stubbed client) can never spin here.
            for _page in range(_MAX_CE_PAGES):
                kwargs: dict[str, Any] = {
                    "TimePeriod": {"Start": start, "End": end},
                    "Granularity": "DAILY",
                    "Metrics": ["UnblendedCost"],
                    "GroupBy": [{"Type": "DIMENSION", "Key": "INSTANCE_TYPE"}],
                    "Filter": {
                        "And": [
                            {"Dimensions": {"Key": "SERVICE", "Values": [_CE_SERVICE_DIM[svc]]}},
                            {"Dimensions": {"Key": "REGION", "Values": [ctx.region]}},
                            {"Dimensions": {"Key": "PURCHASE_TYPE", "Values": ["On Demand Instances"]}},
                        ]
                    },
                }
                if token:
                    kwargs["NextPageToken"] = token
                resp = ce.get_cost_and_usage(**kwargs)
                for day in resp.get("ResultsByTime", []):
                    for grp in day.get("Groups", []):
                        raw = (grp.get("Keys") or [""])[0]
                        # Non-instance RDS/EC2 spend (storage, IO, data transfer).
                        if not raw or raw.lower().startswith("noinstancetype"):
                            continue
                        key = normalize_type(raw)
                        amount = grp.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")
                        try:
                            totals[key] = totals.get(key, 0.0) + float(amount or 0)
                        except (TypeError, ValueError):
                            continue
                next_token = resp.get("NextPageToken")
                if not isinstance(next_token, str) or not next_token or next_token == token:
                    break
                token = next_token
        except Exception as exc:  # noqa: BLE001 — fail-safe per service
            _report(ctx, exc, f"Could not read {svc} on-demand spend (headroom cap unavailable)", "ce:GetCostAndUsage")
            continue

        scale = 30.0 / _HEADROOM_DAYS
        for key, amount in totals.items():
            if key and amount > 0:
                out[f"{svc}:{key}"] = out.get(f"{svc}:{key}", 0.0) + amount * scale
    return out


def _trailing_window(ctx: Any, days: int) -> tuple[str, str]:
    """(start, end) ISO dates for the trailing ``days``-day window."""
    from datetime import UTC, datetime, timedelta

    end = datetime.now(UTC).date()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


def _last_30d(ctx: Any) -> tuple[str, str]:
    """(start, end) ISO dates for the trailing 30-day window."""
    return _trailing_window(ctx, 30)


def fetch_commitment_coverage(ctx: Any, selected: set[str]) -> CommitmentCoverage:
    """Resolve the account's active commitments for the scan region.

    Only queries the APIs a selected service needs. Every source is individually
    error-isolated — a failure leaves that dimension empty (no demotion) and
    surfaces a warning/permission issue, so the scan never crashes and
    degradation is visible.
    """
    want_sp = bool(selected & {"ec2", "lambda", "containers", "sagemaker"})
    ec2_fams: frozenset[str] = frozenset()
    has_compute = has_sagemaker = False
    ec2_ri_fams: frozenset[str] = frozenset()
    ec2_ri_types: frozenset[str] = frozenset()
    util_pct = unused = None

    if want_sp:
        ec2_fams, has_compute, has_sagemaker = _fetch_savings_plans(ctx)
    if "ec2" in selected:
        ec2_ri_fams, ec2_ri_types = _fetch_ec2_reserved(ctx)

    has_sp = bool(ec2_fams or has_compute or has_sagemaker)
    if has_sp:
        util_pct, unused = _fetch_sp_utilization(ctx)

    # Aurora and RDS share one Reserved DB Instance pool — one read serves both.
    rds_fams: frozenset[str] = frozenset()
    rds_engine_fams: frozenset[tuple[str, str]] = frozenset()
    if selected & _RDS_RI_SERVICES:
        rds_fams, rds_engine_fams = _fetch_rds_reserved(ctx)

    coverage = CommitmentCoverage(
        region=ctx.region,
        ec2_sp_families=ec2_fams,
        ec2_ri_families=ec2_ri_fams,
        ec2_ri_types=ec2_ri_types,
        has_compute_sp=has_compute,
        has_sagemaker_sp=has_sagemaker,
        rds_ri_families=rds_fams,
        rds_ri_engine_families=rds_engine_fams,
        elasticache_ri_families=_fetch_ri_families(ctx, "elasticache") if "elasticache" in selected else frozenset(),
        redshift_ri_types=_fetch_ri_families(ctx, "redshift") if "redshift" in selected else frozenset(),
        opensearch_ri_types=_fetch_ri_families(ctx, "opensearch") if "opensearch" in selected else frozenset(),
        dynamodb_reserved=_fetch_dynamodb_reserved(ctx) if "dynamodb" in selected else False,
        sp_utilization_pct=util_pct,
        sp_unused_monthly=unused,
    )
    # CE headroom cap — only worth the calls when something is actually reserved.
    if coverage.has_any_commitment:
        coverage = replace(coverage, uncovered_on_demand=_fetch_uncovered_on_demand(ctx, selected))
    return coverage
