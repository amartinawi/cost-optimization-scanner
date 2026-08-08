# Commitment Deep-Dive: Per-Type RI/SP Purchase Analysis + Reporting

**Date**: 2026-08-08
**Status**: Approved (design); implementation pending
**Owner request**: "Focus on reservation and savings plans for all services — more
detailed per instance, covering all options and recommendations, and the
reporting part of displaying the findings."

## Goal

Expand the Commitment Analysis tab from account-level utilization/coverage
ratios into a per-instance-type purchase advisor: every commitment-eligible
service, the full term x payment scenario matrix per type, coverage context,
break-even math, and a redesigned rendering — while leaving the counted
headline's "realizable waste-removal only" invariant untouched.

## Decisions (settled with the owner)

| Question | Decision |
|----------|----------|
| Granularity | Per instance TYPE (CE-native, AWS-computed dollars). Per-resource attribution is OUT of scope. |
| Services | All CE-supported: EC2, RDS, ElastiCache, Redshift, OpenSearch RIs + Compute SP, EC2-Instance SP, SageMaker SP. DynamoDB reserved capacity if the CE API supports it (verify against docs at implementation). |
| Placement | Commitment Analysis tab only. Per-service tabs unchanged. |
| Exec summary | New SEPARATE "projected commitment savings" fact. Counted headline untouched. |
| Card content | Full scenario matrix + break-even/risk line + coverage context + SP-vs-RI strip (EC2). |
| Cost | Always on (~45-50 CE calls ≈ $0.50/scan accepted). |

## Architecture

Pure-logic module + slim adapter (pattern: `rds_logic.py`, `commitment_coverage.py`):

- `services/commitment_scenarios.py` (NEW, ~350 lines): dataclasses + pure
  functions. Raw CE response dicts in → normalized cards out. No boto3, no ctx.
- `services/adapters/commitment_analysis.py`: fetches the matrix cells,
  delegates all math/grouping to the new module. Existing purchase-rec code
  moves out; the file must land below the 800-line repo cap.
- `reporter_phase_b.py`: one new card renderer + the EC2 SP-vs-RI strip.
- `html_report_generator.py`: projected fact in the exec summary + 2 stat cards.
- `core/result_builder.py`: two additive summary fields.
- `tools/output_audit.py`: projected-figure reconcile sweep.

## Data collection

**RI matrix** — `ce:GetReservationPurchaseRecommendation` for
{EC2, RDS, ElastiCache, Redshift, OpenSearch} x {ONE_YEAR, THREE_YEARS} x
{NO_UPFRONT, PARTIAL_UPFRONT, ALL_UPFRONT} = 30 cells. Response details are
per instance type (type, region, platform/engine, recommended count, monthly
savings, upfront). CE service strings use the full names (e.g. "Amazon Elastic
Compute Cloud - Compute" — the short form is rejected; already learned once).

**SP matrix** — `ce:GetSavingsPlansPurchaseRecommendation` for
{COMPUTE_SP, EC2_INSTANCE_SP, SAGEMAKER_SP} x 2 terms x 3 payments = 18 cells.
Hourly-commitment based, account-level — no instance type by nature; the card
says so rather than faking type detail.

- Every cell is an independent call: throttle/denial degrades that cell only
  (existing `_route_ce_error`). CE is a global API — recs span regions; cards
  are region-tagged, scan-region sorted first, all included in totals
  (commitments are account-level constructs).
- Coverage context joins from `ctx.commitment_coverage` (already prefetched):
  per-type coverage % and uncovered on-demand $/mo. No new calls.

## Rec model

All purchase cards are `Counted=False` projections. Per the existing B1-ii
convention the projection dollar lives in `monthly_savings`; projections never
inflate `total_recommendations` (D4) and never touch the counted headline.

**RI type-card** — one per (service, instance_type, region), grouped from up
to 6 matrix cells:

```json
{
  "card_kind": "ri_type",
  "service": "RDS", "instance_type": "db.r7i.4xlarge", "region": "eu-west-1",
  "platform": "aurora-postgresql",
  "recommended_count": 7,
  "current_ondemand_monthly": 4102.11,
  "coverage_pct": 22.0,
  "uncovered_monthly": 3199.65,
  "scenarios": [
    {"term": "1yr", "payment": "No Upfront", "monthly_savings": 1210.40,
     "upfront": 0.0, "recurring_monthly": 2891.71, "discount_pct": 29.5,
     "break_even_months": 0.0}
  ],
  "recommended_scenario": 3,
  "Counted": false,
  "monthly_savings": 1210.40,
  "AuditBasis": {"source": "ce:GetReservationPurchaseRecommendation",
                 "lookback_days": 30,
                 "basis": "AWS-computed purchase recommendation; projection, not counted"}
}
```

(`monthly_savings` carries the BEST cell's figure — the projection dollar per
the B1-ii convention; it is never summed into the counted headline.)

- `break_even_months = upfront / monthly_net_saving` (0 upfront → "immediate").
- Risk line: "still saves if usage stays >= N% of today", where
  `N = (recurring_monthly + upfront/term_months) / current_ondemand_monthly`.
- Cells CE omits (denied/empty) are absent, never zero-filled.

**SP commitment-card** — one per SP type: hourly commitment $/hr, the same
scenario list, estimated post-purchase coverage, services spanned
(Compute SP: EC2 + Lambda + Fargate; SageMaker SP: SageMaker only).

**CoH dedup** — CoH-routed SP/RI purchase recs merge into the matching card as
a "CoH concurs: $X/mo" line (dedup key: instrument + service + term). No
duplicate cards.

## Projected figure + non-overlap rule

New exec-summary fact: "Projected commitment savings — up to $X/mo (best
purchase path; requires upfront/term commitment)".

SP and RI discount the SAME on-demand spend, so:

- Group 1 (compute): `max( max(best Compute-SP, best EC2-Instance-SP) totals,
  sum of best EC2 RI cards )`
- Group 2: sum of best RI cards for RDS + ElastiCache + Redshift + OpenSearch
  (disjoint services)
- Group 3: best SageMaker SP cell
- `X = group1 + group2 + group3`; "best" = highest monthly_savings cell/card.

JSON: `summary.projected_commitment_monthly_savings` (float) +
`summary.projected_commitment_basis` (string naming the winning path).
Additive fields; `total_monthly_savings` untouched; S1/S7 sweeps unaffected.

## Rendering (Commitment tab, top to bottom)

1. Stat strip (existing 4 cards) + NEW "Uncovered on-demand $/mo" + NEW
   "Projected savings (best path)".
2. Existing counted sections unchanged: under-utilized commitments, expiring.
3. NEW purchase sections, one per instrument, savings-ordered; header carries
   the instrument's best-path total.
4. Type-cards inside each section, savings-sorted. Card layout: title
   (`db.r7i.4xlarge x7 — eu-west-1 — aurora-postgresql`), coverage context
   line, 6-cell table (rows = term, cols = payment; cell = monthly saving /
   upfront / discount %), AWS-recommended cell visually marked, break-even +
   risk line beneath. Scan-region first, others region-tagged after.
   Zero/negative-saving cells split by kind: an RI detail with zero/negative
   savings is dropped at the parser — it never reaches a card. An SP cell is
   KEPT whenever its `hourly_commitment > 0`, even at `monthly_savings == 0`
   (a whole SP-type matrix can legitimately net $0 while AWS still
   recommends the commitment); that cell renders greyed and is excluded from
   the AWS-recommended marker and from best-path math, rather than dropped.
5. EC2 section: SP-vs-RI strip — best EC2 RI total vs best Compute/EC2-Instance
   SP cell, flexibility trade-off stated. Aggregate-level only (AWS emits no
   per-type SP detail; we do not invent it).

Mechanics: new render function in `reporter_phase_b.py` registered for the
redesigned `purchase_recommendations` source; descriptor-driven via
`reporter_phase_a`; ADVISORY source-confidence prefix + "projection — requires
purchase" chip on every card; advisory-only tab still renders (D2); dark mode
+ existing type system.

## Failure modes (fail closed — C8)

| Condition | Behavior |
|-----------|----------|
| Cell denied/throttled | Cell omitted, `_route_ce_error` records; card renders remaining cells |
| All cells of a card missing | No card; warning |
| No SP/RI held (e.g. afs-prod) | Purchase sections still render (CE recommends NEW purchases); coverage context shows 0% |
| `commitment_coverage` unavailable | Card renders without coverage line — never a fabricated 0 |
| Upfront = 0 | Break-even "immediate" |
| Negative/zero net-saving RI detail | Dropped at the parser — never reaches a card |
| Negative/zero net-saving SP cell | Kept (AWS still recommended the commitment); rendered greyed, excluded from best-path |
| Type in another region | Rendered, region-tagged, included in projected total |

## Testing (TDD, RED first)

- `tests/test_commitment_scenarios.py`: cell grouping, card build, break-even
  math, risk-line math, best-path/non-overlap rule (incl. SP-vs-RI max),
  CoH merge, malformed/partial CE payloads, negative-cell exclusion.
- Renderer: scenario-table output into the reporter snapshot machinery;
  D2 (tab renders) + D4 (projections don't inflate counts) assertions.
- Harness: `tools/output_audit.py` sweep — projected figure recomputes from
  cards within a $0.50 tolerance; PROJECTION exemption already covers this tab.
- Regression gate green throughout:
  `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py`.

## Out of scope

- Per-resource coverage attribution (which exact instance a commitment
  discounts) — AWS does not publish it; revisit only with a defensible source.
- Folding projections into the counted headline — permanently out.
- The `_demote` numeric-on-demoted-recs convention (parked question from the
  2026-08-08 output audit) — separate decision.
- MemoryDB / CloudFront security-bundle commitments — not CE-supported.

## Open items to verify during implementation

1. DynamoDB reserved capacity support in `GetReservationPurchaseRecommendation`
   (check docs; include as a 6th RI service if supported).
2. Exact CE service strings for OpenSearch ("Amazon Elasticsearch Service" vs
   "Amazon OpenSearch Service") — verify against the supported-values error.
3. `EC2_INSTANCE_SP` recommendations may be per-family — if the API returns a
   family dimension, surface it on the SP card.

## Estimated scope

~1,200 lines including tests. No new dependencies. Additive JSON only — no
schema break.
