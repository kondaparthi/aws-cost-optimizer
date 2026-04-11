#!/usr/bin/env bash
# =============================================================================
# localstack-smoke-test.sh
#
# End-to-end smoke test for AWS Cost Optimizer against LocalStack Community.
#
# What this script does:
#  1. Validates LocalStack is running
#  2. Builds a Lambda deployment package from src/
#  3. Creates prerequisite S3 buckets and uploads code + config
#  4. Creates AWS fixture resources that the analyzers should flag:
#       - 2 unattached EBS volumes
#       - 1 running EC2 instance (idle — no CloudWatch CPU history in LocalStack)
#       - 1 S3 bucket with an intentionally stalled multipart upload
#  5. Deploys cloudformation/cost-optimizer-localstack.yaml
#  6. Invokes the analysis Lambda and waits for findings in S3
#  7. Downloads findings.json and prints a summary
#  8. Copies findings.json and dashboard assets to the dashboard S3 bucket
#
# Requirements:
#   - LocalStack running  (localstack start -d  or  docker run localstack/localstack)
#   - AWS CLI >= 2.x
#   - Python 3 + pip
#   - zip
#
# Usage:
#   chmod +x scripts/localstack-smoke-test.sh
#   ./scripts/localstack-smoke-test.sh
#
# Optional environment overrides:
#   LS_ENDPOINT   LocalStack URL  (default: http://localhost:4566)
#   AWS_REGION    Region          (default: us-east-1)
#   STACK_NAME    CF stack name   (default: aco-smoke)
# =============================================================================

set -euo pipefail

# ---- config -----------------------------------------------------------------
LS_ENDPOINT="${LS_ENDPOINT:-http://localhost:4566}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-aco-smoke}"

CONFIG_BUCKET="${STACK_NAME}-config"
CODE_BUCKET="${STACK_NAME}-code"
REPORT_BUCKET="${STACK_NAME}-reports"
DECISIONS_BUCKET="${STACK_NAME}-decisions"
DASHBOARD_BUCKET="${STACK_NAME}-dashboard"
FIXTURE_BUCKET="${STACK_NAME}-fixture-uploads"

CODE_KEY="lambda.zip"
CONFIG_KEY="config/cost-optimizer.yaml"
REPORT_PREFIX="cost-reports/"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="/tmp/${STACK_NAME}-build"

# ---- helpers ----------------------------------------------------------------
AWS="aws --endpoint-url=${LS_ENDPOINT} --region=${AWS_REGION}"
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION="${AWS_REGION}"
export AWS_PAGER=""

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
step() { echo; echo "==> $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

# ---- 0. preflight -----------------------------------------------------------
step "Preflight checks"

command -v aws    >/dev/null 2>&1 || fail "AWS CLI not found"
command -v python3 >/dev/null 2>&1 || fail "python3 not found"
command -v zip    >/dev/null 2>&1 || fail "zip not found"

log "Checking LocalStack at ${LS_ENDPOINT} ..."
$AWS s3 ls >/dev/null 2>&1 || fail "LocalStack not reachable at ${LS_ENDPOINT}. Is it running?"
log "LocalStack OK"

# ---- 1. build Lambda package ------------------------------------------------
step "Building Lambda deployment package"

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/pkg"

log "Installing Python dependencies into ${BUILD_DIR}/pkg ..."
pip install --quiet --target "${BUILD_DIR}/pkg" -r "${REPO_ROOT}/requirements.txt"

log "Copying source package ..."
cp -r "${REPO_ROOT}/src/aws_cost_optimizer" "${BUILD_DIR}/pkg/"

log "Zipping ..."
(cd "${BUILD_DIR}/pkg" && zip -q -r "${BUILD_DIR}/${CODE_KEY}" .)
log "Package: ${BUILD_DIR}/${CODE_KEY} ($(du -sh "${BUILD_DIR}/${CODE_KEY}" | cut -f1))"

# ---- 2. write LocalStack-specific config ------------------------------------
step "Writing LocalStack smoke-test config"

# Thresholds are set to 0 so fixtures are flagged immediately without waiting.
python3 - "${BUILD_DIR}/config.yaml" <<'PYEOF'
import sys
content = """regions:
  - us-east-1

accounts: []

skip_policies:
  skip_if_tags_match: {}
  skip_if_any_tag: []

thresholds:
  ebs:
    unattached_days: 0
    snapshot_age_days: 0
  ec2:
    idle_cpu_threshold: 5
    idle_days: 1
  s3:
    check_incomplete_multipart: true
    multipart_age_days: 0
  cloudwatch:
    retention_threshold_days: 400
    daily_ingestion_gb: 1.0

scheduler:
  timezone: "UTC"
  schedules: {}

output:
  directory: "/tmp/aco-reports"
  formats:
    - json
  include_details: true

logging:
  level: INFO
"""
with open(sys.argv[1], "w") as f:
    f.write(content)
PYEOF

# ---- 3. S3 buckets + upload -------------------------------------------------
step "Creating S3 buckets"

for b in "${CONFIG_BUCKET}" "${CODE_BUCKET}" "${REPORT_BUCKET}" \
          "${DECISIONS_BUCKET}" "${DASHBOARD_BUCKET}" "${FIXTURE_BUCKET}"; do
  $AWS s3 mb "s3://${b}" >/dev/null 2>&1 || true
  log "  s3://${b}"
done

log "Uploading Lambda package ..."
$AWS s3 cp "${BUILD_DIR}/${CODE_KEY}" "s3://${CODE_BUCKET}/${CODE_KEY}" >/dev/null

log "Uploading config ..."
$AWS s3 cp "${BUILD_DIR}/config.yaml" "s3://${CONFIG_BUCKET}/${CONFIG_KEY}" >/dev/null

# ---- 4. upload dashboard HTML assets ----------------------------------------
step "Uploading dashboard HTML assets"

if [ -f "${REPO_ROOT}/dashboard/index.html" ]; then
  $AWS s3 cp "${REPO_ROOT}/dashboard/index.html" \
    "s3://${DASHBOARD_BUCKET}/index.html" --content-type "text/html" >/dev/null
  log "  index.html uploaded"
fi
if [ -f "${REPO_ROOT}/dashboard/login.html" ]; then
  $AWS s3 cp "${REPO_ROOT}/dashboard/login.html" \
    "s3://${DASHBOARD_BUCKET}/login.html" --content-type "text/html" >/dev/null
  log "  login.html uploaded"
fi

# ---- 5. create fixture resources --------------------------------------------
step "Creating fixture resources for analyzers to flag"

# 5a. Unattached EBS volumes
log "Creating 2 unattached EBS volumes ..."

VOL1=$($AWS ec2 create-volume \
  --availability-zone "${AWS_REGION}a" \
  --size 20 --volume-type gp3 \
  --tag-specifications '[{"ResourceType":"volume","Tags":[{"Key":"Name","Value":"smoke-unattached-1"}]}]' \
  --query 'VolumeId' --output text)

VOL2=$($AWS ec2 create-volume \
  --availability-zone "${AWS_REGION}a" \
  --size 50 --volume-type gp3 \
  --tag-specifications '[{"ResourceType":"volume","Tags":[{"Key":"Name","Value":"smoke-unattached-2"}]}]' \
  --query 'VolumeId' --output text)

log "  ${VOL1} (20 GiB gp3)"
log "  ${VOL2} (50 GiB gp3)"

# 5b. EC2 instance (idle — no CW CPU history in LocalStack)
log "Creating EC2 instance fixture ..."

INSTANCE_ID=$(set +e; $AWS ec2 run-instances \
  --image-id "ami-12345678" \
  --instance-type t3.medium \
  --tag-specifications '[{"ResourceType":"instance","Tags":[{"Key":"Name","Value":"smoke-idle-instance"}]}]' \
  --query 'Instances[0].InstanceId' --output text 2>/tmp/ec2-run.err) && true

if [ -n "${INSTANCE_ID:-}" ] && [ "${INSTANCE_ID}" != "None" ]; then
  log "  ${INSTANCE_ID} (t3.medium)"
else
  log "  EC2 fixture skipped ($(cat /tmp/ec2-run.err 2>/dev/null | head -1)); EBS and S3 findings will still be generated"
fi

# 5c. Stalled multipart upload
log "Creating stalled multipart upload in s3://${FIXTURE_BUCKET} ..."
UPLOAD_ID=$($AWS s3api create-multipart-upload \
  --bucket "${FIXTURE_BUCKET}" --key "large-object/data.bin" \
  --query 'UploadId' --output text)
log "  Upload ID: ${UPLOAD_ID} (intentionally left incomplete)"

# ---- 6. deploy CloudFormation stack -----------------------------------------
step "Deploying CloudFormation stack '${STACK_NAME}'"

$AWS cloudformation deploy \
  --template-file "${REPO_ROOT}/cloudformation/cost-optimizer-localstack.yaml" \
  --stack-name "${STACK_NAME}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ConfigS3Bucket="${CONFIG_BUCKET}" \
    ConfigS3Key="${CONFIG_KEY}" \
    CodeS3Bucket="${CODE_BUCKET}" \
    CodeS3Key="${CODE_KEY}" \
    ReportS3Bucket="${REPORT_BUCKET}" \
    DecisionsS3Bucket="${DECISIONS_BUCKET}" \
    ReportS3Prefix="${REPORT_PREFIX}" \
    DashboardS3Bucket="${DASHBOARD_BUCKET}"

STACK_STATUS=$($AWS cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query 'Stacks[0].StackStatus' --output text)

[[ "${STACK_STATUS}" == "CREATE_COMPLETE" || "${STACK_STATUS}" == "UPDATE_COMPLETE" ]] \
  || fail "Stack did not reach CREATE_COMPLETE (status: ${STACK_STATUS})"

ANALYSIS_LAMBDA=$($AWS cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query 'Stacks[0].Outputs[?OutputKey==`AnalysisLambdaArn`].OutputValue' \
  --output text)

log "Stack: ${STACK_STATUS}"
log "Analysis Lambda ARN: ${ANALYSIS_LAMBDA}"

# CloudFormation can return "No changes" when CodeS3Key is unchanged.
# Force a code refresh so latest local source is deployed.
log "Refreshing Lambda code from s3://${CODE_BUCKET}/${CODE_KEY} ..."
$AWS lambda update-function-code \
  --function-name "${ANALYSIS_LAMBDA}" \
  --s3-bucket "${CODE_BUCKET}" \
  --s3-key "${CODE_KEY}" >/dev/null
$AWS lambda wait function-updated --function-name "${ANALYSIS_LAMBDA}"
log "Lambda code refreshed"

# ---- 7. invoke analysis Lambda ----------------------------------------------
step "Invoking analysis Lambda"

INVOKE_LOG="${BUILD_DIR}/invoke-response.json"

$AWS lambda invoke \
  --function-name "${ANALYSIS_LAMBDA}" \
  --payload '{"source":"smoke-test","detail-type":"Manual Invoke"}' \
  --cli-binary-format raw-in-base64-out \
  "${INVOKE_LOG}" >/dev/null

STATUS_CODE=$(python3 -c "
import json, sys
d = json.load(open('${INVOKE_LOG}'))
print(d.get('statusCode', d.get('status', 'unknown')))
" 2>/dev/null || echo "unknown")
log "Lambda response status: ${STATUS_CODE}"

if [ -f "${INVOKE_LOG}" ]; then
  log "Raw response payload:"
  python3 -m json.tool "${INVOKE_LOG}" 2>/dev/null || cat "${INVOKE_LOG}"
fi

# ---- 8. retrieve findings from S3 -------------------------------------------
step "Retrieving findings from S3"

# Lambda writes to findings-latest.json at bucket root (not under REPORT_PREFIX)
FINDINGS_KEY="findings-latest.json"
# Wait briefly for S3 consistency in LocalStack
for i in $(seq 1 6); do
  if $AWS s3 ls "s3://${REPORT_BUCKET}/${FINDINGS_KEY}" >/dev/null 2>&1; then
    break
  fi
  log "  Waiting for findings file ... (${i}/6)"
  sleep 5
done

if ! $AWS s3 ls "s3://${REPORT_BUCKET}/${FINDINGS_KEY}" >/dev/null 2>&1; then
  log "WARNING: No findings file found. Lambda may have errored; check CloudWatch Logs."
  log "  $($AWS logs describe-log-groups \
    --log-group-name-prefix "/aws/lambda/${STACK_NAME}" \
    --query 'logGroups[*].logGroupName' --output text)"
else
  FINDINGS_LOCAL="${BUILD_DIR}/findings.json"
  $AWS s3 cp "s3://${REPORT_BUCKET}/${FINDINGS_KEY}" "${FINDINGS_LOCAL}" >/dev/null
  log "Downloaded: ${FINDINGS_LOCAL}"

  echo
  echo "-----------------------------------------------------------------------"
  echo "  FINDINGS SUMMARY"
  echo "-----------------------------------------------------------------------"
  python3 - "${FINDINGS_LOCAL}" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
findings = data.get("findings", [])
by_type = {}
for f in findings:
    t = f.get("resource_type", "unknown")
    by_type.setdefault(t, []).append(f)
print(f"Total findings : {len(findings)}")
print(f"Analysis status: {data.get('analysis_status', 'complete')}")
print()
for rtype, items in sorted(by_type.items()):
    print(f"  {rtype} ({len(items)} finding{'s' if len(items)>1 else ''}):")
    for f in items:
        saving = f.get("estimated_monthly_savings_usd", 0)
        rid    = f.get("resource_id", "?")
        reason = f.get("reason", "")
        print(f"    - {rid}  ~${saving:.2f}/mo  [{reason}]")
PYEOF
  echo "-----------------------------------------------------------------------"

  # ---- 9. copy findings to dashboard bucket -----------------------------------
  step "Copying findings to dashboard bucket"

  $AWS s3 cp "${FINDINGS_LOCAL}" \
    "s3://${DASHBOARD_BUCKET}/findings.json" \
    --content-type "application/json" >/dev/null

  log "findings.json : ${LS_ENDPOINT}/${DASHBOARD_BUCKET}/findings.json"
  log "Dashboard     : ${LS_ENDPOINT}/${DASHBOARD_BUCKET}/index.html"
fi

# ---- done -------------------------------------------------------------------
echo
log "Smoke test complete."
log ""
log "Useful commands:"
log "  List findings : $AWS s3 ls s3://${REPORT_BUCKET}/ --recursive"
log "  Lambda logs   : $AWS logs tail /aws/lambda/${STACK_NAME}-analysis --format short"
log "  Tear down     : $AWS cloudformation delete-stack --stack-name ${STACK_NAME}"
