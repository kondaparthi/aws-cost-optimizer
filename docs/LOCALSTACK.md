# LocalStack Deployment Guide

This guide shows how to validate and smoke-test this project against LocalStack.

## Important CLI Note

Use AWS CLI with an explicit LocalStack endpoint:

```bash
aws --endpoint-url=http://localhost:4566 <service> <operation>
```
## Prerequisites

1. LocalStack running
2. AWS CLI installed
3. Region and test credentials exported

```bash
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
export AWS_PAGER=""
export LS_ENDPOINT="http://localhost:4566"
```

## 1) Template Syntax Validation

```bash
aws --endpoint-url="$LS_ENDPOINT" cloudformation validate-template \
  --template-body file://cloudformation/cost-optimizer-main.yaml

aws --endpoint-url="$LS_ENDPOINT" cloudformation validate-template \
  --template-body file://cloudformation/cost-optimizer-cross-account.yaml
```

## 2) Service Support Matrix

The following table lists every CloudFormation resource family used by this project and its support level across LocalStack editions.

| Resource family | Community (free) | Pro / Team |
|---|---|---|
| S3 | Full | Full |
| IAM | Full | Full |
| Lambda | Full | Full |
| CloudWatch / Logs | Full | Full |
| SQS | Full | Full |
| SNS | Full | Full |
| KMS | Full | Full |
| Secrets Manager | Full | Full |
| EventBridge | Full | Full |
| API Gateway | Partial | Full |
| Cognito User Pools | **Stub only** — ARN not resolvable in IAM policies | **Full** |
| CloudFront | **Stub only** — deployed as no-op fallback | **Full** |
| Lambda@Edge | Not supported | Full |

> The `localstack status services` command reports all services as `available` regardless of tier; that reflects presence, not full fidelity. Cognito and CloudFront require a paid edition for complete CloudFormation lifecycle support.

Validation performed against LocalStack 2026.3.x Community edition:

- `cloudformation validate-template` passes for all three templates on any edition
- Cross-account stack (cost-optimizer-cross-account.yaml) reaches `CREATE_COMPLETE` on Community
- LocalStack smoke-test stack (cost-optimizer-localstack.yaml) reaches `CREATE_COMPLETE` on Community
- Full main stack (cost-optimizer-main.yaml) reaches `ROLLBACK_COMPLETE` on Community due to Cognito ARN stub behaviour
- Full main stack reaches `CREATE_COMPLETE` on Pro / Team edition

## 3) Full End-to-End Smoke Test on Community (Recommended)

A dedicated stripped template and driver script handle all fixture creation,
Lambda packaging, and findings validation in one command:

```bash
# Requires: LocalStack running, AWS CLI, Python 3, pip, zip
chmod +x scripts/localstack-smoke-test.sh
./scripts/localstack-smoke-test.sh
```

The script (`scripts/localstack-smoke-test.sh`) and its companion template
(`cloudformation/cost-optimizer-localstack.yaml`) do the following:

1. Build a Lambda deployment package from `src/` including all pip dependencies
2. Create and seed the required S3 buckets (config, code, reports, decisions, dashboard)
3. Create fixture AWS resources the analyzers will flag:
   - 2 unattached EBS volumes
   - 1 running EC2 instance (idle — no CloudWatch CPU history in LocalStack)
   - 1 S3 bucket with an intentionally stalled multipart upload
4. Deploy `cloudformation/cost-optimizer-localstack.yaml` via CloudFormation
5. Invoke the analysis Lambda directly and stream the response
6. Download `findings.json` from the reports bucket and print a summary table
7. Copy `findings.json` and dashboard assets to the dashboard bucket

After the run, the dashboard is accessible at:

```
http://localhost:4566/<stack-name>-dashboard/index.html
```

and findings at:

```
http://localhost:4566/<stack-name>-dashboard/findings.json
```

### What cost-optimizer-localstack.yaml strips from the production template

| Removed | Reason |
|---|---|
| Cognito UserPool / Client | ARN stub causes IAM rollback |
| CloudFront Distribution + OAI | No-op fallback only |
| Lambda@Edge (Auth + SecurityHeaders) | Not supported |
| AuthLambda + AuthLambdaExecutionRole | Depends on Cognito ARN |
| All API Gateway resources | Depends on AuthLambda |
| VpcConfig on Lambda functions | No VPC needed in LocalStack smoke |
| VpcSubnetIds / VpcSecurityGroupIds params | Removed with VPC config |
| CrossAccountExternalId param / STS condition | Single-account in smoke test |
| AllowedPattern / MinLength / MaxLength | No real CFN validator needed |
| KMS encryption on SNS / SQS | Uses managed keys in Community |
| DashboardLogsBucket | CloudFront access logging irrelevant |

All other resources (Lambda, S3, IAM, EventBridge, SQS, SNS, KMS, CloudWatch,
Secrets Manager) deploy to `CREATE_COMPLETE` on Community.

---

## 4) Cross-Account Stack Smoke Test (Works)

```bash
aws --endpoint-url="$LS_ENDPOINT" cloudformation deploy \
  --template-file cloudformation/cost-optimizer-cross-account.yaml \
  --stack-name aco-cross-local \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    HubLambdaExecutionRoleArn='arn:aws:iam::000000000000:role/cost-optimizer-lambda-role' \
    HubAnalysisLambdaArn='arn:aws:lambda:us-east-1:000000000000:function:cost-optimizer-analysis' \
    ExternalId='localstack-ext-id-1234'
```

## 5) Full Main Stack Smoke Test

### On LocalStack Community (free tier)

The stack will fail at `AuthLambdaExecutionRole` with:

```
MalformedPolicyDocument: Resource unknown must be in ARN format or "*"
```

**Root cause:** In the Community edition, `!GetAtt CognitoUserPool.Arn` resolves to the literal string `"unknown"` because Cognito is a stub. When LocalStack then calls `PutRolePolicy` it rejects that value. CloudFront also deploys as a no-op fallback and Lambda@Edge is a no-op.

This is a **LocalStack edition limitation, not a template bug.** The template is correct and deploys successfully on AWS and on LocalStack Pro.

### On LocalStack Pro / Team

The stack reaches `CREATE_COMPLETE`. All Cognito, CloudFront, and Lambda@Edge resources are fully provisioned.

### Practical guidance by tier

| Goal | Sufficient tier |
|---|---|
| Template syntax check | Community |
| Cross-account stack end-to-end | Community |
| Unit / integration tests for Lambda logic | Community |
| Full main stack deploy (Cognito + CloudFront) | Pro or AWS staging |
| Lambda@Edge behaviour | Pro or AWS staging |

## 6) Cleanup

```bash
aws --endpoint-url="$LS_ENDPOINT" cloudformation delete-stack --stack-name aco-main-local
aws --endpoint-url="$LS_ENDPOINT" cloudformation delete-stack --stack-name aco-cross-local
```
