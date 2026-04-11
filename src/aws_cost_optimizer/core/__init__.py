"""
Core configuration, logging, and AWS client utilities.

Copyright (c) 2026 kondaparthi

Licensed under the MIT License.
"""

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any

import boto3
import yaml
from botocore.exceptions import ClientError


# ============================================================================
# Data Models (Pydantic-like, but dataclass for simplicity)
# ============================================================================

@dataclass
class Account:
    """Cross-account configuration."""
    id: str
    role_arn: str
    name: Optional[str] = None
    external_id: Optional[str] = None


@dataclass
class AnalysisConfig:
    """Top-level configuration."""
    regions: List[str]
    accounts: List[Dict[str, Any]]  # [{ id, role_arn, name?, external_id? }]
    skip_policies: Dict[str, Dict[str, Any]]  # Tag-based skip rules
    thresholds: Dict[str, Any]  # Cost/size thresholds per analyzer
    scheduler: Dict[str, Any]  # Scheduler config (times, schedules)
    output: Dict[str, Any]  # Output paths, formats
    logging: Dict[str, Any]  # Log level, destination


# ============================================================================
# Configuration Loader
# ============================================================================

class ConfigLoader:
    """Load and validate YAML configuration."""

    @staticmethod
    def load(config_path: str) -> AnalysisConfig:
        """Load config from YAML file."""
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        
        # Validate required fields
        required = ["regions", "skip_policies", "thresholds", "output"]
        for field in required:
            if field not in raw:
                raise ValueError(f"Missing required config field: {field}")
        
        # Parse accounts
        accounts = []
        for acc in raw.get("accounts", []):
            accounts.append(Account(
                id=acc["id"],
                role_arn=acc["role_arn"],
                name=acc.get("name"),
                external_id=acc.get("external_id")
            ))
        
        return AnalysisConfig(
            regions=raw["regions"],
            accounts=[asdict(a) for a in accounts],
            skip_policies=raw["skip_policies"],
            thresholds=raw["thresholds"],
            scheduler=raw.get("scheduler", {}),
            output=raw["output"],
            logging=raw.get("logging", {"level": "INFO"})
        )


# ============================================================================
# Structured Logger
# ============================================================================

class StructuredLogger:
    """JSON-serializable logger for audit trails."""

    def __init__(self, name: str, level: str = "INFO"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        
        # Console handler with JSON formatting
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(message)s'
        ))
        self.logger.addHandler(handler)
    
    def log_event(self, event: str, data: Dict[str, Any], level: str = "INFO"):
        """Log structured event as JSON."""
        log_entry = {
            "event": event,
            "data": data
        }
        getattr(self.logger, level.lower())(json.dumps(log_entry))


# ============================================================================
# Multi-Account AWS Client
# ============================================================================

class AWSClient:
    """Handles multi-account access via STS assume-role."""

    def __init__(
        self,
        region: str,
        logger: Optional[StructuredLogger] = None,
        account_id: Optional[str] = None,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None,
    ):
        self.region = region
        self.logger = logger or StructuredLogger(__name__)
        self.default_account_id = account_id
        self.default_role_arn = role_arn
        self.default_external_id = external_id
        self._role_cache = {}  # Cache assumed role sessions
    
    def get_session(
        self,
        account_id: Optional[str] = None,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None,
    ):
        """
        Get boto3 session for account (via assume-role if specified).
        
        Args:
            account_id: AWS account ID (for audit logging)
            role_arn: Full ARN of role to assume in target account
        
        Returns:
            boto3.Session
        """
        account_id = account_id or self.default_account_id
        role_arn = role_arn or self.default_role_arn
        external_id = external_id or self.default_external_id

        # If no role specified, use local credentials
        if not role_arn:
            self.logger.log_event(
                "aws_session_local",
                {"region": self.region}
            )
            return boto3.Session(region_name=self.region)
        
        # Check cache
        cache_key = f"{account_id}:{role_arn}:{external_id or ''}"
        if cache_key in self._role_cache:
            return self._role_cache[cache_key]
        
        try:
            # Assume role
            sts = boto3.client("sts")
            assume_role_kwargs = {
                "RoleArn": role_arn,
                "RoleSessionName": f"cost-optimizer-{account_id or 'local'}",
                "DurationSeconds": 3600,
            }
            if external_id:
                assume_role_kwargs["ExternalId"] = external_id

            assumed = sts.assume_role(
                **assume_role_kwargs
            )
            
            credentials = assumed["Credentials"]
            session = boto3.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
                region_name=self.region
            )
            
            self._role_cache[cache_key] = session
            
            self.logger.log_event(
                "aws_session_assumed",
                {
                    "account_id": account_id,
                    "role_arn": role_arn,
                    "external_id": external_id,
                    "region": self.region
                }
            )
            
            return session
        
        except ClientError as e:
            self.logger.log_event(
                "aws_session_assumed_error",
                {
                    "account_id": account_id,
                    "role_arn": role_arn,
                    "external_id": external_id,
                    "error": str(e)
                },
                level="ERROR"
            )
            raise
    
    def get_client(
        self,
        service: str,
        account_id: Optional[str] = None,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None,
    ):
        """Get boto3 client for service in given account."""
        session = self.get_session(account_id, role_arn, external_id)
        return session.client(service)
    
    def get_resource(
        self,
        service: str,
        account_id: Optional[str] = None,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None,
    ):
        """Get boto3 resource for service in given account."""
        session = self.get_session(account_id, role_arn, external_id)
        return session.resource(service)

    def validate_cross_account_access(
        self,
        account_id: Optional[str] = None,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> None:
        """
        Issue #4: Validate that the assumed role has the required read
        permissions before running any analysis.

        Tests EC2, EBS, and S3 list/describe operations and raises
        PermissionError if any of them are denied, so that missing
        permissions surface as an explicit error rather than silently
        appearing as "resource not found".

        Raises:
            PermissionError: if one or more required permissions are missing.
        """
        session = self.get_session(account_id, role_arn, external_id)
        permission_errors: List[str] = []

        # Test EC2 read
        try:
            ec2 = session.client("ec2", region_name=self.region)
            ec2.describe_instances(MaxResults=5)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "AuthFailure", "UnauthorizedOperation"):
                permission_errors.append(f"EC2 describe_instances: {code}")

        # Test EBS read
        try:
            ec2 = session.client("ec2", region_name=self.region)
            ec2.describe_volumes(MaxResults=5)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "AuthFailure", "UnauthorizedOperation"):
                permission_errors.append(f"EBS describe_volumes: {code}")

        # Test S3 read
        try:
            s3 = session.client("s3")
            s3.list_buckets()
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "AuthFailure"):
                permission_errors.append(f"S3 list_buckets: {code}")

        if permission_errors:
            self.logger.log_event(
                "cross_account_permission_denied",
                {
                    "account_id": account_id,
                    "role_arn": role_arn,
                    "errors": permission_errors,
                },
                level="ERROR",
            )
            raise PermissionError(
                f"Cross-account role {role_arn} is missing permissions: "
                + ", ".join(permission_errors)
            )

        self.logger.log_event(
            "cross_account_access_validated",
            {"account_id": account_id, "role_arn": role_arn},
        )


# ============================================================================
# Dry-Run Context Manager
# ============================================================================

class DryRunMode:
    """Context manager for dry-run operations."""
    
    def __init__(self, enabled: bool, logger: StructuredLogger):
        self.enabled = enabled
        self.logger = logger
        self.operations_blocked = 0
    
    def __enter__(self):
        if self.enabled:
            self.logger.log_event("dry_run_enabled", {})
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.enabled:
            self.logger.log_event(
                "dry_run_summary",
                {"operations_blocked": self.operations_blocked}
            )
    
    def check(self, operation: str) -> bool:
        """Check if operation should be blocked in dry-run mode."""
        if self.enabled:
            self.logger.log_event(
                "operation_blocked_dry_run",
                {"operation": operation}
            )
            self.operations_blocked += 1
            return True
        return False


# ============================================================================
# Tag-Based Skip Policies
# ============================================================================

class SkipPolicy:
    """Evaluate skip rules against resource tags."""
    
    def __init__(self, policy_config: Dict[str, Dict[str, Any]], logger: StructuredLogger):
        """
        Args:
            policy_config: {
                "skip_if_tags_match": { "Environment": "prod", "Exclude": True },
                "skip_if_any_tag": [...]
            }
        """
        self.config = policy_config
        self.logger = logger
    
    def should_skip(self, resource_id: str, tags: Dict[str, str]) -> bool:
        """Check if resource should be skipped based on tags."""
        
        # Skip if tags match all conditions
        if_match = self.config.get("skip_if_tags_match", {})
        if if_match and all(tags.get(k) == v for k, v in if_match.items()):
            self.logger.log_event(
                "resource_skipped_tags",
                {"resource_id": resource_id, "tags": tags}
            )
            return True
        
        # Skip if any tags in list present
        if_any = self.config.get("skip_if_any_tag", [])
        if any(tag in tags for tag in if_any):
            self.logger.log_event(
                "resource_skipped_any_tag",
                {"resource_id": resource_id, "tags": tags}
            )
            return True
        
        return False

    def should_protect_resource(
        self,
        resource_id: str,
        tags: Dict[str, str],
        parent_tags_list: Optional[List[Dict[str, str]]] = None,
    ) -> bool:
        """
        Issue #5: Check if a resource OR any of its parent resources has
        protection tags, supporting parent-tag inheritance chains such as:
            EBS snapshot → source volume → attached EC2 instance

        Args:
            resource_id:      The resource being evaluated.
            tags:             Tag dict for the resource itself.
            parent_tags_list: Ordered list of tag dicts for parent resources
                              (e.g., [volume_tags, instance_tags]).

        Returns:
            True if the resource or any parent should be protected.
        """
        # Check the resource's own tags first
        if self.should_skip(resource_id, tags):
            return True

        # Walk up the parent chain
        if parent_tags_list:
            for idx, parent_tags in enumerate(parent_tags_list):
                if self.should_skip(f"parent_{idx}_of_{resource_id}", parent_tags):
                    self.logger.log_event(
                        "resource_protected_via_parent",
                        {
                            "resource_id": resource_id,
                            "parent_index": idx,
                            "parent_tags": parent_tags,
                        },
                    )
                    return True

        return False
