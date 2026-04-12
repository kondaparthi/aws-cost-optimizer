

## How It Works
```
┌─────────────────────────────────────────┐
│  Config (YAML) + Skip Policies          │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│  Multi-Account Orchestrator             │
│  (STS assume-role per account)          │
└──────────────────┬──────────────────────┘
                   │
            ┌─────────┼─────────┬──────────┐
            │         │         │
          ┌────▼──┐ ┌──▼────┐ ┌──▼────┐
          │  EBS  │ │  EC2  │ │  S3   │
          │Analyzer│ │Analyzer│ │Analyzer│
          └────┬──┘ └──┬────┘ └──┬────┘
            │         │         │
            └─────────┼─────────┘
                   │
         ┌─────────▼──────────┐
         │   Report Generator  │
         │  JSON/CSV/HTML      │
         └─────────┬───────────┘
                   │
           ┌───────▼────────┐
           │  Output (S3)    │
           │ or local /dir   │
           └─────────────────┘
```

### Step 1: Deploy
Use the command in the `Deployment` section above.

Takes about 5 minutes. Everything is automated via CloudFormation.

### Step 2: Wait for Analysis (Next Morning)
At 2 AM UTC, Lambda automatically:
- Scans all EBS volumes
- Checks EC2 CPU metrics
- Finds S3 issues
- Generates findings.json
- Uploads to S3

### Step 3: Login to Dashboard
Users access secure dashboard URL:
- Authenticate with username/password
- Session managed automatically
- HTTPS enforced via CloudFront

### Step 4: Review Findings
Users see:
- findings with estimated monthly and annual savings
- Real-time savings calculation
- Filters by type/severity/region
- action options for Keep, Remove, Notify, Schedule, Resize, and Lifecycle workflows

### Step 5: Make Decisions
Users click buttons:
- [Keep] → Exclude from automated execution
- [Remove] → Mark supported resources for removal
- [Notify] → Send stakeholder notification
- [Schedule] / [Resize] / [Lifecycle] → Apply optimization workflows when supported

Dashboard updates instantly showing total savings.

### Step 6: Automatic Execution
At the configured scheduler windows (defaults 6 AM and 6 PM UTC), Scheduler Lambda:
- Reads user decisions from S3
- Checks safety (tags, policies)
- Executes approved supported actions (remove, schedule, and S3 lifecycle workflows)
- Logs everything
- Sends summary email

### Step 7: Track Results
Dashboard shows:
- What was actually removed
- Actual vs estimated savings
- History of all actions
- Cost impact
