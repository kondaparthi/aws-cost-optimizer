# EC2 Pipeline

Detailed end-to-end flow for EC2 recommendations and actions in AWS Cost Optimizer.

## End-to-End EC2 Flow (Current Implementation)

1. EventBridge triggers analysis Lambda on schedule.
2. Analysis Lambda runs EC2 analyzer for configured accounts/regions.
3. EC2 analyzer creates findings for idle, schedule opportunity, and rightsize.
4. Findings are written to S3 as findings-latest.json.
5. Dashboard loads findings, renders EC2 Recommendations and Custom tabs (when EC2 type is selected), and captures user actions.
6. Dashboard saves schedule_config plus action items to /auth/actions.
7. Auth Lambda persists actions payload to the decisions S3 object.
8. EventBridge triggers scheduler Lambda, which reads decisions payload.
9. Scheduler executes schedule and manual target start/stop actions, while resize remains recommendation-only (tracked in report, not executed).

## Scope

- EC2 finding generation (idle, schedule, rightsize)
- findings publication and dashboard rendering
- user action handling in the dashboard
- action persistence through auth API
- scheduler execution behavior for EC2 actions
- current limitations and safety checks

---

## 1) Analysis Trigger and Runtime

EC2 analysis runs inside the analysis Lambda, invoked by EventBridge on the configured analysis schedule.

- CloudFormation schedule and Lambda wiring:
  - [cloudformation/cost-optimizer-main.yaml](../cloudformation/cost-optimizer-main.yaml)
- Analysis Lambda handler:
  - [src/aws_cost_optimizer/lambda/analysis_handler.py](../src/aws_cost_optimizer/lambda/analysis_handler.py)

High-level runtime sequence:

1. Lambda loads config from S3.
2. For each account and region, analyzers run.
3. EC2 analyzer emits findings.
4. Findings report is uploaded to findings-latest.json in S3.

---

## 2) EC2 Finding Generation Logic

EC2 analysis implementation:

- [src/aws_cost_optimizer/analyzers/ec2_analyzer.py](../src/aws_cost_optimizer/analyzers/ec2_analyzer.py)

### 2.1 Idle Detection

For running instances, the analyzer pulls CloudWatch CPU metrics and checks:

- metric completeness (coverage/confidence)
- average CPU threshold
- p95 CPU spike ceiling

If both utilization checks indicate idle behavior, it creates a high/medium severity finding with stop/terminate recommendation and estimated savings.

### 2.2 Off-Hours Schedule Recommendation

The analyzer computes off-hours/weekend usage and recommends schedule when usage is consistently low.

Output includes schedule metadata in finding details, for example:

- timezone
- business_start
- business_end
- off_days

### 2.3 Rightsize Recommendation

For underutilized instances, analyzer recommends a smaller instance type and calculates savings delta.

It also adds migration context:

- managed_by (manual/cloudformation/terraform)
- stack_name
- migration_instructions

---

## 3) Findings Data Model and Publication

Primary report model:

- [src/aws_cost_optimizer/models.py](../src/aws_cost_optimizer/models.py)

Analyzer-level finding model:

- [src/aws_cost_optimizer/analyzers/base_analyzer.py](../src/aws_cost_optimizer/analyzers/base_analyzer.py)

Analysis handler adds analyzer findings into the aggregate report and writes:

- findings-latest.json (dashboard reads this)
- timestamped history file

Publication code path:

- [src/aws_cost_optimizer/lambda/analysis_handler.py](../src/aws_cost_optimizer/lambda/analysis_handler.py)

---

## 4) Dashboard EC2 Flow

Dashboard implementation:

- [dashboard/index.html](../dashboard/index.html)
- Demo parity:
  - [dashboard/demo/demo.html](../dashboard/demo/demo.html)

### 4.1 Load + Render

1. Dashboard validates auth session.
2. Loads findings JSON from S3.
3. Type filter is populated dynamically from findings.
4. When EC2 type is selected, EC2 tabs are shown:
   - Recommendations tab
   - Custom tab

### 4.2 Recommendations Tab

Shows EC2 findings and supports actions:

- keep
- remove
- notify
- schedule
- resize

Remove action uses a custom warning modal for non-orphaned resources.

### 4.3 Custom Tab

Supports manual schedule targets:

- instance_id
- region
- account_id (optional)

Custom target savings estimate:

- attempts to match manual instance_id to existing EC2 findings
- displays estimated monthly/annual savings per target when match exists
- shows total estimated savings for all custom targets

Important: this estimate is based on existing findings data, not live EC2 pricing lookup for unknown instances.

---

## 5) Action Persistence API

Auth/API handler:

- [src/aws_cost_optimizer/lambda/auth_handler.py](../src/aws_cost_optimizer/lambda/auth_handler.py)

Routes used:

- POST /auth/actions: save schedule config + action items
- GET /auth/actions: load saved schedule config + action items
- POST /auth/notify: notify flow

Persisted payload contains:

- schedule_config
  - timezone
  - business_start
  - business_end
  - off_days
  - enabled
  - manual_targets
- items[]
  - id
  - type
  - issue
  - region
  - account_id
  - user_action
  - details
  - tags

Storage location:

- S3 decisions bucket/key (configured via environment variables in CloudFormation)

---

## 6) Scheduler Execution Path

Scheduler handler:

- [src/aws_cost_optimizer/lambda/scheduler_handler.py](../src/aws_cost_optimizer/lambda/scheduler_handler.py)

Triggered by EventBridge scheduler schedule from CloudFormation.

### 6.1 Input and Schedule Context

Scheduler reads saved actions payload from decisions S3 and computes whether current time is in business window.

### 6.2 Behavior by EC2 User Action

- schedule:
  - Executes real start/stop for matching EC2 instances based on business window and off_days.
- manual_targets:
  - Executes start/stop for manually entered targets by region.
  - Rejects cross-account manual targets when scheduler account does not match target account.
- resize:
  - Does not execute instance-type change.
  - Only records recommendation entry in ui_resize_recommendations for reporting.

### 6.3 Output/Notification

Scheduler returns counts and publishes SNS report including:

- started/stopped
- UI scheduled starts/stops
- ui_resize_recommendations count/details
- errors

---

## 7) Current Safety and Governance Controls

- remove action warning for risky resources in dashboard
- automation-aware analyzer guidance (stack/terraform context)
- scheduler state re-verification before actions
- dry-run support in scheduler for simulation mode
- IAM-scoped API + S3 decisions persistence

---

## 8) Current Limitation: Resize is Advisory Only

Resize is currently a recommendation workflow, not automatic EC2 mutation.

What exists:

- analyzer computes recommended_instance_type and savings
- dashboard can mark resize action
- scheduler tracks resize recommendations in output

What does not exist yet:

- stop instance -> modify instance type -> start instance automation
- CloudFormation/Terraform change-set apply path
- rollback orchestration for failed resize execution

---

## 9) Suggested Next Step for Full Resize Automation

If full execution is required, implement a guarded resize executor with:

1. explicit approval state
2. drift-safe behavior for automation-managed resources
3. backup + maintenance window checks
4. stop/modify/start workflow with validation
5. rollback/failure handling and audit trail
