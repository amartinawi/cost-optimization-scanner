"""Pure decision logic for the RDS adapter — no AWS, no ScanContext.

Extracted so cross-source de-duplication (Cost Hub > Compute Optimizer >
heuristic), the Reserved-Instance demotion, and the savings/count arithmetic
can be unit-tested without boto3 or live pricing. Mirrors ``services/ebs_logic``.
"""

from __future__ import annotations

from typing import Any

from services._savings import compute_optimizer_savings, parse_dollar_savings

# Enhanced-check categories shown for context but whose dollar value is NOT
# summed into the RDS headline ("advisory"):
#   - Reserved Instances: the authoritative domain of the commitment_analysis tab
#     (which renders CoH RI recs); an RI saving stacks with — rather than replaces
#     — a rightsizing/Multi-AZ saving on the same DB.
#   - Backup Retention: the billable backup amount cannot be derived at scan time
#     (free allotment = 100% of provisioned storage; the excess needs Cost
#     Explorer / actual backup bytes), so we surface the lever without a fabricated
#     $ figure rather than guess (audit N-M2).
RI_CATEGORY = "Reserved Instance Opportunities"
BACKUP_CATEGORY = "Backup Retention Optimization"
ADVISORY_CATEGORIES = frozenset({RI_CATEGORY, BACKUP_CATEGORY})

# Authority ranks for cross-source de-duplication (lower wins). Cost Hub
# (rank -1, handled separately via coh_keys suppression) > Compute Optimizer >
# heuristic.
_AUTH_CO = 1
_AUTH_HEURISTIC = 2


def normalize_rds_arn(raw: str) -> str:
    """Canonical de-duplication key for an RDS ARN / resource id.

    DB instances and clusters reduce to their bare name so the three sources
    converge on the same key regardless of representation::

        arn:aws:rds:us-east-1:1:db:prod       -> prod   (Compute Optimizer / heuristic)
        prod                                  -> prod   (Cost Optimization Hub resourceId)
        arn:aws:rds:us-east-1:1:cluster:prod  -> prod

    Snapshot resource types keep their ``<type>:<name>`` prefix so a snapshot id
    never de-duplicates against an instance id (different namespaces)::

        arn:aws:rds:us-east-1:1:snapshot:s1        -> snapshot:s1
        arn:aws:rds:us-east-1:1:cluster-snapshot:c -> cluster-snapshot:c

    A bare id with no ARN structure is returned unchanged; falsy input -> ``""``.
    """
    if not raw:
        return ""
    parts = str(raw).split(":")
    if parts[0] == "arn" and len(parts) >= 7:
        rtype = parts[5]
        name = ":".join(parts[6:])
        return f"{rtype}:{name}" if "snapshot" in rtype else name
    return str(raw)


def co_rds_key(rec: dict[str, Any]) -> str:
    """Normalized dedup key for a Compute Optimizer RDS recommendation."""
    return normalize_rds_arn(rec.get("resourceArn") or rec.get("resourceId") or "")


def coh_rds_key(rec: dict[str, Any]) -> str:
    """Normalized dedup key for a Cost Optimization Hub RDS recommendation."""
    return normalize_rds_arn(rec.get("resourceArn") or rec.get("resourceId") or "")


def enhanced_rds_key(rec: dict[str, Any]) -> str:
    """Normalized dedup key for an in-house heuristic RDS recommendation."""
    return normalize_rds_arn(rec.get("resourceArn") or "")


def is_advisory_rec(rec: dict[str, Any]) -> bool:
    """True when an enhanced rec is advisory (rendered/counted but not summed)."""
    return rec.get("CheckCategory") in ADVISORY_CATEGORIES


def is_snapshot_rec(rec: dict[str, Any]) -> bool:
    """True when an enhanced rec belongs to a snapshot category (independent key)."""
    return "snapshot" in str(rec.get("CheckCategory", "")).lower()


def enhanced_savings(rec: dict[str, Any]) -> float:
    """Monthly $ parsed from an enhanced rec's ``EstimatedSavings`` string."""
    est = rec.get("EstimatedSavings", "")
    return parse_dollar_savings(est) if "$" in est else 0.0


def partition_enhanced(
    recs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split enhanced recs into ``(concrete, snapshots, advisory)``.

    - ``concrete``  — single-remediation cost findings that dedup against each
      other and against Compute Optimizer (Multi-AZ disable, scheduling).
    - ``snapshots`` — old-snapshot findings; independent keys, counted.
    - ``advisory``  — RI + backup-retention cards; rendered/counted but not summed
      into the savings headline (see ADVISORY_CATEGORIES).
    """
    concrete: list[dict[str, Any]] = []
    snaps: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    for rec in recs:
        if is_advisory_rec(rec):
            # Both RI and Backup-Retention advisory cards carry an explicit
            # Counted=False so they render but are excluded from the savings
            # total AND the recommendation count (previously only RI was flagged,
            # so the 7 Backup-Retention cards padded the count — RDS count fix).
            advisory.append(dict(rec, Counted=False))
        elif is_snapshot_rec(rec):
            snaps.append(rec)
        else:
            concrete.append(rec)
    return concrete, snaps, advisory


def reconcile_snapshot_savings(
    snaps: list[dict[str, Any]],
    backup_actuals: dict[str, float] | None,
) -> list[dict[str, Any]]:
    """Cap snapshot upper-bound savings at actual billed backup, per engine pool.

    Snapshot savings use provisioned size (an upper bound). When Cost Explorer
    reports the *actual* billed backup for the region (``services.advisor.
    get_rds_backup_actuals``), cap each engine group's snapshot savings at it:
    manual snapshots are a subset of total backup spend, so the actual is a valid
    (tighter) ceiling. Capping is applied when the actual is a POSITIVE number
    below the group's summed upper bound. A 0/missing/unreadable actual means the
    upper bound cannot be substantiated against billing, so those recs are demoted
    to $0 advisories (the bound survives as ``PotentialMonthlySavings``) — never
    counted. Failing this check open would overstate, since actual backup bytes sit
    well below provisioned size. Advisory/size-unknown snaps (no ``$``) pass through.

    Returns a new list; capped recs are copies with an updated EstimatedSavings
    and AuditBasis (``reconciled_to_actual_billed`` / ``reconciliation_factor``).
    """
    # An absent/empty Cost-Explorer read is NOT "no cap needed" — it means the
    # provisioned-size upper bound is unsubstantiated. Fall through to the
    # per-group branch below, which demotes an uncorroborated bound to a $0
    # advisory (F5). Returning early here counted the UNCAPPED upper bound
    # whenever ce:GetCostAndUsage was denied or throttled: on bnc that is
    # $1,131.45 instead of $411.87 — a $719.58/mo silent overstatement, in the
    # one direction this scanner must never fail.
    backup_actuals = backup_actuals or {}

    def _is_aurora(s: dict[str, Any]) -> bool:
        return str(s.get("engine") or "").lower().startswith("aurora") or "Aurora" in str(
            s.get("CheckCategory", "")
        )

    aurora = [s for s in snaps if _is_aurora(s)]
    standard = [s for s in snaps if not _is_aurora(s)]
    out: list[dict[str, Any]] = []
    for group_key, items in (("aurora", aurora), ("standard", standard)):
        cap = backup_actuals.get(group_key)
        upper = sum(enhanced_savings(s) for s in items)
        apply_cap = cap is not None and 0 < cap < upper
        factor = (cap / upper) if (cap is not None and apply_cap and upper) else 1.0
        for s in items:
            sv = enhanced_savings(s)
            if sv <= 0:  # advisory / size-unknown — leave untouched
                out.append(s)
                continue
            # Always copy + annotate numeric snaps so the report is auditable even
            # when no cap is applied (distinguishes "actual >= upper" from a CE gap).
            new_rec = dict(s)
            basis = dict(s.get("AuditBasis", {}))
            if cap is not None and cap > 0:
                basis["actual_billed_backup_pool"] = round(cap, 2)
            if apply_cap:
                capped_sv = round(sv * factor, 2)
                new_rec["EstimatedSavings"] = (
                    f"${capped_sv:.2f}/month (reconciled to actual billed backup via Cost Explorer)"
                )
                # Cap the NUMERIC field too — the headline sums the EstimatedSavings
                # string (capped) but the per-rec EstimatedMonthlySavings was left at
                # the uncapped upper bound, so any consumer that sums the numeric
                # overstated the saving (confirmed +$719.60 in the field). Keep the
                # numeric and the string in lockstep (cardinal-sin: no overstated $).
                new_rec["EstimatedMonthlySavings"] = capped_sv
                basis["reconciled_to_actual_billed"] = round(cap, 2)  # type: ignore[arg-type]
                basis["reconciliation_factor"] = round(factor, 4)
                basis["upper_bound_before_reconciliation"] = round(sv, 2)
                new_rec["Reconciled"] = True
            elif cap is not None and cap > 0:
                basis["reconciliation"] = "not capped — upper bound <= actual billed backup"
            else:
                # F5 — there is no Cost Explorer actual to validate the
                # provisioned-size upper bound, so counting it would overstate the
                # saving (actual backup bytes are typically well below provisioned
                # size). Demote to a $0 advisory that still surfaces the upper-bound
                # figure for manual review but is never summed into the headline.
                basis["reconciliation"] = (
                    "no Cost Explorer actual available — upper bound retained as advisory (not counted)"
                )
                new_rec["Counted"] = False
                # A Counted=False advisory must carry a 0 numeric — leaving the
                # uncapped upper bound here would let any numeric-summing consumer
                # count a demoted advisory (advisory-leak). Surface the upper bound
                # via PotentialMonthlySavings instead, mirroring the EBS-snapshot
                # advisory convention.
                new_rec["EstimatedMonthlySavings"] = 0.0
                new_rec["PotentialMonthlySavings"] = round(sv, 2)
                new_rec["EstimatedSavings"] = (
                    f"up to ${sv:.2f}/month — advisory (provisioned-size upper bound; "
                    "grant ce:GetCostAndUsage backup actuals to quantify)"
                )
            new_rec["AuditBasis"] = basis
            out.append(new_rec)
    return out


def resolve_rds_findings(
    co_recs: list[dict[str, Any]],
    enhanced_recs: list[dict[str, Any]],
    *,
    coh_recs: list[dict[str, Any]] | None = None,
    backup_actuals: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], float, int]:
    """De-duplicate cost findings across sources; demote RI to advisory.

    Authority **Cost Hub > Compute Optimizer > heuristic**, keyed by normalized
    RDS resource id. Cost Optimization Hub is the authoritative aggregator (it
    re-surfaces Compute Optimizer's own rightsizing finding), so any DB covered
    by CoH suppresses that DB's CO and heuristic findings entirely. Among the
    remaining Compute Optimizer (rightsizing) and heuristic (Multi-AZ disable,
    scheduling, backup) findings — which are *different* remediations the user
    picks between, not redundant detections of the same one — only the single
    **highest-savings** finding survives per DB. The result: the rendered cards,
    the recommendation count, and the savings total all agree (no "cards sum to
    more than the tab total"). Snapshots are independent keys and always counted.
    Reserved-Instance recs are kept for display but excluded from the savings total.

    Args:
        co_recs: actionable Compute Optimizer recs (already opt-in/Optimized
            filtered upstream).
        enhanced_recs: heuristic recs from ``get_enhanced_rds_checks``.
        coh_recs: Cost Optimization Hub recs (highest authority). When a DB is
            covered by CoH, its CO and heuristic findings are suppressed; the CoH
            recs themselves are returned for the caller to render in their own
            source and are summed into the total.
        backup_actuals: ``{"standard": usd, "aurora": usd}`` from Cost Explorer
            (``get_rds_backup_actuals``); caps snapshot upper-bound savings at the
            actual billed backup per engine pool. Omit/``{}`` to keep upper bounds.

    Returns:
        ``(kept_coh, kept_co, kept_enhanced, total_savings, total_recommendations)``.
    """
    coh_recs = coh_recs or []
    coh_keys = {coh_rds_key(r) for r in coh_recs} - {""}

    concrete, snaps, advisory = partition_enhanced(enhanced_recs)
    # Cap snapshot upper-bound savings at actual billed backup (Cost Explorer).
    snaps = reconcile_snapshot_savings(snaps, backup_actuals)

    # Candidate single-remediation cost findings, excluding CoH-covered ids.
    # Each tuple: (authority, savings, origin, key, rec).
    candidates: list[tuple[int, float, str, str, dict[str, Any]]] = []
    for i, rec in enumerate(co_recs):
        key = co_rds_key(rec) or f"_anon_co_{i}"
        sav = compute_optimizer_savings(rec)
        if sav > 0 and key not in coh_keys:
            candidates.append((_AUTH_CO, sav, "co", key, rec))
    for i, rec in enumerate(concrete):
        key = enhanced_rds_key(rec) or f"_anon_enh_{i}"
        sav = enhanced_savings(rec)
        if sav > 0 and key not in coh_keys:
            candidates.append((_AUTH_HEURISTIC, sav, "enh", key, rec))

    # Winner per key: highest savings; ties broken toward Compute Optimizer
    # (lower _AUTH rank) for determinism.
    best: dict[str, tuple[int, float, str, dict[str, Any]]] = {}
    for auth, sav, origin, key, rec in candidates:
        cur = best.get(key)
        if cur is None or (sav, -auth) > (cur[1], -cur[0]):
            best[key] = (auth, sav, origin, rec)

    kept_co = [v[3] for v in best.values() if v[2] == "co"]
    kept_concrete = [v[3] for v in best.values() if v[2] == "enh"]
    cost_savings = sum(v[1] for v in best.values())

    # F5 — only reconciled / actual-validated snapshot savings are summed; an
    # unreconciled provisioned-size upper bound (Counted=False) renders but is not
    # counted toward the savings headline.
    snap_savings = sum(enhanced_savings(r) for r in snaps if r.get("Counted") is not False)
    coh_savings = sum(float(r.get("estimatedMonthlySavings", 0.0) or 0.0) for r in coh_recs)

    total_savings = coh_savings + cost_savings + snap_savings  # advisory excluded by design
    kept_enhanced = kept_concrete + snaps + advisory
    # Count hygiene: only recs that carry a real saving count toward the headline
    # opportunity count. Every Counted=False rec — upper-bound snaps (F5), RI and
    # Backup-Retention advisories — renders but is excluded (RDS count fix).
    total_recs = sum(
        1 for r in (list(coh_recs) + kept_co + kept_enhanced) if r.get("Counted") is not False
    )

    return list(coh_recs), kept_co, kept_enhanced, total_savings, total_recs
