"""Pure scenario math for commitment purchase recommendations.

Raw Cost Explorer response dicts in, normalized per-type scenario cards out.
No boto3, no ctx — every dollar transformation here is unit-testable with
plain dicts (pattern: services/rds_logic.py, services/commitment_coverage.py).

Field names and ``Service``/``SavingsPlansType`` strings verified against the
CE API docs on 2026-08-08 (Task 1 Step 0), via ``aws-knowledge``
``search_documentation``/``read_documentation`` against
``API_GetReservationPurchaseRecommendation``, ``API_InstanceDetails``,
``API_RDSInstanceDetails``, ``API_DynamoDBCapacityDetails``, and
``API_GetSavingsPlansPurchaseRecommendation``:

* ``Service`` request values confirmed: ``"Amazon Elastic Compute Cloud -
  Compute"`` (EC2), ``"Amazon Relational Database Service"`` (RDS),
  ``"Amazon ElastiCache"``, ``"Amazon Redshift"`` (literal AWS CLI doc
  example). DynamoDB's value is ``"Amazon DynamoDB Service"`` — LIVE-VERIFIED
  2026-08-08: the convention-inferred ``"Amazon DynamoDB"`` was rejected with a
  ValidationException whose supported-values list names ``"Amazon DynamoDB
  Service"`` verbatim (tadweer-prod live smoke; the same list also names
  ``"Amazon MemoryDB Service"`` — a possible future seventh RI service).
* **Correction to the brief's initial guess**: OpenSearch's ``Service``
  string is ``"Amazon OpenSearch Service"``, not the legacy ``"Amazon
  Elasticsearch Service"``. ``API_ESInstanceDetails.html`` documents the
  (legacy-named) ``ESInstanceDetails`` shape itself as "Details about the
  **Amazon OpenSearch Service** reservations that AWS recommends that you
  purchase" — current docs use the renamed service throughout, and this
  matches the convention already used elsewhere in this codebase
  (``services/adapters/opensearch.py:_CE_OPENSEARCH_SERVICE``,
  ``services/commitment_coverage.py:_CE_SERVICE_DIM``).
* **DynamoDB is supported**, but through a distinct shape: RI recommendation
  details are only ``InstanceDetails``-based for EC2/RDS/ElastiCache/
  Redshift/OpenSearch. DynamoDB instead surfaces under
  ``RecommendationDetail.ReservedCapacityDetails.DynamoDBCapacityDetails``
  (``CapacityUnits``, ``Region`` — no instance type, no platform/engine), and
  its purchase count is carried in ``RecommendedNumberOfCapacityUnitsToPurchase``
  rather than ``RecommendedNumberOfInstancesToPurchase``. Both count fields
  are read defensively so the generic path is unaffected.
* ``RecommendationDetail`` money/identity fields confirmed present exactly as
  the brief assumed: ``EstimatedMonthlySavingsAmount`` (current field;
  ``EstimatedMonthlySavings`` is not in the current schema but is still read
  as a defensive legacy fallback per the brief), ``RecurringStandardMonthlyCost``,
  ``EstimatedMonthlyOnDemandCost``, ``RecommendedNumberOfInstancesToPurchase``,
  ``UpfrontCost``, and the per-service nested keys: ``EC2InstanceDetails.
  InstanceType/Region/Platform``, ``RDSInstanceDetails.InstanceType/Region/
  DatabaseEngine``, ``ElastiCacheInstanceDetails.NodeType/Region/
  ProductDescription``, ``RedshiftInstanceDetails.NodeType/Region`` (no
  platform-equivalent field), ``ESInstanceDetails.InstanceClass+InstanceSize/
  Region`` (no platform-equivalent field either).
* ``GetSavingsPlansPurchaseRecommendation`` summary/detail fields confirmed:
  ``HourlyCommitmentToPurchase``, ``EstimatedMonthlySavingsAmount``,
  ``EstimatedSavingsPercentage``, ``UpfrontCost``, ``EstimatedOnDemandCost``
  (detail-level) / ``EstimatedOnDemandCostWithCurrentCommitment``
  (summary-level, preferred — read with an ``EstimatedOnDemandCost``
  fallback). ``SavingsPlansType`` enum confirmed as exactly
  ``COMPUTE_SP | EC2_INSTANCE_SP | SAGEMAKER_SP``.
"""

from __future__ import annotations

from typing import Any

from services.commitment_coverage import normalize_type

# (CE service string, display label). EC2's long form is required (the short
# "Amazon EC2" is rejected). See module docstring for the doc source of each
# string, including the OpenSearch correction and DynamoDB caveat.
RI_SERVICES: tuple[tuple[str, str], ...] = (
    ("Amazon Elastic Compute Cloud - Compute", "EC2"),
    ("Amazon Relational Database Service", "RDS"),
    ("Amazon ElastiCache", "ElastiCache"),
    ("Amazon Redshift", "Redshift"),
    ("Amazon OpenSearch Service", "OpenSearch"),
    # Live-verified: the API's supported-values error names this exact string.
    ("Amazon DynamoDB Service", "DynamoDB"),
)
SP_TYPES: tuple[str, ...] = ("COMPUTE_SP", "EC2_INSTANCE_SP", "SAGEMAKER_SP")

TERMS: tuple[tuple[str, str], ...] = (("ONE_YEAR", "1yr"), ("THREE_YEARS", "3yr"))
PAYMENTS: tuple[tuple[str, str], ...] = (
    ("NO_UPFRONT", "No Upfront"),
    ("PARTIAL_UPFRONT", "Partial Upfront"),
    ("ALL_UPFRONT", "All Upfront"),
)

# Per-service nested identity keys inside RecommendationDetails: (outer key,
# inner key). DynamoDB is the odd one out — its detail lives under
# ReservedCapacityDetails, not InstanceDetails (see module docstring).
_DETAIL_KEYS: dict[str, tuple[str, str]] = {
    "EC2": ("InstanceDetails", "EC2InstanceDetails"),
    "RDS": ("InstanceDetails", "RDSInstanceDetails"),
    "ElastiCache": ("InstanceDetails", "ElastiCacheInstanceDetails"),
    "Redshift": ("InstanceDetails", "RedshiftInstanceDetails"),
    "OpenSearch": ("InstanceDetails", "ESInstanceDetails"),
    "DynamoDB": ("ReservedCapacityDetails", "DynamoDBCapacityDetails"),
}


def _money(detail: dict[str, Any], *keys: str) -> float:
    """First parseable money field among ``keys`` (CE returns strings)."""
    for key in keys:
        val = detail.get(key)
        if val not in (None, ""):
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def _identity(service_label: str, detail: dict[str, Any]) -> tuple[str, str, str]:
    """(instance_type, region, platform) from the per-service nested block.

    DynamoDB has neither an instance type nor a platform/engine — it reports
    a capacity-unit count instead, surfaced here as the "instance_type" label
    so callers get a non-empty identity string regardless of service.
    """
    outer_key, inner_key = _DETAIL_KEYS[service_label]
    inner = (detail.get(outer_key) or {}).get(inner_key, {})
    if service_label == "OpenSearch":
        itype = ".".join(p for p in (inner.get("InstanceClass"), inner.get("InstanceSize")) if p)
    elif service_label == "DynamoDB":
        units = inner.get("CapacityUnits")
        itype = f"{units} capacity units" if units else ""
    else:
        itype = inner.get("InstanceType") or inner.get("NodeType") or ""
    platform = (inner.get("Platform") or inner.get("DatabaseEngine")
                or inner.get("ProductDescription") or "")
    return itype or "unknown", inner.get("Region", ""), platform


def _ec2_tenancy_and_offering_class(rec: dict[str, Any], detail: dict[str, Any]) -> tuple[str, str]:
    """EC2-only grouping dimensions CE splits recommendations by.

    ``Tenancy`` lives on the per-detail ``EC2InstanceDetails`` block
    (AWS-SDK-confirmed field, verified via ``aws-knowledge``
    ``search_documentation`` on 2026-08-08). ``OfferingClass`` lives one
    level up, on the recommendation's ``ServiceSpecification.EC2Specification``
    — NOT inside ``EC2InstanceDetails`` despite that being the natural first
    guess — but is also checked defensively inside the detail block in case a
    future/alternate response shape nests it there. Non-EC2 services (whose
    detail dict has no ``EC2InstanceDetails``) always resolve to ``("", "")``.
    """
    inner = (detail.get("InstanceDetails") or {}).get("EC2InstanceDetails", {})
    tenancy = inner.get("Tenancy") or ""
    offering_class = (
        (rec.get("ServiceSpecification") or {}).get("EC2Specification", {}).get("OfferingClass")
        or inner.get("OfferingClass") or ""
    )
    return tenancy, offering_class


def ri_cells_from_response(service_label: str, term: str, payment: str,
                           resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one RI purchase-recommendation response into per-type cells.

    Zero-savings details are dropped (a cell that saves nothing is not an
    option worth rendering, and must never join best-path math). EC2 details
    also carry ``tenancy``/``offering_class`` (CE returns separate details for
    each — the card grouping key must include both or two distinct purchase
    options silently collapse into one, dropping dollars).
    """
    cells: list[dict[str, Any]] = []
    for rec in resp.get("Recommendations", []) or []:
        for detail in rec.get("RecommendationDetails", []) or []:
            if not isinstance(detail, dict):
                continue
            savings = _money(detail, "EstimatedMonthlySavingsAmount", "EstimatedMonthlySavings")
            if savings <= 0:
                continue
            itype, region, platform = _identity(service_label, detail)
            tenancy, offering_class = _ec2_tenancy_and_offering_class(rec, detail)
            cells.append({
                "service": service_label,
                "instance_type": itype,
                "region": region,
                "platform": platform,
                "tenancy": tenancy,
                "offering_class": offering_class,
                "count": int(_money(
                    detail,
                    "RecommendedNumberOfInstancesToPurchase",
                    "RecommendedNumberOfCapacityUnitsToPurchase",
                )),
                "term": term,
                "payment": payment,
                "monthly_savings": round(savings, 2),
                "upfront": round(_money(detail, "UpfrontCost"), 2),
                "recurring_monthly": round(_money(detail, "RecurringStandardMonthlyCost"), 2),
                "ondemand_monthly": round(_money(detail, "EstimatedMonthlyOnDemandCost"), 2),
            })
    return cells


def sp_cell_from_response(sp_type: str, term: str, payment: str,
                          resp: dict[str, Any]) -> dict[str, Any] | None:
    """One scenario cell from an SP purchase-recommendation response, or None.

    ``SavingsPlansPurchaseRecommendationDetails`` carries one entry per
    family/region for EC2_INSTANCE_SP BY DESIGN (Compute/SageMaker SP
    responses typically carry a single detail); ``upfront`` sums
    ``UpfrontCost`` across ALL details — reading only ``details[0]`` silently
    drops every family beyond the first. ``instance_families`` collects the
    sorted set of ``SavingsPlansDetails.InstanceFamily`` values present
    across details (empty list for account-level SP types with no family
    dimension) so the card renderer can list them (see M7 in reporter_phase_b).
    """
    spr = resp.get("SavingsPlansPurchaseRecommendation")
    if not isinstance(spr, dict):
        return None
    summary = spr.get("SavingsPlansPurchaseRecommendationSummary", {})
    details = spr.get("SavingsPlansPurchaseRecommendationDetails", []) or []
    savings = _money(summary, "EstimatedMonthlySavingsAmount")
    commit = _money(summary, "HourlyCommitmentToPurchase")
    if savings <= 0 and commit <= 0:
        return None
    upfront = 0.0
    families: set[str] = set()
    for detail in details:
        if not isinstance(detail, dict):
            continue
        upfront += _money(detail, "UpfrontCost")
        family = (detail.get("SavingsPlansDetails") or {}).get("InstanceFamily")
        if family:
            families.add(str(family))
    return {
        "sp_type": sp_type,
        "term": term,
        "payment": payment,
        "hourly_commitment": commit,
        "monthly_savings": round(savings, 2),
        "savings_pct": _money(summary, "EstimatedSavingsPercentage"),
        "upfront": round(upfront, 2),
        "estimated_ondemand_monthly": _money(
            summary, "EstimatedOnDemandCostWithCurrentCommitment", "EstimatedOnDemandCost"),
        "instance_families": sorted(families),
    }


_TERM_MONTHS = {"1yr": 12, "3yr": 36}
# Coverage-join keys use the commitment_coverage service spelling.
_COVERAGE_SERVICE = {"EC2": "ec2", "RDS": "rds", "ElastiCache": "elasticache",
                     "Redshift": "redshift", "OpenSearch": "opensearch"}

_SCENARIO_ORDER = {(t, p): i for i, (t, p) in enumerate(
    (t_lbl, p_lbl) for _, t_lbl in TERMS for _, p_lbl in PAYMENTS)}


def _finish_scenarios(cells: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Sort cells canonically, add break_even_months, return (scenarios, best_idx).

    Mutates the cell dicts it is handed; callers pass copies.
    """
    scenarios = sorted(cells, key=lambda c: _SCENARIO_ORDER.get((c["term"], c["payment"]), 99))
    for s in scenarios:
        net = s["monthly_savings"]
        if s["upfront"] <= 0:
            s["break_even_months"] = 0.0
        elif net > 0:
            s["break_even_months"] = round(s["upfront"] / net, 1)
        else:
            s["break_even_months"] = None
    best = max(range(len(scenarios)), key=lambda i: scenarios[i]["monthly_savings"])
    return scenarios, best


def build_ri_type_cards(cells: list[dict[str, Any]], uncovered: dict[str, float],
                        scan_region: str) -> list[dict[str, Any]]:
    """Group RI cells into one card per (service, instance_type, region, platform).

    Coverage context joins from ``uncovered`` (CommitmentCoverage.uncovered_on_demand,
    keyed ``"{service}:{normalized_type}"``), but only for cards in the scan_region
    AND only when exactly one platform exists for that (service, instance_type, region);
    uncovered keys are platform-agnostic and cannot be fairly allocated across multiple
    platforms, so all cards are fail-closed when ambiguous. Missing key omits fields entirely.
    """
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = {}
    for c in cells:
        key = (c["service"], c["instance_type"], c["region"], c["platform"],
               c.get("tenancy", ""), c.get("offering_class", ""))
        groups.setdefault(key, []).append(c)

    # Count DISTINCT platforms per (service, instance_type, region) to detect
    # ambiguity — a tenancy/offering_class split must not inflate this count;
    # only a genuine second platform makes coverage unarbitrable.
    platforms_by_key: dict[tuple[str, str, str], set[str]] = {}
    for (service, itype, region, platform, _tenancy, _offering_class) in groups:
        platforms_by_key.setdefault((service, itype, region), set()).add(platform)
    platform_counts = {k: len(v) for k, v in platforms_by_key.items()}

    cards: list[dict[str, Any]] = []
    for (service, itype, region, platform, tenancy, offering_class), group in groups.items():
        scenarios, best = _finish_scenarios([dict(c) for c in group])
        best_cell = scenarios[best]
        ondemand = best_cell["ondemand_monthly"]
        card: dict[str, Any] = {
            "card_kind": "ri_type",
            "service": service,
            "instance_type": itype,
            "region": region,
            "platform": platform,
            "tenancy": tenancy,
            "offering_class": offering_class,
            "recommended_count": best_cell["count"],
            "current_ondemand_monthly": ondemand,
            "scenarios": [
                {k: s[k] for k in ("term", "payment", "monthly_savings", "upfront",
                                   "recurring_monthly", "break_even_months")}
                for s in scenarios
            ],
            "recommended_scenario": best,
            "Counted": False,
            "monthly_savings": best_cell["monthly_savings"],
            "severity": "LOW",
        }
        # Omit risk_pct rather than claim a misleading 0% when the best cell
        # has neither a recurring charge nor an upfront cost to weigh.
        if ondemand > 0 and (best_cell["recurring_monthly"] > 0 or best_cell["upfront"] > 0):
            months = _TERM_MONTHS.get(best_cell["term"], 12)
            card["risk_pct"] = round(
                100.0 * (best_cell["recurring_monthly"] + best_cell["upfront"] / months) / ondemand, 1)
        # Join coverage only if exactly one platform exists for this (service, itype, region).
        if region == scan_region and platform_counts.get((service, itype, region), 0) == 1:
            cov_service = _COVERAGE_SERVICE.get(service, service.lower())
            normalized_itype = normalize_type(itype)
            key = f"{cov_service}:{normalized_itype}"
            if key in uncovered:
                card["uncovered_monthly"] = round(uncovered[key], 2)
                # A differently-scoped measurement (uncovered > ondemand) is
                # not arithmetically sound as a percentage; omit rather than
                # clamp to a false 0% — the dollar figure above still stands.
                if ondemand > 0 and uncovered[key] <= ondemand:
                    card["coverage_pct"] = round(100.0 * (1 - uncovered[key] / ondemand), 1)
        card["AuditBasis"] = {
            "source": "ce:GetReservationPurchaseRecommendation",
            "lookback_days": 30,
            "basis": "AWS-computed purchase recommendation; projection, not counted",
        }
        cards.append(card)

    cards.sort(key=lambda c: (c["region"] != scan_region, -c["monthly_savings"]))
    return cards


def build_sp_cards(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group SP cells into one card per SP type (SPs carry no instance type)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in cells:
        groups.setdefault(c["sp_type"], []).append(c)

    cards: list[dict[str, Any]] = []
    for sp_type, group in groups.items():
        scenarios, best = _finish_scenarios([dict(c) for c in group])
        best_cell = scenarios[best]
        card: dict[str, Any] = {
            "card_kind": "sp_commitment",
            "sp_type": sp_type,
            "scenarios": [
                {k: s[k] for k in ("term", "payment", "monthly_savings", "upfront",
                                   "hourly_commitment", "savings_pct", "break_even_months")}
                for s in scenarios
            ],
            "recommended_scenario": best,
            "Counted": False,
            "monthly_savings": best_cell["monthly_savings"],
            "severity": "LOW",
            "AuditBasis": {
                "source": "ce:GetSavingsPlansPurchaseRecommendation",
                "lookback_days": 30,
                "basis": "AWS-computed purchase recommendation; projection, not counted",
            },
        }
        if best_cell.get("instance_families"):
            card["instance_families"] = best_cell["instance_families"]
        estimated_ondemand = best_cell.get("estimated_ondemand_monthly", 0.0)
        if estimated_ondemand > 0:
            card["risk_pct"] = round(
                100.0 * (1 - best_cell["monthly_savings"] / estimated_ondemand), 1)
        cards.append(card)
    cards.sort(key=lambda c: -c["monthly_savings"])
    return cards


# Which CoH reservation-purchase resource-type substrings concur with which RI card
# service. Maps service label to tuple of acceptable currentResourceType substrings.
# Source: core/scan_orchestrator.py type_map lines 126-131 (reservation-purchase types).
# Note: DynamoDB is absent — CoH's type_map has no reservation-purchase type for it
# (only rightsizing DynamoDBTable), so no CoH recs can concur with DynamoDB RI cards.
# LIVE-VERIFIED 2026-08-09: real CoH payloads spell it "Ec2ReservedInstances"
# (lowercase c2) — matching is case-insensitive and both spellings are listed
# so neither shape ever silently falls through again.
_COH_RI_MATCH = {
    "EC2": ("EC2ReservedInstances", "Ec2ReservedInstances"),
    "RDS": ("RdsReservedInstances",),
    "ElastiCache": ("ElastiCacheReservedInstances",),
    "Redshift": ("RedshiftReservedInstances",),
    "OpenSearch": ("OpenSearchReservedInstances", "EsReservedInstances"),
}

# Which CoH savings-plan resource-type substrings concur with which SP card type.
# Maps sp_type to tuple of acceptable currentResourceType substrings.
# Source: core/scan_orchestrator.py type_map lines 132-134 (savings-plan types).
_COH_SP_MATCH = {
    "COMPUTE_SP": ("ComputeSavingsPlans",),
    "EC2_INSTANCE_SP": ("EC2InstanceSavingsPlans", "Ec2InstanceSavingsPlans"),
    "SAGEMAKER_SP": ("SageMakerSavingsPlans",),
}

# Nested per-service key inside a CoH rec's recommendedResourceSummary /
# recommendedResourceDetails dict, and the type-bearing field name inside
# that nested block. Mirrors the same routed rec shape reporter_phase_b.py's
# _coh_recommended_scenario already parses for term/payment.
_COH_RI_TYPE_KEY = {
    "EC2": ("ec2ReservedInstances", "instanceType"),
    "RDS": ("rdsReservedInstances", "instanceType"),
    "ElastiCache": ("elastiCacheReservedInstances", "instanceType"),
    "Redshift": ("redshiftReservedInstances", "instanceType"),
    "OpenSearch": ("openSearchReservedInstances", "instanceType"),
}


def _coh_rec_instance_type(rec: dict[str, Any], service: str) -> str | None:
    """The instance/node type a CoH RI-purchase rec names, or ``None`` if it
    carries no recognizable type detail at all (the common bare-summary shape).
    """
    nested_key, type_key = _COH_RI_TYPE_KEY.get(service, (None, None))
    if not nested_key:
        return None
    for field in ("recommendedResourceDetails", "recommendedResourceSummary"):
        details = rec.get(field)
        if not isinstance(details, dict):
            continue
        block = details.get(nested_key)
        if not isinstance(block, dict):
            continue
        # GetRecommendation nests the type one level deeper, under
        # "configuration" (live-pinned 2026-08-09); older/synthetic shapes
        # carry it flat on the block.
        cfg = block.get("configuration")
        source = cfg if isinstance(cfg, dict) else block
        itype = source.get(type_key)
        if itype:
            return str(itype)
    # ListRecommendations shape: recommendedResourceSummary is a plain STRING,
    # "{count} {type} {platform} in {region} ..." — the type is token 2.
    summary_str = rec.get("recommendedResourceSummary")
    if isinstance(summary_str, str):
        tokens = summary_str.split()
        if len(tokens) >= 2 and "." in tokens[1]:
            return tokens[1]
    return None


def _ri_targets_for_rec(rec: dict[str, Any], service_targets: list[dict[str, Any]]
                        ) -> list[dict[str, Any]]:
    """Narrow same-service RI cards to the one the rec's named type points at.

    A CoH RI-purchase rec that names a concrete instance/node type must only
    concur with the matching card — attaching it to the richest card of the
    service regardless of type misattributes the dollar. A rec with no
    recognizable type at all (the common bare-summary shape) still matches at
    the service level (the caller's existing ``max()`` picks the richest).
    A rec that names a type absent from every built card returns ``[]`` so
    the caller leaves it unmatched (renders standalone) instead of guessing.
    """
    if not service_targets:
        return []
    named_type = _coh_rec_instance_type(rec, service_targets[0]["service"])
    if named_type is None:
        return service_targets
    return [c for c in service_targets if c.get("instance_type") == named_type]


def projected_savings(ri_cards: list[dict[str, Any]],
                      sp_cards: list[dict[str, Any]]) -> tuple[float, str]:
    """Best non-overlapping purchase path across instruments (spec section
    "Projected figure + non-overlap rule").

    SP and RI discount the SAME on-demand spend, so within the compute group
    the winner is max(best SP type, sum of EC2 RI cards) — never the sum.
    Disjoint RI services (RDS/ElastiCache/Redshift/OpenSearch/DynamoDB) sum
    safely; SageMaker SP overlaps nothing else and adds on top.
    """
    ec2_ri_total = sum(c["monthly_savings"] for c in ri_cards if c["service"] == "EC2")
    ec2_eligible_sp = [c for c in sp_cards if c["sp_type"] in ("COMPUTE_SP", "EC2_INSTANCE_SP")]
    best_sp_card = max(ec2_eligible_sp, key=lambda c: c["monthly_savings"], default=None)
    compute_sp_best = best_sp_card["monthly_savings"] if best_sp_card is not None else 0.0
    if compute_sp_best >= ec2_ri_total:
        # Name the actual winning SP type rather than always crediting
        # "Compute SP" — EC2_INSTANCE_SP can legitimately win this group too.
        sp_basis = (
            "EC2 Instance SP path"
            if best_sp_card is not None and best_sp_card["sp_type"] == "EC2_INSTANCE_SP"
            else "Compute SP path"
        )
        group1, group1_basis = compute_sp_best, sp_basis
    else:
        group1, group1_basis = ec2_ri_total, "EC2 RI path"

    group2 = sum(c["monthly_savings"] for c in ri_cards
                 if c["service"] in ("RDS", "ElastiCache", "Redshift", "OpenSearch", "DynamoDB"))
    group3 = max((c["monthly_savings"] for c in sp_cards if c["sp_type"] == "SAGEMAKER_SP"),
                 default=0.0)

    total = round(group1 + group2 + group3, 2)
    parts = []
    if group1 > 0:
        parts.append(group1_basis)
    if group2 > 0:
        parts.append("service RIs (RDS/ElastiCache/Redshift/OpenSearch/DynamoDB)")
    if group3 > 0:
        parts.append("SageMaker SP")
    return total, " + ".join(parts) if parts else "no purchase recommendations"


def merge_coh_concurrence(cards: list[dict[str, Any]],
                          coh_recs: list[dict[str, Any]]
                          ) -> tuple[list[dict[str, Any]], list[int]]:
    """Annotate cards with a "CoH concurs: $X/mo" figure instead of rendering
    duplicate CoH purchase cards. Returns new card dicts (no input mutation).

    Match: an SP-purchase CoH rec concurs with the same-type SP card. An
    RI-purchase CoH rec that names a concrete instance/node type concurs
    ONLY with the matching card (or stays unmatched if no card carries that
    type — see ``_ri_targets_for_rec``); a rec with no recognizable type
    concurs with the highest-savings RI card of the matching service, as
    before. Unmatched CoH recs are left for the caller's existing CoH render
    path — nothing is dropped here.

    Multiple CoH recs for the same card type accumulate their dollars on the
    matched card (no overwriting).

    Returns:
        Tuple of ``(annotated_cards, matched_indices)``. ``matched_indices``
        holds the positions into ``coh_recs`` that were merged into a card;
        callers must exclude those recs from any separate CoH render path so
        a merged rec never also renders as a duplicate CoH card.
    """
    out = [dict(c) for c in cards]
    matched_indices: list[int] = []
    for i, rec in enumerate(coh_recs):
        action = str(rec.get("actionType") or rec.get("recommendedAction") or "")
        dollars = float(rec.get("estimatedMonthlySavings") or 0)
        if dollars <= 0:
            continue
        if "SavingsPlan" in action:
            rtype = str(rec.get("currentResourceType") or "")
            targets = [c for c in out if c["card_kind"] == "sp_commitment"
                       and any(substr.lower() in rtype.lower() for substr in _COH_SP_MATCH.get(c["sp_type"], []))]
        elif "Reserved" in action:
            rtype = str(rec.get("currentResourceType") or "")
            service_targets = [c for c in out if c["card_kind"] == "ri_type"
                              and any(substr.lower() in rtype.lower() for substr in _COH_RI_MATCH.get(c["service"], []))]
            targets = _ri_targets_for_rec(rec, service_targets)
        else:
            continue
        if targets:
            best = max(targets, key=lambda c: c["monthly_savings"])
            best["coh_concurs_monthly"] = round(best.get("coh_concurs_monthly", 0.0) + dollars, 2)
            matched_indices.append(i)
    return out, matched_indices
