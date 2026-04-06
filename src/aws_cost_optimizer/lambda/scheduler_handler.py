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
    
    def stop_instance(self, instance_id: str, dry_run: bool = False) -> bool:
        """Stop an EC2 instance."""
        try:
            if dry_run:
                logger.info(f"[DRY-RUN] Would stop instance: {instance_id}")
                return True
            
            ec2_client.stop_instances(InstanceIds=[instance_id])
            logger.info(f"Stopped instance: {instance_id}")
            return True
        
        except Exception as e:
            logger.error(f"Error stopping instance {instance_id}: {str(e)}")
            return False
    
    def start_instance(self, instance_id: str, dry_run: bool = False) -> bool:
        """Start an EC2 instance."""
        try:
            if dry_run:
                logger.info(f"[DRY-RUN] Would start instance: {instance_id}")
                return True
            
            ec2_client.start_instances(InstanceIds=[instance_id])
            logger.info(f"Started instance: {instance_id}")
            return True
        
        except Exception as e:
            logger.error(f"Error starting instance {instance_id}: {str(e)}")
            return False


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
            "errors": []
        }
        
        # =====================
        # Process all schedules
        # =====================
        for schedule_name in ScheduleManager.SCHEDULES.keys():
            logger.info(f"Processing schedule: {schedule_name}")
            
            instances = schedule_manager.get_instances_to_schedule(schedule_name)
            logger.info(f"Found {len(instances)} instances with schedule: {schedule_name}")
            
            for instance in instances:
                instance_id = instance["instance_id"]
                current_state = instance["state"]
                
                # Determine desired state
                should_stop = schedule_manager.should_be_stopped(schedule_name, current_time)
                should_start = schedule_manager.should_be_started(schedule_name, current_time)
                
                logger.info(
                    f"{instance_id} ({current_state}): "
                    f"should_stop={should_stop}, should_start={should_start}"
                )
                
                # Take action if needed
                if should_stop and current_state == "running":
                    if schedule_manager.stop_instance(instance_id, dry_run=dry_run):
                        actions["stopped"].append(instance_id)
                    else:
                        actions["errors"].append(f"Failed to stop {instance_id}")
                
                elif should_start and current_state == "stopped":
                    if schedule_manager.start_instance(instance_id, dry_run=dry_run):
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
                "instances_stopped": len(actions["stopped"]),
                "instances_started": len(actions["started"]),
                "errors": len(actions["errors"]),
                "dry_run": dry_run
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
