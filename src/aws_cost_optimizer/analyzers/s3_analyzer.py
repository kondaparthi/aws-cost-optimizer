"""S3 Analyzer: detect lifecycle, tiering, multipart, and stale bucket opportunities."""

from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from botocore.exceptions import ClientError
from .base_analyzer import BaseAnalyzer, AnalyzerResult, Finding

INCOMPLETE_UPLOAD_THRESHOLD_HOURS = 168
UNUSED_BUCKET_THRESHOLD_DAYS = 365 * 3
DEFAULT_STORAGE_LIFECYCLE_SAVINGS_RATIO = 0.45
DEFAULT_INTELLIGENT_TIERING_SAVINGS_RATIO = 0.25


class S3Analyzer(BaseAnalyzer):
    """Analyze S3 buckets for cost optimization."""

    name = "S3Analyzer"

    def analyze(self, config: Dict[str, Any], result: AnalyzerResult, dry_run: bool = True):
        s3_config = config.get("s3", {})
        multipart_age_days = int(s3_config.get("multipart_age_days", 7))
        check_multipart = bool(s3_config.get("check_incomplete_multipart", True))
        recommend_lifecycle = bool(s3_config.get("recommend_lifecycle_policies", True))
        recommend_intelligent_tiering = bool(s3_config.get("recommend_intelligent_tiering", True))
        recommend_bucket_key = bool(s3_config.get("recommend_bucket_key", True))
        recommend_unused_delete = bool(s3_config.get("recommend_unused_bucket_delete", True))
        lifecycle_min_size_gb = float(s3_config.get("lifecycle_min_size_gb", 100))
        intelligent_tiering_min_size_gb = float(s3_config.get("intelligent_tiering_min_size_gb", 128))
        unused_bucket_days = int(s3_config.get("unused_bucket_days", UNUSED_BUCKET_THRESHOLD_DAYS))
        object_scan_limit = int(s3_config.get("object_scan_limit", 500))

        s3_client = self.aws_client.get_client("s3", self.account_id)

        try:
            response = s3_client.list_buckets()
            buckets = response.get("Buckets", [])

            for bucket in buckets:
                bucket_name = bucket["Name"]
                bucket_created = self._normalize_datetime(bucket.get("CreationDate"))

                try:
                    location_response = s3_client.get_bucket_location(Bucket=bucket_name)
                    bucket_region = location_response.get("LocationConstraint") or "us-east-1"
                    if bucket_region != self.region:
                        continue
                except Exception:
                    continue

                tags = self._get_bucket_tags(s3_client, bucket_name)
                if self.skip_policy.should_skip(bucket_name, tags):
                    continue

                lifecycle_config = self._get_lifecycle_configuration(s3_client, bucket_name)
                encryption_config = self._get_bucket_encryption_configuration(s3_client, bucket_name)
                has_abort_rule = self._has_abort_incomplete_rule(lifecycle_config)
                has_transition_rule = self._has_transition_rule(lifecycle_config)
                has_intelligent_tiering = self._has_intelligent_tiering_configuration(s3_client, bucket_name)
                bucket_encryption_state = self._get_bucket_encryption_state(encryption_config)
                bucket_stats = self._inspect_bucket_objects(s3_client, bucket_name, object_scan_limit)

                if check_multipart:
                    self._add_multipart_finding(
                        s3_client=s3_client,
                        bucket_name=bucket_name,
                        tags=tags,
                        multipart_age_days=multipart_age_days,
                        has_abort_rule=has_abort_rule,
                        result=result,
                    )

                if recommend_lifecycle:
                    self._add_lifecycle_finding(
                        bucket_name=bucket_name,
                        tags=tags,
                        bucket_stats=bucket_stats,
                        lifecycle_min_size_gb=lifecycle_min_size_gb,
                        has_transition_rule=has_transition_rule,
                        result=result,
                    )

                if recommend_intelligent_tiering:
                    self._add_intelligent_tiering_finding(
                        bucket_name=bucket_name,
                        tags=tags,
                        bucket_stats=bucket_stats,
                        intelligent_tiering_min_size_gb=intelligent_tiering_min_size_gb,
                        has_intelligent_tiering=has_intelligent_tiering,
                        result=result,
                    )

                if recommend_bucket_key:
                    self._add_bucket_key_finding(
                        bucket_name=bucket_name,
                        tags=tags,
                        bucket_stats=bucket_stats,
                        encryption_state=bucket_encryption_state,
                        result=result,
                    )

                if recommend_unused_delete:
                    self._add_unused_bucket_finding(
                        bucket_name=bucket_name,
                        tags=tags,
                        bucket_stats=bucket_stats,
                        bucket_created=bucket_created,
                        unused_bucket_days=unused_bucket_days,
                        result=result,
                    )

        except Exception as e:
            result.errors.append(f"Error scanning S3: {str(e)}")
            self.logger.log_event("s3_scan_error", {"error": str(e)}, level="ERROR")

        self.logger.log_event(
            "s3_analysis_summary",
            {
                "account_id": self.account_id,
                "region": self.region,
                "findings": result.total_findings,
                "annual_savings": result.total_potential_savings_annual,
            },
        )

    def _add_multipart_finding(
        self,
        s3_client,
        bucket_name: str,
        tags: Dict[str, str],
        multipart_age_days: int,
        has_abort_rule: bool,
        result: AnalyzerResult,
    ) -> None:
        if has_abort_rule:
            return

        try:
            multipart_response = s3_client.list_multipart_uploads(Bucket=bucket_name)
            uploads = multipart_response.get("Uploads", [])
        except Exception as exc:
            self.logger.log_event(
                "s3_multipart_error",
                {"bucket": bucket_name, "error": str(exc)},
                level="WARN",
            )
            return

        current_time = datetime.utcnow()
        threshold_hours = multipart_age_days * 24
        old_uploads = []
        total_size_gb = 0.0

        for upload in uploads:
            initiated = self._normalize_datetime(upload.get("Initiated"))
            if not initiated:
                continue
            age_hours = (current_time - initiated).total_seconds() / 3600
            if age_hours < threshold_hours:
                continue
            old_uploads.append(upload)
            total_size_gb += 0.5

        if not old_uploads:
            return

        monthly_cost = self.cost_calculator.s3_storage_cost(total_size_gb, "standard")
        finding = Finding(
            resource_id=bucket_name,
            resource_type="S3 Bucket",
            account_id=self.account_id,
            region=self.region,
            issue=(
                f"Incomplete multipart uploads - {len(old_uploads)} uploads older than {threshold_hours} hours"
            ),
            recommendation=(
                "Apply an abort-incomplete-multipart lifecycle rule to clean up failed uploads. "
                f"Estimated savings: ${monthly_cost:.2f}/month."
            ),
            severity="medium" if total_size_gb >= 10 else "low",
            current_monthly_cost=monthly_cost,
            potential_savings_monthly=monthly_cost,
            potential_savings_annual=monthly_cost * 12,
            resource_tags=tags,
            details={
                "bucket_name": bucket_name,
                "recommended_action": "lifecycle",
                "s3_workflow": "abort_incomplete_multipart",
                "allowed_actions": ["notify", "lifecycle"],
                "incomplete_count": len(old_uploads),
                "estimated_size_gb": round(total_size_gb, 2),
                "abort_after_days": multipart_age_days,
                "supports_custom": True,
            },
        )
        result.add_finding(finding)

    def _add_lifecycle_finding(
        self,
        bucket_name: str,
        tags: Dict[str, str],
        bucket_stats: Dict[str, Any],
        lifecycle_min_size_gb: float,
        has_transition_rule: bool,
        result: AnalyzerResult,
    ) -> None:
        if has_transition_rule:
            return

        estimated_size_gb = bucket_stats.get("estimated_size_gb", 0.0)
        if estimated_size_gb < lifecycle_min_size_gb:
            return

        current_monthly_cost = self.cost_calculator.s3_storage_cost(estimated_size_gb, "standard")
        savings_monthly = current_monthly_cost * DEFAULT_STORAGE_LIFECYCLE_SAVINGS_RATIO
        finding = Finding(
            resource_id=bucket_name,
            resource_type="S3 Bucket",
            account_id=self.account_id,
            region=self.region,
            issue=(
                f"Bucket stores about {estimated_size_gb:.1f}GB in Standard storage without a lifecycle transition policy"
            ),
            recommendation=(
                "Set lifecycle transitions for colder objects to cheaper storage classes. "
                f"Estimated savings: ${savings_monthly:.2f}/month."
            ),
            severity="medium",
            current_monthly_cost=current_monthly_cost,
            potential_savings_monthly=savings_monthly,
            potential_savings_annual=savings_monthly * 12,
            resource_tags=tags,
            details={
                "bucket_name": bucket_name,
                "recommended_action": "lifecycle",
                "s3_workflow": "lifecycle_transition",
                "allowed_actions": ["notify", "lifecycle"],
                "estimated_size_gb": round(estimated_size_gb, 2),
                "object_count_sampled": bucket_stats.get("object_count_sampled", 0),
                "scan_truncated": bucket_stats.get("scan_truncated", False),
                "lifecycle_template": "storage_savings",
                "supports_custom": True,
            },
        )
        result.add_finding(finding)

    def _add_intelligent_tiering_finding(
        self,
        bucket_name: str,
        tags: Dict[str, str],
        bucket_stats: Dict[str, Any],
        intelligent_tiering_min_size_gb: float,
        has_intelligent_tiering: bool,
        result: AnalyzerResult,
    ) -> None:
        if has_intelligent_tiering:
            return

        estimated_size_gb = bucket_stats.get("estimated_size_gb", 0.0)
        if estimated_size_gb < intelligent_tiering_min_size_gb:
            return

        current_monthly_cost = self.cost_calculator.s3_storage_cost(estimated_size_gb, "standard")
        savings_monthly = current_monthly_cost * DEFAULT_INTELLIGENT_TIERING_SAVINGS_RATIO
        finding = Finding(
            resource_id=bucket_name,
            resource_type="S3 Bucket",
            account_id=self.account_id,
            region=self.region,
            issue=(
                f"Bucket stores about {estimated_size_gb:.1f}GB without Intelligent-Tiering enabled"
            ),
            recommendation=(
                "Enable Intelligent-Tiering for automatically changing access patterns. "
                f"Estimated savings: ${savings_monthly:.2f}/month."
            ),
            severity="medium",
            current_monthly_cost=current_monthly_cost,
            potential_savings_monthly=savings_monthly,
            potential_savings_annual=savings_monthly * 12,
            resource_tags=tags,
            details={
                "bucket_name": bucket_name,
                "recommended_action": "lifecycle",
                "s3_workflow": "intelligent_tiering",
                "allowed_actions": ["notify", "lifecycle"],
                "estimated_size_gb": round(estimated_size_gb, 2),
                "object_count_sampled": bucket_stats.get("object_count_sampled", 0),
                "scan_truncated": bucket_stats.get("scan_truncated", False),
                "lifecycle_template": "intelligent_tiering",
                "supports_custom": True,
            },
        )
        result.add_finding(finding)

    def _add_unused_bucket_finding(
        self,
        bucket_name: str,
        tags: Dict[str, str],
        bucket_stats: Dict[str, Any],
        bucket_created: Optional[datetime],
        unused_bucket_days: int,
        result: AnalyzerResult,
    ) -> None:
        last_activity = bucket_stats.get("latest_activity") or bucket_created
        if not last_activity:
            return

        stale_cutoff = datetime.utcnow() - timedelta(days=unused_bucket_days)
        if last_activity > stale_cutoff:
            return

        estimated_size_gb = bucket_stats.get("estimated_size_gb", 0.0)
        current_monthly_cost = self.cost_calculator.s3_storage_cost(estimated_size_gb, "standard")
        can_delete_now = bucket_stats.get("object_count_sampled", 0) == 0 and not bucket_stats.get("scan_truncated", False)
        issue_suffix = "bucket appears empty" if can_delete_now else "bucket has not been active for over 3 years"
        finding = Finding(
            resource_id=bucket_name,
            resource_type="S3 Bucket",
            account_id=self.account_id,
            region=self.region,
            issue=f"Unused S3 bucket - {issue_suffix}",
            recommendation=(
                "Delete this bucket only if retention/compliance checks are complete. "
                f"Estimated savings: ${current_monthly_cost:.2f}/month."
            ),
            severity="low" if can_delete_now else "medium",
            current_monthly_cost=current_monthly_cost,
            potential_savings_monthly=current_monthly_cost,
            potential_savings_annual=current_monthly_cost * 12,
            resource_tags=tags,
            details={
                "bucket_name": bucket_name,
                "recommended_action": "remove",
                "s3_workflow": "safe_delete",
                "allowed_actions": ["notify", "remove"],
                "estimated_size_gb": round(estimated_size_gb, 2),
                "object_count_sampled": bucket_stats.get("object_count_sampled", 0),
                "scan_truncated": bucket_stats.get("scan_truncated", False),
                "last_activity_at": last_activity.isoformat(),
                "unused_bucket_days": unused_bucket_days,
                "allow_remove": True,
                "delete_candidate": can_delete_now,
            },
        )
        result.add_finding(finding)

    def _add_bucket_key_finding(
        self,
        bucket_name: str,
        tags: Dict[str, str],
        bucket_stats: Dict[str, Any],
        encryption_state: Dict[str, Any],
        result: AnalyzerResult,
    ) -> None:
        if not encryption_state.get("uses_kms"):
            return
        if encryption_state.get("bucket_key_enabled"):
            return

        estimated_size_gb = bucket_stats.get("estimated_size_gb", 0.0)
        baseline_monthly_cost = self.cost_calculator.s3_storage_cost(estimated_size_gb, "standard")
        savings_monthly = max(0.5, baseline_monthly_cost * 0.05)

        finding = Finding(
            resource_id=bucket_name,
            resource_type="S3 Bucket",
            account_id=self.account_id,
            region=self.region,
            issue="SSE-KMS is enabled but S3 Bucket Key is not enabled",
            recommendation=(
                "Enable Bucket Key - Reducing the cost of SSE-KMS with Amazon S3 Bucket Keys. "
                f"Estimated savings: ${savings_monthly:.2f}/month."
            ),
            severity="low",
            current_monthly_cost=baseline_monthly_cost,
            potential_savings_monthly=savings_monthly,
            potential_savings_annual=savings_monthly * 12,
            resource_tags=tags,
            details={
                "bucket_name": bucket_name,
                "recommended_action": "lifecycle",
                "s3_workflow": "enable_bucket_key",
                "allowed_actions": ["notify", "lifecycle"],
                "kms_key_id": encryption_state.get("kms_key_id"),
                "estimated_size_gb": round(estimated_size_gb, 2),
                "object_count_sampled": bucket_stats.get("object_count_sampled", 0),
                "scan_truncated": bucket_stats.get("scan_truncated", False),
                "supports_custom": True,
            },
        )
        result.add_finding(finding)

    def _get_bucket_tags(self, s3_client, bucket_name: str) -> Dict[str, str]:
        try:
            tags_response = s3_client.get_bucket_tagging(Bucket=bucket_name)
            return {t["Key"]: t["Value"] for t in tags_response.get("TagSet", [])}
        except Exception:
            return {}

    def _get_lifecycle_configuration(self, s3_client, bucket_name: str) -> Dict[str, Any]:
        try:
            return s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchLifecycleConfiguration", "NoSuchBucket"}:
                return {}
            raise

    def _get_bucket_encryption_configuration(self, s3_client, bucket_name: str) -> Dict[str, Any]:
        try:
            response = s3_client.get_bucket_encryption(Bucket=bucket_name)
            return response.get("ServerSideEncryptionConfiguration", {})
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {
                "ServerSideEncryptionConfigurationNotFoundError",
                "NoSuchBucket",
                "AccessDenied",
            }:
                return {}
            raise

    def _get_bucket_encryption_state(self, encryption_config: Dict[str, Any]) -> Dict[str, Any]:
        kms_rules = []
        for rule in encryption_config.get("Rules", []):
            default_cfg = rule.get("ApplyServerSideEncryptionByDefault", {})
            if default_cfg.get("SSEAlgorithm") == "aws:kms":
                kms_rules.append(rule)

        if not kms_rules:
            return {
                "uses_kms": False,
                "bucket_key_enabled": False,
                "kms_key_id": None,
            }

        bucket_key_enabled = all(rule.get("BucketKeyEnabled", False) for rule in kms_rules)
        kms_key_id = kms_rules[0].get("ApplyServerSideEncryptionByDefault", {}).get("KMSMasterKeyID")

        return {
            "uses_kms": True,
            "bucket_key_enabled": bucket_key_enabled,
            "kms_key_id": kms_key_id,
        }

    def _has_abort_incomplete_rule(self, lifecycle_config: Dict[str, Any]) -> bool:
        for rule in lifecycle_config.get("Rules", []):
            if rule.get("AbortIncompleteMultipartUpload"):
                return True
        return False

    def _has_transition_rule(self, lifecycle_config: Dict[str, Any]) -> bool:
        for rule in lifecycle_config.get("Rules", []):
            if rule.get("Transitions") or rule.get("NoncurrentVersionTransitions"):
                return True
        return False

    def _has_intelligent_tiering_configuration(self, s3_client, bucket_name: str) -> bool:
        try:
            response = s3_client.list_bucket_intelligent_tiering_configurations(Bucket=bucket_name)
            return bool(response.get("IntelligentTieringConfigurationList"))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"AccessDenied", "NoSuchBucket", "NotImplemented"}:
                return False
            raise

    def _inspect_bucket_objects(self, s3_client, bucket_name: str, object_scan_limit: int) -> Dict[str, Any]:
        continuation_token = None
        total_size_bytes = 0
        object_count = 0
        latest_activity = None
        scan_truncated = False

        while object_count < object_scan_limit:
            kwargs = {"Bucket": bucket_name, "MaxKeys": min(1000, object_scan_limit - object_count)}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token

            response = s3_client.list_objects_v2(**kwargs)
            contents = response.get("Contents", [])
            if not contents:
                break

            for obj in contents:
                total_size_bytes += obj.get("Size", 0)
                object_count += 1
                last_modified = self._normalize_datetime(obj.get("LastModified"))
                if last_modified and (latest_activity is None or last_modified > latest_activity):
                    latest_activity = last_modified
                if object_count >= object_scan_limit:
                    break

            if not response.get("IsTruncated") or object_count >= object_scan_limit:
                scan_truncated = bool(response.get("IsTruncated"))
                break
            continuation_token = response.get("NextContinuationToken")

        return {
            "estimated_size_gb": round(total_size_bytes / (1024 ** 3), 2),
            "object_count_sampled": object_count,
            "latest_activity": latest_activity,
            "scan_truncated": scan_truncated,
        }

    def _normalize_datetime(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        if hasattr(value, "tzinfo") and value.tzinfo:
            return value.replace(tzinfo=None)
        return value
