"""
EBS Analyzer: Detect unattached volumes, old snapshots, and estimate cleanup costs.

Copyright (c) 2026 kondaparthi

Licensed under the MIT License.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from .base_analyzer import BaseAnalyzer, AnalyzerResult, Finding


class EBSAnalyzer(BaseAnalyzer):
    """Analyze EBS volumes and snapshots for cost optimization."""
    
    name = "EBSAnalyzer"

    # ------------------------------------------------------------------ #
    # Issue #11: Explicit unattached check (guards against filter gaps)   #
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_truly_unattached(volume: Dict[str, Any]) -> bool:
        """
        Return True only if the volume is genuinely orphaned.

        A volume is NOT truly unattached when:
        - It has any active attachment records, OR
        - Any of its attachment records has DeleteOnTermination=True
          (meaning it is lifecycle-managed by an instance).
        """
        attachments = volume.get("Attachments", [])
        if not attachments:
            return True
        # Any attachment — including one with DeleteOnTermination=True — means
        # the volume is managed; do not flag it.
        for attachment in attachments:
            if attachment.get("DeleteOnTermination", False):
                return False
        return False  # Has live attachments regardless of flag

    # ------------------------------------------------------------------ #
    # Issue #2: Snapshot safety checks before flagging for deletion       #
    # ------------------------------------------------------------------ #
    def _is_snapshot_safe_to_flag(
        self, ec2_client, snapshot: Dict[str, Any]
    ) -> bool:
        """
        Return True only when the snapshot is safe to recommend for deletion.

        Checks (all logged before any decision):
        1. No other snapshot depends on it (incremental chain).
        2. It was not created by AWS Backup.
        3. It is not managed by AWS Data Lifecycle Manager (DLM).
        """
        snapshot_id = snapshot["SnapshotId"]
        tags = {t["Key"]: t["Value"] for t in snapshot.get("Tags", [])}

        # --- Check 1: Incremental chain dependency ---
        try:
            dependents_resp = ec2_client.describe_snapshots(
                Filters=[{"Name": "parent-id", "Values": [snapshot_id]}]
            )
            dependent_ids = [
                s["SnapshotId"]
                for s in dependents_resp.get("Snapshots", [])
            ]
            self.logger.log_event(
                "snapshot_chain_check",
                {"snapshot_id": snapshot_id, "dependent_count": len(dependent_ids)},
            )
            if dependent_ids:
                self.logger.log_event(
                    "snapshot_chain_dependency_found",
                    {"snapshot_id": snapshot_id, "dependents": dependent_ids},
                    level="WARN",
                )
                return False
        except Exception as chain_err:
            self.logger.log_event(
                "snapshot_chain_check_error",
                {"snapshot_id": snapshot_id, "error": str(chain_err)},
                level="WARN",
            )

        # --- Check 2: AWS Backup service marker ---
        description = snapshot.get("Description", "")
        aws_backup_markers = [
            "Created by AWS Backup",
            "AwsBackup",
        ]
        aws_backup_tag_keys = {"aws:backup:source-resource-arn", "aws:backup:vault-name"}
        is_backup_managed = (
            any(m.lower() in description.lower() for m in aws_backup_markers)
            or bool(aws_backup_tag_keys & set(tags.keys()))
        )
        self.logger.log_event(
            "snapshot_backup_check",
            {
                "snapshot_id": snapshot_id,
                "aws_backup_managed": is_backup_managed,
                "description": description[:120],
            },
        )
        if is_backup_managed:
            self.logger.log_event(
                "snapshot_aws_backup_protected",
                {"snapshot_id": snapshot_id},
                level="WARN",
            )
            return False

        # --- Check 3: AWS DLM lifecycle policy tag ---
        dlm_policy_id = tags.get("aws:dlm:lifecycle-policy-id")
        self.logger.log_event(
            "snapshot_dlm_check",
            {"snapshot_id": snapshot_id, "dlm_managed": bool(dlm_policy_id)},
        )
        if dlm_policy_id:
            self.logger.log_event(
                "snapshot_dlm_protected",
                {"snapshot_id": snapshot_id, "policy_id": dlm_policy_id},
                level="WARN",
            )
            return False

        self.logger.log_event(
            "snapshot_safe_to_flag", {"snapshot_id": snapshot_id}
        )
        return True

    def _get_snapshot_parent_tags(
        self, ec2_client, snapshot: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        """
        Return tag dicts for the snapshot's source volume and any instance
        currently attached to that volume (for parent-tag inheritance).
        """
        parent_tags: List[Dict[str, str]] = []
        volume_id = snapshot.get("VolumeId")
        if not volume_id:
            return parent_tags
        try:
            vol_resp = ec2_client.describe_volumes(VolumeIds=[volume_id])
            for vol in vol_resp.get("Volumes", []):
                vol_tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
                parent_tags.append(vol_tags)
                # Walk up to attached instance(s)
                for att in vol.get("Attachments", []):
                    inst_id = att.get("InstanceId")
                    if inst_id:
                        try:
                            inst_resp = ec2_client.describe_instances(
                                InstanceIds=[inst_id]
                            )
                            for res in inst_resp.get("Reservations", []):
                                for inst in res.get("Instances", []):
                                    inst_tags = {
                                        t["Key"]: t["Value"]
                                        for t in inst.get("Tags", [])
                                    }
                                    parent_tags.append(inst_tags)
                        except Exception:
                            pass
        except Exception as e:
            self.logger.log_event(
                "snapshot_parent_lookup_error",
                {"snapshot_id": snapshot.get("SnapshotId"), "error": str(e)},
                level="WARN",
            )
        return parent_tags

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

                # Issue #11: Explicitly verify the volume is genuinely orphaned.
                # The status=available filter already excludes in-use volumes, but
                # this guard prevents false positives if the query is ever broadened.
                if not self.is_truly_unattached(volume):
                    self.logger.log_event(
                        "ebs_volume_skip_managed_attachment",
                        {"volume_id": volume_id},
                    )
                    continue

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

                # Issue #2: Verify snapshot is not part of a chain or managed service
                if not self._is_snapshot_safe_to_flag(ec2_client, snapshot):
                    continue

                # Issue #5: Check parent resource tags (volume → instance) for protection
                parent_tags = self._get_snapshot_parent_tags(ec2_client, snapshot)
                if self.skip_policy.should_protect_resource(snapshot_id, tags, parent_tags):
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
