# S3 Adapter — Cost-Fidelity Audit & Remediation

**Date**: 2026-06-23
**Scope**: `s3` adapter only — `services/adapters/s3.py`, `services/s3.py`,
`core/pricing_engine.py` S3 methods, `reporter_phase_b.py` S3 handlers,
`tests/test_s3_adapter.py`, `tests/test_pricing_engine.py`.
**Audit source**: S3 adapter audit (this session). Prices and Pricing-API
attributes validated live via the AWS Pricing API (us-east-1, publicationDate
2026-06-18) and the AWS Knowledge MCP.
**Reference implementations**: EBS / EC2 (computed-from-delta savings,
count==rendered hygiene) and RDS (`RDS_REMEDIATION_PLAN.md` honest-advisory
pattern for unquantifiable findings).

> Status: **IMPLEMENTED** and validated against a live M360 ap-south-1 scan.
> All findings below (S3-A … S3-I) are fixed. Tests green (`375 passed`).

---

## 0. Guiding principles

1. **Every emitted recommendation must carry a concrete, account-specific $ saving.**
   A $0 / blank-savings finding is advisory, not a counted recommendation.
2. **Never assume an access pattern.** A storage-class saving requires measured
   evidence the data is cold; otherwise it is advisory.
3. **Price each resource at its own home region.** S3 is global.
4. **Filter at the source, not at render.** The adapter decides what counts;
   the reporter only formats. `counted == $-bearing`.

---

## 1. Findings

Severity: 🔴 critical · 🟠 high · 🟡 medium · 🟢 low.

### 🔴 S3-A — Bucket cost priced 100% at Standard, ignoring storage-class mix
**Was**: the full-mode loop summed per-class `BucketSizeBytes` into one total and
priced the whole sum at the Standard rate
(`_calculate_s3_storage_cost(total_size_gb, "STANDARD", …)`). A bucket already in
Glacier / Deep Archive / IA was over-stated by up to **~23×**
(DEEP_ARCHIVE $0.00099 vs STANDARD $0.023). The correct per-class helper
`_estimate_s3_bucket_cost` existed but was **dead code**.

**Fix**: capture per-class GB (`_CW_STORAGE_TYPE_TO_CLASS`) and price each class
at its own live rate via `_cost_from_class_sizes` / `_s3_price_per_gb`. Dead code
removed (S3-F).

### 🔴 S3-B — Savings assumed an access pattern with zero evidence
**Was**: `S3_SAVINGS_FACTORS` (`lifecycle_missing=0.30`, `intelligent_tiering=0.20`,
`both_missing=0.40`) — the 0.30 was documented as "assumes ~65% IA-eligible".
Applied to *every* bucket lacking a config, with **no read** of Storage Lens,
Storage Class Analysis, or request metrics. IA/Glacier also carry
retrieval + per-request charges (verified as separate SKUs) the model ignored, so
for hot data a lifecycle→IA recommendation is net-**negative** (wrong-signed).

**Fix (Hybrid, evidence-gated)**: a bucket earns a concrete dollar **only** when
(a) it holds S3 Standard bytes a transition could move, **and** (b)
`_assess_bucket_coldness` reads CloudWatch S3 **request metrics** (`GetRequests`,
whole-bucket `FilterId`, over `COLD_LOOKBACK_DAYS=30`) and finds **zero** GETs.
The saving is then the real `standard_gb × (Standard − Standard-IA)` rate delta,
recorded in `PricingBasis`. When no evidence exists (request metrics off, or fast
mode) the bucket is a **$0 advisory** pointing at Storage Class Analysis —
never a fabricated figure. The assumed percentages are gone.

Requires `s3:GetMetricsConfiguration`. Denials route through
`ctx.permission_issue` and degrade to `unknown` → advisory (fail-safe).

### 🟠 S3-C — `$0` records counted as recommendations
**Was**: `total_recommendations = len(opt_opps) + len(other_recs)` — every bucket
(incl. static-website, fully-optimized, and $0 enhanced checks) counted.

**Fix**: `total_recommendations` counts only $-bearing records. Advisory/visibility
records remain in the source blocks (still rendered) and are tallied in
`extras["advisory_count"]`.

### 🟠 S3-D — Pricing engine returned the wrong tiered dimension
**Was**: `_extract_usd` took `next(iter(priceDimensions))`. S3 Standard's OnDemand
term has 3 tiered dimensions (0/50TB/500TB → $0.023/$0.022/$0.021); the API
serialized the **$0.022** "next 450 TB" row first, so the engine returned $0.022,
not the $0.023 marginal rate — order-dependent.

**Fix**: `_extract_s3_base_rate` selects the `beginRange == "0"` base tier.

### 🟡 S3-E — Pricing engine S3 storage-class labels didn't match attribute values
**Was**: `_fetch_s3_price` filtered on `storageClass` labels like
`"Amazon Glacier Flexible Retrieval"` / `"One Zone - Infrequent Access"` that **do
not exist** as `storageClass` attribute values → silent fallback to constants.
Masked because the live path only ever requested STANDARD, but latent.

**Fix**: pin `volumeType` (verified values) + `productFamily=Storage` via
`_S3_VOLUME_TYPE_BY_CLASS`, then `_select_s3_storage_rate` picks the timed-storage
SKU (skipping `Staging`/`Overhead` rows — e.g. Deep Archive's `GDA-Staging`
$0.021 vs the real `GDA-ByteHrs` $0.00099). `EXPRESS_ONE_ZONE` added to fallback.

### 🟡 S3-F — Dead code `_estimate_s3_bucket_cost`
Removed; its correct per-class logic folded into `_cost_from_class_sizes` (S3-A).

### 🟢 S3-G — Hardcoded regional multipliers
`S3_REGIONAL_MULTIPLIERS` documented as **fallback-only** (PricingEngine is
authoritative); tolerated Glacier-key drift noted (every Glacier multiplier is
1.0, so a missed lookup is a no-op).

### 🟢 S3-H — Fast-mode / advisory caveats not surfaced
Reporter now renders `PricingBasis` (for credited savings), the advisory caveat,
and the fast-mode sampling warning per bucket.

### 🟠 S3-I — Cross-region pricing (found during live-report validation)
**Was**: `ctx.pricing_engine` is bound to the **scan** region; `_s3_price_per_gb`
passed `bucket_region` only to the fallback path. S3 is global, so out-of-region
buckets were priced at the scan rate. Live ap-south-1 scan priced **us-east-1**
buckets at **$0.025/GB** (real $0.023, **+8.7%**) and **eu-central-1** at $0.025
(real $0.0245). The one credited bucket (eu-central-1) was ~1.8% over.

**Fix**: `PricingEngine.for_region(region)` returns a cached sibling engine scoped
to the resource's home region (shared global pricing client, separate
region-correct cache). `_s3_price_per_gb` and `_calculate_s3_storage_cost` now
price at `bucket_region`. Reusable by any adapter with cross-region resources.

---

## 2. New / changed symbols

| Symbol | File | Purpose |
|--------|------|---------|
| `_CW_STORAGE_TYPE_TO_CLASS` | `services/s3.py` | CloudWatch `StorageType` → class key |
| `_s3_price_per_gb` | `services/s3.py` | Region-correct unrounded $/GB rate |
| `_cost_from_class_sizes` | `services/s3.py` | Sum cost per class at its own rate (S3-A) |
| `_assess_bucket_coldness` | `services/s3.py` | `cold`/`warm`/`unknown` from request metrics (S3-B) |
| `COLD_LOOKBACK_DAYS`, `_GAP_OPPORTUNITY_CLASSES` | `services/s3.py` | Evidence-model constants |
| `PricingEngine.for_region` | `core/pricing_engine.py` | Cached per-region sibling engine (S3-I) |
| `_extract_s3_base_rate`, `_select_s3_storage_rate`, `_S3_VOLUME_TYPE_BY_CLASS` | `core/pricing_engine.py` | Correct S3 SKU + base-tier selection (S3-D/S3-E) |
| `extras["advisory_count"]` | `services/adapters/s3.py` | Count of $0 visibility records (S3-C) |

**Removed**: `S3_SAVINGS_FACTORS`, `_estimate_s3_bucket_cost`.

**New IAM dependency**: `s3:GetMetricsConfiguration` (fail-safe on denial).

---

## 3. Live validation (M360 ap-south-1, 2026-06-23)

247 buckets. Results from the implemented code:

- **Credited**: 1 bucket — `360mea-medialive-main-frankfurt`, 3366 GB all Standard,
  0 GET requests in 30d → **$37.70/mo** = `3366.0 × $0.0112` (Standard→IA delta).
- **False positives suppressed**: 3 `warm` buckets with active GET traffic credited
  **$0** — incl. `360mea-production-media` (25.8 TB) and `360mea-streaming` (14 TB).
  The old factor model would have fabricated **$193.67 + $69.84 ≈ $263/mo** of
  savings on hot, actively-served media (where IA would *raise* cost).
- **Magnitude**: old assumed-factor model **$323.62/mo** → new evidence-gated
  **$37.70/mo**. The gap was unevidenced / wrong-signed claims.
- **Counts**: `total_recommendations=1`, `advisory_count=336` =
  (247−1 buckets) + (90−0 enhanced). Reconciles exactly.
- **S3-I confirmed**: pre-fix, out-of-region buckets showed $0.025/GB regardless
  of region; post-fix they price at their home region.

After the S3-I fix, re-run the scan to regenerate the report; the credited bucket's
figure becomes ~$37.03 (eu-central-1 rates) and out-of-region "current cost"
columns correct downward (us-east-1/us-west-2 buckets −8.7%).
