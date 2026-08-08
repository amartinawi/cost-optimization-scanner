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
  example). ``"Amazon DynamoDB"`` is supported by the API (see below) but has
  no literal documented request example; the string is inferred from this
  codebase's own established CE ``SERVICE``-dimension convention
  (``services/commitment_coverage.py:_fetch_dynamodb_reserved``) — lower
  confidence than the other five, called out again in the task report.
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
    ("Amazon DynamoDB", "DynamoDB"),
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


def ri_cells_from_response(service_label: str, term: str, payment: str,
                           resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one RI purchase-recommendation response into per-type cells.

    Zero-savings details are dropped (a cell that saves nothing is not an
    option worth rendering, and must never join best-path math).
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
            cells.append({
                "service": service_label,
                "instance_type": itype,
                "region": region,
                "platform": platform,
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
    """One scenario cell from an SP purchase-recommendation response, or None."""
    spr = resp.get("SavingsPlansPurchaseRecommendation")
    if not isinstance(spr, dict):
        return None
    summary = spr.get("SavingsPlansPurchaseRecommendationSummary", {})
    details = spr.get("SavingsPlansPurchaseRecommendationDetails", [])
    savings = _money(summary, "EstimatedMonthlySavingsAmount")
    commit = _money(summary, "HourlyCommitmentToPurchase")
    if savings <= 0 and commit <= 0:
        return None
    upfront = _money(details[0], "UpfrontCost") if details and isinstance(details[0], dict) else 0.0
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
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for c in cells:
        groups.setdefault((c["service"], c["instance_type"], c["region"], c["platform"]), []).append(c)

    # Count platform groups per (service, instance_type, region) to detect ambiguity.
    platform_counts: dict[tuple[str, str, str], int] = {}
    for (service, itype, region, platform), _ in groups.items():
        key = (service, itype, region)
        platform_counts[key] = platform_counts.get(key, 0) + 1

    cards: list[dict[str, Any]] = []
    for (service, itype, region, platform), group in groups.items():
        scenarios, best = _finish_scenarios([dict(c) for c in group])
        best_cell = scenarios[best]
        ondemand = best_cell["ondemand_monthly"]
        card: dict[str, Any] = {
            "card_kind": "ri_type",
            "service": service,
            "instance_type": itype,
            "region": region,
            "platform": platform,
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
        }
        if ondemand > 0:
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
                if ondemand > 0:
                    card["coverage_pct"] = round(max(0.0, 100.0 * (1 - uncovered[key] / ondemand)), 1)
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
            "AuditBasis": {
                "source": "ce:GetSavingsPlansPurchaseRecommendation",
                "lookback_days": 30,
                "basis": "AWS-computed purchase recommendation; projection, not counted",
            },
        }
        estimated_ondemand = best_cell.get("estimated_ondemand_monthly", 0.0)
        if estimated_ondemand > 0:
            card["risk_pct"] = round(
                100.0 * (1 - best_cell["monthly_savings"] / estimated_ondemand), 1)
        cards.append(card)
    cards.sort(key=lambda c: -c["monthly_savings"])
    return cards


# Which CoH resource-type substrings concur with which RI card service.
_COH_RI_MATCH = {"EC2": "Ec2Instance", "RDS": "RdsDb", "ElastiCache": "ElastiCache",
                 "Redshift": "Redshift", "OpenSearch": "OpenSearch"}


def projected_savings(ri_cards: list[dict[str, Any]],
                      sp_cards: list[dict[str, Any]]) -> tuple[float, str]:
    """Best non-overlapping purchase path across instruments (spec section
    "Projected figure + non-overlap rule").

    SP and RI discount the SAME on-demand spend, so within the compute group
    the winner is max(best SP type, sum of EC2 RI cards) — never the sum.
    Disjoint RI services (RDS/ElastiCache/Redshift/OpenSearch) sum safely;
    SageMaker SP overlaps nothing else and adds on top.
    """
    ec2_ri_total = sum(c["monthly_savings"] for c in ri_cards if c["service"] == "EC2")
    compute_sp_best = max(
        (c["monthly_savings"] for c in sp_cards if c["sp_type"] in ("COMPUTE_SP", "EC2_INSTANCE_SP")),
        default=0.0)
    if compute_sp_best >= ec2_ri_total:
        group1, group1_basis = compute_sp_best, "Compute SP path"
    else:
        group1, group1_basis = ec2_ri_total, "EC2 RI path"

    group2 = sum(c["monthly_savings"] for c in ri_cards
                 if c["service"] in ("RDS", "ElastiCache", "Redshift", "OpenSearch"))
    group3 = max((c["monthly_savings"] for c in sp_cards if c["sp_type"] == "SAGEMAKER_SP"),
                 default=0.0)

    total = round(group1 + group2 + group3, 2)
    parts = []
    if group1 > 0:
        parts.append(group1_basis)
    if group2 > 0:
        parts.append("service RIs (RDS/ElastiCache/Redshift/OpenSearch)")
    if group3 > 0:
        parts.append("SageMaker SP")
    return total, " + ".join(parts) if parts else "no purchase recommendations"


def merge_coh_concurrence(cards: list[dict[str, Any]],
                          coh_recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate cards with a "CoH concurs: $X/mo" figure instead of rendering
    duplicate CoH purchase cards. Returns new card dicts (no input mutation).

    Match: an SP-purchase CoH rec concurs with the same-type SP card; an
    RI-purchase CoH rec concurs with the highest-savings RI card of the
    matching service. Unmatched CoH recs are left for the existing CoH render
    path — nothing is dropped here.
    """
    out = [dict(c) for c in cards]
    for rec in coh_recs:
        action = str(rec.get("actionType") or rec.get("recommendedAction") or "")
        dollars = float(rec.get("estimatedMonthlySavings") or 0)
        if dollars <= 0:
            continue
        if "SavingsPlan" in action:
            targets = [c for c in out if c["card_kind"] == "sp_commitment"]
        elif "Reserved" in action:
            rtype = str(rec.get("currentResourceType") or "")
            targets = [c for c in out if c["card_kind"] == "ri_type"
                       and _COH_RI_MATCH.get(c["service"], "\x00") in rtype]
        else:
            continue
        if targets:
            best = max(targets, key=lambda c: c["monthly_savings"])
            best["coh_concurs_monthly"] = round(dollars, 2)
    return out
