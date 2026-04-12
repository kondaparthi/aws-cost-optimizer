# AWS Cost Optimizer Framework

## Production-Ready Infrastructure for Multi-Account AWS Cost Optimization

This is a **complete, enterprise-ready solution** for AWS cost optimization built from real-world experience managing complex multi-account AWS environments.

---

## What This Is

A **CloudFormation-deployed, fully automated AWS cost optimization system** that:

✅ **Runs automatically** - No manual scripts or CLI commands needed  
✅ **Analyzes costs daily** - Identifies optimization opportunities overnight  
✅ **Provides visibility** - Web dashboard shows estimated savings opportunities  
✅ **Executes safely** - Tag-based protection prevents accidental deletions  
✅ **Tracks everything** - Complete audit trail for compliance  
✅ **Multi-account ready** - Analyze across 100+ AWS accounts  
✅ **Production-proven** - Built on patterns used in enterprise environments  

---

## Real Problems This Solves

### Problem 1: Unattached EBS Volumes
- **Cost**: $10-50/month per unattached volume
- **How it's found**: Scans all EBS volumes, identifies unused
- **How it's fixed**: Dashboard shows cost, user approves, Lambda deletes
- **Result**: $2K-5K/month typical savings

### Problem 2: Idle EC2 Instances
- **Cost**: $20-100/month per idle instance  
- **How it's found**: CloudWatch CPU metrics (< 5% for 7 days)
- **How it's fixed**: Dashboard shows CPU and savings context; user approves supported actions (schedule, rightsize, notify, or remove where appropriate)
- **Result**: $3K-10K/month typical savings

### Problem 3: S3 Storage Waste
- **Cost**: $1-200+/month per bucket across stale multipart uploads, missing lifecycle rules, and cold data left in Standard storage
- **How it's found**: Scans S3 for incomplete multipart uploads, missing lifecycle transitions, missing Intelligent-Tiering, and buckets inactive for more than 3 years
- **How it's fixed**: Dashboard shows S3 recommendations with `Notify`, `Set Lifecycle Policy`, and `Remove` only for safe-delete candidates
- **Result**: $500-2K/month typical savings from multipart cleanup plus larger savings from lifecycle and tiering changes

### Problem 4: Off-Hours Scheduling
- **Cost**: 40-50% of EC2 budget for non-production environments
- **How it's found**: Tag-based scheduling policies
- **How it's fixed**: Automatic stop/start on schedule
- **Result**: $5K-20K/month typical savings

### Problem 5: Lack of Visibility
- **Cost**: Unknown waste, no optimization priorities
- **How it's fixed**: Dashboard shows prioritized findings with estimated savings and supporting context
- **Result**: Data-driven decision making

---

## Architecture 

```
┌─────────────────────────────────────────────────────────┐
│ AWS CloudFormation Stack (Deploy Once)                 │
│                                                         │
│ ┌──────────────────────────────────────────────────┐   │
│ │ EventBridge (Cron)                               │   │
│ │ • Trigger: Daily analysis                        │   │
│ │ • Trigger: Scheduler checks                       │
│ └────────────┬─────────────────────────────────────┘   │
│              │                                          │
│    ┌─────────┴──────────┐                              │
│    │                    │                              │
│ ┌──▼─────────────────┐ ┌▼──────────────────────┐     │
│ │ Analysis Lambda    │ │ Scheduler Lambda      │     │
│ │ • EBS, EC2, S3     │ │ • EC2/EMR start-stop   │
│ │ • Metrics context  │ │ • S3 lifecycle flows   │
│ │ • JSON findings    │ │ • Dry-run / execute    │
│ └────────┬──────────┘ └─────────────────────────┘     │
│          │                                            │
│          └────┬────────────────────────────────────┘
│               │
│        ┌──────▼──────┐
│        │ S3 Reports  │
│        │ • findings  │
│        │ • actions   │
│        │ • history   │
│        └──────┬──────┘
│               │
└───────────────┼────────────────────────────────────────┘
                │
        ┌───────▼──────────┐
        │ CloudFront       │
        │ Distribution     │
        │ • HTTPS only     │
        │ • Lambda@Edge    │
        │ • Auth checks    │
        └───────┬──────────┘
                │
        ┌───────▼──────────┐
        │ Web Dashboard    │
        │ (Static S3 site) │
        │ • Login required │
        │ • Review findings│
        │ • Action choices │
        │ • Export CSV     │
        └──────────────────┘
```
---

### Deployment
```
One-time setup (5 minutes):
./deploy.sh --stack-name cost-optimizer \
  --config-bucket my-config \
  --report-bucket my-reports \
  --decisions-bucket my-decisions \
  --dashboard-bucket my-dashboard-12345 \
  --cross-account-external-id cost-optimizer-external-id-123 \
  --admin-email admin@company.com \
  --email ops@company.com
```

### Daily Workflow
```
2 AM UTC  → Analysis Lambda runs and publishes findings
Any time  → Users review findings and approve actions in dashboard
6 AM UTC  → Scheduler Lambda executes approved actions with safety checks
```
---
## Safety & Compliance

### Five-Layer Protection

**Layer 1: Authentication**
- Dashboard requires login (username/password)
- Session-based access with automatic logout
- HTTPS-only access via CloudFront
- No direct S3 bucket access

**Layer 2: User Decision**
- User explicitly chooses [Keep], [Remove], [Notify], [Schedule], [Resize], or [Lifecycle]
- Can change decision before execution
- Fully reversible until the next scheduler run window

**Layer 3: Tag-Based Protection**
- Resources tagged `Environment=prod` → PROTECTED
- Resources tagged `DoNotDelete=true` → PROTECTED
- Resources tagged `ProtectFromCostOptimizer=true` → PROTECTED
- Even if user marks for deletion, tags protect them

**Layer 4: Audit Logging**
- Every action logged to CloudWatch Logs
- Includes: timestamp, action, resource, status, savings
- Full compliance trail
- Searchable & queryable

**Layer 5: Human Review**
- SNS email sent with summary
- Shows what was deleted/kept
- Allows verification before next run
- Dashboard shows results

### No Accidental Deletions
- `stop_instances()` → Instance stopped (can restart)
- `delete_volume()` → Volume deleted only if explicitly approved AND not protected
- All actions logged
- Never called without user approval + safety checks

---

## Real Value

### Cost Savings (Typical Monthly)

| Optimization | Monthly Savings | Annual |
|--------------|-----------------|--------|
| EBS cleanup | $2,000-5,000 | $24K-60K |
| EC2 idle shutdown | $3,000-10,000 | $36K-120K |
| Off-hours scheduling | $5,000-20,000 | $60K-240K |
| S3 multipart cleanup | $500-2,000 | $6K-24K |
| **TOTAL** | **$10,000-50,000+** | **$120K-600K+** |

### ROI
- **Deploy cost**: < 1 hour of engineering time
- **Deployment time**: 5 minutes
- **First report**: Next morning
- **ROI**: 3,000x - 30,000x in first month

### Payback Period
Usually **same month** - savings exceed deployment cost on day 1

---

## Multi-Account Support

### For Organizations with Multiple AWS Accounts

Deploy in central account (with analysis Lambda):
```yaml
accounts:
  - id: "111111111111"
    name: "production"
    role_arn: "arn:aws:iam::111111111111:role/CostOptimizerRole"
  - id: "222222222222"
    name: "staging"
    role_arn: "arn:aws:iam::222222222222:role/CostOptimizerRole"
  - id: "333333333333"
    name: "development"
    role_arn: "arn:aws:iam::333333333333:role/CostOptimizerRole"
```

Deploy cross-account role in each target account (using provided CloudFormation template).

Dashboard shows findings from ALL accounts with regional breakdown.

---

## Enterprise Ready

✅ **CloudFormation IaC** - Version control, repeatable deployments  
✅ **Secure Authentication** - Login-protected dashboard access  
✅ **HTTPS Everywhere** - CloudFront enforces SSL/TLS  
✅ **Least-privilege IAM** - Minimal permissions, no wildcards  
✅ **Audit logging** - CloudWatch Logs for compliance  
✅ **Encryption** - S3 encrypted, no secrets in code  
✅ **Error handling** - Comprehensive try/catch throughout  
✅ **Monitoring** - CloudWatch alarms on Lambda failures  
✅ **Documentation** - Production-grade code documentation  
✅ **Testing** - GitHub Actions CI/CD pipeline included  

---

## Who Should Use This

✅ **Organizations with 5+ AWS accounts**  
✅ **Teams with $10K+/month AWS spend**  
✅ **Companies needing cost visibility**  
✅ **Enterprises requiring audit trails**  
✅ **DevOps teams managing cloud infrastructure**  
✅ **FinOps organizations tracking cloud costs**  
✅ **MSPs managing customer AWS accounts**  

---
## Demo Dashboard

Experience the cost optimizer with realistic synthetic data:
![AWS Cost Optimizer Dashboard Login](dashboard/demo/dashboard-login.png)

![AWS Cost Optimizer Dashboard](dashboard/demo/dashboard-screenshot-1.png)

```bash
cd dashboard/demo
python3 -m http.server 8000
# Open http://localhost:8000/demo.html in your browser
```

The demo includes:
- **28 realistic findings** across EBS, EC2, S3, RDS, and other AWS services
- **Interactive decision-making** - mark resources to keep or remove
- **Real cost calculations** with accurate AWS pricing
- **Filtering and export** capabilities
- **Sample user actions** showing realistic decision patterns

Perfect for understanding the optimization workflow before production deployment.

---

## FAQs

### If AWS already has cost tools, why use this framework?
AWS provides strong recommendation engines, but recommendations are distributed across different services and often stop at insight.

This framework adds the operating layer needed to safely act on those insights:
- One consolidated workflow for findings and actions
- Human approval options (Keep, Remove, Notify, Schedule, Rightsize, Lifecycle workflows)
- Risk-aware guardrails for automation-managed resources
- Decision tracking and auditability
- Communication workflows before high-risk actions

In short: AWS tells you what might be optimized. This framework helps teams safely decide and execute.

### Is this replacing native AWS services?
No. It complements them.

AWS services provide core telemetry and many recommendations.
This framework also adds custom recommendation logic and standardizes triage, governance, and action execution across teams and accounts.

### What value does this add for enterprise teams?
- **Governance**: explicit decision capture instead of ad-hoc cleanup
- **Safety**: drift-aware handling for CloudFormation/Terraform/CDK-managed resources
- **Consistency**: repeatable optimization process across environments
- **Speed**: less context-switching across multiple AWS consoles
- **Communication**: built-in notify path for stakeholders before removals

### Why not auto-delete everything with high savings?
Because optimization without context can cause outages, deployment failures, and infrastructure drift.

This framework is intentionally designed to prioritize safe, reviewable optimization over blind automation.

---

## 🤖AI Assistance

AI assistants (Claude & GitHub Copilot Chat) were used as a **productivity aid** for parts of the implementation.
The end-to-end architecture, design integrity, and cost optimization framework reflect my hands-on experience building and optimizing AWS environments at scale.

---

## License

MIT - Use freely in your organization
