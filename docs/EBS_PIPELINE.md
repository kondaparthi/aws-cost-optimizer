# EBS Pipeline

Detailed end-to-end flow for EBS findings and actions in AWS Cost Optimizer.

## End-to-End EBS Flow (Current Implementation)

1. EventBridge triggers analysis Lambda on schedule.
2. Analysis Lambda runs EBS analyzer for configured accounts/regions.
3. EBS analyzer generates findings for unattached volumes and old snapshots.
4. Findings are written to S3 as findings-latest.json.
5. Dashboard loads findings and shows EBS items in the Recommendations list.
6. User marks actions (keep/remove/notify) and saves via /auth/actions.
7. Auth Lambda persists decisions payload to decisions S3.
8. Scheduler reads decisions payload but currently executes EC2-only actions.
9. EBS actions are tracked as decisions, not automatically executed.

## Scope

- EBS finding generation and safety checks
- findings publication and dashboard rendering
- user action handling and persistence
- scheduler behavior for EBS actions
- current limitations and next steps

---

## 1) Analysis Trigger and Runtime

EBS analysis runs as part of the main analysis Lambda.

- Infra and schedules:
  - [cloudformation/cost-optimizer-main.yaml](../cloudformation/cost-optimizer-main.yaml)
- Analysis handler:
  - [src/aws_cost_optimizer/lambda/analysis_handler.py](../src/aws_cost_optimizer/lambda/analysis_handler.py)

Runtime sequence:

1. Lambda loads config from S3.
2. Iterates accounts and regions.
3. Runs analyzers including EBS analyzer.
4. Aggregates findings into report.
5. Uploads findings-latest.json.

---

## 2) EBS Finding Generation Logic

EBS analyzer implementation:

- [src/aws_cost_optimizer/analyzers/ebs_analyzer.py](../src/aws_cost_optimizer/analyzers/ebs_analyzer.py)

### 2.1 Unattached Volume Findings

The analyzer scans available EBS volumes and applies explicit orphan checks.

Key behaviors:

- requires truly unattached volume state
- applies skip policy protection tags
- computes monthly and annual savings estimates

### 2.2 Old Snapshot Findings

The analyzer scans snapshots older than configured threshold and applies safety gates before recommending deletion.

Safety checks include:

- incremental chain/dependency checks
- AWS Backup-managed snapshot detection
- DLM lifecycle-managed snapshot detection
- parent resource tag protection inheritance (volume/instance)

Only snapshots passing safety checks are flagged.

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

## 4) Dashboard EBS Flow

Dashboard implementation:

- [dashboard/index.html](../dashboard/index.html)
- Demo parity:
  - [dashboard/demo/demo.html](../dashboard/demo/demo.html)

For EBS findings:

1. User filters Type = EBS Volume or EBS Snapshot.
2. Sees issue, costs, annual savings, and recommendation.
3. Can mark keep/remove/notify.
4. Remove on risky resources shows custom warning modal.

Current note:

- EBS does not have a dedicated custom tab like EC2.

---

## 5) Action Persistence API

Auth/API handler:

- [src/aws_cost_optimizer/lambda/auth_handler.py](../src/aws_cost_optimizer/lambda/auth_handler.py)

Used routes:

- POST /auth/actions
- GET /auth/actions
- POST /auth/notify

Saved items include EBS decisions when user marks EBS findings.

Storage:

- decisions S3 bucket/key configured in CloudFormation env vars

---

## 6) Scheduler Behavior for EBS

Scheduler implementation:

- [src/aws_cost_optimizer/lambda/scheduler_handler.py](../src/aws_cost_optimizer/lambda/scheduler_handler.py)

Current behavior:

- scheduler iterates saved items but only processes type == EC2 Instance for execution path
- EBS remove/notify decisions are not auto-executed by scheduler

Implication:

- EBS actions are currently governance/approval records unless implemented elsewhere

---

## 7) Safety and Governance Controls

- skip policy support to avoid protected resources
- snapshot safety checks before delete recommendation
- dashboard warning modal for risky remove operations
- user approval persistence before any automation path

---

## 8) Current Limitation

EBS delete/cleanup actions are recommendation and decision tracking only.

Not implemented yet:

- automated EBS volume delete executor
- automated snapshot delete executor with rollback/workflow orchestration

---

## 9) Suggested Next Step

To make EBS fully actionable, add an EBS execution Lambda/Step Function that:

1. consumes approved EBS actions
2. revalidates live dependency and protection state
3. executes delete actions safely
4. captures audit and rollback metadata
5. reports results back to dashboard status/history
