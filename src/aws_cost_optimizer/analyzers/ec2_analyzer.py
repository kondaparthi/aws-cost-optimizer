"""
EC2 Analyzer: Detect idle instances, right-sizing opportunities, unused ASGs.

Copyright (c) 2026 kondaparthi

Licensed under the MIT License.
"""

from typing import Dict, Any
from datetime import datetime, timedelta
from .base_analyzer import BaseAnalyzer, AnalyzerResult, Finding


class EC2Analyzer(BaseAnalyzer):
    """Analyze EC2 instances for cost optimization."""
    
    name = "EC2Analyzer"
    
    def analyze(self, config: Dict[str, Any], result: AnalyzerResult, dry_run: bool = True):
        """
        Find:
        - Idle instances (CPU <5% for N days)
        - Oversized instances (underutilized)
        - Unused Auto Scaling Groups
        """
        
        # Get config thresholds
        ec2_config = config.get("ec2", {})
        idle_cpu_threshold = ec2_config.get("idle_cpu_threshold", 5)
        idle_days = ec2_config.get("idle_days", 7)
        
        # Query EC2 instances
        ec2_client = self.aws_client.get_client("ec2", self.account_id)
        cloudwatch = self.aws_client.get_client("cloudwatch", self.account_id)
        
        try:
            # =====================
            # Find idle instances
            # =====================
            instances_response = ec2_client.describe_instances(
                Filters=[
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )
            
            for reservation in instances_response.get("Reservations", []):
                for instance in reservation["Instances"]:
                    instance_id = instance["InstanceId"]
                    instance_type = instance.get("InstanceType")
                    launch_time = instance.get("LaunchTime")
                    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                    
                    # Skip if in skip policy
                    if self.skip_policy.should_skip(instance_id, tags):
                        continue
                    
                    # Get CPU metrics from CloudWatch
                    try:
                        metrics_response = cloudwatch.get_metric_statistics(
                            Namespace="AWS/EC2",
                            MetricName="CPUUtilization",
                            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                            StartTime=datetime.utcnow() - timedelta(days=idle_days),
                            EndTime=datetime.utcnow(),
                            Period=3600,  # 1 hour
                            Statistics=["Average"]
                        )
                        
                        datapoints = metrics_response.get("Datapoints", [])
                        if datapoints:
                            avg_cpu = sum(dp["Average"] for dp in datapoints) / len(datapoints)
                            
                            # If idle, create finding
                            if avg_cpu < idle_cpu_threshold:
                                monthly_cost = self._get_instance_cost(instance_type)
                                
                                finding = Finding(
                                    resource_id=instance_id,
                                    resource_type="EC2 Instance",
                                    account_id=self.account_id,
                                    region=self.region,
                                    issue=f"Idle instance - Average CPU {avg_cpu:.1f}% over {idle_days} days (threshold: {idle_cpu_threshold}%)",
                                    recommendation=f"Stop or terminate this instance. Cost: ${monthly_cost:.2f}/month.",
                                    severity="high" if avg_cpu < 1 else "medium",
                                    current_monthly_cost=monthly_cost,
                                    potential_savings_monthly=monthly_cost,
                                    potential_savings_annual=monthly_cost * 12,
                                    resource_tags=tags,
                                    details={
                                        "instance_type": instance_type,
                                        "average_cpu": round(avg_cpu, 2),
                                        "idle_days": idle_days,
                                        "launched": launch_time.isoformat() if launch_time else None,
                                    }
                                )
                                
                                result.add_finding(finding)
                                self.logger.log_event(
                                    "ec2_idle_found",
                                    {
                                        "instance_id": instance_id,
                                        "instance_type": instance_type,
                                        "avg_cpu": avg_cpu,
                                        "monthly_cost": monthly_cost
                                    }
                                )
                    
                    except Exception as metric_error:
                        self.logger.log_event(
                            "ec2_metric_error",
                            {"instance_id": instance_id, "error": str(metric_error)},
                            level="WARN"
                        )
        
        except Exception as e:
            result.errors.append(f"Error scanning EC2 instances: {str(e)}")
            self.logger.log_event("ec2_scan_error", {"error": str(e)}, level="ERROR")
        
        # =====================
        # Find unused ASGs
        # =====================
        try:
            asg_client = self.aws_client.get_client("autoscaling", self.account_id)
            asg_response = asg_client.describe_auto_scaling_groups()
            
            for asg in asg_response.get("AutoScalingGroups", []):
                asg_name = asg["AutoScalingGroupName"]
                desired_capacity = asg.get("DesiredCapacity", 0)
                current_instances = len(asg.get("Instances", []))
                
                # Skip if active
                if current_instances > 0:
                    continue
                
                # Empty ASG = unused
                asg_tags = {t["Key"]: t["Value"] for t in asg.get("Tags", [])}
                if self.skip_policy.should_skip(asg_name, asg_tags):
                    continue
                
                finding = Finding(
                    resource_id=asg_name,
                    resource_type="Auto Scaling Group",
                    account_id=self.account_id,
                    region=self.region,
                    issue=f"Unused Auto Scaling Group - 0 instances (desired: {desired_capacity})",
                    recommendation="Delete this Auto Scaling Group if no longer needed.",
                    severity="low",
                    current_monthly_cost=0.0,
                    potential_savings_monthly=0.0,
                    potential_savings_annual=0.0,
                    resource_tags=asg_tags,
                    details={
                        "desired_capacity": desired_capacity,
                        "current_instances": current_instances,
                    }
                )
                
                result.add_finding(finding)
                self.logger.log_event(
                    "ec2_asg_unused",
                    {"asg_name": asg_name, "desired_capacity": desired_capacity}
                )
        
        except Exception as e:
            result.errors.append(f"Error scanning ASGs: {str(e)}")
            self.logger.log_event("ec2_asg_error", {"error": str(e)}, level="ERROR")
        
        # Log summary
        self.logger.log_event(
            "ec2_analysis_summary",
            {
                "account_id": self.account_id,
                "region": self.region,
                "findings": result.total_findings,
                "annual_savings": result.total_potential_savings_annual
            }
        )
    
    def _get_instance_cost(self, instance_type: str) -> float:
        """Get hourly cost for instance type and multiply by monthly hours."""
        # Simplified pricing (should use AWS Pricing API in production)
        hourly_rates = {
            "t3.micro": 0.0104,
            "t3.small": 0.0208,
            "t3.medium": 0.0416,
            "t3.large": 0.0832,
            "t3.xlarge": 0.1664,
            "t2.micro": 0.0116,
            "t2.small": 0.0232,
            "t2.medium": 0.0464,
            "m5.large": 0.096,
            "m5.xlarge": 0.192,
            "c5.large": 0.085,
            "c5.xlarge": 0.17,
        }
        
        hourly_rate = hourly_rates.get(instance_type, 0.1)  # Default estimate
        monthly_hours = 730  # Average hours per month
        return hourly_rate * monthly_hours
