"""
Core configuration, logging, and AWS client utilities.
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


@dataclass
class AnalysisConfig:
    """Top-level configuration."""
    regions: List[str]
    accounts: List[Dict[str, str]]  # [{ id, role_arn, name? }]
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
                name=acc.get("name")
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

    def __init__(self, region: str, logger: Optional[StructuredLogger] = None):
        self.region = region
        self.logger = logger or StructuredLogger(__name__)
        self._role_cache = {}  # Cache assumed role sessions
    
    def get_session(self, account_id: Optional[str] = None, role_arn: Optional[str] = None):
        """
        Get boto3 session for account (via assume-role if specified).
        
        Args:
            account_id: AWS account ID (for audit logging)
            role_arn: Full ARN of role to assume in target account
        
        Returns:
            boto3.Session
        """
        # If no account specified, use local credentials
        if not role_arn:
            self.logger.log_event(
                "aws_session_local",
                {"region": self.region}
            )
            return boto3.Session(region_name=self.region)
        
        # Check cache
        cache_key = f"{account_id}:{role_arn}"
        if cache_key in self._role_cache:
            return self._role_cache[cache_key]
        
        try:
            # Assume role
            sts = boto3.client("sts")
            assumed = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=f"cost-optimizer-{account_id}",
                DurationSeconds=3600
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
                    "error": str(e)
                },
                level="ERROR"
            )
            raise
    
    def get_client(self, service: str, account_id: Optional[str] = None, role_arn: Optional[str] = None):
        """Get boto3 client for service in given account."""
        session = self.get_session(account_id, role_arn)
        return session.client(service)
    
    def get_resource(self, service: str, account_id: Optional[str] = None, role_arn: Optional[str] = None):
        """Get boto3 resource for service in given account."""
        session = self.get_session(account_id, role_arn)
        return session.resource(service)


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
        if all(tags.get(k) == v for k, v in if_match.items()):
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
