"""
AWS Lambda handler for automated cost analysis.

Triggered by CloudWatch Events (EventBridge) on a schedule.
Reads config from S3/Systems Manager Parameter Store.
Generates findings.json report and sends to S3 + SNS.
"""

import json
import logging
import boto3
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

# Import core framework modules
import sys
sys.path.insert(0, '/var/task')  # Lambda function root

from aws_cost_optimizer.core import (
    ConfigLoader, AWSClient, StructuredLogger, SkipPolicy
)
from aws_cost_optimizer.models import FindingsReport
from aws_cost_optimizer.analyzers.ebs_analyzer import EBSAnalyzer
from aws_cost_optimizer.analyzers.ec2_analyzer import EC2Analyzer
from aws_cost_optimizer.analyzers.s3_analyzer import S3Analyzer


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
s3_client = boto3.client("s3")
ssm_client = boto3.client("ssm")
sns_client = boto3.client("sns")
sfn_client = boto3.client("stepfunctions")

# Issue #10: Stop processing new accounts/regions when fewer than this many
# seconds remain in the Lambda execution window.
TIMEOUT_SAFETY_MARGIN_SECONDS = 30


class LambdaConfigLoader:
    """Load config from S3 or Systems Manager Parameter Store."""
    
    @staticmethod
    def load_from_s3(bucket: str, key: str) -> str:
        """Load config file from S3."""
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read().decode("utf-8")
        except Exception as e:
            logger.error(f"Error loading config from S3: {str(e)}")
            raise
    
    @staticmethod
    def load_from_parameter_store(parameter_name: str) -> str:
        """Load config from Systems Manager Parameter Store."""
        try:
            response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
            return response["Parameter"]["Value"]
        except Exception as e:
            logger.error(f"Error loading config from Parameter Store: {str(e)}")
            raise


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for cost analysis.
    
    Environment variables:
    - CONFIG_S3_BUCKET: S3 bucket containing config.yaml
    - CONFIG_S3_KEY: S3 key for config.yaml (e.g., config/cost-optimizer.yaml)
    - REPORT_S3_BUCKET: S3 bucket for output reports
    - REPORT_S3_PREFIX: S3 prefix for reports (e.g., cost-reports/)
    - SNS_TOPIC_ARN: Optional SNS topic for notifications
    - DRY_RUN: "true" or "false" (default: false for Lambda, true for testing)
    
    Event (from EventBridge):
    {
        "source": "aws.events",
        "detail-type": "Scheduled Event",
        "detail": {}
    }
    """
    
    logger.info("Cost Optimizer Lambda triggered")
    logger.info(f"Event: {json.dumps(event)}")
    
    try:
        # Get environment config
        config_bucket = os.environ.get("CONFIG_S3_BUCKET")
        config_key = os.environ.get("CONFIG_S3_KEY", "config/cost-optimizer.yaml")
        report_bucket = os.environ.get("REPORT_S3_BUCKET", config_bucket)
        report_prefix = os.environ.get("REPORT_S3_PREFIX", "cost-reports/")
        sns_topic = os.environ.get("SNS_TOPIC_ARN")
        dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
        
        if not config_bucket:
            raise ValueError("CONFIG_S3_BUCKET environment variable not set")
        
        # Load config from S3
        logger.info(f"Loading config from s3://{config_bucket}/{config_key}")
        config_content = LambdaConfigLoader.load_from_s3(config_bucket, config_key)
        
        # Write to /tmp for ConfigLoader to parse
        tmp_config_path = "/tmp/config.yaml"
        with open(tmp_config_path, "w") as f:
            f.write(config_content)
        
        # Parse config
        config = ConfigLoader.load(tmp_config_path)
        
        # Setup logging (Lambda-optimized)
        lambda_logger = StructuredLogger(
            "cost-optimizer-lambda",
            config.logging.get("level", "INFO")
        )
        
        # Parse accounts and regions
        accounts = config.accounts if config.accounts else [{"id": None, "role_arn": None}]
        regions = config.regions
        
        lambda_logger.log_event("analysis_started", {
            "accounts": len(accounts),
            "regions": len(regions),
            "dry_run": dry_run
        })
        
        # =====================
        # Run analyzers
        # =====================
        findings_report = FindingsReport()
        # Issue #10: flag set when a timeout safety margin triggers early stop
        timed_out_early = False
        
        for account in accounts:
            if timed_out_early:
                break

            account_id = account.get("id")
            role_arn = account.get("role_arn")
            external_id = account.get("external_id")
            account_name = account.get("name", account_id or "local")
            
            for region in regions:
                # Issue #10: Check remaining execution time before starting
                # each account/region combination.  If we are within the safety
                # margin, save partial findings and stop processing.
                remaining_ms = context.get_remaining_time_in_millis()
                remaining_seconds = remaining_ms / 1000
                if remaining_seconds < TIMEOUT_SAFETY_MARGIN_SECONDS:
                    lambda_logger.log_event(
                        "lambda_timeout_imminent",
                        {
                            "remaining_seconds": round(remaining_seconds, 1),
                            "current_account": account_name,
                            "current_region": region,
                            "findings_so_far": findings_report.total_findings,
                        },
                        level="WARN",
                    )
                    findings_report.analysis_status = "partial"
                    findings_report.partial_reason = (
                        f"Lambda timeout safety margin reached with "
                        f"{round(remaining_seconds, 1)}s remaining; "
                        f"stopped before {account_name}/{region}"
                    )
                    timed_out_early = True
                    # Optionally trigger Step Functions for continuation
                    sfn_arn = os.environ.get("CONTINUATION_STATE_MACHINE_ARN")
                    if sfn_arn:
                        try:
                            sfn_client.start_execution(
                                stateMachineArn=sfn_arn,
                                input=json.dumps({
                                    "remaining_accounts": accounts[accounts.index(account):],
                                    "remaining_regions": regions,
                                }),
                            )
                            logger.info(
                                f"Triggered Step Functions continuation: {sfn_arn}"
                            )
                        except Exception as sfn_err:
                            logger.error(
                                f"Failed to trigger continuation: {sfn_err}"
                            )
                    break  # Break out of region loop

                logger.info(f"Analyzing {account_name} / {region}...")
                
                try:
                    # Create AWS client
                    aws_client = AWSClient(
                        region,
                        lambda_logger,
                        account_id=account_id,
                        role_arn=role_arn,
                        external_id=external_id,
                    )
                    
                    # Create skip policy
                    skip_policy = SkipPolicy(config.skip_policies, lambda_logger)
                    
                    # Run all analyzers
                    analyzers = [
                        EBSAnalyzer(aws_client, account_id or "local", region, skip_policy, lambda_logger),
                        EC2Analyzer(aws_client, account_id or "local", region, skip_policy, lambda_logger),
                        S3Analyzer(aws_client, account_id or "local", region, skip_policy, lambda_logger),
                    ]
                    
                    for analyzer in analyzers:
                        result = analyzer.run(config.thresholds, dry_run=dry_run)
                        
                        # Add findings to report
                        for finding in result.findings:
                            findings_report.add_finding(finding)
                        
                        # Track errors
                        findings_report.errors.extend(result.errors)
                        
                        logger.info(
                            f"{analyzer.name} {region}: {result.total_findings} findings, "
                            f"${result.total_potential_savings_annual:,.2f} annual savings"
                        )
                
                except Exception as region_error:
                    logger.error(f"Error analyzing {account_name}/{region}: {str(region_error)}")
                    findings_report.errors.append(f"{account_name}/{region}: {str(region_error)}")
        
        # =====================
        # Generate findings.json report
        # =====================
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        findings_json_key = f"findings-latest.json"  # Always "latest" for web dashboard
        findings_dated_key = f"findings-{timestamp}.json"  # Dated version for history
        
        logger.info(f"Uploading findings to S3...")
        
        findings_json = findings_report.to_json()
        
        # Upload latest (for dashboard)
        s3_client.put_object(
            Bucket=report_bucket,
            Key=findings_json_key,
            Body=findings_json,
            ContentType="application/json"
        )
        
        # Upload dated version (for history)
        s3_client.put_object(
            Bucket=report_bucket,
            Key=findings_dated_key,
            Body=findings_json,
            ContentType="application/json"
        )
        
        logger.info(f"Findings uploaded: s3://{report_bucket}/{findings_json_key}")
        
        # =====================
        # Send SNS notification
        # =====================
        if sns_topic:
            message = f"""
AWS Cost Optimizer - Analysis Report
=====================================

Generated: {datetime.utcnow().isoformat()}
Dry Run: {dry_run}

Summary:
  Total Findings: {findings_report.total_findings}
  Potential Monthly Savings: ${findings_report.potential_monthly_savings:,.2f}
  Potential Annual Savings: ${findings_report.potential_annual_savings:,.2f}
  
  By Type:
{chr(10).join([f'    {t}: {c} finding(s)' for t, c in findings_report.findings_by_type.items()])}
  
  By Severity:
{chr(10).join([f'    {s.upper()}: {c} finding(s)' for s, c in findings_report.findings_by_severity.items()])}

Dashboard: https://s3.amazonaws.com/{report_bucket}/dashboard/index.html
Report: s3://{report_bucket}/{findings_json_key}

---
{len(findings_report.errors)} errors occurred. Check CloudWatch Logs for details.
            """
            
            try:
                sns_client.publish(
                    TopicArn=sns_topic,
                    Subject="AWS Cost Optimizer Report",
                    Message=message
                )
                logger.info(f"Notification sent to {sns_topic}")
            except Exception as sns_error:
                logger.error(f"Error sending SNS notification: {str(sns_error)}")
        
        # =====================
        # Return response
        # =====================
        response = {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Cost analysis completed",
                "total_findings": findings_report.total_findings,
                "total_monthly_savings": findings_report.potential_monthly_savings,
                "total_annual_savings": findings_report.potential_annual_savings,
                "report_uri": f"s3://{report_bucket}/{findings_json_key}",
                "dashboard_uri": f"https://s3.amazonaws.com/{report_bucket}/dashboard/index.html"
            })
        }
        
        lambda_logger.log_event("analysis_completed", {
            "findings": findings_report.total_findings,
            "monthly_savings": findings_report.potential_monthly_savings,
            "annual_savings": findings_report.potential_annual_savings,
            "report_uri": f"s3://{report_bucket}/{findings_json_key}"
        })
        
        logger.info(f"Lambda execution successful: {json.dumps(response)}")
        return response
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        
        # Try to send error notification
        if os.environ.get("SNS_TOPIC_ARN"):
            try:
                sns_client.publish(
                    TopicArn=os.environ.get("SNS_TOPIC_ARN"),
                    Subject="AWS Cost Optimizer - ERROR",
                    Message=f"Analysis failed: {str(e)}\n\nCheck CloudWatch Logs for details."
                )
            except:
                pass
        
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Cost analysis failed",
                "error": str(e)
            })
        }
