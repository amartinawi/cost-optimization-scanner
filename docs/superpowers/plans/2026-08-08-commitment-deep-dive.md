# Commitment Deep-Dive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-instance-type RI/SP purchase analysis across all CE-supported services with full term x payment scenario matrices, coverage context, break-even math, a projected-savings exec-summary fact, and redesigned Commitment-tab rendering.

**Architecture:** New pure-logic module `services/commitment_scenarios.py` (raw CE dicts in → normalized cards out; no boto3), slimmed `commitment_analysis` adapter that only fetches cells and delegates, one new card renderer in `reporter_phase_b.py`, additive summary fields.

**Tech Stack:** Python 3.8+, boto3 Cost Explorer (`get_reservation_purchase_recommendation`, `get_savings_plans_purchase_recommendation`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-08-commitment-deep-dive-design.md` — read it first; its Decisions table and non-overlap rule are normative.

## Global Constraints

- Strictly cost scope: purchase cards are `Counted=False` projections; `monthly_savings` numeric carries the projection (B1-ii convention); they never inflate `total_recommendations` (D4) or the counted headline.
- All pricing from CE responses — no hardcoded rates, no invented per-type SP detail.
- Fail closed (C8): a missing/denied CE cell is omitted, never zero-filled; no coverage data → no coverage line, never a fabricated 0.
- Files ≤ 800 lines; `commitment_analysis.py` (912 now) must END below 800.
- Google-style docstrings, ATX headers, no emoji.
- Regression gate after every task: `./venv/bin/python -m pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py -q`
- Run all tests with `./venv/bin/python -m pytest` (NOT bare `pytest` — system python lacks it).
- Commit after every task; conventional commits; no attribution footer.
- AWS field names MUST be verified against docs (aws-knowledge MCP or botocore docs) before parsing code is written — Task 1 Step 0 does this once for everything.

---

### Task 1: Cell parsers — `commitment_scenarios.py` part 1

**Files:**
- Create: `services/commitment_scenarios.py`
- Test: `tests/test_commitment_scenarios.py`

**Interfaces:**
- Consumes: raw boto3 response dicts (no other project code).
- Produces (later tasks rely on these exact signatures):
  - `ri_cells_from_response(service_label: str, term: str, payment: str, resp: dict) -> list[dict]`
    — one cell dict per RecommendationDetail:
    `{"service", "instance_type", "region", "platform", "count", "term", "payment", "monthly_savings", "upfront", "recurring_monthly", "ondemand_monthly"}`
  - `sp_cell_from_response(sp_type: str, term: str, payment: str, resp: dict) -> dict | None`
    — `{"sp_type", "term", "payment", "hourly_commitment", "monthly_savings", "savings_pct", "upfront", "estimated_ondemand_monthly"}`
  - `RI_SERVICES: tuple[tuple[str, str], ...]` (CE service string, label) and `SP_TYPES: tuple[str, ...]`

- [ ] **Step 0: Verify CE field names + service strings against docs**

Query the aws-knowledge MCP (`search_documentation` then `read_documentation`) for
`GetReservationPurchaseRecommendation` and `GetSavingsPlansPurchaseRecommendation`.
Confirm and write into a module comment:
1. The exact `Service` strings accepted (EC2 is known: `"Amazon Elastic Compute Cloud - Compute"`; confirm RDS / ElastiCache / Redshift / OpenSearch-vs-Elasticsearch; check whether DynamoDB is supported — if yes add it to `RI_SERVICES`).
2. Detail field names: `EstimatedMonthlySavingsAmount` (vs the legacy `EstimatedMonthlySavings` the old code read), `RecurringStandardMonthlyCost`, `EstimatedMonthlyOnDemandCost`, `RecommendedNumberOfInstancesToPurchase`, `UpfrontCost`, and the per-service `InstanceDetails` sub-keys (`EC2InstanceDetails.InstanceType/Region/Platform`, `RDSInstanceDetails.InstanceType/Region/DatabaseEngine`, `ElastiCacheInstanceDetails.NodeType/Region/ProductDescription`, `RedshiftInstanceDetails.NodeType/Region`, `ESInstanceDetails.InstanceClass+InstanceSize/Region`).
3. SP summary/detail fields: `HourlyCommitmentToPurchase`, `EstimatedMonthlySavingsAmount`, `EstimatedSavingsPercentage`, `UpfrontCost`, `EstimatedOnDemandCost`.

Parse money fields defensively either way: `float(d.get("EstimatedMonthlySavingsAmount") or d.get("EstimatedMonthlySavings") or 0)` — CE returns strings.

- [ ] **Step 1: Write failing tests**

```python
"""Tests for services/commitment_scenarios.py — pure-logic card math."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.commitment_scenarios import (
    ri_cells_from_response,
    sp_cell_from_response,
)


def _ri_resp(details):
    return {"Recommendations": [{"RecommendationDetails": details}]}


def _rds_detail(**over):
    base = {
        "InstanceDetails": {"RDSInstanceDetails": {
            "InstanceType": "db.r7i.4xlarge", "Region": "eu-west-1",
            "DatabaseEngine": "aurora-postgresql"}},
        "RecommendedNumberOfInstancesToPurchase": "7",
        "EstimatedMonthlySavingsAmount": "1210.40",
        "UpfrontCost": "0",
        "RecurringStandardMonthlyCost": "2891.71",
        "EstimatedMonthlyOnDemandCost": "4102.11",
    }
    base.update(over)
    return base


def test_ri_cells_parse_nested_instance_details():
    cells = ri_cells_from_response("RDS", "1yr", "No Upfront", _ri_resp([_rds_detail()]))
    assert len(cells) == 1
    c = cells[0]
    assert c["instance_type"] == "db.r7i.4xlarge"
    assert c["region"] == "eu-west-1"
    assert c["platform"] == "aurora-postgresql"
    assert c["count"] == 7
    assert c["monthly_savings"] == pytest.approx(1210.40)
    assert c["recurring_monthly"] == pytest.approx(2891.71)
    assert c["ondemand_monthly"] == pytest.approx(4102.11)
    assert (c["term"], c["payment"]) == ("1yr", "No Upfront")


def test_ri_cells_read_legacy_savings_field():
    d = _rds_detail()
    d["EstimatedMonthlySavings"] = d.pop("EstimatedMonthlySavingsAmount")
    cells = ri_cells_from_response("RDS", "1yr", "No Upfront", _ri_resp([d]))
    assert cells[0]["monthly_savings"] == pytest.approx(1210.40)


def test_ri_cells_es_type_joins_class_and_size():
    d = {
        "InstanceDetails": {"ESInstanceDetails": {
            "InstanceClass": "r6g", "InstanceSize": "large.search", "Region": "eu-west-1"}},
        "RecommendedNumberOfInstancesToPurchase": "2",
        "EstimatedMonthlySavingsAmount": "80.00",
        "UpfrontCost": "100",
        "RecurringStandardMonthlyCost": "50",
        "EstimatedMonthlyOnDemandCost": "200",
    }
    cells = ri_cells_from_response("OpenSearch", "3yr", "All Upfront", _ri_resp([d]))
    assert cells[0]["instance_type"] == "r6g.large.search"


def test_ri_cells_zero_savings_detail_dropped():
    cells = ri_cells_from_response(
        "RDS", "1yr", "No Upfront",
        _ri_resp([_rds_detail(EstimatedMonthlySavingsAmount="0")]))
    assert cells == []


def test_sp_cell_parses_summary():
    resp = {"SavingsPlansPurchaseRecommendation": {
        "SavingsPlansPurchaseRecommendationSummary": {
            "EstimatedMonthlySavingsAmount": "512.30",
            "HourlyCommitmentToPurchase": "1.2345",
            "EstimatedSavingsPercentage": "22.1",
            "EstimatedOnDemandCostWithCurrentCommitment": "3000.00",
        },
        "SavingsPlansPurchaseRecommendationDetails": [{"UpfrontCost": "900"}],
    }}
    cell = sp_cell_from_response("COMPUTE_SP", "3yr", "Partial Upfront", resp)
    assert cell["hourly_commitment"] == pytest.approx(1.2345)
    assert cell["monthly_savings"] == pytest.approx(512.30)
    assert cell["upfront"] == pytest.approx(900.0)


def test_sp_cell_empty_response_is_none():
    assert sp_cell_from_response("COMPUTE_SP", "1yr", "No Upfront", {}) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_commitment_scenarios.py -q`
Expected: collection error — `services.commitment_scenarios` not found.

- [ ] **Step 3: Implement parsers**

```python
"""Pure scenario math for commitment purchase recommendations.

Raw Cost Explorer response dicts in, normalized per-type scenario cards out.
No boto3, no ctx — every dollar transformation here is unit-testable with
plain dicts (pattern: services/rds_logic.py, services/commitment_coverage.py).

Field names verified against the CE API docs on 2026-08-08 (Task 1 Step 0):
<paste the confirmed strings/fields here>.
"""

from __future__ import annotations

from typing import Any

# (CE service string, display label). Populate from the Step-0 doc check;
# EC2's long form is known-required (the short "Amazon EC2" is rejected).
RI_SERVICES: tuple[tuple[str, str], ...] = (
    ("Amazon Elastic Compute Cloud - Compute", "EC2"),
    ("Amazon Relational Database Service", "RDS"),
    ("Amazon ElastiCache", "ElastiCache"),
    ("Amazon Redshift", "Redshift"),
    ("Amazon Elasticsearch Service", "OpenSearch"),
)
SP_TYPES: tuple[str, ...] = ("COMPUTE_SP", "EC2_INSTANCE_SP", "SAGEMAKER_SP")

TERMS: tuple[tuple[str, str], ...] = (("ONE_YEAR", "1yr"), ("THREE_YEARS", "3yr"))
PAYMENTS: tuple[tuple[str, str], ...] = (
    ("NO_UPFRONT", "No Upfront"),
    ("PARTIAL_UPFRONT", "Partial Upfront"),
    ("ALL_UPFRONT", "All Upfront"),
)

# Per-service nested identity keys inside RecommendationDetails.InstanceDetails.
_DETAIL_KEYS: dict[str, str] = {
    "EC2": "EC2InstanceDetails",
    "RDS": "RDSInstanceDetails",
    "ElastiCache": "ElastiCacheInstanceDetails",
    "Redshift": "RedshiftInstanceDetails",
    "OpenSearch": "ESInstanceDetails",
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
    """(instance_type, region, platform) from the per-service nested block."""
    inner = (detail.get("InstanceDetails") or {}).get(_DETAIL_KEYS[service_label], {})
    if service_label == "OpenSearch":
        itype = ".".join(p for p in (inner.get("InstanceClass"), inner.get("InstanceSize")) if p)
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
                "count": int(_money(detail, "RecommendedNumberOfInstancesToPurchase")),
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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `./venv/bin/python -m pytest tests/test_commitment_scenarios.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/commitment_scenarios.py tests/test_commitment_scenarios.py
git commit -m "feat(commitment): CE purchase-recommendation cell parsers (per-type, doc-verified fields)"
```

---

### Task 2: Card builders — grouping, break-even, risk line

**Files:**
- Modify: `services/commitment_scenarios.py` (append)
- Test: `tests/test_commitment_scenarios.py` (append)

**Interfaces:**
- Consumes: cell dicts from Task 1.
- Produces:
  - `build_ri_type_cards(cells: list[dict], uncovered: dict[str, float], scan_region: str) -> list[dict]`
    — one card per (service, instance_type, region); spec's "RI type-card" JSON shape; sorted scan-region first then `monthly_savings` desc. `uncovered` is `CommitmentCoverage.uncovered_on_demand` (keys `"{service_lower}:{instance_type}"`).
  - `build_sp_cards(cells: list[dict]) -> list[dict]` — one card per sp_type, `card_kind: "sp_commitment"`.
  - Both card kinds: `Counted: False`, `monthly_savings` = best cell's savings, `scenarios` sorted (term, payment) in TERMS/PAYMENTS order, `recommended_scenario` = index of highest-savings cell, per-scenario `break_even_months` and card-level `risk_pct`.

- [ ] **Step 1: Write failing tests (append to tests/test_commitment_scenarios.py)**

```python
from services.commitment_scenarios import build_ri_type_cards, build_sp_cards


def _cell(**over):
    base = {"service": "RDS", "instance_type": "db.r7i.4xlarge", "region": "eu-west-1",
            "platform": "aurora-postgresql", "count": 7, "term": "1yr",
            "payment": "No Upfront", "monthly_savings": 1210.40, "upfront": 0.0,
            "recurring_monthly": 2891.71, "ondemand_monthly": 4102.11}
    base.update(over)
    return base


def test_cards_group_six_cells_into_one():
    cells = [_cell(term=t, payment=p, monthly_savings=s)
             for (t, p, s) in [("1yr", "No Upfront", 1210.40), ("1yr", "Partial Upfront", 1300.0),
                               ("1yr", "All Upfront", 1350.0), ("3yr", "No Upfront", 1500.0),
                               ("3yr", "Partial Upfront", 1600.0), ("3yr", "All Upfront", 1700.0)]]
    cards = build_ri_type_cards(cells, uncovered={}, scan_region="eu-west-1")
    assert len(cards) == 1
    card = cards[0]
    assert card["card_kind"] == "ri_type"
    assert len(card["scenarios"]) == 6
    assert card["monthly_savings"] == pytest.approx(1700.0)          # best cell
    assert card["scenarios"][card["recommended_scenario"]]["monthly_savings"] == pytest.approx(1700.0)
    assert card["Counted"] is False


def test_break_even_months():
    cells = [_cell(upfront=1200.0, monthly_savings=100.0)]
    card = build_ri_type_cards(cells, {}, "eu-west-1")[0]
    assert card["scenarios"][0]["break_even_months"] == pytest.approx(12.0)


def test_break_even_zero_upfront_is_zero():
    card = build_ri_type_cards([_cell(upfront=0.0)], {}, "eu-west-1")[0]
    assert card["scenarios"][0]["break_even_months"] == 0.0


def test_risk_pct_from_best_cell():
    # risk = (recurring + upfront/term_months) / ondemand for the BEST cell.
    cells = [_cell(term="3yr", upfront=3600.0, recurring_monthly=2000.0,
                   monthly_savings=2002.11, ondemand_monthly=4102.11)]
    card = build_ri_type_cards(cells, {}, "eu-west-1")[0]
    # (2000 + 3600/36) / 4102.11 = 2100/4102.11 = 51.2%
    assert card["risk_pct"] == pytest.approx(51.2, abs=0.1)


def test_coverage_join_and_missing_key():
    cells = [_cell()]
    cards = build_ri_type_cards(cells, {"rds:db.r7i.4xlarge": 3199.65}, "eu-west-1")
    assert cards[0]["uncovered_monthly"] == pytest.approx(3199.65)
    # coverage_pct = 1 - uncovered/ondemand, floored at 0
    assert cards[0]["coverage_pct"] == pytest.approx(22.0, abs=0.1)
    # Missing key -> fields absent entirely (fail closed, never fabricated 0)
    bare = build_ri_type_cards(cells, {}, "eu-west-1")[0]
    assert "uncovered_monthly" not in bare and "coverage_pct" not in bare


def test_scan_region_sorts_first():
    cells = [_cell(region="us-east-1", monthly_savings=9999.0),
             _cell(instance_type="db.t3.medium", region="eu-west-1", monthly_savings=5.0)]
    cards = build_ri_type_cards(cells, {}, "eu-west-1")
    assert cards[0]["region"] == "eu-west-1"


def test_sp_cards_one_per_type():
    cells = [
        {"sp_type": "COMPUTE_SP", "term": "1yr", "payment": "No Upfront",
         "hourly_commitment": 1.2, "monthly_savings": 500.0, "savings_pct": 20.0,
         "upfront": 0.0, "estimated_ondemand_monthly": 2500.0},
        {"sp_type": "COMPUTE_SP", "term": "3yr", "payment": "All Upfront",
         "hourly_commitment": 1.1, "monthly_savings": 800.0, "savings_pct": 32.0,
         "upfront": 9000.0, "estimated_ondemand_monthly": 2500.0},
    ]
    cards = build_sp_cards(cells)
    assert len(cards) == 1
    assert cards[0]["card_kind"] == "sp_commitment"
    assert cards[0]["monthly_savings"] == pytest.approx(800.0)
    assert cards[0]["Counted"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_commitment_scenarios.py -q`
Expected: FAIL — `build_ri_type_cards` not defined.

- [ ] **Step 3: Implement builders (append to services/commitment_scenarios.py)**

```python
_TERM_MONTHS = {"1yr": 12, "3yr": 36}
# Coverage-join keys use the commitment_coverage service spelling.
_COVERAGE_SERVICE = {"EC2": "ec2", "RDS": "rds", "ElastiCache": "elasticache",
                     "Redshift": "redshift", "OpenSearch": "opensearch"}

_SCENARIO_ORDER = {(t, p): i for i, (t, p) in enumerate(
    (t_lbl, p_lbl) for _, t_lbl in TERMS for _, p_lbl in PAYMENTS)}


def _finish_scenarios(cells: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Sort cells canonically, add break_even_months, return (scenarios, best_idx)."""
    scenarios = sorted(cells, key=lambda c: _SCENARIO_ORDER.get((c["term"], c["payment"]), 99))
    for s in scenarios:
        net = s["monthly_savings"]
        s["break_even_months"] = round(s["upfront"] / net, 1) if s["upfront"] > 0 and net > 0 else 0.0
    best = max(range(len(scenarios)), key=lambda i: scenarios[i]["monthly_savings"])
    return scenarios, best


def build_ri_type_cards(cells: list[dict[str, Any]], uncovered: dict[str, float],
                        scan_region: str) -> list[dict[str, Any]]:
    """Group RI cells into one card per (service, instance_type, region).

    Coverage context joins from ``uncovered`` (CommitmentCoverage.uncovered_on_demand,
    keyed ``"{service}:{type}"``); a missing key omits the fields entirely —
    an unknown coverage is not a 0% coverage (C8).
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for c in cells:
        groups.setdefault((c["service"], c["instance_type"], c["region"]), []).append(c)

    cards: list[dict[str, Any]] = []
    for (service, itype, region), group in groups.items():
        scenarios, best = _finish_scenarios([dict(c) for c in group])
        best_cell = scenarios[best]
        ondemand = best_cell["ondemand_monthly"]
        card: dict[str, Any] = {
            "card_kind": "ri_type",
            "service": service,
            "instance_type": itype,
            "region": region,
            "platform": best_cell["platform"],
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
        key = f"{_COVERAGE_SERVICE.get(service, service.lower())}:{itype}"
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
        cards.append({
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
        })
    cards.sort(key=lambda c: -c["monthly_savings"])
    return cards
```

Note: `_finish_scenarios` keeps the full cell in `scenarios` for SP cards (they
lack `recurring_monthly`) — the SP comprehension above selects SP-shaped keys;
the RI comprehension selects RI-shaped keys. `discount_pct` for RI cells is
derivable in render as `100 * monthly_savings / ondemand_monthly`; do NOT store
a rounded duplicate that can drift from its factors (B2 lockstep).

- [ ] **Step 4: Run tests**

Run: `./venv/bin/python -m pytest tests/test_commitment_scenarios.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/commitment_scenarios.py tests/test_commitment_scenarios.py
git commit -m "feat(commitment): per-type RI cards + SP commitment cards with break-even and risk math"
```

---

### Task 3: Projected figure (non-overlap) + CoH merge

**Files:**
- Modify: `services/commitment_scenarios.py` (append)
- Test: `tests/test_commitment_scenarios.py` (append)

**Interfaces:**
- Consumes: card dicts from Task 2; CoH commitment rec dicts (have `estimatedMonthlySavings` camelCase, an action/type field naming SP or RI, and optionally `term`).
- Produces:
  - `projected_savings(ri_cards: list[dict], sp_cards: list[dict]) -> tuple[float, str]` — spec's non-overlap rule; basis string names the winning path.
  - `merge_coh_concurrence(cards: list[dict], coh_recs: list[dict]) -> list[dict]` — returns NEW card list (immutability rule) with `coh_concurs_monthly` set on matched cards; unmatched CoH recs returned via second element? No — signature returns cards only; unmatched CoH recs stay rendered by the existing CoH path (no data loss).

- [ ] **Step 1: Write failing tests (append)**

```python
from services.commitment_scenarios import merge_coh_concurrence, projected_savings


def _ri_card(service="RDS", savings=1000.0):
    return {"card_kind": "ri_type", "service": service, "instance_type": "x",
            "region": "eu-west-1", "Counted": False, "monthly_savings": savings,
            "scenarios": [], "recommended_scenario": 0}


def _sp_card(sp_type="COMPUTE_SP", savings=1500.0):
    return {"card_kind": "sp_commitment", "sp_type": sp_type, "Counted": False,
            "monthly_savings": savings, "scenarios": [], "recommended_scenario": 0}


def test_projected_compute_group_takes_max_not_sum():
    # EC2 RIs total 1000, Compute SP 1500 -> group1 = 1500 (max), never 2500.
    total, basis = projected_savings([_ri_card("EC2", 1000.0)], [_sp_card("COMPUTE_SP", 1500.0)])
    assert total == pytest.approx(1500.0)
    assert "Compute SP" in basis


def test_projected_sp_types_overlap_each_other():
    # Compute SP 1500 vs EC2-Instance SP 1600 -> 1600, not 3100.
    total, _ = projected_savings([], [_sp_card("COMPUTE_SP", 1500.0),
                                      _sp_card("EC2_INSTANCE_SP", 1600.0)])
    assert total == pytest.approx(1600.0)


def test_projected_disjoint_ri_services_sum():
    total, _ = projected_savings(
        [_ri_card("RDS", 1000.0), _ri_card("ElastiCache", 200.0)], [])
    assert total == pytest.approx(1200.0)


def test_projected_sagemaker_adds_on_top():
    total, _ = projected_savings([_ri_card("RDS", 1000.0)],
                                 [_sp_card("SAGEMAKER_SP", 300.0)])
    assert total == pytest.approx(1300.0)


def test_coh_merge_annotates_matching_card():
    cards = [_ri_card("RDS", 1000.0)]
    coh = [{"actionType": "PurchaseReservedInstances", "currentResourceType": "RdsDbInstance",
            "estimatedMonthlySavings": 950.0}]
    merged = merge_coh_concurrence(cards, coh)
    assert merged[0]["coh_concurs_monthly"] == pytest.approx(950.0)
    assert "coh_concurs_monthly" not in cards[0]  # input not mutated


def test_coh_merge_no_match_leaves_cards_untouched():
    cards = [_ri_card("RDS", 1000.0)]
    coh = [{"actionType": "PurchaseSavingsPlans", "estimatedMonthlySavings": 10.0}]
    merged = merge_coh_concurrence(cards, coh)
    assert "coh_concurs_monthly" not in merged[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_commitment_scenarios.py -q`
Expected: FAIL — `projected_savings` not defined.

- [ ] **Step 3: Implement (append)**

```python
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
```

- [ ] **Step 4: Run tests**

Run: `./venv/bin/python -m pytest tests/test_commitment_scenarios.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/commitment_scenarios.py tests/test_commitment_scenarios.py
git commit -m "feat(commitment): non-overlap projected-savings rule + CoH concurrence merge"
```

---

### Task 4: Adapter rewiring

**Files:**
- Modify: `services/adapters/commitment_analysis.py` — replace `_fetch_sp_recommendations` / `_fetch_ri_recommendations` bodies (lines ~502-691) with thin cell-collection loops; delete `_SP_TERMS/_SP_PAYMENTS/_SP_TYPES/_RI_SERVICES` class constants (module now owns them); wire cards + extras in `scan()`.
- Test: `tests/test_commitment_scenarios.py` (adapter-level test, append)

**Interfaces:**
- Consumes: Task 1-3 functions; `ctx.commitment_coverage.uncovered_on_demand` (may be None); `ctx.cost_hub_splits.get("commitment_analysis", [])` for CoH merge; existing `_route_ce_error`.
- Produces: `purchase_recommendations` SourceBlock whose recommendations are the card dicts; findings extras gain
  `projected_commitment_monthly_savings: float`, `projected_commitment_basis: str`, `uncovered_ondemand_monthly_total: float` (sum of `uncovered_on_demand` values, 0.0 when coverage absent — this one is a total of REAL CE reads, not fabricated).

- [ ] **Step 1: Write failing adapter test (append; stub ce client)**

```python
class _MatrixCe:
    """CE stub: RDS RI matrix returns one detail; everything else empty."""

    def get_reservation_purchase_recommendation(self, Service, LookbackPeriodInDays,
                                                TermInYears, PaymentOption):
        if "Relational" not in Service:
            return {"Recommendations": []}
        return _ri_resp([_rds_detail()])

    def get_savings_plans_purchase_recommendation(self, **kwargs):
        return {}

    # The adapter's other checks call these; empty answers are fine.
    def get_savings_plans_utilization(self, **kwargs):
        return {"Total": {}}

    def get_savings_plans_utilization_details(self, **kwargs):
        return {"SavingsPlansUtilizationDetails": []}

    def get_savings_plans_coverage(self, **kwargs):
        return {"SavingsPlansCoverages": []}

    def get_reservation_utilization(self, **kwargs):
        return {"Total": {}}

    def get_reservation_coverage(self, **kwargs):
        return {"CoveragesByTime": []}

    def get_cost_and_usage(self, **kwargs):
        return {"ResultsByTime": []}


def test_adapter_emits_ri_type_cards_and_projected_extras(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from services.adapters.commitment_analysis import CommitmentAnalysisModule

    ce = _MatrixCe()
    ctx = SimpleNamespace(
        region="eu-west-1", fast_mode=True, pricing_multiplier=1.0, pricing_engine=None,
        commitment_coverage=None, cost_hub_splits={},
        client=lambda name, region=None: ce if name == "ce" else MagicMock(),
        warn=MagicMock(), permission_issue=MagicMock(),
    )
    findings = CommitmentAnalysisModule().scan(ctx)
    cards = [r for r in findings.sources["purchase_recommendations"].recommendations
             if isinstance(r, dict) and r.get("card_kind") == "ri_type"]
    assert len(cards) == 1
    assert cards[0]["instance_type"] == "db.r7i.4xlarge"
    assert findings.extras["projected_commitment_monthly_savings"] == pytest.approx(
        cards[0]["monthly_savings"])
    # Projections never count (D4).
    assert all(r.get("Counted") is False for r in
               findings.sources["purchase_recommendations"].recommendations
               if isinstance(r, dict))
```

Adjust the ctx stub to whatever `scan()` actually touches (read the current
`scan()` first; e.g. if it reads `ctx.account_id`, add it). Keep the stub
minimal — every attribute it needs and nothing more.

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_commitment_scenarios.py -q`
Expected: new test FAILS (`card_kind` absent from old-shape recs / extras key missing).

- [ ] **Step 3: Rewire the adapter**

In `services/adapters/commitment_analysis.py`:

```python
from services.commitment_scenarios import (
    PAYMENTS,
    RI_SERVICES,
    SP_TYPES,
    TERMS,
    build_ri_type_cards,
    build_sp_cards,
    merge_coh_concurrence,
    projected_savings,
    ri_cells_from_response,
    sp_cell_from_response,
)


def _fetch_purchase_cards(self, ctx: Any, ce: Any) -> tuple[list[dict[str, Any]], float, str]:
    """Fan out the full CE purchase matrix and build per-type scenario cards.

    Every cell is one independent CE call; a denied/throttled cell degrades
    that cell only (routed through _route_ce_error). Returns
    (cards, projected_monthly, basis).
    """
    ri_cells: list[dict[str, Any]] = []
    for service_api, service_label in RI_SERVICES:
        for term_api, term_label in TERMS:
            for payment_api, payment_label in PAYMENTS:
                try:
                    resp = ce.get_reservation_purchase_recommendation(
                        Service=service_api,
                        LookbackPeriodInDays="THIRTY_DAYS",
                        TermInYears=term_api,
                        PaymentOption=payment_api,
                    )
                except Exception as e:
                    _route_ce_error(
                        ctx,
                        f"ce:GetReservationPurchaseRecommendation[{service_label}/({term_label}, {payment_label})]",
                        e,
                    )
                    continue
                ri_cells.extend(ri_cells_from_response(service_label, term_label, payment_label, resp))

    sp_cells: list[dict[str, Any]] = []
    for sp_type in SP_TYPES:
        for term_api, term_label in TERMS:
            for payment_api, payment_label in PAYMENTS:
                try:
                    resp = ce.get_savings_plans_purchase_recommendation(
                        SavingsPlansType=sp_type,
                        TermInYears=term_api,
                        PaymentOption=payment_api,
                        LookbackPeriodInDays="THIRTY_DAYS",
                    )
                except Exception as e:
                    _route_ce_error(
                        ctx,
                        f"ce:GetSavingsPlansPurchaseRecommendation[{sp_type}/({term_label}, {payment_label})]",
                        e,
                    )
                    continue
                cell = sp_cell_from_response(sp_type, term_label, payment_label, resp)
                if cell:
                    sp_cells.append(cell)

    coverage = getattr(ctx, "commitment_coverage", None)
    uncovered = dict(coverage.uncovered_on_demand) if coverage is not None else {}
    ri_cards = build_ri_type_cards(ri_cells, uncovered, getattr(ctx, "region", ""))
    sp_cards = build_sp_cards(sp_cells)
    projected, basis = projected_savings(ri_cards, sp_cards)

    coh_recs = [r for r in (getattr(ctx, "cost_hub_splits", {}) or {}).get("commitment_analysis", [])
                if isinstance(r, dict)]
    cards = merge_coh_concurrence(ri_cards + sp_cards, coh_recs)
    return cards, projected, basis
```

Then in `scan()`: replace the `purchase_recs = self._check_purchase_recommendations(ctx, ce)`
call with `purchase_cards, projected, basis = self._fetch_purchase_cards(ctx, ce)`;
put `purchase_cards` in the `purchase_recommendations` SourceBlock; extend extras:

```python
extras["projected_commitment_monthly_savings"] = projected
extras["projected_commitment_basis"] = basis
extras["uncovered_ondemand_monthly_total"] = round(
    sum((coverage.uncovered_on_demand or {}).values()), 2) if coverage is not None else 0.0
```

Delete: `_check_purchase_recommendations`, `_fetch_sp_recommendations`,
`_fetch_ri_recommendations`, and the `_SP_TERMS/_SP_PAYMENTS/_SP_TYPES/_RI_SERVICES`
class constants. Keep `_check_fargate_savings_plan` and everything else untouched.

- [ ] **Step 4: Run tests + line-count gate**

Run: `./venv/bin/python -m pytest tests/test_commitment_scenarios.py tests/test_commitment_coverage.py tests/test_commitment_fargate_sp.py -q`
Expected: PASS. Then `wc -l services/adapters/commitment_analysis.py` — must be < 800.

- [ ] **Step 5: Run regression gate**

Run: `./venv/bin/python -m pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py tests/test_offline_scan.py -q`
Expected: PASS (offline stubs return empty CE matrices → no cards, adapter still shapes correctly).

- [ ] **Step 6: Commit**

```bash
git add services/adapters/commitment_analysis.py services/commitment_scenarios.py tests/test_commitment_scenarios.py
git commit -m "feat(commitment): adapter fans out full RI/SP matrix, emits per-type scenario cards"
```

---

### Task 5: Summary plumbing — projected fields into scan JSON

**Files:**
- Modify: `core/result_builder.py` (`_summary`, lines ~115-128)
- Test: `tests/test_result_builder.py` (append)

**Interfaces:**
- Consumes: `findings["commitment_analysis"].extras["projected_commitment_monthly_savings"/"projected_commitment_basis"]`.
- Produces: `summary.projected_commitment_monthly_savings` (float, 0.0 default) and `summary.projected_commitment_basis` (str, "" default) in the scan JSON. `total_monthly_savings` untouched.

- [ ] **Step 1: Write failing test (append to tests/test_result_builder.py, mirroring its existing fixture style)**

```python
def test_summary_carries_projected_commitment_fields():
    findings = {
        "commitment_analysis": ServiceFindings(
            service_name="Commitment Analysis",
            total_recommendations=0,
            total_monthly_savings=0.0,
            sources={},
            extras={"projected_commitment_monthly_savings": 1234.56,
                    "projected_commitment_basis": "Compute SP path"},
        )
    }
    summary = ScanResultBuilder._summary(findings)
    assert summary["projected_commitment_monthly_savings"] == 1234.56
    assert summary["projected_commitment_basis"] == "Compute SP path"
    assert "total_monthly_savings" in summary  # headline untouched


def test_summary_projected_defaults_when_absent():
    findings = {"ec2": ServiceFindings("EC2", 0, 0.0, sources={})}
    summary = ScanResultBuilder._summary(findings)
    assert summary["projected_commitment_monthly_savings"] == 0.0
    assert summary["projected_commitment_basis"] == ""
```

(Use the import style already at the top of `tests/test_result_builder.py`.)

- [ ] **Step 2: Run to verify failure** — `./venv/bin/python -m pytest tests/test_result_builder.py -q` → FAIL (KeyError).

- [ ] **Step 3: Implement** — in `ScanResultBuilder._summary`, after the existing dict build:

```python
        commitment = findings.get("commitment_analysis")
        extras = dict(commitment.extras) if commitment is not None and commitment.extras else {}
        return {
            "total_services_scanned": scanned,
            "total_recommendations": sum(ScanResultBuilder._counted_recommendations(f) for f in findings.values()),
            "total_monthly_savings": sum(f.total_monthly_savings for f in findings.values()),
            # Projections are reported BESIDE the counted headline, never inside
            # it (commitment deep-dive spec, non-overlap rule).
            "projected_commitment_monthly_savings": float(
                extras.get("projected_commitment_monthly_savings", 0.0) or 0.0),
            "projected_commitment_basis": str(extras.get("projected_commitment_basis", "") or ""),
        }
```

- [ ] **Step 4: Run** — `./venv/bin/python -m pytest tests/test_result_builder.py tests/test_regression_snapshot.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add core/result_builder.py tests/test_result_builder.py
git commit -m "feat(summary): projected commitment savings fields beside the counted headline"
```

---

### Task 6: Card renderer — scenario tables in the Commitment tab

**Files:**
- Modify: `reporter_phase_b.py` — new functions `_render_commitment_purchase_cards`, `_render_scenario_table`, `_render_sp_vs_ri_strip`; register for the `purchase_recommendations` source of `commitment_analysis` wherever the current purchase-rec rendering dispatches (find the dispatch that today handles `check_category` "SP/RI Purchase Recommendation" recs — follow `_render_coh_commitment_scenarios` (line ~2184) as the pattern for table markup and dark-mode-safe classes).
- Test: `tests/test_commitment_scenarios.py` (renderer assertions, append)

**Interfaces:**
- Consumes: card dicts (Task 2/3 shapes).
- Produces: `_render_commitment_purchase_cards(recs: list[dict], source_name: str, descriptions: dict) -> str` matching the signature convention of the existing `_render_rds_enhanced_checks(recs, source_name, descriptions)`.

- [ ] **Step 1: Write failing renderer tests (append)**

```python
def test_render_ri_card_has_matrix_and_marked_recommendation():
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "ri_type", "service": "RDS", "instance_type": "db.r7i.4xlarge",
            "region": "eu-west-1", "platform": "aurora-postgresql",
            "recommended_count": 7, "current_ondemand_monthly": 4102.11,
            "coverage_pct": 22.0, "uncovered_monthly": 3199.65,
            "scenarios": [
                {"term": "1yr", "payment": "No Upfront", "monthly_savings": 1210.40,
                 "upfront": 0.0, "recurring_monthly": 2891.71, "break_even_months": 0.0},
                {"term": "3yr", "payment": "All Upfront", "monthly_savings": 1700.0,
                 "upfront": 12000.0, "recurring_monthly": 1800.0, "break_even_months": 7.1},
            ],
            "recommended_scenario": 1, "risk_pct": 51.2,
            "Counted": False, "monthly_savings": 1700.0,
            "coh_concurs_monthly": 1650.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "db.r7i.4xlarge" in html and "x7" in html
    assert "$1,210.40" in html and "$1,700.00" in html      # matrix cells
    assert "break-even" in html.lower()
    assert "51.2" in html                                    # risk line
    assert "22.0%" in html                                   # coverage context
    assert "CoH concurs" in html
    assert "projection" in html.lower()                      # advisory chip
    assert html.count("recommended") >= 1                    # AWS pick marked


def test_render_sp_card_states_no_instance_type():
    from reporter_phase_b import _render_commitment_purchase_cards

    card = {"card_kind": "sp_commitment", "sp_type": "COMPUTE_SP",
            "scenarios": [{"term": "3yr", "payment": "All Upfront",
                           "monthly_savings": 800.0, "upfront": 9000.0,
                           "hourly_commitment": 1.1, "savings_pct": 32.0,
                           "break_even_months": 11.3}],
            "recommended_scenario": 0, "Counted": False, "monthly_savings": 800.0}
    html = _render_commitment_purchase_cards([card], "purchase_recommendations", {})
    assert "$1.1000/hr" in html
    assert "EC2 + Lambda + Fargate" in html                  # services spanned
    assert "account-level" in html.lower()                   # no fake type detail


def test_render_groups_by_instrument_and_orders_by_savings():
    from reporter_phase_b import _render_commitment_purchase_cards

    small = {"card_kind": "ri_type", "service": "ElastiCache", "instance_type": "cache.t3.micro",
             "region": "eu-west-1", "platform": "redis", "recommended_count": 1,
             "current_ondemand_monthly": 20.0, "scenarios": [], "recommended_scenario": 0,
             "Counted": False, "monthly_savings": 5.0}
    big = {"card_kind": "sp_commitment", "sp_type": "COMPUTE_SP",
           "scenarios": [], "recommended_scenario": 0, "Counted": False,
           "monthly_savings": 900.0}
    html = _render_commitment_purchase_cards([small, big], "purchase_recommendations", {})
    assert html.index("Compute Savings Plan") < html.index("ElastiCache")
```

- [ ] **Step 2: Run to verify failure** — import error / missing function.

- [ ] **Step 3: Implement**

Follow `_render_coh_commitment_scenarios` (reporter_phase_b.py:2184) for table
markup and CSS class conventions. Structure:

```python
_SP_LABELS = {"COMPUTE_SP": ("Compute Savings Plan", "EC2 + Lambda + Fargate"),
              "EC2_INSTANCE_SP": ("EC2 Instance Savings Plan", "EC2 (family-locked)"),
              "SAGEMAKER_SP": ("SageMaker Savings Plan", "SageMaker only")}
_INSTRUMENT_ORDER = ["EC2", "RDS", "ElastiCache", "OpenSearch", "Redshift",
                     "COMPUTE_SP", "EC2_INSTANCE_SP", "SAGEMAKER_SP"]


def _render_scenario_table(card: Rec) -> str:
    """Term x payment table; the AWS-recommended cell gets the marker class."""
    ...


def _render_commitment_purchase_cards(recs: list[Rec], source_name: str,
                                      descriptions: dict) -> str:
    """Sections per instrument (savings-ordered), type-cards inside."""
    ...
```

Requirements the tests pin (implement exactly):
- group cards by instrument (`service` for ri_type, `sp_type` for sp_commitment);
  sections ordered by each instrument's best-path total, descending;
- section header carries the instrument label + its total;
- RI card title: `{instance_type} x{recommended_count} — {region} — {platform}`;
- coverage line only when the fields exist (fail-closed rendering);
- scenario table: rows = terms, cols = payments; each cell shows monthly saving
  (`${:,.2f}`), upfront, and derived discount `100*savings/ondemand` for RI cells;
  the `recommended_scenario` cell gets a `recommended` class + label;
- break-even + risk line beneath the table: "break-even {n} mo — still saves if
  usage stays >= {risk_pct}% of today";
- `coh_concurs_monthly` present → append "CoH concurs: ${:,.2f}/mo";
- every card carries the ADVISORY prefix + "projection — requires purchase" chip
  (reuse the existing advisory-chip helper the tab already uses — grep
  `_advisory_chip` / the SR-2 helper at line ~1886);
- SP card: hourly commitment (`${:.4f}/hr`), services spanned from `_SP_LABELS`,
  the sentence "account-level commitment — Savings Plans carry no instance type";
- negative/zero-savings cells: grey class, no dollars emphasized.

Register the renderer: find where `purchase_recommendations` recs dispatch today
(the generic phase-A path renders them as plain recs) and add the source-name
branch routing `commitment_analysis`/`purchase_recommendations` to
`_render_commitment_purchase_cards` — same mechanism the RDS enhanced_checks
renderer uses (`_render_rds_enhanced_checks` registration point).

- [ ] **Step 4: Run** — `./venv/bin/python -m pytest tests/test_commitment_scenarios.py tests/test_reporter_snapshots.py -q` → PASS. If a reporter snapshot legitimately changes (the tab's purchase section now renders cards), follow the snapshot-update procedure documented in `tests/test_reporter_snapshots.py` — read the diff first and confirm every change is the new section, nothing else.

- [ ] **Step 5: Commit**

```bash
git add reporter_phase_b.py tests/test_commitment_scenarios.py
git commit -m "feat(report): per-type commitment scenario cards with term x payment matrix"
```

---

### Task 7: Exec summary fact + stat cards + SP-vs-RI strip

**Files:**
- Modify: `html_report_generator.py` — exec summary facts block (lines ~2280-2290) + `commitment_analysis` `multi_source_cards` (line ~171)
- Modify: `reporter_phase_b.py` — `_render_sp_vs_ri_strip` wired into the EC2 RI section of `_render_commitment_purchase_cards`
- Test: `tests/test_commitment_scenarios.py` (append)

**Interfaces:**
- Consumes: `summary.projected_commitment_monthly_savings` / `_basis` (Task 5), extras `uncovered_ondemand_monthly_total` (Task 4).
- Produces: exec-summary `<dt>Projected commitment</dt>` fact; two stat cards ("Uncovered On-Demand", "Projected Savings"); EC2-section strip comparing best EC2 RI total vs best compute-SP cell.

- [ ] **Step 1: Write failing tests (append)**

```python
def test_exec_summary_shows_projected_fact():
    from html_report_generator import generate_html_report_from_json

    data = make_report()  # reuse the tests/test_output_audit.py fixture builder via import
    data["summary"]["projected_commitment_monthly_savings"] = 2345.67
    data["summary"]["projected_commitment_basis"] = "Compute SP path"
    html = generate_html_report_from_json(data)
    assert "Projected commitment" in html
    assert "$2,345.67" in html
    assert "Compute SP path" in html


def test_exec_summary_omits_projected_fact_when_zero():
    from html_report_generator import generate_html_report_from_json

    data = make_report()
    html = generate_html_report_from_json(data)
    assert "Projected commitment" not in html


def test_sp_vs_ri_strip_renders_on_ec2_section():
    from reporter_phase_b import _render_commitment_purchase_cards

    ec2_ri = {"card_kind": "ri_type", "service": "EC2", "instance_type": "r6i.4xlarge",
              "region": "eu-west-1", "platform": "Windows", "recommended_count": 3,
              "current_ondemand_monthly": 4000.0, "scenarios": [], "recommended_scenario": 0,
              "Counted": False, "monthly_savings": 1000.0}
    sp = {"card_kind": "sp_commitment", "sp_type": "COMPUTE_SP", "scenarios": [],
          "recommended_scenario": 0, "Counted": False, "monthly_savings": 1500.0}
    html = _render_commitment_purchase_cards([ec2_ri, sp], "purchase_recommendations", {})
    assert "SP vs RI" in html
    assert "$1,500.00" in html and "$1,000.00" in html
    assert "Lambda" in html          # flexibility trade-off stated
```

(`make_report` imports from `tests.test_output_audit`; adjust it there if the
exec-summary path requires additional top-level keys — check
`generate_html_report_from_json`'s required inputs and mirror the golden JSON's
top-level shape.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

1. `html_report_generator.py` exec summary (following the existing
   `<dt>Annual</dt>` / `<dt>Top services</dt>` pattern at ~2280): after "Open
   risks", when `summary.get("projected_commitment_monthly_savings", 0) > 0`:

```python
        projected = summary.get("projected_commitment_monthly_savings", 0) or 0
        if projected > 0:
            basis = html.escape(str(summary.get("projected_commitment_basis", "")))
            facts += (
                "<dt>Projected commitment</dt>"
                f"<dd>up to ${projected:,.2f}/mo <span class=\"fact-note\">"
                f"({basis}; requires purchase — not in the counted total)</span></dd>"
            )
```

2. Stat cards — extend the `commitment_analysis` entry at line ~171:

```python
    "commitment_analysis": {
        "multi_source_cards": [
            ("SP Utilization", "extras", "sp_utilization_rate"),
            ("SP Coverage", "extras", "sp_coverage_rate"),
            ("RI Utilization", "extras", "ri_utilization_rate"),
            ("RI Coverage", "extras", "ri_coverage_rate"),
            ("Uncovered On-Demand", "extras", "uncovered_ondemand_monthly_total"),
            ("Projected Savings", "extras", "projected_commitment_monthly_savings"),
        ],
    },
```

(Check the card formatter: the existing four use `formatter="percent"` via
StatCardSpec in the adapter — the two new ones are dollars; add them to the
adapter's `stat_cards` spec with the dollar formatter the StatCardSpec API
offers; read `core/contracts.py` StatCardSpec for the exact formatter names.)

3. `_render_sp_vs_ri_strip(ec2_ri_total: float, best_sp: Rec | None) -> str` in
   reporter_phase_b, called from the EC2 section only:

```python
def _render_sp_vs_ri_strip(ec2_ri_total: float, best_sp: Rec | None) -> str:
    """Aggregate-level comparison strip: best EC2 RI path vs best compute-SP cell.

    Aggregate only — AWS emits no per-type SP detail and we do not invent it.
    """
    if not best_sp or ec2_ri_total <= 0:
        return ""
    sp_dollars = best_sp["monthly_savings"]
    winner = "Savings Plan" if sp_dollars >= ec2_ri_total else "Reserved Instances"
    return (
        '<div class="sp-vs-ri">'
        f"<strong>SP vs RI:</strong> best Savings Plan ${sp_dollars:,.2f}/mo vs "
        f"EC2 RIs ${ec2_ri_total:,.2f}/mo — {winner} leads. "
        "Trade-off: a Compute SP also covers Lambda and Fargate and survives "
        "family changes; RIs can carry a capacity reservation."
        "</div>"
    )
```

- [ ] **Step 4: Run** — `./venv/bin/python -m pytest tests/test_commitment_scenarios.py tests/test_reporter_snapshots.py tests/test_regression_snapshot.py -q` → PASS (same snapshot-diff discipline as Task 6).

- [ ] **Step 5: Commit**

```bash
git add html_report_generator.py reporter_phase_b.py services/adapters/commitment_analysis.py tests/test_commitment_scenarios.py
git commit -m "feat(report): projected-commitment exec fact, stat cards, SP-vs-RI strip"
```

---

### Task 8: Output-audit sweep — projected figure reconciles

**Files:**
- Modify: `tools/output_audit.py` (new sweep + wire into `run_sweeps`)
- Test: `tests/test_output_audit.py` (append)

**Interfaces:**
- Consumes: scan JSON `summary.projected_commitment_monthly_savings` + commitment cards.
- Produces: `sweep_projected_commitment(data) -> list[Finding]` — recomputes the non-overlap rule from the cards via `services.commitment_scenarios.projected_savings` (import it — one source of truth, no reimplementation) and FAILs on >$0.50 disagreement.

- [ ] **Step 1: Write failing test (append to tests/test_output_audit.py)**

```python
def test_projected_commitment_reconciles():
    from tools.output_audit import sweep_projected_commitment

    data = make_report()
    data["services"]["commitment_analysis"] = {
        "service_name": "Commitment Analysis", "total_recommendations": 0,
        "total_monthly_savings": 0.0,
        "sources": {"purchase_recommendations": {"count": 1, "recommendations": [
            {"card_kind": "ri_type", "service": "RDS", "instance_type": "x",
             "region": "eu-west-1", "Counted": False, "monthly_savings": 1000.0,
             "scenarios": [], "recommended_scenario": 0}]}},
    }
    data["summary"]["projected_commitment_monthly_savings"] = 1000.0
    assert _fails(sweep_projected_commitment(data)) == []
    data["summary"]["projected_commitment_monthly_savings"] = 999999.0
    assert _fails(sweep_projected_commitment(data))
```

- [ ] **Step 2: Run to verify failure** — ImportError.

- [ ] **Step 3: Implement (add to tools/output_audit.py, wire into run_sweeps)**

```python
def sweep_projected_commitment(data: dict[str, Any]) -> list[Finding]:
    """S14 — summary projected figure recomputes from the commitment cards."""
    reported = float(data.get("summary", {}).get("projected_commitment_monthly_savings", 0) or 0)
    svc = data.get("services", {}).get("commitment_analysis", {})
    recs = [r for s in (svc.get("sources") or {}).values() if isinstance(s, dict)
            for r in s.get("recommendations", []) if isinstance(r, dict)]
    ri = [r for r in recs if r.get("card_kind") == "ri_type"]
    sp = [r for r in recs if r.get("card_kind") == "sp_commitment"]
    if not ri and not sp:
        if reported > 0:
            return [_finding("S14-projected", "FAIL", "commitment_analysis",
                             f"summary projects ${reported:,.2f}/mo but no purchase cards exist")]
        return []
    try:
        from services.commitment_scenarios import projected_savings
    except ImportError:
        return [_finding("S14-projected", "WARN", "commitment_analysis",
                         "cannot recompute projection: services package not importable")]
    expected, _ = projected_savings(ri, sp)
    if abs(expected - reported) > 0.5:
        return [_finding("S14-projected", "FAIL", "commitment_analysis",
                         f"summary ${reported:,.2f} != recomputed ${expected:,.2f} from cards")]
    return []
```

In `run_sweeps`, add `findings += sweep_projected_commitment(data)` after
`sweep_summary_semantics`. Update the module docstring sweep list (S14).

- [ ] **Step 4: Run** — `./venv/bin/python -m pytest tests/test_output_audit.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/output_audit.py tests/test_output_audit.py
git commit -m "feat(audit): S14 sweep — projected commitment figure reconciles from cards"
```

---

### Task 9: Docs, changelog, full gate

**Files:**
- Modify: `CHANGELOG.md` (new entry), `README.md` (feature bullet), `docs/audits/prompts/commitment_analysis_AUDIT_PROMPT.md` (note the new card shapes + S14), `ARCHITECTURE.md` if it lists commitment sources.

- [ ] **Step 1: Update docs** — CHANGELOG entry under a new heading following the file's existing format; README feature bullet: per-type RI/SP purchase matrix + projected figure; audit prompt: document `card_kind` shapes, the B1-ii projection convention for cards, and the S14 sweep.

- [ ] **Step 2: Full suite** — `./venv/bin/python -m pytest tests/ -q` → ALL PASS.

- [ ] **Step 3: Live smoke (operator assist)** — ask the operator to run `python3 cli.py --profile afs-prod eu-west-1 2>&1 | tee afs2.log`, then run `./venv/bin/python tools/output_audit.py afs-prod_eu-west-1.html --log afs2.log`: expect count 691 (round-1 RECONCILED), S14 clean, purchase sections rendered (afs-prod holds no commitments, so CE emits fresh purchase recs — coverage lines absent, correctly).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md README.md docs/audits/prompts/commitment_analysis_AUDIT_PROMPT.md ARCHITECTURE.md
git commit -m "docs: commitment deep-dive — changelog, README, audit prompt"
```

---

## Self-Review (done at plan time)

- **Spec coverage:** granularity/services/placement → Tasks 1-4; card content (matrix, break-even, coverage, SP-vs-RI) → Tasks 2, 6, 7; projected figure + non-overlap → Tasks 3, 5, 7, 8; failure modes → cell-level try/except (Task 4), fail-closed coverage join (Task 2), grey cells (Task 6); CoH dedup → Task 3; open items (DynamoDB, ES string, EC2_INSTANCE_SP family) → Task 1 Step 0.
- **Placeholders:** Task 6 Step 3 uses `...` for two function bodies deliberately — the requirements list beneath pins every behavior the tests assert; markup specifics must follow the existing `_render_coh_commitment_scenarios` pattern, which the implementer must read. All other code is complete.
- **Type consistency:** cell/card field names identical across Tasks 1-8 (`monthly_savings`, `card_kind`, `recommended_scenario`, `scenarios`, `uncovered_monthly`, `coverage_pct`, `risk_pct`, `coh_concurs_monthly`).
