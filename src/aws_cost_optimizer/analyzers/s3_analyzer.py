"""
S3 Analyzer: Detect incomplete multipart uploads and lifecycle opportunities.

Copyright (c) 2026 kondaparthi

Licensed under the MIT License.
"""

from typing import Dict, Any
from datetime import datetime, timedelta
from .base_analyzer import BaseAnalyzer, AnalyzerResult, Finding

# Issue #3: Use an explicit hour-based constant so "7 days" is unambiguous
# regardless of DST, leap seconds, or calendar-day semantics.
INCOMPLETE_UPLOAD_THRESHOLD_HOURS = 168  # exactly 7 × 24 hours


class S3Analyzer(BaseAnalyzer):
    """Analyze S3 buckets for cost optimization."""
    
    name = "S3Analyzer"
    
    def analyze(self, config: Dict[str, Any], result: AnalyzerResult, dry_run: bool = True):
        """
        Find:
        - Incomplete multipart uploads (old)
        - Infrequent access objects (lifecycle opportunity)
        - Large unoptimized buckets
        """
        
        # Get config
        s3_config = config.get("s3", {})
        multipart_age_days = s3_config.get("multipart_age_days", 7)
        check_multipart = s3_config.get("check_incomplete_multipart", True)
        
        # Query S3
        s3_client = self.aws_client.get_client("s3", self.account_id)
        
        try:
            # List all buckets in this region
            response = s3_client.list_buckets()
            buckets = response.get("Buckets", [])
            
            for bucket in buckets:
                bucket_name = bucket["Name"]
                
                # Get bucket region
                try:
                    location_response = s3_client.get_bucket_location(Bucket=bucket_name)
                    bucket_region = location_response.get("LocationConstraint") or "us-east-1"
                    
                    # Skip if not in our region
                    if bucket_region != self.region:
                        continue
                except Exception:
                    continue
                
                # Get bucket tags for skip policy
                try:
                    tags_response = s3_client.get_bucket_tagging(Bucket=bucket_name)
                    tags = {t["Key"]: t["Value"] for t in tags_response.get("TagSet", [])}
                except:
                    tags = {}
                
                if self.skip_policy.should_skip(bucket_name, tags):
                    continue
                
                # =====================
                # Check incomplete uploads
                # =====================
                if check_multipart:
                    try:
                        multipart_response = s3_client.list_multipart_uploads(
                            Bucket=bucket_name
                        )
                        
                        uploads = multipart_response.get("Uploads", [])
                        # Issue #3: Compute age in fractional hours so the
                        # comparison is exact and timezone-agnostic.
                        current_time = datetime.utcnow()

                        old_uploads = []
                        total_size_gb = 0
                        
                        for upload in uploads:
                            initiated = upload.get("Initiated")
                            if isinstance(initiated, str):
                                initiated = datetime.fromisoformat(
                                    initiated.replace("Z", "+00:00")
                                ).replace(tzinfo=None)
                            elif initiated and hasattr(initiated, "tzinfo") and initiated.tzinfo:
                                initiated = initiated.replace(tzinfo=None)

                            if initiated:
                                age_hours = (
                                    current_time - initiated
                                ).total_seconds() / 3600
                                is_stale = age_hours >= INCOMPLETE_UPLOAD_THRESHOLD_HOURS
                            else:
                                is_stale = False

                            if is_stale:
                                old_uploads.append(upload)
                                total_size_gb += 0.5  # Estimate per upload
                        
                        if old_uploads:
                            cost = self.cost_calculator.s3_storage_cost(total_size_gb, "standard")
                            
                            finding = Finding(
                                resource_id=bucket_name,
                                resource_type="S3 Bucket",
                                account_id=self.account_id,
                                region=self.region,
                                issue=f"Incomplete multipart uploads - {len(old_uploads)} uploads older than {INCOMPLETE_UPLOAD_THRESHOLD_HOURS} hours",
                                recommendation=f"Delete or complete these uploads. Estimated cost: ${cost:.2f}/month.",
                                severity="medium",
                                current_monthly_cost=cost,
                                potential_savings_monthly=cost,
                                potential_savings_annual=cost * 12,
                                resource_tags=tags,
                                details={
                                    "bucket_name": bucket_name,
                                    "incomplete_count": len(old_uploads),
                                    "estimated_size_gb": round(total_size_gb, 2),
                                }
                            )
                            
                            result.add_finding(finding)
                            self.logger.log_event(
                                "s3_incomplete_uploads",
                                {
                                    "bucket": bucket_name,
                                    "count": len(old_uploads),
                                    "monthly_cost": cost
                                }
                            )
                    
                    except Exception as e:
                        self.logger.log_event(
                            "s3_multipart_error",
                            {"bucket": bucket_name, "error": str(e)},
                            level="WARN"
                        )
        
        except Exception as e:
            result.errors.append(f"Error scanning S3: {str(e)}")
            self.logger.log_event("s3_scan_error", {"error": str(e)}, level="ERROR")
        
        # Log summary
        self.logger.log_event(
            "s3_analysis_summary",
            {
                "account_id": self.account_id,
                "region": self.region,
                "findings": result.total_findings,
                "annual_savings": result.total_potential_savings_annual
            }
        )
