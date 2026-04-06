"""
Base analyzer and data models for all cost optimization modules.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Optional
from datetime import datetime


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class Finding:
    """A single cost optimization finding."""
    
    # Identification
    resource_id: str
    resource_type: str
    account_id: str
    region: str
    
    # Analysis
    issue: str  # e.g., "Unattached EBS volume", "Idle EC2 instance"
    recommendation: str
    severity: str  # critical, high, medium, low
    
    # Cost
    current_monthly_cost: float
    potential_savings_monthly: float
    potential_savings_annual: float
    
    # Details
    resource_tags: Dict[str, str] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)  # Extra info
    
    # Metadata
    discovered_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for reporting."""
        return asdict(self)


@dataclass
class AnalyzerResult:
    """Result from a single analyzer run."""
    
    analyzer_name: str
    account_id: str
    region: str
    
    findings: List[Finding] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    
    # Summary stats
    total_findings: int = 0
    total_potential_savings_monthly: float = 0.0
    total_potential_savings_annual: float = 0.0
    
    executed_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    def add_finding(self, finding: Finding):
        """Add a finding and update totals."""
        self.findings.append(finding)
        self.total_findings += 1
        self.total_potential_savings_monthly += finding.potential_savings_monthly
        self.total_potential_savings_annual += finding.potential_savings_annual
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for reporting."""
        return {
            "analyzer_name": self.analyzer_name,
            "account_id": self.account_id,
            "region": self.region,
            "findings": [f.to_dict() for f in self.findings],
            "errors": self.errors,
            "total_findings": self.total_findings,
            "total_potential_savings_monthly": self.total_potential_savings_monthly,
            "total_potential_savings_annual": self.total_potential_savings_annual,
            "executed_at": self.executed_at
        }


# ============================================================================
# Base Analyzer
# ============================================================================

class BaseAnalyzer(ABC):
    """Abstract base class for all analyzers."""
    
    name: str = "BaseAnalyzer"
    
    def __init__(self, 
                 aws_client: Any,  # AWSClient instance
                 account_id: str,
                 region: str,
                 skip_policy: Any,  # SkipPolicy instance
                 logger: Any):  # StructuredLogger instance
        """
        Args:
            aws_client: AWSClient for multi-account access
            account_id: AWS account ID to analyze
            region: AWS region to analyze
            skip_policy: SkipPolicy instance for tag-based skipping
            logger: StructuredLogger for audit logging
        """
        self.aws_client = aws_client
        self.account_id = account_id
        self.region = region
        self.skip_policy = skip_policy
        self.logger = logger
    
    def run(self, config: Dict[str, Any], dry_run: bool = True) -> AnalyzerResult:
        """
        Run analysis for this analyzer.
        
        Args:
            config: Analyzer-specific config from main config.yaml
            dry_run: If True, don't modify resources
        
        Returns:
            AnalyzerResult with findings and metadata
        """
        result = AnalyzerResult(
            analyzer_name=self.name,
            account_id=self.account_id,
            region=self.region
        )
        
        try:
            self.logger.log_event(
                f"{self.name}_started",
                {"account_id": self.account_id, "region": self.region}
            )
            
            # Run analyzer
            self.analyze(config, result, dry_run)
            
            self.logger.log_event(
                f"{self.name}_completed",
                {
                    "account_id": self.account_id,
                    "region": self.region,
                    "findings": result.total_findings,
                    "potential_savings_annual": result.total_potential_savings_annual
                }
            )
        
        except Exception as e:
            error_msg = f"Error in {self.name}: {str(e)}"
            result.errors.append(error_msg)
            self.logger.log_event(
                f"{self.name}_error",
                {
                    "account_id": self.account_id,
                    "region": self.region,
                    "error": error_msg
                },
                level="ERROR"
            )
        
        return result
    
    @abstractmethod
    def analyze(self, config: Dict[str, Any], result: AnalyzerResult, dry_run: bool):
        """
        Implement analyzer logic.
        
        This method should:
        1. Query AWS resources
        2. Evaluate cost/usage
        3. Create Finding objects
        4. Add to result via result.add_finding()
        
        Args:
            config: Analyzer-specific config
            result: AnalyzerResult to populate
            dry_run: If True, don't modify resources
        """
        raise NotImplementedError
    
    def get_resource_tags(self, service: str, resource_id: str) -> Dict[str, str]:
        """Helper: Get tags for a resource."""
        try:
            # Most services support describe-tags or similar
            # This is a template; override per analyzer for specifics
            return {}
        except Exception as e:
            self.logger.log_event(
                "error_fetching_tags",
                {"resource_id": resource_id, "error": str(e)},
                level="WARN"
            )
            return {}


# ============================================================================
# Common Cost Calculation Helpers
# ============================================================================

class CostCalculator:
    """Helper for common cost calculations."""
    
    # Public AWS pricing (simplified, should be pulled from AWS Pricing API for production)
    PRICING = {
        "ebs_gp3": 0.10,  # per GB-month
        "ebs_gp2": 0.12,
        "ebs_io1": 0.20,
        "ebs_st1": 0.054,
        "ebs_sc1": 0.025,
        "snapshot": 0.05,  # per GB-month
        "s3_standard": 0.023,  # per GB-month
        "s3_ia": 0.0125,
        "s3_glacier": 0.004,
        "nat_gateway": 32.0,  # per month (45/GB data processed)
        "ec2_on_demand": {  # per hour (example: t3.micro)
            "t3.micro": 0.0104,
            "t3.small": 0.0208,
            "t3.medium": 0.0416,
        }
    }
    
    @staticmethod
    def ebs_volume_cost(size_gb: int, volume_type: str = "gp3", months: int = 1) -> float:
        """Calculate EBS volume cost."""
        price_per_gb = CostCalculator.PRICING.get(f"ebs_{volume_type}", 0.10)
        return size_gb * price_per_gb * months
    
    @staticmethod
    def ebs_snapshot_cost(size_gb: int, months: int = 1) -> float:
        """Calculate EBS snapshot cost."""
        return size_gb * CostCalculator.PRICING["snapshot"] * months
    
    @staticmethod
    def s3_storage_cost(size_gb: float, storage_class: str = "standard", months: int = 1) -> float:
        """Calculate S3 storage cost."""
        price_key = f"s3_{storage_class.lower()}"
        price_per_gb = CostCalculator.PRICING.get(price_key, 0.023)
        return size_gb * price_per_gb * months
    
    @staticmethod
    def nat_gateway_cost(months: int = 1) -> float:
        """Calculate NAT gateway monthly cost."""
        return CostCalculator.PRICING["nat_gateway"] * months
    
    @staticmethod
    def ec2_instance_cost(instance_type: str, hours_per_month: int = 730) -> float:
        """Calculate EC2 instance cost."""
        hourly_rate = CostCalculator.PRICING.get(f"ec2_on_demand", {}).get(instance_type, 0.05)
        return hourly_rate * hours_per_month
