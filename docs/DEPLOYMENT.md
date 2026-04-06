# AWS Cost Optimizer - CloudFormation & Lambda Deployment Guide

## Overview

The AWS Cost Optimizer framework can be deployed entirely via **CloudFormation**, with automated Lambda functions for:
- **Nightly cost analysis** (triggered on schedule)
- **EC2/EMR scheduling** (start/stop instances based on tags)

This guide covers:
1. Single-account deployment
2. Multi-account setup (cross-account IAM roles)
3. CI/CD integration
4. Monitoring and troubleshooting

---

## Quick Start Options

### Option 1: Manual Deployment (Recommended for First Time)
Follow the step-by-step instructions below for complete control.

### Option 2: CI/CD Pipeline (Automated)
For automated deployments with GitHub Actions:

1. **Set up OIDC authentication** (see [CI/CD Integration Guide](CICD.md))
2. **Push to main branch** → Automatic deployment
3. **Monitor via GitHub Actions** → Real-time status

**Benefits:**
- ✅ Zero manual intervention
- ✅ Automated testing and security scans
- ✅ Version-controlled deployments
- ✅ Rollback capabilities

See [CI/CD Integration Guide](CICD.md) for complete setup instructions.

---

## Local CLI Usage
The project includes a local CLI implementation in `src/aws_cost_optimizer/main.py` for ad-hoc analysis and scheduler testing.

Use this when you want to run analysis manually from the repository without deploying the full stack:

```bash
export PYTHONPATH=src
python -m aws_cost_optimizer analyze --config config/example-config.yaml
```

If you prefer not to set `PYTHONPATH`, run the module directly:

```bash
python src/aws_cost_optimizer/main.py analyze --config config/example-config.yaml
```

For production automation, this guide recommends CloudFormation and Lambda deployment instead of manual CLI execution.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Central Account (Analysis & Scheduler Lambdas)              │
│                                                              │
│  ┌──────────────────────────────────────────────────┐       │
│  │ EventBridge                                      │       │
│  │  • Nightly analysis (2 AM UTC)                  │       │
│  │  • Scheduler trigger (6 AM & 6 PM UTC)         │       │
│  └────────────────┬─────────────────────────────────┘       │
│                   │                                          │
│         ┌─────────┴──────────┐                              │
│         │                    │                              │
│    ┌────▼────────────┐  ┌───▼────────────┐                │
│    │ Analysis Lambda │  │ Scheduler      │                │
│    │                │  │ Lambda         │                │
│    └────┬──────────┘  └────┬───────────┘                │
│         │                   │                              │
│         │  STS AssumeRole   │  STS AssumeRole            │
│         └─────────┬─────────┘                              │
│                   │                                        │
└───────────────────┼────────────────────────────────────────┘
                    │
         ┌──────────┴──────────┬───────────┐
         │                     │           │
    ┌────▼──────┐         ┌───▼────┐  ┌──▼───┐
    │ Target    │         │ Target │  │Target│
    │ Account 1 │         │ Account│  │ ... │
    │ (Role)    │         │ 2(Role)│  │     │
    └────────────┘         └────────┘  └──────┘
```

---

## Prerequisites

1. **AWS Account** with admin access
2. **AWS CLI** v2+ (`aws --version`)
3. **Python 3.9+** (`python3 --version`)
4. **S3 buckets**:
   - One for configuration (must exist)
   - One for reports (can be same as config bucket)
5. **Email address** for SNS notifications

---

## Step 1: Prepare S3 Buckets & Configuration

### 1.1 Create S3 buckets (if needed)

```bash
# Configuration bucket
aws s3 mb s3://cost-optimizer-config-$(date +%s) --region us-east-1

# Reports bucket (can be the same)
# aws s3 mb s3://cost-optimizer-reports-$(date +%s) --region us-east-1
```

### 1.2 Upload configuration file

```bash
# Copy example config
cp config/example-config.yaml /tmp/cost-optimizer.yaml

# Edit for your environment
vim /tmp/cost-optimizer.yaml

# Upload to S3
aws s3 cp /tmp/cost-optimizer.yaml \
  s3://cost-optimizer-config-12345/config/cost-optimizer.yaml \
  --region us-east-1
```

### 1.3 Customize configuration

Edit `/tmp/cost-optimizer.yaml`:

```yaml
# Set correct regions for your analysis
regions:
  - us-east-1
  - us-west-2
  - eu-west-1

# Leave empty for single-account, or add cross-account roles
accounts:
  # - id: "111111111111"
  #   role_arn: "arn:aws:iam::111111111111:role/CostOptimizerRole"
  #   name: "Production"

# Customize thresholds
thresholds:
  ebs:
    unattached_days: 7
    snapshot_age_days: 90
  ec2:
    idle_cpu_threshold: 5
    idle_days: 7
```

---

## Step 2: Deploy CloudFormation Stack

### 2.1 Using deployment script (recommended)

```bash
cd aws-cost-optimizer-template

./scripts/deploy.sh \
  --stack-name cost-optimizer \
  --config-bucket cost-optimizer-config-12345 \
  --config-key config/cost-optimizer.yaml \
  --report-bucket cost-optimizer-reports-12345 \
  --email ops@company.com \
  --region us-east-1
```

**Parameters:**
- `--stack-name`: CloudFormation stack name (e.g., `cost-optimizer`)
- `--config-bucket`: S3 bucket with configuration file
- `--config-key`: S3 path to config.yaml
- `--report-bucket`: S3 bucket for generated reports
- `--email`: Email for SNS notifications
- `--region`: AWS region (default: us-east-1)
- `--analysis-schedule`: EventBridge cron for nightly analysis (default: `cron(0 2 * * ? *)`)
- `--scheduler-schedule`: EventBridge cron for scheduler (default: `cron(0 6,18 * * ? *)`)

### 2.2 Manual CloudFormation deployment (if preferred)

```bash
aws cloudformation deploy \
  --template-file cloudformation/cost-optimizer-main.yaml \
  --stack-name cost-optimizer \
  --parameter-overrides \
    ConfigS3Bucket=cost-optimizer-config-12345 \
    ConfigS3Key=config/cost-optimizer.yaml \
    ReportS3Bucket=cost-optimizer-reports-12345 \
    ReportS3Prefix=cost-reports/ \
    NotificationEmail=ops@company.com \
    AnalysisSchedule="cron(0 2 * * ? *)" \
    SchedulerSchedule="cron(0 6,18 * * ? *)" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

### 2.3 Verify deployment

```bash
# Check stack status
aws cloudformation describe-stacks \
  --stack-name cost-optimizer \
  --region us-east-1 \
  --query 'Stacks[0].StackStatus'

# Output should show: CREATE_COMPLETE or UPDATE_COMPLETE
```

---

## Step 3: Confirm SNS Subscription

1. **Check your email** for AWS SNS subscription confirmation
2. **Click the "Confirm subscription" link**
3. You'll start receiving cost optimizer notifications

---

## Step 4: Test the Deployment

### 4.1 Manually invoke analysis Lambda

```bash
aws lambda invoke \
  --function-name cost-optimizer-analysis \
  --region us-east-1 \
  /tmp/analysis-response.json

cat /tmp/analysis-response.json
```

### 4.2 Monitor Lambda logs

```bash
# Follow analysis logs
aws logs tail /aws/lambda/cost-optimizer-analysis --follow --region us-east-1

# Follow scheduler logs
aws logs tail /aws/lambda/cost-optimizer-scheduler --follow --region us-east-1
```

### 4.3 Check CloudWatch Dashboard

```bash
# Get dashboard URL
aws cloudformation describe-stacks \
  --stack-name cost-optimizer \
  --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardURL`].OutputValue' \
  --output text
```

---

## Step 5: Multi-Account Setup (Optional)

To analyze multiple AWS accounts, deploy cross-account IAM roles in each target account.

### 5.1 In each target account:

```bash
# Deploy cross-account role
aws cloudformation deploy \
  --template-file cloudformation/cost-optimizer-cross-account.yaml \
  --stack-name cost-optimizer-cross-account \
  --parameter-overrides \
    CentralAccountId=123456789012 \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

Replace `123456789012` with your **central account ID** (where Lambdas are deployed).

### 5.2 Get cross-account role ARN

```bash
aws cloudformation describe-stacks \
  --stack-name cost-optimizer-cross-account \
  --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue' \
  --output text

# Output: arn:aws:iam::111111111111:role/CostOptimizerRole
```

### 5.3 Update configuration with role ARNs

Add to your `cost-optimizer.yaml` in S3:

```yaml
accounts:
  - id: "111111111111"
    role_arn: "arn:aws:iam::111111111111:role/CostOptimizerRole"
    name: "Production"
  
  - id: "222222222222"
    role_arn: "arn:aws:iam::222222222222:role/CostOptimizerRole"
    name: "Staging"
```

Then upload updated config:

```bash
aws s3 cp /tmp/cost-optimizer.yaml \
  s3://cost-optimizer-config-12345/config/cost-optimizer.yaml
```

The Lambdas will pick up the new configuration on the next scheduled run (or manually invoke).

---

## CloudFormation Resources Created

| Resource | Name | Purpose |
|----------|------|---------|
| **Lambda Function** | `cost-optimizer-analysis` | Nightly cost analysis |
| **Lambda Function** | `cost-optimizer-scheduler` | EC2/EMR start-stop scheduler |
| **EventBridge Rule** | `cost-optimizer-analysis-schedule` | Trigger analysis on schedule |
| **EventBridge Rule** | `cost-optimizer-scheduler` | Trigger scheduler on schedule |
| **IAM Role** | `cost-optimizer-lambda-role` | Lambda execution permissions |
| **SNS Topic** | `aws-cost-optimizer-notifications` | Notifications |
| **CloudWatch Log Groups** | `/aws/lambda/cost-optimizer-*` | Lambda logs |
| **CloudWatch Dashboard** | `CostOptimizerMetrics` | Monitoring dashboard |
| **CloudWatch Alarms** | `CostOptimizer-*-Errors` | Error alerts |

---

## Monitoring

### CloudWatch Dashboard

The CloudFormation stack creates a dashboard showing:
- Lambda invocations
- Average duration
- Error count
- Throttling events
- Cost findings count

View at: **CloudWatch → Dashboards → CostOptimizerMetrics**

### CloudWatch Alarms

Alarms are set up to notify you of:
- Analysis Lambda errors
- Scheduler Lambda errors

Triggered via SNS topic.

### Logs

View Lambda logs:
```bash
# Analysis Lambda
aws logs tail /aws/lambda/cost-optimizer-analysis --follow

# Scheduler Lambda
aws logs tail /aws/lambda/cost-optimizer-scheduler --follow
```

---

## Customization

### Changing Schedule

Edit EventBridge rules to change execution schedule:

```bash
# Change analysis schedule
aws events put-rule \
  --name cost-optimizer-analysis-schedule \
  --schedule-expression "cron(0 3 * * ? *)" \
  --state ENABLED
```

**Cron format examples:**
- `cron(0 2 * * ? *)` — 2 AM UTC every day
- `cron(0 2 ? * MON-FRI *)` — 2 AM UTC weekdays only
- `cron(0 6,18 * * ? *)` — 6 AM and 6 PM UTC every day
- `cron(0 2 1 * ? *)` — 2 AM UTC on 1st of month

### Adjusting Lambda Settings

Update memory or timeout:

```bash
aws lambda update-function-configuration \
  --function-name cost-optimizer-analysis \
  --timeout 1200 \
  --memory-size 1024
```

### Modifying Notifications

Change SNS recipients:

```bash
aws sns set-subscription-attributes \
  --subscription-arn <subscription-arn> \
  --attribute-name Endpoint \
  --attribute-value new-email@example.com
```

---

## Troubleshooting

### Lambda fails with "No config found"

**Issue:** `ConfigS3Bucket not set`

**Solution:**
```bash
# Verify environment variables
aws lambda get-function-configuration \
  --function-name cost-optimizer-analysis \
  | jq .Environment.Variables

# Should show CONFIG_S3_BUCKET, REPORT_S3_BUCKET, etc.
```

### Lambda timeout

**Issue:** Analysis takes >15 minutes

**Solution:** Increase timeout and memory:
```bash
aws lambda update-function-configuration \
  --function-name cost-optimizer-analysis \
  --timeout 1800 \
  --memory-size 2048
```

### Permissions error: "User is not authorized"

**Issue:** Lambda can't access S3 or EC2

**Solution:** Check IAM role permissions:
```bash
# Get role name
aws cloudformation describe-stack-resources \
  --stack-name cost-optimizer \
  --region us-east-1 | jq '.StackResources[] | select(.LogicalResourceId=="LambdaExecutionRole").PhysicalResourceId'

# Review inline policy
aws iam get-role-policy \
  --role-name cost-optimizer-lambda-role \
  --policy-name CostOptimizerPolicy
```

### No SNS email received

**Issue:** Didn't confirm SNS subscription

**Solution:**
1. Check email (including spam folder)
2. Re-subscribe:
```bash
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:ACCOUNT:aws-cost-optimizer-notifications \
  --protocol email \
  --notification-endpoint ops@company.com
```

---

## Cost of Running This Framework

**Typical monthly cost for small-to-medium deployment:**

| Service | Usage | Cost |
|---------|-------|------|
| **Lambda** | 2 functions × 30 invocations = 60 GB-seconds | ~$0.00 (free tier) |
| **EventBridge** | 60 rules triggered × 30 days = 1,800 invocations | ~$1.00 |
| **CloudWatch Logs** | ~50 MB logs/month | ~$0.25 |
| **S3** (config + reports) | ~100 MB storage, 100 PUT/GET | ~$0.05 |
| **CloudWatch Alarms** | 2 alarms | ~$0.10 |
| **SNS** | 60 emails/month | ~$0.00 |
| **EC2 Savings** | Off-hours scheduling | **$5,000-$50,000+** ✓ |

**Total overhead: ~$1.50/month**  
**Typical savings: $5,000-$50,000/month**  
**ROI: 3,333x - 33,333x** 🎯

---

## Cleanup

To remove all resources:

```bash
# Delete CloudFormation stacks
aws cloudformation delete-stack --stack-name cost-optimizer
aws cloudformation wait stack-delete-complete --stack-name cost-optimizer

# Delete S3 buckets (if no longer needed)
aws s3 rb s3://cost-optimizer-config-12345 --force
aws s3 rb s3://cost-optimizer-reports-12345 --force

# Delete cross-account stacks (in target accounts)
aws cloudformation delete-stack --stack-name cost-optimizer-cross-account
```

**Built for mid-market AWS organizations. Deploy once, save continuously.** 🚀
