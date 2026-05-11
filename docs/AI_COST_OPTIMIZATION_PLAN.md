# AI/ML Cost Optimization — Deep-Dive Build Plan

> **Scope**: New and enhanced checks for AI/ML services on AWS.
> **Approach**: Detect real waste programmatically, quantify savings in dollars, surface actionable recommendations.
> **Last Updated**: May 2026

---

## What's Already Built (Do Not Duplicate)

| Adapter | Existing Checks |
|---------|----------------|
| `bedrock.py` | Idle Provisioned Throughput, PT breakeven, idle Knowledge Bases (flat flag), idle Agents (flat $5) |
| `sagemaker.py` | Idle real-time endpoints (7-day CW window), running notebooks, Spot training, multi-model consolidation |

---

## Overview: New Checks by Priority

| # | Check | Service | File | Impact | Complexity | CW? |
|---|-------|---------|------|--------|-----------|-----|
| B1 | Orphaned OpenSearch Serverless from KBs | Bedrock | `bedrock.py` | HIGH ($350/mo each) | Medium | Optional |
| B2 | Batch inference candidates | Bedrock | `bedrock.py` | HIGH (50% savings) | Medium | Yes |
| B3 | Prompt caching not enabled | Bedrock | `bedrock.py` | HIGH (RAG apps) | Medium | Yes |
| B4 | Idle / over-configured Guardrails | Bedrock | `bedrock.py` | Low-Medium | Easy | Yes |
| B5 | Invocation logging data cost | Bedrock | `bedrock.py` | Low-Medium | Easy | Yes |
| S1 | Idle Studio KernelGateway apps | SageMaker | `sagemaker.py` | HIGH (per engineer) | Easy | No |
| S2 | Serverless inference candidates | SageMaker | `sagemaker.py` | HIGH (dev/test) | Medium | Yes |
| S3 | Endpoints without auto-scaling | SageMaker | `sagemaker.py` | Medium-High | Easy | Optional |
| S4 | Graviton instance migration | SageMaker | `sagemaker.py` | Medium-High | Easy | No |
| S5 | Canvas idle sessions / no shutdown | SageMaker | `sagemaker.py` | Medium | Easy | No |
| S6 | Feature Store idle online groups | SageMaker | `sagemaker.py` | Medium | Medium | Yes |
| K1 | Idle Kendra indexes | Kendra | `kendra.py` | HIGH ($1,008/mo each) | Easy | Yes |
| K2 | Kendra edition downgrade | Kendra | `kendra.py` | Medium ($198/mo) | Easy | Yes |
| R1 | Idle Rekognition Custom Labels models | Rekognition | `rekognition.py` | HIGH ($2,880/mo each) | Easy | Yes |
| C1 | Idle Comprehend custom endpoints | Comprehend | `comprehend.py` | HIGH ($1,296/mo each) | Easy | Yes |
| Q1 | Q Business inactive subscriptions | Q Business | `q_business.py` | Medium ($20/user/mo) | Medium | No |

---

## Part 1: Bedrock Enhancements

### B1 — Orphaned OpenSearch Serverless Collections from Knowledge Bases

**Why this matters**: When a Bedrock Knowledge Base is deleted (or failed), the backing OpenSearch Serverless collection continues billing at $0.24/OCU-hr with a hard minimum of 2 OCUs. The charge appears under OpenSearch in Cost Explorer, not Bedrock — teams never see it.

**AWS APIs:**
```python
# bedrock-agent client
agent.list_knowledge_bases()
agent.get_knowledge_base(knowledgeBaseId=kb_id)
# opensearchserverless client
oss.list_collections()
oss.batch_get_collection(names=[...])
```

**Detection logic:**
1. Call `list_collections()` — find any collection with a tag `CreatedBy=AmazonBedrock` or name prefix `bedrock-knowledge-base-*`.
2. Call `list_knowledge_bases()` — build a set of `storageConfiguration.opensearchServerlessConfiguration.collectionArn` values from all ACTIVE KBs.
3. Any OSS collection in step 1 NOT in step 2's ARN set = orphaned.
4. Also flag KBs in `FAILED` or `DELETING` state whose backing collection is still `ACTIVE`.
5. Optional CW: check `AWS/Bedrock/KnowledgeBaseRetrievalResults` over 30 days as confirmation of zero activity.

**Savings formula:**
```python
# StandbyReplicas == "DISABLED" → dev/test (1 OCU minimum)
# StandbyReplicas == "ENABLED"  → production (2 OCU minimum)
min_ocus = 1 if collection.get("standbyReplicas") == "DISABLED" else 2
monthly_waste = min_ocus * 0.24 * 730 * pricing_multiplier
# Dev: $175/mo, Production: $350/mo
```

**New source block in `bedrock.py` `ServiceFindings`:**
```python
"orphaned_vector_stores": SourceBlock(count=..., recommendations=...)
```

Each recommendation dict: `collection_id`, `collection_name`, `collection_arn`, `standby_replicas`, `check_category="Orphaned Vector Stores"`, `monthly_savings`, `reason`.

**New stat card:** `StatCardSpec(label="Orphaned Vector Stores", source_path="sources.orphaned_vector_stores.count", formatter="int")`

**New `required_clients` entry:** add `"opensearchserverless"` to the tuple.

**IAM permissions needed:**
```json
"aoss:ListCollections", "aoss:BatchGetCollection"
```

---

### B2 — Batch Inference Candidates (On-Demand Workloads With Batch Traffic Patterns)

**Why this matters**: Bedrock Batch Inference costs 50% of on-demand. Document pipelines, nightly summarization jobs, and bulk classification running as real-time `invoke_model` calls are wasting half their budget.

**AWS APIs:**
```python
bedrock.list_model_invocation_jobs(statusEquals="Completed")  # check if batch ever used
cloudwatch.get_metric_statistics(
    Namespace="AWS/Bedrock",
    MetricName="Invocations",
    Dimensions=[{"Name": "ModelId", "Value": model_id}],
    Period=3600,  # hourly granularity
    Statistics=["Sum"],
    StartTime=now - timedelta(days=30),
    EndTime=now,
)
```

**Detection logic (skip in `fast_mode`):**
1. Call `list_model_invocation_jobs()`. If account has zero completed batch jobs: flag entire account as "batch inference never used."
2. For each model with CW invocations: pull 30-day hourly distribution (720 data points).
3. Batch pattern signal: ≥80% of invocations fall within a 6-hour window each day (nightly job pattern) AND the model supports batch (`anthropic.claude-*`, `amazon.nova-*`, `amazon.titan-*`).
4. Compute estimated on-demand cost for those invocations; apply 50% savings rate.

**Savings formula:**
```python
BATCH_SAVINGS_RATE = 0.50
# Estimate on-demand cost for batch-pattern invocations
# Input tokens × model_input_price + output tokens × model_output_price → × 0.50
```
Since per-token cost per model is complex, use a simplified estimate:
```python
BATCH_CANDIDATE_SAVINGS_ESTIMATE = total_invocations_in_batch_window * avg_cost_per_invocation * BATCH_SAVINGS_RATE * pricing_multiplier
```

**New source block:** `"batch_inference_candidates": SourceBlock(...)`

**IAM:** `bedrock:ListModelInvocationJobs`

---

### B3 — Prompt Caching Not Enabled (High Input:Output Token Ratio)

**Why this matters**: Prompt cache reads cost ~10% of standard input token price. For RAG apps, coding assistants, and document analysis, 60-80% of input tokens are a repeated static prefix (system prompt + retrieved context). Teams that never added `cachePoint` markers to their inference calls are paying 10× for the cacheable portion.

**AWS APIs:**
```python
cloudwatch.get_metric_statistics(
    Namespace="AWS/Bedrock",
    MetricName="InputTokenCount",  # and "OutputTokenCount"
    Dimensions=[{"Name": "ModelId", "Value": model_id}],
    Period=86400 * 30,
    Statistics=["Sum"],
    StartTime=now - timedelta(days=30),
    EndTime=now,
)
```

**Detection logic (skip in `fast_mode`):**
1. Pull 30-day sum of `InputTokenCount` and `OutputTokenCount` per `ModelId`.
2. Compute `ratio = input_tokens / max(output_tokens, 1)`.
3. Cache candidate signal: `ratio > 4.0` AND model supports caching (Claude 3.x/3.5/4.x, Nova Pro/Lite/Micro, Llama 3.x) AND total input tokens > 500,000/month.
4. Savings estimate assumes 60% cache hit rate on a 70% cacheable prefix fraction.

**Savings formula:**
```python
CACHEABLE_PREFIX_FRACTION = 0.70
CACHE_HIT_RATE = 0.60
# Per model per month:
cacheable_tokens = input_tokens * CACHEABLE_PREFIX_FRACTION
cached_tokens = cacheable_tokens * CACHE_HIT_RATE
# Input token prices per model (module constants — AWS does not expose via Pricing API)
INPUT_TOKEN_PRICE = {
    "anthropic.claude-3-5-sonnet": 6.00 / 1_000_000,
    "anthropic.claude-3-haiku": 0.80 / 1_000_000,
    "anthropic.claude-3-5-haiku": 1.00 / 1_000_000,
    "amazon.nova-pro": 0.80 / 1_000_000,
    "amazon.nova-lite": 0.06 / 1_000_000,
}
CACHE_READ_DISCOUNT = 0.90  # cache read = 10% of input price
savings = cached_tokens * input_price * CACHE_READ_DISCOUNT * pricing_multiplier
```

**New source block:** `"prompt_caching_opportunities": SourceBlock(...)`

**IAM:** `cloudwatch:GetMetricStatistics` (already present)

---

### B4 — Idle / Over-Configured Guardrails

**Why this matters**: Guardrails with all 4 policy types enabled add $0.50-$0.70 per 1,000 invocations. Idle guardrails (configured but zero invocations) waste configuration slots and signal unused resources.

**AWS APIs:**
```python
bedrock.list_guardrails()
bedrock.get_guardrail(guardrailIdentifier=gid, guardrailVersion="DRAFT")
cloudwatch.get_metric_statistics(
    Namespace="AWS/Bedrock",
    MetricName="GuardrailInvocations",
    Dimensions=[{"Name": "GuardrailId", "Value": gid}],
    ...
)
```

**Detection logic (skip in `fast_mode`):**
1. List all guardrails. For each, count enabled policy types: `contentPolicy`, `topicPolicy`, `sensitiveInformationPolicy`, `contextualGroundingPolicy`.
2. Pull 30-day `GuardrailInvocations` from CW. If zero: idle guardrail (flag for deletion).
3. For active guardrails: if all 4 policies enabled AND `GuardrailInvocations` < 10,000/month: over-configured candidate.
4. Compute per-invocation guardrail overhead and monthly cost.

**Savings formula:**
```python
POLICY_COST_PER_1K = 0.075  # post-Dec 2024 pricing (85% reduction from original)
enabled_policies = count_enabled_policy_types(guardrail)
# For over-configured: disabling one unused policy saves:
monthly_savings = (invocations / 1000) * POLICY_COST_PER_1K * pricing_multiplier
# For idle: $0 savings (no active billing) but flag for cleanup
```

**New source block:** `"idle_guardrails": SourceBlock(...)`

**IAM:** `bedrock:ListGuardrails`, `bedrock:GetGuardrail`

---

### B5 — Invocation Logging with Full Content to CloudWatch

**Why this matters**: `includeBodyJson=True` sends every prompt and response to CloudWatch Logs at $0.50/GB ingestion. A production app with 1M calls/month at 2KB average = 2GB/day = $30/month in ingestion alone, with unbounded storage if no retention policy is set. Switching to S3 delivery eliminates the ingestion cost entirely.

**AWS APIs:**
```python
bedrock.get_model_invocation_logging_configuration()
logs.describe_log_groups(logGroupNamePrefix="/aws/bedrock/")
cloudwatch.get_metric_statistics(
    Namespace="AWS/Logs",
    MetricName="IncomingBytes",
    Dimensions=[{"Name": "LogGroupName", "Value": log_group_name}],
    ...
)
```

**Detection logic (no `fast_mode` skip — pure config check):**
1. Get logging config. If `cloudWatchConfig.enabled=True` and `includeBodyJson=True`: flag.
2. Find the Bedrock log group. Check `retentionInDays` — if absent (infinite): flag separately.
3. Pull 30-day `IncomingBytes` from CW. Compute ingestion cost.

**Savings formula:**
```python
CW_INGESTION_PER_GB = 0.50
CW_STORAGE_PER_GB = 0.03
S3_STORAGE_PER_GB = 0.023
monthly_ingestion_cost = daily_bytes_gb * 30 * CW_INGESTION_PER_GB * pricing_multiplier
monthly_storage_cost = stored_bytes_gb * CW_STORAGE_PER_GB * pricing_multiplier
# Switch to S3: saves all ingestion cost + (CW_STORAGE - S3_STORAGE) * stored_bytes
savings = monthly_ingestion_cost + stored_bytes_gb * (CW_STORAGE_PER_GB - S3_STORAGE_PER_GB) * pricing_multiplier
```

**New source block:** `"logging_cost_optimization": SourceBlock(...)`

**IAM:** `bedrock:GetModelInvocationLoggingConfiguration`, `logs:DescribeLogGroups`, `cloudwatch:GetMetricStatistics`

---

## Part 2: SageMaker Enhancements

### S1 — Idle KernelGateway Apps in Studio Domains

**Why this matters**: Every open Jupyter kernel in SageMaker Studio runs on a paid ML instance. A single `ml.m5.xlarge` left open over a weekend costs $129. AWS published an auto-shutdown extension specifically because this is one of the most common ML waste patterns.

**AWS APIs (no CloudWatch needed):**
```python
sagemaker.list_domains()
sagemaker.list_user_profiles(DomainId=domain_id)
sagemaker.list_apps(DomainIdEquals=domain_id, UserProfileNameEquals=profile_name)
sagemaker.describe_app(DomainId=..., UserProfileName=..., AppType="KernelGateway", AppName=...)
```

**Detection logic:**
1. For each domain → each user profile → list apps with `AppType='KernelGateway'` and `Status='InService'`.
2. Check `LastUserActivityTimestamp`. Flag if > 24 hours ago. Strong flag if > 72 hours.
3. Extract `ResourceSpec.InstanceType` from `describe_app()`.
4. Aggregate by user to surface top idle-cost offenders.
5. Also check `AppType='SpaceApp'` (private spaces also bill for instances).

**Savings formula:**
```python
# Instance type → hourly rate via pricing_engine or module constants
STUDIO_INSTANCE_HOURLY = {
    "ml.t3.medium": 0.05, "ml.m5.large": 0.115, "ml.m5.xlarge": 0.230,
    "ml.c5.xlarge": 0.204, "ml.p3.2xlarge": 3.825, "ml.g4dn.xlarge": 0.736,
}
hours_idle = (now - last_activity_ts).total_seconds() / 3600
current_waste = hourly_rate * hours_idle
monthly_projection = hourly_rate * 730 * pricing_multiplier  # if never shut down
```

**New source block:** `"idle_studio_apps": SourceBlock(...)`

Recommendation dict keys: `app_name`, `user_profile`, `domain_id`, `instance_type`, `hours_idle`, `last_activity`, `check_category="Idle Studio Apps"`, `monthly_savings`, `reason`.

**New stat card:** `StatCardSpec(label="Idle Studio Apps", source_path="sources.idle_studio_apps.count", formatter="int")`

**IAM:** `sagemaker:ListDomains`, `sagemaker:ListUserProfiles`, `sagemaker:ListApps`, `sagemaker:DescribeApp`

---

### S2 — Serverless Inference Candidates

**Why this matters**: Serverless Inference bills only for actual compute (per GB-second), with zero idle charge. For endpoints receiving <50,000 invocations/month with spiky patterns (8+ hours/day with zero traffic), Serverless is typically 80-99% cheaper.

**AWS APIs:**
```python
sagemaker.list_endpoints(StatusEquals="InService")
sagemaker.describe_endpoint_config(EndpointConfigName=config_name)
application_autoscaling.describe_scalable_targets(ServiceNamespace="sagemaker")
cloudwatch.get_metric_statistics(
    Namespace="AWS/SageMaker",
    MetricName="Invocations",
    Dimensions=[{"Name": "EndpointName", "Value": ep_name}],
    Period=3600,  # hourly
    Statistics=["Sum"],
)
```

**Detection logic (skip in `fast_mode`):**
1. Filter to endpoints where `ProductionVariants[].ServerlessConfig` is absent (real-time, not already serverless).
2. Exclude GPU instance types (`ml.g4*`, `ml.g5*`, `ml.p3*`, `ml.inf*`, `ml.trn*`) — serverless is CPU only.
3. Pull 30-day hourly invocations. Candidate signal:
   - Total invocations/month < 50,000, AND
   - Hours with zero invocations > 8 per day on average.
4. Compare: current cost vs estimated serverless cost.

**Savings formula:**
```python
# Current cost:
current_monthly = instance_hourly * 730 * pricing_multiplier
# Serverless estimate (GB-second pricing):
SERVERLESS_PRICE_PER_GB_SEC = 0.000_010
avg_duration_sec = 0.2  # assume 200ms average — conservative
model_memory_gb = 2.0   # assume 2GB model — conservative
serverless_monthly = total_invocations * avg_duration_sec * model_memory_gb * SERVERLESS_PRICE_PER_GB_SEC * pricing_multiplier
savings = current_monthly - serverless_monthly
```

**New source block:** `"serverless_candidates": SourceBlock(...)`

**IAM:** `application-autoscaling:DescribeScalableTargets` (new), `sagemaker:DescribeEndpointConfig` (already present)

---

### S3 — Endpoints Without Auto-Scaling Policies

**Why this matters**: Fixed-instance endpoints pay for peak capacity 24/7. Endpoints with variable traffic (peak >3× trough) and no scaling policy are perpetually over-provisioned.

**AWS APIs:**
```python
application_autoscaling.describe_scalable_targets(ServiceNamespace="sagemaker")
# ResourceId format: "endpoint/{endpoint-name}/variant/{variant-name}"
```

**Detection logic:**
1. Build a set of endpoint+variant resource IDs from `describe_scalable_targets()`.
2. For each `InService` endpoint, check if any variant has a registered scaling target. If not: no auto-scaling.
3. Pull 7-day hourly invocations. If `peak_hour / max(trough_hour, 1) > 3.0`: variable traffic pattern → scaling benefit.
4. Flag endpoints with `DesiredInstanceCount > 1` and no scaling (most impactful).

**Savings formula:**
```python
AUTOSCALING_SAVINGS_RATE = 0.20  # conservative: 20% reduction from right-sizing
savings = instance_hourly * desired_count * 730 * AUTOSCALING_SAVINGS_RATE * pricing_multiplier
```

**New source block:** `"endpoints_without_autoscaling": SourceBlock(...)`

**IAM:** `application-autoscaling:DescribeScalableTargets`

---

### S4 — Graviton Instance Migration Opportunities

**Why this matters**: `ml.c7g` (Graviton3) costs 15-50% less than equivalent `ml.c5`/`ml.c6i` instances and delivers equal or better latency for PyTorch, TensorFlow, XGBoost, and scikit-learn inference. Endpoints deployed before 2023 have never been migrated.

**AWS APIs (no CloudWatch needed):**
```python
sagemaker.list_endpoints(StatusEquals="InService")
sagemaker.describe_endpoint_config(EndpointConfigName=config_name)
# Extract ProductionVariants[].InstanceType
```

**Detection logic:**
1. For each InService endpoint, extract instance type.
2. Filter for migratable x86 CPU families: `ml.c5.*`, `ml.c6i.*`, `ml.m5.*`, `ml.m6i.*`, `ml.r5.*`.
3. Exclude GPU (`ml.g*`, `ml.p*`), inferentia (`ml.inf*`), trainium (`ml.trn*`).
4. Look up Graviton equivalent and price difference.

**Instance migration map:**
```python
GRAVITON_MAP = {
    "ml.c5.large": ("ml.c7g.large", 0.095, 0.052),      # (current_hr, graviton_hr)
    "ml.c5.xlarge": ("ml.c7g.xlarge", 0.204, 0.104),
    "ml.c5.2xlarge": ("ml.c7g.2xlarge", 0.408, 0.208),
    "ml.c6i.large": ("ml.c7g.large", 0.104, 0.052),
    "ml.c6i.xlarge": ("ml.c7g.xlarge", 0.208, 0.104),
    "ml.m5.large": ("ml.m7g.large", 0.115, 0.092),
    "ml.m5.xlarge": ("ml.m7g.xlarge", 0.230, 0.184),
    "ml.m6i.xlarge": ("ml.m7g.xlarge", 0.235, 0.184),
}
savings_per_hr = current_hr - graviton_hr
monthly_savings = savings_per_hr * desired_count * 730 * pricing_multiplier
```

**New source block:** `"graviton_migration_candidates": SourceBlock(...)`

Recommendation note: include caveat that BYOC containers must be rebuilt for ARM64.

**IAM:** No new permissions needed.

---

### S5 — Canvas Sessions Without Idle Shutdown Config

**Why this matters**: Canvas charges $1.90/session-hour continuously. An analyst leaving Canvas open for 12 idle hours/day across 20 working days = $456/month per user. Domains without lifecycle configurations have no automatic protection.

**AWS APIs (no CloudWatch needed):**
```python
sagemaker.list_domains()
sagemaker.describe_domain(DomainId=domain_id)
sagemaker.list_user_profiles(DomainId=domain_id)
sagemaker.list_apps(DomainIdEquals=domain_id, AppType="Canvas")
sagemaker.describe_app(DomainId=..., UserProfileName=..., AppType="Canvas", AppName=...)
```

**Detection logic:**
1. For each domain with Canvas apps: check `describe_domain()` for `DefaultUserSettings.CanvasAppSettings` — look for `IdentityProviderOAuthSettings` and lifecycle configuration ARN.
2. If no lifecycle configuration ARN on the domain or user profile: flag domain as lacking idle shutdown.
3. For each `InService` Canvas app: check `LastUserActivityTimestamp`. Flag if > 4 hours ago.
4. Compute waste from idle hours.

**Savings formula:**
```python
CANVAS_HOURLY = 1.90
hours_idle = (now - last_activity_ts).total_seconds() / 3600
current_waste = hours_idle * CANVAS_HOURLY
monthly_projection = CANVAS_HOURLY * 8 * 22 * pricing_multiplier  # 8 idle hrs/day × 22 working days
```

**New source block:** `"idle_canvas_sessions": SourceBlock(...)`

**IAM:** `sagemaker:ListApps`, `sagemaker:DescribeApp`, `sagemaker:DescribeDomain` (new)

---

### S6 — Feature Store Idle Online Groups

**Why this matters**: Feature Store online store bills $0.45/GB/month for storage plus provisioned read/write capacity units even at zero usage. Prototype feature groups are almost never audited.

**AWS APIs:**
```python
sagemaker.list_feature_groups(FeatureGroupStatusEquals="Created")
sagemaker.describe_feature_group(FeatureGroupName=name)
cloudwatch.get_metric_statistics(
    Namespace="AWS/SageMaker/FeatureStore",
    MetricName="GetRecordRequestSuccess",
    Dimensions=[{"Name": "FeatureGroupName", "Value": name}],
    ...
)
```

**Detection logic (skip in `fast_mode`):**
1. Filter for feature groups with `OnlineStoreConfig.EnableOnlineStore=True`.
2. Check `GetRecordRequestSuccess` + `PutRecordRequestSuccess` from CW over 30 days. If both zero: idle.
3. For `PROVISIONED` throughput mode: flag if `ProvisionedReadCapacityUnits` > 5 and zero read traffic.

**Savings formula:**
```python
ONLINE_STORE_PRICE_PER_GB = 0.45
PROVISIONED_RCU_HOURLY = 0.000065
PROVISIONED_WCU_HOURLY = 0.000325
record_count = feature_group.get("OnlineStoreConfig", {}).get("RecordCount", 0)
# Estimate bytes: record_count * avg_record_bytes (default: 1KB = 0.001 GB)
storage_gb = record_count * 0.001
monthly_storage = storage_gb * ONLINE_STORE_PRICE_PER_GB * pricing_multiplier
monthly_capacity = (prov_rcu * PROVISIONED_RCU_HOURLY + prov_wcu * PROVISIONED_WCU_HOURLY) * 730 * pricing_multiplier
savings = monthly_storage + monthly_capacity
```

**New source block:** `"idle_feature_store_groups": SourceBlock(...)`

**IAM:** `sagemaker:ListFeatureGroups`, `sagemaker:DescribeFeatureGroup`

---

## Part 3: New Adapters

---

### Adapter: `kendra.py`

**File**: `services/adapters/kendra.py`

#### Checks: K1 — Idle Kendra Indexes, K2 — Edition Downgrade

**Why this matters**: A Kendra Enterprise Edition index costs $1,008/month from creation until deletion, regardless of query volume. A single forgotten index costs $12,096/year. These are often left running after Bedrock Knowledge Base migrations.

**Required clients:**
```python
required_clients() -> ("kendra",)
```

**AWS APIs:**
```python
kendra.list_indices()
kendra.describe_index(Id=index_id)
kendra.list_data_sources(IndexId=index_id)
cloudwatch.get_metric_statistics(
    Namespace="AWS/Kendra",
    MetricName="IndexQueryCount",
    Dimensions=[{"Name": "IndexId", "Value": index_id}],
    Period=86400 * 30,
    Statistics=["Sum"],
    StartTime=now - timedelta(days=30),
    EndTime=now,
)
```

**Detection logic:**
1. List all indexes. For each: get `Edition` (`ENTERPRISE` or `DEVELOPER`), `CreatedAt`, `IndexStatistics`.
2. Pull 30-day `IndexQueryCount` sum. If zero: idle index.
3. Edition downgrade check (K2): if `ENTERPRISE` AND query avg/day < 500 AND `IndexedTextDocumentsCount` < 10,000: downgrade candidate.
4. Flag indexes with `Status` = `ACTIVE` and created >90 days ago with zero queries as high-priority.

**Savings formula:**
```python
KENDRA_ENTERPRISE_MONTHLY = 1008.0
KENDRA_DEVELOPER_MONTHLY = 810.0
# K1 — idle:
monthly_savings = (KENDRA_ENTERPRISE_MONTHLY if edition == "ENTERPRISE" else KENDRA_DEVELOPER_MONTHLY) * pricing_multiplier
# K2 — downgrade:
monthly_savings = (KENDRA_ENTERPRISE_MONTHLY - KENDRA_DEVELOPER_MONTHLY) * pricing_multiplier  # = $198
```

**`ServiceFindings` shape:**
```python
ServiceFindings(
    service_name="Kendra",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "idle_indexes": SourceBlock(count=..., recommendations=...),
        "edition_downgrade": SourceBlock(count=..., recommendations=...),
    },
    extras={"index_count": int, "enterprise_count": int, "developer_count": int},
    optimization_descriptions={
        "idle_indexes": {
            "title": "Idle Kendra Indexes",
            "description": "Indexes with zero query activity in 30 days — billing continuously",
        },
        "edition_downgrade": {
            "title": "Enterprise → Developer Downgrade",
            "description": "Enterprise indexes with low query volume and document count under 10K",
        },
    },
)
```

**Stat cards:**
```python
StatCardSpec(label="Kendra Indexes", source_path="extras.index_count", formatter="int"),
StatCardSpec(label="Idle Indexes", source_path="sources.idle_indexes.count", formatter="int"),
StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
```

**IAM permissions:**
```json
"kendra:ListIndices", "kendra:DescribeIndex", "kendra:ListDataSources",
"cloudwatch:GetMetricStatistics"
```

---

### Adapter: `rekognition.py`

**File**: `services/adapters/rekognition.py`

#### Check: R1 — Idle Rekognition Custom Labels Models Running

**Why this matters**: Rekognition Custom Labels charges $4.00/inference-unit/hour for running models — a running model costs $2,880/month per inference unit at minimum. Unlike endpoints, there is no auto-scaling. Teams run models for batch jobs and forget to stop them.

**Required clients:**
```python
required_clients() -> ("rekognition",)
```

**AWS APIs:**
```python
rekognition.describe_projects()
rekognition.describe_project_versions(
    ProjectArn=project_arn,
    VersionNames=[],  # all versions
)
# Filter for Status == "RUNNING"
cloudwatch.get_metric_statistics(
    Namespace="AWS/Rekognition",
    MetricName="SuccessfulRequestCount",
    Dimensions=[{"Name": "ProjectVersionArn", "Value": version_arn}],
    Period=86400 * 7,
    Statistics=["Sum"],
    StartTime=now - timedelta(days=7),
    EndTime=now,
)
```

**Detection logic:**
1. List all projects and their versions. Filter for `Status='RUNNING'`.
2. For each running version: pull 7-day `SuccessfulRequestCount` from CW. If zero: idle.
3. Get `MinInferenceUnits` from `describe_project_versions()` response.
4. Compute hours running: `(now - CreationTimestamp).total_seconds() / 3600`.

**Savings formula:**
```python
REKOGNITION_CUSTOM_LABELS_HOURLY_PER_IU = 4.00
monthly_savings = min_inference_units * REKOGNITION_CUSTOM_LABELS_HOURLY_PER_IU * 730 * pricing_multiplier
# 1 IU idle = $2,920/month
```

**`ServiceFindings` shape:**
```python
ServiceFindings(
    service_name="Rekognition",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "idle_custom_labels_models": SourceBlock(count=..., recommendations=...),
    },
    extras={"running_model_count": int, "total_inference_units": int},
    optimization_descriptions={
        "idle_custom_labels_models": {
            "title": "Idle Rekognition Custom Labels Models",
            "description": "Custom Labels models in RUNNING state with zero requests in 7 days",
        },
    },
)
```

**Stat cards:**
```python
StatCardSpec(label="Running Models", source_path="extras.running_model_count", formatter="int"),
StatCardSpec(label="Idle Models", source_path="sources.idle_custom_labels_models.count", formatter="int"),
StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
```

**IAM permissions:**
```json
"rekognition:DescribeProjects", "rekognition:DescribeProjectVersions",
"cloudwatch:GetMetricStatistics"
```

---

### Adapter: `comprehend.py`

**File**: `services/adapters/comprehend.py`

#### Check: C1 — Idle Comprehend Custom Endpoints

**Why this matters**: Comprehend custom endpoints bill $0.0005/second/inference-unit (~$1.80/IU-hour) continuously once started, whether processing documents or not. A 1-IU endpoint costs $1,296/month. Teams use them for one-time batch jobs and forget to delete them.

**Required clients:**
```python
required_clients() -> ("comprehend",)
```

**AWS APIs:**
```python
comprehend.list_endpoints()  # returns Status, CurrentInferenceUnits, CreationTime, ModelArn
comprehend.describe_endpoint(EndpointArn=arn)
cloudwatch.get_metric_statistics(
    Namespace="AWS/Comprehend",
    MetricName="ClassifyDocumentRequestCount",
    Dimensions=[{"Name": "EndpointArn", "Value": arn}],
    Period=86400 * 7,
    Statistics=["Sum"],
    StartTime=now - timedelta(days=7),
    EndTime=now,
)
```

**Detection logic:**
1. List all endpoints with `Status='IN_SERVICE'`.
2. Pull 7-day request count from CW. If zero: idle.
3. Compute historical spend: `(now - creation_time).total_seconds() * inference_units * 0.0005`.
4. Compute monthly forward cost.

**Savings formula:**
```python
COMPREHEND_ENDPOINT_HOURLY_PER_IU = 1.80  # $0.0005/sec × 3600
monthly_savings = current_inference_units * COMPREHEND_ENDPOINT_HOURLY_PER_IU * 730 * pricing_multiplier
# 1 IU idle = $1,314/month
```

**`ServiceFindings` shape:**
```python
ServiceFindings(
    service_name="Comprehend",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "idle_custom_endpoints": SourceBlock(count=..., recommendations=...),
    },
    extras={"endpoint_count": int, "idle_count": int},
    optimization_descriptions={
        "idle_custom_endpoints": {
            "title": "Idle Comprehend Custom Endpoints",
            "description": "IN_SERVICE endpoints with zero requests in 7 days — billing continuously",
        },
    },
)
```

**Stat cards:**
```python
StatCardSpec(label="Active Endpoints", source_path="extras.endpoint_count", formatter="int"),
StatCardSpec(label="Idle Endpoints", source_path="sources.idle_custom_endpoints.count", formatter="int"),
StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
```

**IAM permissions:**
```json
"comprehend:ListEndpoints", "comprehend:DescribeEndpoint",
"cloudwatch:GetMetricStatistics"
```

---

### Adapter: `q_business.py`

**File**: `services/adapters/q_business.py`

#### Check: Q1 — Inactive Q Business Subscriptions

**Why this matters**: Q Business Pro charges $20/user/month. Subscriptions persist after employees leave or stop using the tool. No built-in idle detection exists. 50 inactive Pro users = $1,000/month waste.

**Required clients:**
```python
required_clients() -> ("qbusiness",)
```

**AWS APIs:**
```python
qbusiness.list_applications()
qbusiness.list_subscriptions(applicationId=app_id)
qbusiness.list_conversations(applicationId=app_id, userId=user_id, maxResults=1)
# Proxy for activity: if no conversations exist, user is inactive
```

**Detection logic:**
1. List all Q Business applications.
2. For each: list all subscriptions (`subscriptionType`: `Q_LITE` or `Q_BUSINESS`).
3. For each subscription, call `list_conversations(applicationId, userId, maxResults=1)`. If empty: no recorded activity — inactive.
4. Note: `list_conversations` may return empty for new users or users in groups. Group subscriptions cannot be per-user inspected — flag as group-level review required.

**Savings formula:**
```python
Q_PRO_MONTHLY_PER_USER = 20.00
Q_LITE_MONTHLY_PER_USER = 3.00
per_user_savings = Q_PRO_MONTHLY_PER_USER if sub_type == "Q_BUSINESS" else Q_LITE_MONTHLY_PER_USER
monthly_savings = per_user_savings * pricing_multiplier
```

**`ServiceFindings` shape:**
```python
ServiceFindings(
    service_name="Amazon Q Business",
    total_recommendations=total,
    total_monthly_savings=total_savings,
    sources={
        "inactive_subscriptions": SourceBlock(
            count=len(inactive),
            recommendations=tuple(inactive),
            extras={"inactive_pro_count": int, "inactive_lite_count": int},
        ),
    },
    extras={"application_count": int, "total_subscriptions": int},
    optimization_descriptions={
        "inactive_subscriptions": {
            "title": "Inactive Q Business Subscriptions",
            "description": "Subscribed users with no recorded conversation activity",
        },
    },
)
```

**Stat cards:**
```python
StatCardSpec(label="Applications", source_path="extras.application_count", formatter="int"),
StatCardSpec(label="Inactive Subscriptions", source_path="sources.inactive_subscriptions.count", formatter="int"),
StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
```

**IAM permissions:**
```json
"qbusiness:ListApplications", "qbusiness:ListSubscriptions",
"qbusiness:ListConversations"
```

---

## Part 4: services/__init__.py Registrations

```python
# AI/ML — new adapters (append to ALL_MODULES)
from services.adapters.kendra import KendraModule
from services.adapters.rekognition import RekognitionModule
from services.adapters.comprehend import ComprehendModule
from services.adapters.q_business import QBusinessModule
```

---

## Part 5: PricingEngine Extensions Required

| Method | Notes |
|--------|-------|
| None required | All AI/ML pricing uses module-level constants (AWS Pricing API does not cover Bedrock, Kendra, Rekognition, Comprehend, or Q Business custom pricing) |

All adapter pricing constants must be documented in each adapter file with the AWS pricing page URL as a comment source.

---

## Part 6: ScanOrchestrator / ScanContext Changes

None required. No pre-fetch patterns needed — all new adapters operate independently against their respective service APIs.

---

## Part 7: Implementation Order

| Sprint | Items | New Adapters | Estimated Days |
|--------|-------|-------------|----------------|
| A1 | S1 (Studio apps) + S4 (Graviton) + S5 (Canvas) | 0 (SageMaker enhancements) | 2–3 |
| A2 | K1+K2 (Kendra) + R1 (Rekognition) + C1 (Comprehend) | 3 new adapters | 3–4 |
| A3 | B1 (Orphaned OSS) + B4 (Guardrails) + B5 (Logging cost) | 0 (Bedrock enhancements) | 2–3 |
| A4 | S2 (Serverless candidates) + S3 (Auto-scaling) | 0 (SageMaker enhancements) | 3–4 |
| A5 | S6 (Feature Store) + Q1 (Q Business) | 1 new adapter | 3–4 |
| A6 | B2 (Batch inference) + B3 (Prompt caching) | 0 (Bedrock enhancements) | 4–5 |

**Rationale for order**: Sprint A1 is pure API checks (no CloudWatch, no new clients) — easiest to validate. Sprints A2-A3 cover the highest dollar-impact items (Kendra $1,008/mo, Rekognition $2,880/mo). Sprints A4-A6 cover the more complex CloudWatch-dependent checks.

---

## Part 8: Validation Criteria (Per Item)

Each item is NOT done until:
1. Adapter file exists (or existing adapter updated)
2. Registered in `ALL_MODULES` (new adapters only)
3. `pytest tests/test_regression_snapshot.py tests/test_reporter_snapshots.py` passes — 125 tests
4. IAM permissions listed above added to `README.md` or `AGENTS.md`
5. `fast_mode` respected on all CloudWatch-dependent checks
6. Returns empty `ServiceFindings` (not exception) when service is not available or not used in the account
