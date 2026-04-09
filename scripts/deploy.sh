#!/bin/bash

#
# AWS Cost Optimizer - Deployment Script
#
# Builds the Lambda package, uploads to S3, and deploys via CloudFormation
#
# Usage:
#   ./scripts/deploy.sh --stack-name my-cost-optimizer \
#     --config-bucket my-config-bucket \
#     --config-key config/cost-optimizer.yaml \
#     --report-bucket my-reports-bucket \
#     --email me@example.com
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Options:
  --stack-name <name>           CloudFormation stack name (required)
  --config-bucket <bucket>      S3 bucket for configuration (required)
  --config-key <key>            S3 key for config.yaml (default: config/cost-optimizer.yaml)
  --report-bucket <bucket>      S3 bucket for reports (required)
    --decisions-bucket <bucket>   S3 bucket for scheduler decisions (required)
  --report-prefix <prefix>      S3 prefix for reports (default: cost-reports/)
  --dashboard-bucket <bucket>   S3 bucket for dashboard (required, unique globally)
    --cross-account-external-id <id> External ID for cross-account role trust (required)
  --admin-email <email>          Admin email for Cognito user (required)
    --vpc-subnet-ids <ids>        Comma-separated subnet IDs for Lambda VPC (required)
    --vpc-security-group-ids <ids> Comma-separated security group IDs for Lambda VPC (required)
  --email <email>               Email for SNS notifications (required)
  --region <region>             AWS region (default: us-east-1)
  --analysis-schedule <cron>    EventBridge cron for analysis (default: cron(0 2 * * ? *))
  --scheduler-schedule <cron>   EventBridge cron for scheduler (default: cron(0 6,18 * * ? *))
  --lambda-timeout <seconds>    Lambda timeout (default: 900)
    --lambda-memory <MB>          Lambda memory (default: 1024)
  --help                        Show this help message

Example:
  $0 --stack-name cost-optimizer \
     --config-bucket my-config \
     --report-bucket my-reports \
     --dashboard-bucket my-dashboard-12345 \
     --admin-email admin@company.com \
     --email ops@company.com
EOF
}

# Default values
STACK_NAME=""
CONFIG_BUCKET=""
CONFIG_KEY="config/cost-optimizer.yaml"
REPORT_BUCKET=""
DECISIONS_BUCKET=""
REPORT_PREFIX="cost-reports/"
DASHBOARD_BUCKET=""
EXTERNAL_ID=""
ADMIN_EMAIL=""
EMAIL=""
VPC_SUBNET_IDS=""
VPC_SECURITY_GROUP_IDS=""
REGION="us-east-1"
ANALYSIS_SCHEDULE="cron(0 2 * * ? *)"
SCHEDULER_SCHEDULE="cron(0 6,18 * * ? *)"
LAMBDA_TIMEOUT="900"
LAMBDA_MEMORY="1024"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --stack-name) STACK_NAME="$2"; shift 2 ;;
        --config-bucket) CONFIG_BUCKET="$2"; shift 2 ;;
        --config-key) CONFIG_KEY="$2"; shift 2 ;;
        --report-bucket) REPORT_BUCKET="$2"; shift 2 ;;
        --decisions-bucket) DECISIONS_BUCKET="$2"; shift 2 ;;
        --report-prefix) REPORT_PREFIX="$2"; shift 2 ;;
        --dashboard-bucket) DASHBOARD_BUCKET="$2"; shift 2 ;;
        --cross-account-external-id) EXTERNAL_ID="$2"; shift 2 ;;
        --admin-email) ADMIN_EMAIL="$2"; shift 2 ;;
        --vpc-subnet-ids) VPC_SUBNET_IDS="$2"; shift 2 ;;
        --vpc-security-group-ids) VPC_SECURITY_GROUP_IDS="$2"; shift 2 ;;
        --email) EMAIL="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --analysis-schedule) ANALYSIS_SCHEDULE="$2"; shift 2 ;;
        --scheduler-schedule) SCHEDULER_SCHEDULE="$2"; shift 2 ;;
        --lambda-timeout) LAMBDA_TIMEOUT="$2"; shift 2 ;;
        --lambda-memory) LAMBDA_MEMORY="$2"; shift 2 ;;
        --help) print_usage; exit 0 ;;
        *) log_error "Unknown option: $1"; print_usage; exit 1 ;;
    esac
done

# Validate required arguments
if [ -z "$STACK_NAME" ] || [ -z "$CONFIG_BUCKET" ] || [ -z "$REPORT_BUCKET" ] || [ -z "$DECISIONS_BUCKET" ] || [ -z "$DASHBOARD_BUCKET" ] || [ -z "$EXTERNAL_ID" ] || [ -z "$ADMIN_EMAIL" ] || [ -z "$EMAIL" ] || [ -z "$VPC_SUBNET_IDS" ] || [ -z "$VPC_SECURITY_GROUP_IDS" ]; then
    log_error "Missing required arguments"
    print_usage
    exit 1
fi

log_info "AWS Cost Optimizer Deployment Script"
log_info "======================================"
log_info "Stack Name: $STACK_NAME"
log_info "Config Bucket: $CONFIG_BUCKET"
log_info "Report Bucket: $REPORT_BUCKET"
log_info "Decisions Bucket: $DECISIONS_BUCKET"
log_info "Dashboard Bucket: $DASHBOARD_BUCKET"
log_info "VPC Subnets: $VPC_SUBNET_IDS"
log_info "VPC Security Groups: $VPC_SECURITY_GROUP_IDS"
log_info "Region: $REGION"

CFN_VPC_SUBNET_IDS="$(echo "$VPC_SUBNET_IDS" | sed 's/,/\\,/g')"
CFN_VPC_SECURITY_GROUP_IDS="$(echo "$VPC_SECURITY_GROUP_IDS" | sed 's/,/\\,/g')"

# ========================================================================
# Step 1: Validate prerequisites
# ========================================================================

log_info "Step 1: Validating prerequisites..."

if ! command -v aws &> /dev/null; then
    log_error "AWS CLI not found. Please install it."
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    log_error "Python 3 not found. Please install it."
    exit 1
fi

# ========================================================================
# Step 2: Create venv and install dependencies
# ========================================================================

log_info "Step 2: Setting up Python environment..."

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ========================================================================
# Step 3: Build Lambda package
# ========================================================================

log_info "Step 3: Building Lambda deployment package..."

PACKAGE_DIR="build/lambda"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

# Copy framework code
cp -r src/aws_cost_optimizer "$PACKAGE_DIR/"

# Copy dependencies
pip install --quiet --target "$PACKAGE_DIR" -r requirements.txt

# Create deployment ZIP
LAMBDA_ZIP="build/cost-optimizer-lambda.zip"
rm -f "$LAMBDA_ZIP"
cd "$PACKAGE_DIR"
zip -r -q "../../$LAMBDA_ZIP" .
cd ../../

log_info "Lambda package created: $LAMBDA_ZIP (size: $(du -h $LAMBDA_ZIP | cut -f1))"

# ========================================================================
# Step 4: Upload Lambda package to S3
# ========================================================================

log_info "Step 4: Uploading Lambda package to S3..."

LAMBDA_S3_KEY="cost-optimizer/lambda/$(date +%Y%m%d_%H%M%S)-lambda.zip"

aws s3 cp "$LAMBDA_ZIP" "s3://$CONFIG_BUCKET/$LAMBDA_S3_KEY" \
    --region "$REGION" \
    --quiet

log_info "Lambda package uploaded to s3://$CONFIG_BUCKET/$LAMBDA_S3_KEY"

# ========================================================================
# Step 5: Validate CloudFormation template
# ========================================================================

log_info "Step 5: Validating CloudFormation template..."

aws cloudformation validate-template \
    --template-body file://cloudformation/cost-optimizer-main.yaml \
    --region "$REGION" \
    > /dev/null

log_info "CloudFormation template is valid"

# ========================================================================
# Step 6: Deploy CloudFormation stack
# ========================================================================

log_info "Step 6: Deploying CloudFormation stack..."

aws cloudformation deploy \
    --template-file cloudformation/cost-optimizer-main.yaml \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --parameter-overrides \
        ConfigS3Bucket="$CONFIG_BUCKET" \
        ConfigS3Key="$CONFIG_KEY" \
        CodeS3Bucket="$CONFIG_BUCKET" \
        CodeS3Key="$LAMBDA_S3_KEY" \
        ReportS3Bucket="$REPORT_BUCKET" \
        DecisionsS3Bucket="$DECISIONS_BUCKET" \
        ReportS3Prefix="$REPORT_PREFIX" \
        DashboardS3Bucket="$DASHBOARD_BUCKET" \
        CrossAccountExternalId="$EXTERNAL_ID" \
        AdminEmail="$ADMIN_EMAIL" \
        VpcSubnetIds="$CFN_VPC_SUBNET_IDS" \
        VpcSecurityGroupIds="$CFN_VPC_SECURITY_GROUP_IDS" \
        NotificationEmail="$EMAIL" \
        AnalysisSchedule="$ANALYSIS_SCHEDULE" \
        SchedulerSchedule="$SCHEDULER_SCHEDULE" \
        LambdaTimeout="$LAMBDA_TIMEOUT" \
        LambdaMemory="$LAMBDA_MEMORY" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset

log_info "CloudFormation stack deployed: $STACK_NAME"

# ========================================================================
# Step 7: Update Lambda functions with actual code
# ========================================================================

log_info "Step 7: Updating Lambda functions with code..."

# Get the Lambda function names from CloudFormation stack
ANALYSIS_LAMBDA=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='AnalysisLambdaFunctionArn'].OutputValue" \
    --output text | xargs -I {} basename {} | cut -d: -f6)

SCHEDULER_LAMBDA=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='SchedulerLambdaFunctionArn'].OutputValue" \
    --output text | xargs -I {} basename {} | cut -d: -f6)

AUTH_LAMBDA=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='AuthLambdaFunctionArn'].OutputValue" \
    --output text | xargs -I {} basename {} | cut -d: -f6)

log_info "Updating $ANALYSIS_LAMBDA with code..."
aws lambda update-function-code \
    --function-name "$ANALYSIS_LAMBDA" \
    --s3-bucket "$CONFIG_BUCKET" \
    --s3-key "$LAMBDA_S3_KEY" \
    --region "$REGION" \
    > /dev/null

log_info "Updating $SCHEDULER_LAMBDA with code..."
aws lambda update-function-code \
    --function-name "$SCHEDULER_LAMBDA" \
    --s3-bucket "$CONFIG_BUCKET" \
    --s3-key "$LAMBDA_S3_KEY" \
    --region "$REGION" \
    > /dev/null

log_info "Updating $AUTH_LAMBDA with code..."
aws lambda update-function-code \
    --function-name "$AUTH_LAMBDA" \
    --s3-bucket "$CONFIG_BUCKET" \
    --s3-key "$LAMBDA_S3_KEY" \
    --region "$REGION" \
    > /dev/null

# ========================================================================
# Step 7.5: Upload Dashboard Files
# ========================================================================

log_info "Step 7.5: Uploading dashboard files to S3..."

DASHBOARD_SECRET_ARN=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='DashboardAccessSecretArn'].OutputValue" \
    --output text)

SECRET_JSON=$(aws secretsmanager get-secret-value \
    --secret-id "$DASHBOARD_SECRET_ARN" \
    --region "$REGION" \
    --query 'SecretString' \
    --output text)

AUTH_API_URL=$(echo "$SECRET_JSON" | sed -n 's/.*"auth_api_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

# Replace placeholders with actual values
sed "s|YOUR_REPORTS_BUCKET|$REPORT_BUCKET|g; s|__AUTH_API_URL__|$AUTH_API_URL/auth|g" dashboard/index.html > /tmp/index.html
sed "s|__AUTH_API_URL__|$AUTH_API_URL/auth|g" dashboard/login.html > /tmp/login.html

aws s3 cp /tmp/index.html "s3://$DASHBOARD_BUCKET/index.html" \
    --region "$REGION" \
    --content-type "text/html" \
    --cache-control "max-age=300" \
    --quiet

aws s3 cp /tmp/login.html "s3://$DASHBOARD_BUCKET/login.html" \
    --region "$REGION" \
    --content-type "text/html" \
    --cache-control "max-age=300" \
    --quiet

log_info "Dashboard files uploaded to s3://$DASHBOARD_BUCKET/"

# ========================================================================
# Step 7.6: Bootstrap Cognito Group and Admin User
# ========================================================================

log_info "Step 7.6: Bootstrapping Cognito users..."

COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
    --output text)

aws cognito-idp create-group \
    --group-name dashboard-users \
    --user-pool-id "$COGNITO_USER_POOL_ID" \
    --region "$REGION" \
    > /dev/null 2>&1 || true

ADMIN_TEMP_PASSWORD="ChangeMe$(date +%s)!A1"

aws cognito-idp admin-create-user \
    --user-pool-id "$COGNITO_USER_POOL_ID" \
    --username "$ADMIN_EMAIL" \
    --temporary-password "$ADMIN_TEMP_PASSWORD" \
    --message-action SUPPRESS \
    --region "$REGION" \
    > /dev/null 2>&1 || true

aws cognito-idp admin-add-user-to-group \
    --user-pool-id "$COGNITO_USER_POOL_ID" \
    --username "$ADMIN_EMAIL" \
    --group-name dashboard-users \
    --region "$REGION" \
    > /dev/null 2>&1 || true

# ========================================================================
# Step 8: Verify deployment
# ========================================================================

log_info "Step 8: Verifying deployment..."

# Test Lambda functions (dry-run)
log_info "Testing analysis Lambda (dry-run)..."
aws lambda invoke \
    --function-name "$ANALYSIS_LAMBDA" \
    --payload '{"source": "test"}' \
    --region "$REGION" \
    /dev/null > /dev/null 2>&1 || log_warn "Analysis Lambda test invocation failed (may need config)"

log_info "Testing scheduler Lambda (dry-run)..."
aws lambda invoke \
    --function-name "$SCHEDULER_LAMBDA" \
    --payload '{"source": "test"}' \
    --region "$REGION" \
    /dev/null > /dev/null 2>&1 || log_warn "Scheduler Lambda test invocation failed (may be expected)"

# ========================================================================
# Step 9: Print deployment summary
# ========================================================================

# Get dashboard URL from stack outputs
DASHBOARD_URL=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue" \
    --output text)

log_info ""
log_info "════════════════════════════════════════════════════════"
log_info "✓ Deployment Successful!"
log_info "════════════════════════════════════════════════════════"
log_info ""
log_info "Stack Name: $STACK_NAME"
log_info "Region: $REGION"
log_info "Configuration Location: s3://$CONFIG_BUCKET/$CONFIG_KEY"
log_info "Report Location: s3://$REPORT_BUCKET/$REPORT_PREFIX"
log_info "Dashboard URL: $DASHBOARD_URL"
log_info ""
log_info "Next Steps:"
log_info "  1. Update configuration: s3://$CONFIG_BUCKET/$CONFIG_KEY"
log_info "     cp config/example-config.yaml s3://$CONFIG_BUCKET/$CONFIG_KEY"
log_info ""
log_info "  2. Confirm SNS email subscription"
log_info "     (Check your email for confirmation link)"
log_info ""
log_info "  3. Access the secure dashboard:"
log_info "     $DASHBOARD_URL"
log_info "     Admin user: $ADMIN_EMAIL"
log_info "     Temporary password: $ADMIN_TEMP_PASSWORD"
log_info "     You will be prompted to set a new password on first login"
log_info ""
log_info "  4. View CloudWatch Dashboard:"
log_info "     https://console.aws.amazon.com/cloudwatch/home?region=$REGION#dashboards:name=CostOptimizerMetrics"
log_info ""
log_info "  4. Monitor logs:"
log_info "     aws logs tail /aws/lambda/cost-optimizer-analysis --follow --region $REGION"
log_info "     aws logs tail /aws/lambda/cost-optimizer-scheduler --follow --region $REGION"
log_info ""
log_info "To deploy in additional accounts, use:"
log_info "  aws cloudformation deploy --template-file cloudformation/cost-optimizer-cross-account.yaml \\"
log_info "    --stack-name cost-optimizer-cross-account \\"
log_info "    --parameter-overrides CentralAccountId=$(aws sts get-caller-identity --query Account --output text) \\"
log_info "    --region $REGION \\"
log_info "    --capabilities CAPABILITY_NAMED_IAM"
log_info ""
