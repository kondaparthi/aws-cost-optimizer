# Continuous Integration & Deployment Setup

## Overview

Automated code delivery through GitHub Actions enables testing, validation, and deployment of the Cost Optimizer framework without manual intervention. This document describes the workflow automation features available and federation setup.

---

## Pipeline Components

```
Code Push → Validation → Build → Deploy → Verify
  │          │            │        │        │
  └─ Tests   └─ Security  └─ Pkg   └─ Live  └─ Status
```

---

## Prerequisites

- GitHub repository with Actions enabled
- AWS account with IAM administrative access
- AWS CLI version 2 or later
- Local terminal access

---

## Federation Setup: OpenID Connect

The automation establishes a trust relationship between GitHub and AWS using OpenID Connect (OIDC) federation rather than static credentials.

### Initial Configuration

Within your AWS account, establish the federation endpoint:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  --client-id-list sts.amazonaws.com
```

Verify successful creation:

```bash
aws iam list-open-id-connect-providers
```

---

## Service Role Configuration

Create a service role that workflows will assume through the federation provider.

### Trust Policy Definition

The role receives requests from GitHub workflows and validates them using OIDC. Update placeholders with your repository path:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:GITHUB_ORG/GITHUB_REPO:*"
        }
      }
    }
  ]
}
```

### Role Provisioning

Establish the role using the AWS CLI (save the policy JSON above to a local file first):

```bash
aws iam create-role \
  --role-name WorkflowExecutionRole \
  --assume-role-policy-document file://trust-policy.json
```

Note the returned role ARN for later reference.

### Execution Permissions

Attach a permission policy governing which AWS operations workflows may perform:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:CreateBucket",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::deployment-artifacts-*",
        "arn:aws:s3:::deployment-artifacts-*/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "lambda:UpdateFunctionCode",
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
        "lambda:ListFunctions"
      ],
      "Resource": "arn:aws:lambda:*:*:function:optimizer-*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "cloudformation:DescribeStacks",
        "cloudformation:GetTemplate"
      ],
      "Resource": "arn:aws:cloudformation:*:*:stack/optimizer/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iam:PassRole"
      ],
      "Resource": "arn:aws:iam::*:role/optimizer-*"
    }
  ]
}
```

Create and associate this policy:

```bash
aws iam create-policy \
  --policy-name WorkflowExecutionPolicy \
  --policy-document file://permissions-policy.json

aws iam attach-role-policy \
  --role-name WorkflowExecutionRole \
  --policy-arn arn:aws:iam::ACCOUNT_ID:policy/WorkflowExecutionPolicy
```

Retrieve the role ARN:

```bash
aws iam get-role \
  --role-name WorkflowExecutionRole \
  --query Role.Arn \
  --output text
```

---

## GitHub Repository Configuration

### Secret Registration

Store the AWS role ARN as a repository secret so workflows can access it securely:

1. Navigate to the repository **Settings** page
2. Select **Secrets and variables** → **Actions**
3. Create a new secret named `AWS_ROLE_ARN`
4. Paste the role ARN from the previous step
5. Save the secret

The workflows reference this value as `${{ secrets.AWS_ROLE_ARN }}`.

---

## Workflow Execution

### Test Workflow Trigger

The test workflow runs upon code push or pull request events:

```bash
git add .
git commit -m "Implementation update"
git push origin feature/enhancement
```

Automated checks execute:
- Unit test suites (Python 3.9, 3.10, 3.11)
- Code style validation
- Security scanning
- Dependency analysis

### Deployment Workflow

Triggers automatically when code reaches the main branch, or manually through the Actions interface:

1. Open the **Actions** tab in GitHub
2. Select the deployment workflow
3. Click **Run workflow**
4. Provide optional parameters
5. Confirm execution

The workflow then:
- Compiles necessary dependencies
- Performs security validation
- Packages code for Lambda
- Updates functions in AWS
- Reports completion status

---

## Monitoring & Observation

### Workflow Status

Track execution in GitHub:

```bash
# Using GitHub CLI
gh run list --repo org/repo-name

# Via browser: https://github.com/org/repo-name/actions
```

### Lambda Function Status

Verify deployment success:

```bash
aws lambda get-function-configuration \
  --function-name optimizer-main \
  --query LastUpdateStatus

aws logs tail /aws/lambda/optimizer-main --follow
```

### Metrics & Alerts

The deployed infrastructure includes CloudWatch monitoring of:
- Workflow execution frequency
- Success/failure rates
- Lambda performance metrics
- Error occurrence patterns

---

## Operational Considerations

### Access Control

- **Limited scope**: Each workflow request proves origin from specific GitHub repository
- **Temporary credentials**: AWS issues session tokens valid only during workflow execution
- **Immediate expiration**: Sessions terminate when workflow completes
- **Audit trail**: CloudTrail records all federated role assumption

### Environments & Promotion

Implement staged deployment using GitHub environments:

```yaml
environments:
  development:
    deployment_branch: develop
  production:
    deployment_branch: main
    required_reviewers:
      - approver-id
```

Branch-specific workflows execute only when code reaches the designated branch.

### Cost Impact

Infrastructure expense for CI/CD automation:

| Component | Volume | Cost |
|-----------|--------|------|
| Workflow execution | 100 runs/month | Free (included) |
| Artifact storage | 50 MB/month | $0.01 |
| Log retention | 10 GB/month | $0.05 |
| **Monthly Total** | | **$0.06** |

---

## Troubleshooting Reference

### Cannot Assume Role

Validate these elements:
- OIDC provider exists in account
- Role trust policy specifies correct GitHub organization/repository
- Repository secret contains correct role ARN
- Actions are enabled on the repository

### Insufficient Permissions

Verify policy attachment:

```bash
aws iam list-role-policies --role-name WorkflowExecutionRole

aws iam get-role-policy \
  --role-name WorkflowExecutionRole \
  --policy-name WorkflowExecutionPolicy
```

### Deployment Failure

Review execution logs:

```bash
# GitHub Actions logs (visible in Actions tab)
# AWS Lambda logs
aws logs tail /aws/lambda/optimizer-main --follow

# CloudFormation events (if applicable)
aws cloudformation describe-stack-events \
  --stack-name optimizer-stack
```

---

## Related Resources

- **Deployment Instructions**: `docs/DEPLOYMENT.md`
- **Architecture Reference**: `ARCHITECTURE.md`
- **Configuration Guide**: `docs/CONFIG.md`
- **GitHub Actions Documentation**: https://docs.github.com/actions

---

**Automated, credential-free deployment infrastructure for continuous optimization.** 🔄