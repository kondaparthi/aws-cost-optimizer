"""
AWS Lambda handler for automated EC2/EMR start-stop scheduling.

Triggered by CloudWatch Events (EventBridge) on a schedule.
Reads schedule config from S3/Parameter Store.
Stops/starts instances based on tags and time-based rules.
"""

import json
import logging
import boto3
import os
from datetime import datetime
from typing import Dict, Any, List, Optional
from zoneinfo import ZoneInfo  # Python 3.9+
from botocore.exceptions import ClientError

# Import core framework
import sys
sys.path.insert(0, '/var/task')

from aws_cost_optimizer.core import StructuredLogger


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
ec2_client = boto3.client("ec2")
s3_client = boto3.client("s3")
ssm_client = boto3.client("ssm")
sns_client = boto3.client("sns")


class ScheduleManager:
    """Manage EC2/EMR instance start-stop based on schedule policies."""
    
    SCHEDULE_TAG_KEY = "SchedulePolicy"
    
    # Predefined schedules
    SCHEDULES = {
        "off-hours": {
            "stop_time": "18:00",      # 6 PM
            "stop_days": [4, 5],        # Fri, Sat
            "start_time": "08:00",      # 8 AM
            "start_days": [0, 1, 2, 3, 4]  # Mon-Fri
        },
        "business-hours": {
            "stop_time": "18:00",
            "start_time": "08:00",
            "stop_days": [5, 6],        # Sat, Sun
            "start_days": [0, 1, 2, 3, 4]
        },
        "weekends-only": {
            "stop_time": "18:00",
            "stop_days": [4, 5],        # Fri, Sat
            "start_time": "08:00",
            "start_days": [0, 1, 2, 3, 4]
        },
        "always-off": {
            "stop_time": "00:00",
            "stop_days": [0, 1, 2, 3, 4, 5, 6],
            "start_time": "23:59",
            "start_days": []
        }
    }
    
    def __init__(self, timezone: str = "UTC", logger_instance: Optional[StructuredLogger] = None):
        self.timezone = timezone
        self.logger = logger_instance or StructuredLogger("scheduler", "INFO")
    
    def should_be_stopped(self, schedule_name: str, current_time: datetime) -> bool:
        """
        Determine if instance should be stopped based on schedule and current time.
        
        Args:
            schedule_name: Name of schedule (e.g., "off-hours")
            current_time: Current datetime (timezone-aware)
        
        Returns:
            True if instance should be stopped, False otherwise
        """
        schedule = self.SCHEDULES.get(schedule_name)
        if not schedule:
            logger.warning(f"Unknown schedule: {schedule_name}")
            return False
        
        current_day = current_time.weekday()  # 0=Mon, 6=Sun
        current_hour_min = current_time.strftime("%H:%M")
        
        # Check if current day is in stop_days
        if current_day not in schedule["stop_days"]:
            return False
        
        # Check if current time is past stop_time
        stop_time = schedule["stop_time"]
        return current_hour_min >= stop_time
    
    def should_be_started(self, schedule_name: str, current_time: datetime) -> bool:
        """
        Determine if instance should be started based on schedule and current time.
        
        Args:
            schedule_name: Name of schedule (e.g., "off-hours")
            current_time: Current datetime (timezone-aware)
        
        Returns:
            True if instance should be started, False otherwise
        """
        schedule = self.SCHEDULES.get(schedule_name)
        if not schedule:
            logger.warning(f"Unknown schedule: {schedule_name}")
            return False
        
        current_day = current_time.weekday()
        current_hour_min = current_time.strftime("%H:%M")
        
        # Check if current day is in start_days
        if current_day not in schedule["start_days"]:
            return False
        
        # Check if current time is past or equal to start_time
        start_time = schedule["start_time"]
        return current_hour_min >= start_time
    
    def get_instances_to_schedule(self, schedule_name: str) -> List[Dict[str, Any]]:
        """
        Find all EC2 instances with the given schedule policy tag.
        
        Returns:
            List of instances with schedule tag
        """
        try:
            response = ec2_client.describe_instances(
                Filters=[
                    {
                        "Name": f"tag:{self.SCHEDULE_TAG_KEY}",
                        "Values": [schedule_name]
                    },
                    {
                        "Name": "instance-state-name",
                        "Values": ["running", "stopped"]
                    }
                ]
            )
            
            instances = []
            for reservation in response["Reservations"]:
                for instance in reservation["Instances"]:
                    instances.append({
                        "instance_id": instance["InstanceId"],
                        "state": instance["State"]["Name"],
                        "tags": {t["Key"]: t["Value"] for t in instance.get("Tags", [])},
                        "instance_type": instance.get("InstanceType"),
                        "launch_time": instance.get("LaunchTime")
                    })
            
            return instances
        
        except Exception as e:
            logger.error(f"Error fetching instances: {str(e)}")
            return []
    
    def stop_instance(self, instance_id: str, dry_run: bool = False, ec2_api=None) -> bool:
        """Stop an EC2 instance."""
        try:
            ec2_api = ec2_api or ec2_client
            if dry_run:
                logger.info(f"[DRY-RUN] Would stop instance: {instance_id}")
                return True
            
            ec2_api.stop_instances(InstanceIds=[instance_id])
            logger.info(f"Stopped instance: {instance_id}")
            return True
        
        except Exception as e:
            logger.error(f"Error stopping instance {instance_id}: {str(e)}")
            return False
    
    def start_instance(self, instance_id: str, dry_run: bool = False, ec2_api=None) -> bool:
        """Start an EC2 instance."""
        try:
            ec2_api = ec2_api or ec2_client
            if dry_run:
                logger.info(f"[DRY-RUN] Would start instance: {instance_id}")
                return True
            
            ec2_api.start_instances(InstanceIds=[instance_id])
            logger.info(f"Started instance: {instance_id}")
            return True
        
        except Exception as e:
            logger.error(f"Error starting instance {instance_id}: {str(e)}")
            return False

    def verify_resource_current_state(self, instance_id: str, ec2_api=None) -> Optional[str]:
        """
        Issue #8: Re-fetch the live instance state immediately before acting.

        Returns the current state string (e.g. 'running', 'stopped') or None
        if the instance no longer exists.
        """
        try:
            ec2_api = ec2_api or ec2_client
            resp = ec2_api.describe_instances(InstanceIds=[instance_id])
            for reservation in resp.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    return inst["State"]["Name"]
            return None  # Instance not found
        except Exception as e:
            logger.error(
                f"Error re-fetching state for {instance_id}: {str(e)}"
            )
            return None


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for EC2/EMR scheduling.
    
    Environment variables:
    - TIMEZONE: Timezone for schedule evaluation (e.g., US/Eastern, UTC)
    - DRY_RUN: "true" or "false" (default: false)
    - SNS_TOPIC_ARN: Optional SNS topic for notifications
    
    Event (from EventBridge):
    {
        "source": "aws.events",
        "detail-type": "Scheduled Event"
    }
    """
    
    logger.info("Scheduler Lambda triggered")
    
    try:
        # Get environment config
        timezone_str = os.environ.get("TIMEZONE", "UTC")
        dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
        sns_topic = os.environ.get("SNS_TOPIC_ARN")
        decisions_bucket = os.environ.get("DECISIONS_S3_BUCKET")
        decisions_key = os.environ.get("DECISIONS_ACTIONS_KEY", "actions/actions-latest.json")
        
        # Get current time in specified timezone
        tz = ZoneInfo(timezone_str)
        current_time = datetime.now(tz)
        
        logger.info(f"Current time: {current_time} ({timezone_str})")
        logger.info(f"Dry-run mode: {dry_run}")
        
        # Create scheduler
        schedule_manager = ScheduleManager(timezone=timezone_str)
        
        # Track results
        actions = {
            "stopped": [],
            "started": [],
            "ui_scheduled_stopped": [],
            "ui_scheduled_started": [],
            "ui_resize_recommendations": [],
            "ui_ebs_deleted": [],
            "ui_s3_lifecycle_updated": [],
            "ui_s3_deleted": [],
            "ui_ebs_delete_skipped": [],
            "errors": [],
            # Issue #6: dry-run simulation results (never persisted to S3)
            "dry_run_would_stop": [],
            "dry_run_would_start": [],
            "dry_run_would_skip_state_changed": [],
        }

        # =====================
        # Process UI-saved actions from S3
        # =====================
        if decisions_bucket:
            try:
                obj = s3_client.get_object(Bucket=decisions_bucket, Key=decisions_key)
                payload = json.loads(obj["Body"].read().decode("utf-8"))
                schedule_cfg = payload.get("schedule_config", {})
                schedule_enabled = bool(schedule_cfg.get("enabled", True))
                schedule_tz_name = schedule_cfg.get("timezone", timezone_str)
                schedule_tz = ZoneInfo(schedule_tz_name)
                schedule_now = datetime.now(schedule_tz)
                business_start = schedule_cfg.get("business_start", "08:00")
                business_end = schedule_cfg.get("business_end", "18:00")
                off_days = set(schedule_cfg.get("off_days", [5, 6]))

                def _in_business_window(now_dt: datetime) -> bool:
                    now_hm = now_dt.strftime("%H:%M")
                    is_off_day = now_dt.weekday() in off_days
                    return (not is_off_day) and business_start <= now_hm < business_end

                current_account_id = boto3.client("sts").get_caller_identity().get("Account")

                def _regional_ec2(region_name: str):
                    return boto3.client("ec2", region_name=region_name)

                def _regional_s3(region_name: str):
                    return boto3.client("s3", region_name=region_name)

                def _delete_ebs_volume(region_ec2, volume_id: str) -> bool:
                    try:
                        if dry_run:
                            actions["ui_ebs_deleted"].append(f"[DRY-RUN] {volume_id}")
                            return True
                        region_ec2.delete_volume(VolumeId=volume_id)
                        actions["ui_ebs_deleted"].append(volume_id)
                        return True
                    except ClientError as ce:
                        actions["errors"].append(f"Failed to delete EBS volume {volume_id}: {ce}")
                        return False

                def _delete_ebs_snapshot(region_ec2, snapshot_id: str) -> bool:
                    try:
                        if dry_run:
                            actions["ui_ebs_deleted"].append(f"[DRY-RUN] {snapshot_id}")
                            return True
                        region_ec2.delete_snapshot(SnapshotId=snapshot_id)
                        actions["ui_ebs_deleted"].append(snapshot_id)
                        return True
                    except ClientError as ce:
                        actions["errors"].append(f"Failed to delete EBS snapshot {snapshot_id}: {ce}")
                        return False

                def _put_bucket_lifecycle_rule(region_s3, bucket_name: str, rule: Dict[str, Any]) -> bool:
                    try:
                        try:
                            existing = region_s3.get_bucket_lifecycle_configuration(Bucket=bucket_name)
                            rules = existing.get("Rules", [])
                        except ClientError as ce:
                            if ce.response.get("Error", {}).get("Code") != "NoSuchLifecycleConfiguration":
                                raise
                            rules = []

                        rules = [existing_rule for existing_rule in rules if existing_rule.get("ID") != rule.get("ID")]
                        rules.append(rule)

                        if dry_run:
                            actions["ui_s3_lifecycle_updated"].append(f"[DRY-RUN] {bucket_name}:{rule.get('ID')}")
                            return True

                        region_s3.put_bucket_lifecycle_configuration(
                            Bucket=bucket_name,
                            LifecycleConfiguration={"Rules": rules},
                        )
                        actions["ui_s3_lifecycle_updated"].append(f"{bucket_name}:{rule.get('ID')}")
                        return True
                    except ClientError as ce:
                        actions["errors"].append(f"Failed to update lifecycle on {bucket_name}: {ce}")
                        return False

                def _apply_s3_workflow(region_s3, bucket_name: str, workflow: str, details: Dict[str, Any]) -> bool:
                    if workflow == "abort_incomplete_multipart":
                        abort_after_days = int(details.get("abort_after_days") or 7)
                        return _put_bucket_lifecycle_rule(
                            region_s3,
                            bucket_name,
                            {
                                "ID": "cost-optimizer-abort-multipart",
                                "Status": "Enabled",
                                "Filter": {"Prefix": ""},
                                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": abort_after_days},
                            },
                        )

                    if workflow == "intelligent_tiering":
                        config = {
                            "Id": "cost-optimizer-intelligent-tiering",
                            "Status": "Enabled",
                            "Filter": {"Prefix": ""},
                            "Tierings": [
                                {"Days": 90, "AccessTier": "ARCHIVE_ACCESS"},
                                {"Days": 180, "AccessTier": "DEEP_ARCHIVE_ACCESS"},
                            ],
                        }
                        try:
                            if dry_run:
                                actions["ui_s3_lifecycle_updated"].append(f"[DRY-RUN] {bucket_name}:intelligent-tiering")
                                return True

                            region_s3.put_bucket_intelligent_tiering_configuration(
                                Bucket=bucket_name,
                                Id=config["Id"],
                                IntelligentTieringConfiguration=config,
                            )
                            actions["ui_s3_lifecycle_updated"].append(f"{bucket_name}:intelligent-tiering")
                            return True
                        except ClientError as ce:
                            actions["errors"].append(f"Failed to enable intelligent tiering on {bucket_name}: {ce}")
                            return False

                    if workflow == "enable_bucket_key":
                        try:
                            encryption = region_s3.get_bucket_encryption(Bucket=bucket_name)
                            rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
                        except ClientError as ce:
                            actions["errors"].append(f"Failed to read bucket encryption on {bucket_name}: {ce}")
                            return False

                        kms_rules_found = False
                        for rule in rules:
                            default_cfg = rule.get("ApplyServerSideEncryptionByDefault", {})
                            if default_cfg.get("SSEAlgorithm") == "aws:kms":
                                rule["BucketKeyEnabled"] = True
                                kms_rules_found = True

                        if not kms_rules_found:
                            actions["errors"].append(
                                f"Skipped bucket-key enablement for {bucket_name}: bucket is not configured for SSE-KMS"
                            )
                            return False

                        if dry_run:
                            actions["ui_s3_lifecycle_updated"].append(f"[DRY-RUN] {bucket_name}:bucket-key")
                            return True

                        try:
                            region_s3.put_bucket_encryption(
                                Bucket=bucket_name,
                                ServerSideEncryptionConfiguration={"Rules": rules},
                            )
                            actions["ui_s3_lifecycle_updated"].append(f"{bucket_name}:bucket-key")
                            return True
                        except ClientError as ce:
                            actions["errors"].append(f"Failed to enable bucket key on {bucket_name}: {ce}")
                            return False

                    transition_days = int(details.get("transition_after_days") or 30)
                    glacier_days = int(details.get("glacier_after_days") or 90)
                    return _put_bucket_lifecycle_rule(
                        region_s3,
                        bucket_name,
                        {
                            "ID": "cost-optimizer-storage-transitions",
                            "Status": "Enabled",
                            "Filter": {"Prefix": ""},
                            "Transitions": [
                                {"Days": transition_days, "StorageClass": "STANDARD_IA"},
                                {"Days": glacier_days, "StorageClass": "GLACIER_IR"},
                            ],
                        },
                    )

                def _delete_s3_bucket(region_s3, bucket_name: str) -> bool:
                    try:
                        probe = region_s3.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
                        if probe.get("KeyCount", 0) > 0:
                            actions["errors"].append(
                                f"Failed to delete S3 bucket {bucket_name}: bucket is not empty"
                            )
                            return False

                        if dry_run:
                            actions["ui_s3_deleted"].append(f"[DRY-RUN] {bucket_name}")
                            return True

                        region_s3.delete_bucket(Bucket=bucket_name)
                        actions["ui_s3_deleted"].append(bucket_name)
                        return True
                    except ClientError as ce:
                        actions["errors"].append(f"Failed to delete S3 bucket {bucket_name}: {ce}")
                        return False

                for item in payload.get("items", []):
                    item_type = item.get("type")
                    resource_id = item.get("id")
                    user_action = item.get("user_action")
                    details = item.get("details") or {}
                    item_region = item.get("region") or os.environ.get("AWS_REGION") or "us-east-1"
                    target_account = item.get("account_id")

                    if not resource_id or not user_action:
                        continue

                    if target_account and target_account != current_account_id:
                        actions["errors"].append(
                            f"{resource_id}: target account {target_account} differs from scheduler account {current_account_id}; cross-account execution not configured"
                        )
                        continue

                    if item_type == "EBS Volume" and user_action == "remove":
                        _delete_ebs_volume(_regional_ec2(item_region), resource_id)
                        continue

                    if item_type == "EBS Snapshot" and user_action == "remove":
                        _delete_ebs_snapshot(_regional_ec2(item_region), resource_id)
                        continue

                    if item_type == "S3 Bucket":
                        workflow = details.get("s3_workflow") or "lifecycle_transition"
                        if user_action == "lifecycle":
                            _apply_s3_workflow(_regional_s3(item_region), resource_id, workflow, details)
                            continue
                        if user_action == "remove":
                            if not details.get("allow_remove") and workflow != "safe_delete":
                                actions["errors"].append(
                                    f"{resource_id}: S3 remove is only allowed for safe-delete candidates"
                                )
                                continue
                            _delete_s3_bucket(_regional_s3(item_region), resource_id)
                            continue

                    if item_type != "EC2 Instance":
                        continue

                    instance_id = resource_id

                    current_state = schedule_manager.verify_resource_current_state(instance_id)
                    if current_state is None:
                        actions["errors"].append(f"{instance_id}: not found for UI action")
                        continue

                    if user_action == "schedule" and schedule_enabled:
                        if _in_business_window(schedule_now):
                            if current_state == "stopped":
                                if schedule_manager.start_instance(instance_id, dry_run=dry_run):
                                    actions["ui_scheduled_started"].append(instance_id)
                                else:
                                    actions["errors"].append(f"Failed to start {instance_id} from UI schedule")
                        else:
                            if current_state == "running":
                                if schedule_manager.stop_instance(instance_id, dry_run=dry_run):
                                    actions["ui_scheduled_stopped"].append(instance_id)
                                else:
                                    actions["errors"].append(f"Failed to stop {instance_id} from UI schedule")

                    if user_action == "resize":
                        actions["ui_resize_recommendations"].append({
                            "instance_id": instance_id,
                            "current_type": details.get("instance_type"),
                            "recommended_type": details.get("recommended_instance_type"),
                            "stack_name": details.get("stack_name"),
                            "managed_by": details.get("managed_by"),
                        })

                for target in schedule_cfg.get("manual_targets", []):
                    instance_id = target.get("instance_id")
                    region_name = target.get("region") or os.environ.get("AWS_REGION") or "us-east-1"
                    target_account = target.get("account_id")

                    if not instance_id:
                        continue

                    if target_account and target_account != current_account_id:
                        actions["errors"].append(
                            f"{instance_id}: target account {target_account} differs from scheduler account {current_account_id}; cross-account manual scheduling not configured"
                        )
                        continue

                    region_ec2 = _regional_ec2(region_name)
                    current_state = schedule_manager.verify_resource_current_state(instance_id, ec2_api=region_ec2)
                    if current_state is None:
                        actions["errors"].append(f"{instance_id}: not found in region {region_name}")
                        continue

                    if schedule_enabled and _in_business_window(schedule_now):
                        if current_state == "stopped":
                            if schedule_manager.start_instance(instance_id, dry_run=dry_run, ec2_api=region_ec2):
                                actions["ui_scheduled_started"].append(f"{instance_id}@{region_name}")
                            else:
                                actions["errors"].append(f"Failed to start {instance_id} in {region_name}")
                    else:
                        if current_state == "running":
                            if schedule_manager.stop_instance(instance_id, dry_run=dry_run, ec2_api=region_ec2):
                                actions["ui_scheduled_stopped"].append(f"{instance_id}@{region_name}")
                            else:
                                actions["errors"].append(f"Failed to stop {instance_id} in {region_name}")

                for target in schedule_cfg.get("ebs_manual_targets", []):
                    resource_id = target.get("resource_id") or target.get("volume_id")
                    resource_type = target.get("resource_type") or target.get("type") or "EBS Volume"
                    region_name = target.get("region") or os.environ.get("AWS_REGION") or "us-east-1"
                    target_account = target.get("account_id")
                    target_action = target.get("user_action", "remove")

                    if not resource_id or target_action != "remove":
                        continue

                    if target_account and target_account != current_account_id:
                        actions["errors"].append(
                            f"{resource_id}: target account {target_account} differs from scheduler account {current_account_id}; cross-account manual EBS execution not configured"
                        )
                        continue

                    regional_ec2 = _regional_ec2(region_name)
                    if resource_type == "EBS Snapshot":
                        _delete_ebs_snapshot(regional_ec2, resource_id)
                    else:
                        _delete_ebs_volume(regional_ec2, resource_id)

                for target in schedule_cfg.get("s3_manual_targets", []):
                    bucket_name = target.get("bucket_name") or target.get("resource_id")
                    workflow = target.get("s3_workflow") or target.get("workflow") or "lifecycle_transition"
                    region_name = target.get("region") or os.environ.get("AWS_REGION") or "us-east-1"
                    target_account = target.get("account_id")
                    target_action = target.get("user_action", "lifecycle")

                    if not bucket_name or target_action != "lifecycle":
                        continue

                    if target_account and target_account != current_account_id:
                        actions["errors"].append(
                            f"{bucket_name}: target account {target_account} differs from scheduler account {current_account_id}; cross-account manual S3 execution not configured"
                        )
                        continue

                    _apply_s3_workflow(_regional_s3(region_name), bucket_name, workflow, target)

            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "NoSuchKey":
                    logger.info(f"No UI actions found at s3://{decisions_bucket}/{decisions_key}")
                else:
                    raise
            except Exception as ui_action_error:
                logger.error(f"Failed to process UI actions: {ui_action_error}")
                actions["errors"].append(f"UI actions processing failed: {ui_action_error}")
        
        # =====================
        # Process all schedules
        # =====================
        for schedule_name in ScheduleManager.SCHEDULES.keys():
            logger.info(f"Processing schedule: {schedule_name}")
            
            instances = schedule_manager.get_instances_to_schedule(schedule_name)
            logger.info(f"Found {len(instances)} instances with schedule: {schedule_name}")
            
            for instance in instances:
                instance_id = instance["instance_id"]
                analysis_state = instance["state"]
                
                # Determine desired state
                should_stop = schedule_manager.should_be_stopped(schedule_name, current_time)
                should_start = schedule_manager.should_be_started(schedule_name, current_time)
                
                logger.info(
                    f"{instance_id} ({analysis_state}): "
                    f"should_stop={should_stop}, should_start={should_start}"
                )
                
                # Issue #8: Re-verify live instance state before acting to
                # guard against races between analysis and execution time.
                live_state = schedule_manager.verify_resource_current_state(instance_id)
                if live_state is None:
                    logger.warning(
                        f"Instance {instance_id} not found at execution time — skipping"
                    )
                    actions["errors"].append(
                        f"Instance {instance_id} not found at execution time"
                    )
                    continue
                if live_state != analysis_state:
                    logger.warning(
                        f"Instance {instance_id} state changed from "
                        f"'{analysis_state}' to '{live_state}' since analysis — skipping"
                    )
                    if dry_run:
                        actions["dry_run_would_skip_state_changed"].append(instance_id)
                    else:
                        actions["errors"].append(
                            f"{instance_id}: state changed {analysis_state}→{live_state}"
                        )
                    continue

                # Issue #6: In dry-run mode, simulate the outcome but do NOT
                # persist any decisions to S3 or take real actions.
                if dry_run:
                    if should_stop and live_state == "running":
                        actions["dry_run_would_stop"].append(instance_id)
                        logger.info(f"[DRY-RUN] Would stop instance: {instance_id}")
                    elif should_start and live_state == "stopped":
                        actions["dry_run_would_start"].append(instance_id)
                        logger.info(f"[DRY-RUN] Would start instance: {instance_id}")
                    continue

                # Take real action
                if should_stop and live_state == "running":
                    if schedule_manager.stop_instance(instance_id, dry_run=False):
                        actions["stopped"].append(instance_id)
                    else:
                        actions["errors"].append(f"Failed to stop {instance_id}")
                
                elif should_start and live_state == "stopped":
                    if schedule_manager.start_instance(instance_id, dry_run=False):
                        actions["started"].append(instance_id)
                    else:
                        actions["errors"].append(f"Failed to start {instance_id}")
        
        # =====================
        # Send notification
        # =====================
        if sns_topic:
            message = f"""
AWS Cost Optimizer - Scheduler Report
======================================

Timestamp: {current_time.isoformat()}
Timezone: {timezone_str}
Dry-Run: {dry_run}

Actions Taken:
  Instances Stopped: {len(actions['stopped'])}
  {', '.join(actions['stopped']) if actions['stopped'] else '(none)'}
  
  Instances Started: {len(actions['started'])}
  {', '.join(actions['started']) if actions['started'] else '(none)'}

    UI Scheduled Stops: {len(actions['ui_scheduled_stopped'])}
    {', '.join(actions['ui_scheduled_stopped']) if actions['ui_scheduled_stopped'] else '(none)'}

    UI Scheduled Starts: {len(actions['ui_scheduled_started'])}
    {', '.join(actions['ui_scheduled_started']) if actions['ui_scheduled_started'] else '(none)'}

    UI Resize Recommendations: {len(actions['ui_resize_recommendations'])}
    {json.dumps(actions['ui_resize_recommendations'][:10], default=str) if actions['ui_resize_recommendations'] else '(none)'}

    UI EBS Deleted: {len(actions['ui_ebs_deleted'])}
    {', '.join(actions['ui_ebs_deleted']) if actions['ui_ebs_deleted'] else '(none)'}

    UI S3 Lifecycle Updated: {len(actions['ui_s3_lifecycle_updated'])}
    {', '.join(actions['ui_s3_lifecycle_updated']) if actions['ui_s3_lifecycle_updated'] else '(none)'}

    UI S3 Deleted: {len(actions['ui_s3_deleted'])}
    {', '.join(actions['ui_s3_deleted']) if actions['ui_s3_deleted'] else '(none)'}

Errors: {len(actions['errors'])}
{chr(10).join(actions['errors']) if actions['errors'] else '(none)'}
            """
            
            try:
                sns_client.publish(
                    TopicArn=sns_topic,
                    Subject="AWS Cost Optimizer - Scheduler Report",
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
                "message": "Scheduling complete",
                "timestamp": current_time.isoformat(),
                "dry_run": dry_run,
                # Real actions (only populated when dry_run=False)
                "instances_stopped": len(actions["stopped"]),
                "instances_started": len(actions["started"]),
                "ui_scheduled_stopped": len(actions["ui_scheduled_stopped"]),
                "ui_scheduled_started": len(actions["ui_scheduled_started"]),
                "ui_resize_recommendations": len(actions["ui_resize_recommendations"]),
                "ui_ebs_deleted": len(actions["ui_ebs_deleted"]),
                "ui_s3_lifecycle_updated": len(actions["ui_s3_lifecycle_updated"]),
                "ui_s3_deleted": len(actions["ui_s3_deleted"]),
                "errors": len(actions["errors"]),
                # Dry-run simulation results (only populated when dry_run=True)
                "dry_run_would_stop": len(actions["dry_run_would_stop"]),
                "dry_run_would_start": len(actions["dry_run_would_start"]),
                "dry_run_skipped_state_changed": len(
                    actions["dry_run_would_skip_state_changed"]
                ),
            })
        }
        
        logger.info(f"Scheduler execution successful: {json.dumps(response['body'])}")
        return response
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        
        # Try to send error notification
        if os.environ.get("SNS_TOPIC_ARN"):
            try:
                sns_client.publish(
                    TopicArn=os.environ.get("SNS_TOPIC_ARN"),
                    Subject="AWS Cost Optimizer - Scheduler ERROR",
                    Message=f"Scheduling failed: {str(e)}\n\nCheck CloudWatch Logs for details."
                )
            except:
                pass
        
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Scheduling failed",
                "error": str(e)
            })
        }
