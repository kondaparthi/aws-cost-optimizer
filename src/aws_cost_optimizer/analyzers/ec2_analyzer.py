"""
EC2 Analyzer: Detect idle instances, right-sizing opportunities, unused ASGs.

Copyright (c) 2026 kondaparthi

Licensed under the MIT License.
"""

from typing import Dict, Any, List
from datetime import datetime, timedelta
from .base_analyzer import BaseAnalyzer, AnalyzerResult, Finding

# Issue #1: 95th-percentile spike ceiling for idle classification
_IDLE_P95_CPU_CEILING = 8.0  # percent
_SCHEDULE_OFF_HOURS_CPU_MAX = 5.0  # percent
_SCHEDULE_OFF_HOURS_CPU_AVG = 2.0  # percent


class EC2Analyzer(BaseAnalyzer):
    """Analyze EC2 instances for cost optimization."""
    
    name = "EC2Analyzer"

    # ------------------------------------------------------------------ #
    # Issue #7: CloudWatch metric completeness validation                 #
    # ------------------------------------------------------------------ #
    def validate_metric_completeness(
        self,
        datapoints: List[Dict[str, Any]],
        days: int,
        period_seconds: int = 3600,
    ) -> Dict[str, Any]:
        """
        Check whether a CloudWatch metric dataset is at least 95% complete.

        Args:
            datapoints:      Raw Datapoints list from get_metric_statistics.
            days:            Analysis window in days.
            period_seconds:  CloudWatch period used (default 3600 = 1 hour).

        Returns:
            dict:
                confidence       – 'high' (≥95% complete) or 'low' (<95%)
                expected         – expected number of datapoints
                actual           – actual number of datapoints received
                completeness_pct – percentage of expected data present
                gaps             – list of detected time-gap dicts (up to 10)
        """
        expected = (days * 24 * 3600) // period_seconds
        actual = len(datapoints)
        completeness = actual / expected if expected > 0 else 0.0
        confidence = "high" if completeness >= 0.95 else "low"

        gaps: List[Dict[str, Any]] = []
        if confidence == "low" and actual > 1:
            sorted_dps = sorted(datapoints, key=lambda dp: dp["Timestamp"])
            for i in range(1, len(sorted_dps)):
                gap_s = (
                    sorted_dps[i]["Timestamp"] - sorted_dps[i - 1]["Timestamp"]
                ).total_seconds()
                if gap_s > period_seconds * 2:
                    gaps.append({
                        "from": sorted_dps[i - 1]["Timestamp"].isoformat(),
                        "to": sorted_dps[i]["Timestamp"].isoformat(),
                        "gap_hours": round(gap_s / 3600, 1),
                    })
                    if len(gaps) >= 10:
                        break

        return {
            "confidence": confidence,
            "expected": expected,
            "actual": actual,
            "completeness_pct": round(completeness * 100, 1),
            "gaps": gaps,
        }

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
                    
                    # Issue #1 + #7: Fetch average AND 95th-percentile CPU.
                    # Two separate calls are required because get_metric_statistics
                    # accepts either Statistics OR ExtendedStatistics, not both.
                    try:
                        start_time = datetime.utcnow() - timedelta(days=idle_days)
                        end_time = datetime.utcnow()
                        cw_dims = [{"Name": "InstanceId", "Value": instance_id}]

                        avg_response = cloudwatch.get_metric_statistics(
                            Namespace="AWS/EC2",
                            MetricName="CPUUtilization",
                            Dimensions=cw_dims,
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=3600,
                            Statistics=["Average"],
                        )
                        p95_response = cloudwatch.get_metric_statistics(
                            Namespace="AWS/EC2",
                            MetricName="CPUUtilization",
                            Dimensions=cw_dims,
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=3600,
                            ExtendedStatistics=["p95"],
                        )

                        avg_datapoints = avg_response.get("Datapoints", [])
                        p95_datapoints = p95_response.get("Datapoints", [])

                        if avg_datapoints and p95_datapoints:
                            # Issue #7: Skip idle detection if data coverage is low
                            completeness = self.validate_metric_completeness(
                                avg_datapoints, idle_days
                            )
                            if completeness["confidence"] == "low":
                                self.logger.log_event(
                                    "ec2_idle_skipped_incomplete_metrics",
                                    {
                                        "instance_id": instance_id,
                                        "expected_datapoints": completeness["expected"],
                                        "actual_datapoints": completeness["actual"],
                                        "completeness_pct": completeness["completeness_pct"],
                                        "gaps": completeness["gaps"],
                                    },
                                    level="WARN",
                                )
                                continue

                            avg_cpu = (
                                sum(dp["Average"] for dp in avg_datapoints)
                                / len(avg_datapoints)
                            )
                            # Issue #1: Take the maximum p95 across all hourly
                            # buckets to catch any sustained spike periods.
                            p95_cpu = max(
                                dp.get("ExtendedStatistics", {}).get("p95", 0.0)
                                for dp in p95_datapoints
                            )

                            off_hours_metrics = self._off_hours_metrics(avg_datapoints)

                            # Issue #1: Both conditions must hold to flag idle.
                            # avg < threshold guards against sustained low use,
                            # p95 < ceiling guards against sporadic spikes being
                            # misclassified as "always idle".
                            if avg_cpu < idle_cpu_threshold and p95_cpu < _IDLE_P95_CPU_CEILING:
                                monthly_cost = self._get_instance_cost(instance_type)
                                
                                finding = Finding(
                                    resource_id=instance_id,
                                    resource_type="EC2 Instance",
                                    account_id=self.account_id,
                                    region=self.region,
                                    issue=(
                                        f"Idle instance - Average CPU {avg_cpu:.1f}% "
                                        f"(p95: {p95_cpu:.1f}%) over {idle_days} days "
                                        f"(threshold: avg<{idle_cpu_threshold}%, p95<{_IDLE_P95_CPU_CEILING}%)"
                                    ),
                                    recommendation=f"Stop or terminate this instance. Cost: ${monthly_cost:.2f}/month.",
                                    severity="high" if avg_cpu < 1 else "medium",
                                    current_monthly_cost=monthly_cost,
                                    potential_savings_monthly=monthly_cost,
                                    potential_savings_annual=monthly_cost * 12,
                                    resource_tags=tags,
                                    details={
                                        "instance_type": instance_type,
                                        "average_cpu": round(avg_cpu, 2),
                                        "p95_cpu": round(p95_cpu, 2),
                                        "idle_days": idle_days,
                                        "metric_completeness_pct": completeness["completeness_pct"],
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
                                continue

                            # Instance is active in business hours but mostly idle off-hours/weekends.
                            # Recommend schedule policy when off-hours usage is consistently low.
                            if (
                                off_hours_metrics["samples"] >= 24
                                and off_hours_metrics["max_cpu"] <= _SCHEDULE_OFF_HOURS_CPU_MAX
                                and off_hours_metrics["avg_cpu"] <= _SCHEDULE_OFF_HOURS_CPU_AVG
                            ):
                                monthly_cost = self._get_instance_cost(instance_type)
                                schedule_savings = monthly_cost * 0.45  # Typical off-hours savings estimate
                                finding = Finding(
                                    resource_id=instance_id,
                                    resource_type="EC2 Instance",
                                    account_id=self.account_id,
                                    region=self.region,
                                    issue=(
                                        "Low off-hours/weekend usage detected - "
                                        f"off-hours avg CPU {off_hours_metrics['avg_cpu']:.1f}%, "
                                        f"max {off_hours_metrics['max_cpu']:.1f}%"
                                    ),
                                    recommendation=(
                                        "Set a business-hours schedule from UI. "
                                        "Instance can be stopped off-hours/weekends and started during business hours."
                                    ),
                                    severity="high",
                                    current_monthly_cost=monthly_cost,
                                    potential_savings_monthly=schedule_savings,
                                    potential_savings_annual=schedule_savings * 12,
                                    resource_tags=tags,
                                    details={
                                        "instance_type": instance_type,
                                        "recommended_action": "schedule",
                                        "off_hours_avg_cpu": round(off_hours_metrics["avg_cpu"], 2),
                                        "off_hours_max_cpu": round(off_hours_metrics["max_cpu"], 2),
                                        "off_hours_samples": off_hours_metrics["samples"],
                                        "business_start": "08:00",
                                        "business_end": "18:00",
                                        "off_days": [5, 6],
                                        "timezone": "UTC",
                                    },
                                )
                                result.add_finding(finding)
                                self.logger.log_event(
                                    "ec2_schedule_recommendation",
                                    {
                                        "instance_id": instance_id,
                                        "off_hours_avg_cpu": off_hours_metrics["avg_cpu"],
                                        "off_hours_max_cpu": off_hours_metrics["max_cpu"],
                                        "monthly_savings": schedule_savings,
                                    }
                                )

                            # Underutilized but not strictly idle: right-size recommendation.
                            if avg_cpu < 25 and p95_cpu < 45:
                                recommended_type = self._recommend_downsize(instance_type)
                                if recommended_type and recommended_type != instance_type:
                                    current_monthly = self._get_instance_cost(instance_type)
                                    recommended_monthly = self._get_instance_cost(recommended_type)
                                    savings_monthly = max(0.0, current_monthly - recommended_monthly)

                                    stack_ctx = self._stack_context(tags)
                                    recommendation = (
                                        f"Right-size from {instance_type} to {recommended_type}. "
                                        f"Estimated savings: ${savings_monthly:.2f}/month."
                                    )
                                    if stack_ctx["stack_name"]:
                                        recommendation += (
                                            f" Update stack '{stack_ctx['stack_name']}' to avoid drift."
                                        )
                                    else:
                                        recommendation += " Use migration steps in details before change."

                                    finding = Finding(
                                        resource_id=instance_id,
                                        resource_type="EC2 Instance",
                                        account_id=self.account_id,
                                        region=self.region,
                                        issue=(
                                            f"Underutilized instance - Average CPU {avg_cpu:.1f}% "
                                            f"(p95: {p95_cpu:.1f}%) over {idle_days} days"
                                        ),
                                        recommendation=recommendation,
                                        severity="medium",
                                        current_monthly_cost=current_monthly,
                                        potential_savings_monthly=savings_monthly,
                                        potential_savings_annual=savings_monthly * 12,
                                        resource_tags=tags,
                                        details={
                                            "instance_type": instance_type,
                                            "recommended_instance_type": recommended_type,
                                            "average_cpu": round(avg_cpu, 2),
                                            "p95_cpu": round(p95_cpu, 2),
                                            "idle_days": idle_days,
                                            "managed_by": stack_ctx["managed_by"],
                                            "stack_name": stack_ctx["stack_name"],
                                            "migration_instructions": stack_ctx["migration_instructions"],
                                        },
                                    )
                                    result.add_finding(finding)
                                    self.logger.log_event(
                                        "ec2_rightsize_recommendation",
                                        {
                                            "instance_id": instance_id,
                                            "current_type": instance_type,
                                            "recommended_type": recommended_type,
                                            "monthly_savings": savings_monthly,
                                            "stack_name": stack_ctx["stack_name"],
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

    def _recommend_downsize(self, instance_type: str) -> str:
        """Recommend one size down within common EC2 families."""
        downsizing_map = {
            "t3.xlarge": "t3.large",
            "t3.large": "t3.medium",
            "t3.medium": "t3.small",
            "t3.small": "t3.micro",
            "m5.4xlarge": "m5.2xlarge",
            "m5.2xlarge": "m5.xlarge",
            "m5.xlarge": "m5.large",
            "c5.4xlarge": "c5.2xlarge",
            "c5.2xlarge": "c5.xlarge",
            "c5.xlarge": "c5.large",
            "r5.4xlarge": "r5.2xlarge",
            "r5.2xlarge": "r5.xlarge",
            "r5.xlarge": "r5.large",
        }
        return downsizing_map.get(instance_type, instance_type)

    def _stack_context(self, tags: Dict[str, str]) -> Dict[str, str]:
        """Extract automation ownership and migration guidance from tags."""
        stack_name = tags.get("aws:cloudformation:stack-name")
        if stack_name:
            return {
                "managed_by": "cloudformation",
                "stack_name": stack_name,
                "migration_instructions": (
                    f"Update EC2 instance type in CloudFormation stack '{stack_name}', "
                    "deploy change set, then verify replacement/update policy."
                ),
            }

        managed_by = (tags.get("managed-by") or tags.get("ManagedBy") or "manual").lower()
        if "terraform" in managed_by:
            return {
                "managed_by": "terraform",
                "stack_name": tags.get("terraform:workspace") or "terraform-workspace",
                "migration_instructions": (
                    "Update instance_type in Terraform code, run terraform plan, "
                    "review impact, then apply during a maintenance window."
                ),
            }

        return {
            "managed_by": "manual",
            "stack_name": "",
            "migration_instructions": (
                "Create AMI/snapshot backup, stop instance, change instance type, "
                "start instance, validate app health and rollback plan."
            ),
        }

    def _off_hours_metrics(self, datapoints: List[Dict[str, Any]]) -> Dict[str, float]:
        """Compute CPU usage stats outside business hours (08:00-18:00 weekdays)."""
        off_hours_values: List[float] = []

        for dp in datapoints:
            ts = dp.get("Timestamp")
            avg = dp.get("Average")
            if ts is None or avg is None:
                continue

            hour_min = ts.strftime("%H:%M")
            weekday = ts.weekday()
            is_weekend = weekday in (5, 6)
            in_business_window = (weekday < 5) and ("08:00" <= hour_min < "18:00")

            if is_weekend or not in_business_window:
                off_hours_values.append(float(avg))

        if not off_hours_values:
            return {"samples": 0, "avg_cpu": 0.0, "max_cpu": 0.0}

        return {
            "samples": len(off_hours_values),
            "avg_cpu": sum(off_hours_values) / len(off_hours_values),
            "max_cpu": max(off_hours_values),
        }
