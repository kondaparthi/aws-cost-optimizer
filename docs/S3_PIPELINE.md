# S3 Pipeline

Detailed end-to-end flow for S3 findings and actions in AWS Cost Optimizer.

## End-to-End S3 Flow (Current Implementation)

1. EventBridge triggers analysis Lambda on schedule.
2. Analysis Lambda runs S3 analyzer for configured accounts/regions.
3. S3 analyzer generates four S3 recommendation types:
  - lifecycle transition savings
  - Intelligent-Tiering enablement
  - abort incomplete multipart uploads
  - safe-delete candidates for buckets inactive for more than 3 years
4. Analysis handler converts analyzer findings into the dashboard report schema and writes findings-latest.json to S3.
5. Dashboard shows `S3 Bucket` with `Recommendations` and `Custom` tabs.
6. From Recommendations, user can mark `Notify`, `Set Lifecycle Policy` / `Enable Intelligent Tiering`, and `Remove` only for safe-delete candidates.
7. From Custom, user can add manual S3 lifecycle/tiering targets that are persisted alongside EC2/EBS manual targets.
8. Auth Lambda saves `items` plus `schedule_config.s3_manual_targets` into decisions S3.
9. Scheduler reads the saved payload and executes supported S3 workflows:
  - apply lifecycle transition rules
  - enable Intelligent-Tiering
  - apply abort-incomplete-multipart lifecycle rules
  - delete empty safe-delete candidate buckets

## Scope

- S3 finding generation logic
- findings publication and dashboard rendering
- user decision handling and persistence
- scheduler behavior for S3 actions
- current limitations and next steps

---

## 1) Analysis Trigger and Runtime

S3 analysis runs as part of the main analysis Lambda.

- Infra and schedules:
  - [cloudformation/cost-optimizer-main.yaml](../cloudformation/cost-optimizer-main.yaml)
- Analysis handler:
  - [src/aws_cost_optimizer/lambda/analysis_handler.py](../src/aws_cost_optimizer/lambda/analysis_handler.py)

Runtime sequence:

1. Lambda loads config from S3.
2. Iterates accounts and regions.
3. Runs analyzers including S3 analyzer.
4. Aggregates findings into report.
5. Uploads findings-latest.json.

---

## 2) S3 Finding Generation Logic

S3 analyzer implementation:

- [src/aws_cost_optimizer/analyzers/s3_analyzer.py](../src/aws_cost_optimizer/analyzers/s3_analyzer.py)

### 2.1 Implemented Finding Types

Current implemented detection:

- lifecycle transition opportunities for larger buckets without transition rules
- Intelligent-Tiering opportunities for larger buckets without tiering config
- incomplete multipart uploads older than threshold when no abort rule exists
- buckets with no activity for more than 3 years as safe-delete candidates

Key behaviors:

- scans buckets in target region
- applies skip policy using bucket tags
- samples objects to estimate size and recent activity
- inspects lifecycle and Intelligent-Tiering configuration
- emits workflow-specific finding details (`s3_workflow`, `allowed_actions`, `recommended_action`) for the UI and scheduler

### 2.2 Config Inputs

Analyzer reads config values such as:

- multipart_age_days
- check_incomplete_multipart
- recommend_lifecycle_policies
- recommend_intelligent_tiering
- recommend_unused_bucket_delete
- lifecycle_min_size_gb
- intelligent_tiering_min_size_gb
- unused_bucket_days
- object_scan_limit

---

## 3) Findings Model and Publication

Model paths:

- aggregate report model:
  - [src/aws_cost_optimizer/models.py](../src/aws_cost_optimizer/models.py)
- analyzer finding model:
  - [src/aws_cost_optimizer/analyzers/base_analyzer.py](../src/aws_cost_optimizer/analyzers/base_analyzer.py)

Publication path:

- [src/aws_cost_optimizer/lambda/analysis_handler.py](../src/aws_cost_optimizer/lambda/analysis_handler.py)

Output consumed by dashboard:

- findings-latest.json in reports bucket

---

## 4) Dashboard S3 Flow

Dashboard implementation:

- [dashboard/index.html](../dashboard/index.html)
- Demo parity:
  - [dashboard/demo/demo.html](../dashboard/demo/demo.html)

For S3 findings:

1. User filters Type = S3 Bucket.
2. Recommendations tab lists analyzer-generated S3 findings.
3. S3 findings expose workflow-specific buttons:
  - `Notify`
  - `Set Lifecycle Policy` for lifecycle transitions and multipart cleanup
  - `Enable Intelligent Tiering` for tiering findings
  - `Remove` only for safe-delete candidates
4. Custom tab allows manual S3 lifecycle/tiering targets and estimates savings by matching existing S3 findings.

---

## 5) Action Persistence API

Auth/API handler:

- [src/aws_cost_optimizer/lambda/auth_handler.py](../src/aws_cost_optimizer/lambda/auth_handler.py)

Used routes:

- POST /auth/actions
- GET /auth/actions
- POST /auth/notify

Saved items include S3 decisions when user marks S3 findings.

Saved schedule config now also includes:

- `s3_manual_targets`

Storage:

- decisions S3 bucket/key configured in CloudFormation env vars

---

## 6) Scheduler Behavior for S3

Scheduler implementation:

- [src/aws_cost_optimizer/lambda/scheduler_handler.py](../src/aws_cost_optimizer/lambda/scheduler_handler.py)

Current behavior:

- `user_action = lifecycle` applies the workflow described in `details.s3_workflow`
- `user_action = remove` deletes the bucket only when the finding is marked as a safe-delete candidate and the bucket is empty at execution time
- custom S3 entries from `schedule_config.s3_manual_targets` apply the selected lifecycle or tiering workflow

---

## 7) Safety and Governance Controls

- skip policy protection support
- explicit user approval before actions are persisted
- dashboard warning modal for risky remove operations
- safe-delete gate for S3 remove actions
- scheduler emptiness check before bucket deletion
- auditability through persisted actions payload

---

## 8) Current Limitation

Current S3 execution is intentionally conservative.

Not implemented yet:

- object-level deletion for non-empty stale buckets
- merge-aware policy authoring beyond the fixed cost-optimizer managed rules
- post-execution status history surfaced directly in the dashboard

---

## 9) Suggested Next Step

The next improvement area is richer bucket safety validation, especially before deletes.

1. Add optional retention/compliance allowlists before safe-delete execution.
2. Surface scheduler execution history for S3 actions in the dashboard.
3. Expand size/activity estimation with S3 Inventory or CloudWatch storage metrics for large buckets.
