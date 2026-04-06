"""
EBS Analyzer: Detect unattached volumes, old snapshots, and estimate cleanup costs.

Copyright (c) 2026 kondaparthi

Licensed under the MIT License.
"""

from typing import Dict, Any
from datetime import datetime, timedelta
from .base_analyzer import BaseAnalyzer, AnalyzerResult, Finding


class EBSAnalyzer(BaseAnalyzer):
    """Analyze EBS volumes and snapshots for cost optimization."""
    
    name = "EBSAnalyzer"
    
    def analyze(self, config: Dict[str, Any], result: AnalyzerResult, dry_run: bool = True):
        """
        Find:
        - Unattached volumes
        - Old snapshots (>90 days by default)
        - Misconfigured volume types
        """
        
        # Get config thresholds
        ebs_config = config.get("ebs", {})
        unattached_threshold_days = ebs_config.get("unattached_days", 7)
        snapshot_age_threshold_days = ebs_config.get("snapshot_age_days", 90)
        
        # Query EBS volumes
        ec2_client = self.aws_client.get_client("ec2", self.account_id)
        
        # =====================
        # 1. Find unattached volumes
        # =====================
        try:
            volumes_response = ec2_client.describe_volumes(
                Filters=[
                    {"Name": "status", "Values": ["available"]},  # Unattached
                ]
            )
            
            for volume in volumes_response.get("Volumes", []):
                volume_id = volume["VolumeId"]
                size_gb = volume["Size"]
                volume_type = volume["VolumeType"]
                create_time = volume["CreateTime"]
                tags = {t["Key"]: t["Value"] for t in volume.get("Tags", [])}
                
                # Check skip policy
                if self.skip_policy.should_skip(volume_id, tags):
                    continue
                
                # Calculate cost
                monthly_cost = self.cost_calculator.ebs_volume_cost(size_gb, volume_type)
                
                # Create finding
                finding = Finding(
                    resource_id=volume_id,
                    resource_type="EBS Volume",
                    account_id=self.account_id,
                    region=self.region,
                    issue=f"Unattached EBS volume ({volume_type}, {size_gb}GB)",
                    recommendation=f"Delete or attach to instance. Cost: ${monthly_cost:.2f}/month.",
                    severity="medium" if size_gb < 100 else "high",
                    current_monthly_cost=monthly_cost,
                    potential_savings_monthly=monthly_cost,
                    potential_savings_annual=monthly_cost * 12,
                    resource_tags=tags,
                    details={
                        "volume_type": volume_type,
                        "size_gb": size_gb,
                        "created": create_time.isoformat(),
                    }
                )
                
                result.add_finding(finding)
                self.logger.log_event(
                    "ebs_unattached_found",
                    {
                        "volume_id": volume_id,
                        "size_gb": size_gb,
                        "monthly_cost": monthly_cost
                    }
                )
        
        except Exception as e:
            result.errors.append(f"Error scanning volumes: {str(e)}")
            self.logger.log_event("ebs_volume_scan_error", {"error": str(e)}, level="ERROR")
        
        # =====================
        # 2. Find old snapshots
        # =====================
        try:
            snapshots_response = ec2_client.describe_snapshots(
                OwnerIds=["self"]
            )
            
            now = datetime.utcnow().replace(tzinfo=None)
            threshold_date = now - timedelta(days=snapshot_age_threshold_days)
            
            for snapshot in snapshots_response.get("Snapshots", []):
                snapshot_id = snapshot["SnapshotId"]
                start_time = snapshot["StartTime"].replace(tzinfo=None)
                size_gb = snapshot.get("VolumeSize", 0)
                tags = {t["Key"]: t["Value"] for t in snapshot.get("Tags", [])}
                
                # Skip if recent or matches policy
                if start_time > threshold_date:
                    continue
                if self.skip_policy.should_skip(snapshot_id, tags):
                    continue
                
                # Calculate cost
                snapshot_cost = self.cost_calculator.ebs_snapshot_cost(size_gb)
                days_old = (now - start_time).days
                
                finding = Finding(
                    resource_id=snapshot_id,
                    resource_type="EBS Snapshot",
                    account_id=self.account_id,
                    region=self.region,
                    issue=f"Old EBS snapshot ({days_old} days old, {size_gb}GB)",
                    recommendation=f"Delete if no longer needed. Cost: ${snapshot_cost:.2f}/month.",
                    severity="low",
                    current_monthly_cost=snapshot_cost,
                    potential_savings_monthly=snapshot_cost,
                    potential_savings_annual=snapshot_cost * 12,
                    resource_tags=tags,
                    details={
                        "snapshot_id": snapshot_id,
                        "size_gb": size_gb,
                        "created": start_time.isoformat(),
                        "days_old": days_old,
                    }
                )
                
                result.add_finding(finding)
                self.logger.log_event(
                    "ebs_snapshot_old_found",
                    {
                        "snapshot_id": snapshot_id,
                        "days_old": days_old,
                        "size_gb": size_gb,
                        "monthly_cost": snapshot_cost
                    }
                )
        
        except Exception as e:
            result.errors.append(f"Error scanning snapshots: {str(e)}")
            self.logger.log_event("ebs_snapshot_scan_error", {"error": str(e)}, level="ERROR")
        
        # Log summary
        self.logger.log_event(
            "ebs_analysis_summary",
            {
                "account_id": self.account_id,
                "region": self.region,
                "findings": result.total_findings,
                "annual_savings": result.total_potential_savings_annual
            }
        )
