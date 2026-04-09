"""
Data models for cost optimizer findings and actions.
"""

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Any, Optional
from datetime import datetime
import json


@dataclass
class Finding:
    """A cost optimization finding from an analyzer."""
    
    id: str  # Resource ID (vol-xxx, i-xxx, etc)
    type: str  # EBS Volume, EC2 Instance, S3 Bucket, etc
    issue: str  # Description of the problem
    region: str  # AWS region
    account_id: str  # AWS account ID
    
    # Cost data
    cost_monthly: float  # Current monthly cost
    cost_annual: float  # Current annual cost
    
    # Metadata
    severity: str  # high, medium, low
    action: str  # delete, stop, modify, etc
    tags: Dict[str, str] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)
    
    # Timestamps
    discovered_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Finding':
        """Create from dictionary."""
        return cls(**data)


@dataclass
class FindingsReport:
    """Complete findings report from analysis."""
    
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    # Summary
    total_findings: int = 0
    potential_monthly_savings: float = 0.0
    potential_annual_savings: float = 0.0
    
    # Findings by type
    findings_by_type: Dict[str, int] = field(default_factory=dict)
    findings_by_severity: Dict[str, int] = field(default_factory=dict)
    findings_by_account: Dict[str, int] = field(default_factory=dict)
    findings_by_region: Dict[str, int] = field(default_factory=dict)
    
    # Actual findings
    findings: List[Finding] = field(default_factory=list)
    
    # Errors (if any analyzers failed)
    errors: List[str] = field(default_factory=list)

    # Issue #10: Track whether analysis completed fully or was cut short by a
    # Lambda timeout safety margin.
    analysis_status: str = "complete"      # "complete" | "partial"
    partial_reason: Optional[str] = None   # Human-readable explanation when partial
    
    def add_finding(self, finding: Finding):
        """Add a finding and update summary."""
        self.findings.append(finding)
        self.total_findings += 1
        self.potential_monthly_savings += finding.cost_monthly
        self.potential_annual_savings += finding.cost_annual
        
        # Update breakdown
        self.findings_by_type[finding.type] = self.findings_by_type.get(finding.type, 0) + 1
        self.findings_by_severity[finding.severity] = self.findings_by_severity.get(finding.severity, 0) + 1
        self.findings_by_account[finding.account_id] = self.findings_by_account.get(finding.account_id, 0) + 1
        self.findings_by_region[finding.region] = self.findings_by_region.get(finding.region, 0) + 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'generated_at': self.generated_at,
            'analysis_status': self.analysis_status,
            'partial_reason': self.partial_reason,
            'summary': {
                'total_findings': self.total_findings,
                'potential_monthly_savings': round(self.potential_monthly_savings, 2),
                'potential_annual_savings': round(self.potential_annual_savings, 2),
                'findings_by_type': self.findings_by_type,
                'findings_by_severity': self.findings_by_severity,
                'findings_by_account': self.findings_by_account,
                'findings_by_region': self.findings_by_region,
            },
            'findings': [f.to_dict() for f in self.findings],
            'errors': self.errors,
        }
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2, default=str)


@dataclass
class UserAction:
    """User's decision on a finding."""
    
    id: str  # Finding ID
    user_action: str  # keep, remove
    user_timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    estimated_savings_monthly: float = 0.0
    estimated_savings_annual: float = 0.0
    notes: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class ActionsReport:
    """Track user actions and decisions."""
    
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    findings_reviewed: int = 0
    items: List[UserAction] = field(default_factory=list)
    
    # Summary
    keep_count: int = 0
    remove_count: int = 0
    pending_count: int = 0
    
    total_estimated_monthly_savings: float = 0.0
    total_estimated_annual_savings: float = 0.0
    
    def add_action(self, action: UserAction):
        """Add user action and update summary."""
        self.items.append(action)
        
        if action.user_action == 'keep':
            self.keep_count += 1
        elif action.user_action == 'remove':
            self.remove_count += 1
            self.total_estimated_monthly_savings += action.estimated_savings_monthly
            self.total_estimated_annual_savings += action.estimated_savings_annual
        else:
            self.pending_count += 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'generated_at': self.generated_at,
            'findings_reviewed': self.findings_reviewed,
            'actions_taken': {
                'keep': self.keep_count,
                'remove': self.remove_count,
                'pending': self.pending_count,
            },
            'total_estimated_monthly_savings': round(self.total_estimated_monthly_savings, 2),
            'total_estimated_annual_savings': round(self.total_estimated_annual_savings, 2),
            'items': [item.to_dict() for item in self.items],
        }
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2, default=str)
