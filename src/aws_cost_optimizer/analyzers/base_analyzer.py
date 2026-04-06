"""
Base analyzer and data models for all cost optimization modules.

Copyright (c) 2026 kondaparthi

Licensed under the MIT License.
"""

import boto3
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta


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
        self.cost_calculator = RealTimeCostCalculator(aws_client, region, logger, account_id)
    
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
# Real-Time Cost Calculation Helpers
# ============================================================================

class RealTimeCostCalculator:
    """Helper for real-time cost calculations using AWS Pricing API."""
    
    # Region mapping for AWS Pricing API
    REGION_MAPPING = {
        "us-east-1": "US East (N. Virginia)",
        "us-east-2": "US East (Ohio)",
        "us-west-1": "US West (N. California)",
        "us-west-2": "US West (Oregon)",
        "eu-west-1": "EU (Ireland)",
        "eu-central-1": "EU (Frankfurt)",
        "ap-southeast-1": "Asia Pacific (Singapore)",
        "ap-northeast-1": "Asia Pacific (Tokyo)",
        "ap-southeast-2": "Asia Pacific (Sydney)",
        # Add more as needed
    }
    
    # Fallback pricing in case API fails
    FALLBACK_PRICING = {
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
    
    def __init__(self, aws_client: Any, region: str, logger: Any, account_id: str = None):
        self.aws_client = aws_client
        self.region = region
        self.account_id = account_id
        self.logger = logger
        self._pricing_cache = {}
        self._cache_timestamp = None
        self._cache_duration = timedelta(hours=24)  # Cache for 24 hours
    
    def _get_pricing_region(self) -> str:
        """Get the region name for AWS Pricing API."""
        return self.REGION_MAPPING.get(self.region, self.region)
    
    def _get_pricing_client(self):
        """Get AWS Pricing API client."""
        return self.aws_client.get_client("pricing", self.account_id)
    
    def _is_cache_valid(self) -> bool:
        """Check if cached pricing is still valid."""
        if self._cache_timestamp is None:
            return False
        return datetime.utcnow() - self._cache_timestamp < self._cache_duration
    
    def _fetch_ec2_pricing(self) -> Dict[str, float]:
        """Fetch EC2 on-demand pricing for common instance types."""
        try:
            pricing_client = self._get_pricing_client()
            
            # Common instance types to fetch
            instance_types = [
                "t3.micro", "t3.small", "t3.medium", "t3.large", "t3.xlarge",
                "m5.large", "m5.xlarge", "m5.2xlarge", "m5.4xlarge",
                "c5.large", "c5.xlarge", "c5.2xlarge", "c5.4xlarge",
                "r5.large", "r5.xlarge", "r5.2xlarge", "r5.4xlarge"
            ]
            
            pricing = {}
            for instance_type in instance_types:
                try:
                    response = pricing_client.get_products(
                        ServiceCode="AmazonEC2",
                        Filters=[
                            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                            {"Type": "TERM_MATCH", "Field": "location", "Value": self._get_pricing_region()},
                            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                        ],
                        MaxResults=1
                    )
                    
                    if response.get("PriceList"):
                        price_list = json.loads(response["PriceList"][0])
                        # Extract hourly rate from the complex pricing structure
                        on_demand = price_list.get("terms", {}).get("OnDemand", {})
                        if on_demand:
                            for term_key, term_data in on_demand.items():
                                price_dimensions = term_data.get("priceDimensions", {})
                                for dim_key, dim_data in price_dimensions.items():
                                    if dim_data.get("unit") == "Hrs":
                                        price_per_hour = float(dim_data["pricePerUnit"]["USD"])
                                        pricing[instance_type] = price_per_hour
                                        break
                                if instance_type in pricing:
                                    break
                    
                except Exception as e:
                    self.logger.log_event(
                        "pricing_fetch_error",
                        {"service": "EC2", "instance_type": instance_type, "error": str(e)},
                        level="WARN"
                    )
                    continue
            
            return pricing
            
        except Exception as e:
            self.logger.log_event(
                "pricing_api_error",
                {"service": "EC2", "error": str(e)},
                level="ERROR"
            )
            return self.FALLBACK_PRICING.get("ec2_on_demand", {})
    
    def _fetch_ebs_pricing(self) -> Dict[str, float]:
        """Fetch EBS pricing."""
        try:
            pricing_client = self._get_pricing_client()
            
            volume_types = {
                "gp3": "General Purpose SSD (gp3)",
                "gp2": "General Purpose SSD (gp2)",
                "io1": "Provisioned IOPS SSD (io1)",
                "st1": "Throughput Optimized HDD",
                "sc1": "Cold HDD"
            }
            
            pricing = {}
            for vol_type, vol_name in volume_types.items():
                try:
                    response = pricing_client.get_products(
                        ServiceCode="AmazonEC2",
                        Filters=[
                            {"Type": "TERM_MATCH", "Field": "volumeType", "Value": vol_name},
                            {"Type": "TERM_MATCH", "Field": "location", "Value": self._get_pricing_region()},
                        ],
                        MaxResults=1
                    )
                    
                    if response.get("PriceList"):
                        price_list = json.loads(response["PriceList"][0])
                        on_demand = price_list.get("terms", {}).get("OnDemand", {})
                        if on_demand:
                            for term_key, term_data in on_demand.items():
                                price_dimensions = term_data.get("priceDimensions", {})
                                for dim_key, dim_data in price_dimensions.items():
                                    if dim_data.get("unit") == "GB-Mo":
                                        price_per_gb_month = float(dim_data["pricePerUnit"]["USD"])
                                        pricing[f"ebs_{vol_type}"] = price_per_gb_month
                                        break
                                if f"ebs_{vol_type}" in pricing:
                                    break
                    
                except Exception as e:
                    self.logger.log_event(
                        "pricing_fetch_error",
                        {"service": "EBS", "volume_type": vol_type, "error": str(e)},
                        level="WARN"
                    )
                    continue
            
            # Fetch snapshot pricing
            try:
                response = pricing_client.get_products(
                    ServiceCode="AmazonEC2",
                    Filters=[
                        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
                        {"Type": "TERM_MATCH", "Field": "location", "Value": self._get_pricing_region()},
                    ],
                    MaxResults=1
                )
                
                if response.get("PriceList"):
                    price_list = json.loads(response["PriceList"][0])
                    on_demand = price_list.get("terms", {}).get("OnDemand", {})
                    if on_demand:
                        for term_key, term_data in on_demand.items():
                            price_dimensions = term_data.get("priceDimensions", {})
                            for dim_key, dim_data in price_dimensions.items():
                                if dim_data.get("unit") == "GB-Mo":
                                    pricing["snapshot"] = float(dim_data["pricePerUnit"]["USD"])
                                    break
                            if "snapshot" in pricing:
                                break
            except Exception as e:
                self.logger.log_event(
                    "pricing_fetch_error",
                    {"service": "EBS", "type": "snapshot", "error": str(e)},
                    level="WARN"
                )
            
            return pricing
            
        except Exception as e:
            self.logger.log_event(
                "pricing_api_error",
                {"service": "EBS", "error": str(e)},
                level="ERROR"
            )
            return {
                k: v for k, v in self.FALLBACK_PRICING.items() 
                if k.startswith("ebs_") or k == "snapshot"
            }
    
    def _fetch_s3_pricing(self) -> Dict[str, float]:
        """Fetch S3 pricing."""
        try:
            pricing_client = self._get_pricing_client()
            
            storage_classes = {
                "standard": "General Purpose",
                "ia": "Infrequent Access",
                "glacier": "Glacier"
            }
            
            pricing = {}
            for class_key, class_name in storage_classes.items():
                try:
                    response = pricing_client.get_products(
                        ServiceCode="AmazonS3",
                        Filters=[
                            {"Type": "TERM_MATCH", "Field": "storageClass", "Value": class_name},
                            {"Type": "TERM_MATCH", "Field": "location", "Value": self._get_pricing_region()},
                        ],
                        MaxResults=1
                    )
                    
                    if response.get("PriceList"):
                        price_list = json.loads(response["PriceList"][0])
                        on_demand = price_list.get("terms", {}).get("OnDemand", {})
                        if on_demand:
                            for term_key, term_data in on_demand.items():
                                price_dimensions = term_data.get("priceDimensions", {})
                                for dim_key, dim_data in price_dimensions.items():
                                    if dim_data.get("unit") == "GB-Mo":
                                        price_per_gb_month = float(dim_data["pricePerUnit"]["USD"])
                                        pricing[f"s3_{class_key}"] = price_per_gb_month
                                        break
                                if f"s3_{class_key}" in pricing:
                                    break
                    
                except Exception as e:
                    self.logger.log_event(
                        "pricing_fetch_error",
                        {"service": "S3", "storage_class": class_key, "error": str(e)},
                        level="WARN"
                    )
                    continue
            
            return pricing
            
        except Exception as e:
            self.logger.log_event(
                "pricing_api_error",
                {"service": "S3", "error": str(e)},
                level="ERROR"
            )
            return {
                k: v for k, v in self.FALLBACK_PRICING.items() 
                if k.startswith("s3_")
            }
    
    def _fetch_nat_gateway_pricing(self) -> float:
        """Fetch NAT Gateway pricing."""
        try:
            pricing_client = self._get_pricing_client()
            
            response = pricing_client.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
                    {"Type": "TERM_MATCH", "Field": "location", "Value": self._get_pricing_region()},
                ],
                MaxResults=1
            )
            
            if response.get("PriceList"):
                price_list = json.loads(response["PriceList"][0])
                on_demand = price_list.get("terms", {}).get("OnDemand", {})
                if on_demand:
                    for term_key, term_data in on_demand.items():
                        price_dimensions = term_data.get("priceDimensions", {})
                        for dim_key, dim_data in price_dimensions.items():
                            if dim_data.get("unit") == "Hrs":
                                price_per_hour = float(dim_data["pricePerUnit"]["USD"])
                                return price_per_hour * 730  # Convert to monthly (730 hours)
            
            return self.FALLBACK_PRICING["nat_gateway"]
            
        except Exception as e:
            self.logger.log_event(
                "pricing_api_error",
                {"service": "NAT Gateway", "error": str(e)},
                level="ERROR"
            )
            return self.FALLBACK_PRICING["nat_gateway"]
    
    def _load_pricing_data(self):
        """Load all pricing data from AWS Pricing API."""
        if self._is_cache_valid():
            return
        
        self.logger.log_event("pricing_data_loading", {"region": self.region})
        
        pricing_data = {}
        
        # Fetch pricing for different services
        pricing_data.update(self._fetch_ec2_pricing())
        pricing_data.update(self._fetch_ebs_pricing())
        pricing_data.update(self._fetch_s3_pricing())
        pricing_data["nat_gateway"] = self._fetch_nat_gateway_pricing()
        
        # Merge with fallback pricing for any missing values
        for key, fallback_value in self.FALLBACK_PRICING.items():
            if key not in pricing_data:
                pricing_data[key] = fallback_value
        
        self._pricing_cache = pricing_data
        self._cache_timestamp = datetime.utcnow()
        
        self.logger.log_event(
            "pricing_data_loaded", 
            {"region": self.region, "cached_items": len(pricing_data)}
        )
    
    def get_price(self, key: str, default: Any = None) -> Any:
        """Get pricing value by key."""
        self._load_pricing_data()
        return self._pricing_cache.get(key, default or 0)
    
    def ebs_volume_cost(self, size_gb: int, volume_type: str = "gp3", months: int = 1) -> float:
        """Calculate EBS volume cost."""
        price_per_gb = self.get_price(f"ebs_{volume_type}", 0.10)
        return size_gb * price_per_gb * months
    
    def ebs_snapshot_cost(self, size_gb: int, months: int = 1) -> float:
        """Calculate EBS snapshot cost."""
        price_per_gb = self.get_price("snapshot", 0.05)
        return size_gb * price_per_gb * months
    
    def s3_storage_cost(self, size_gb: float, storage_class: str = "standard", months: int = 1) -> float:
        """Calculate S3 storage cost."""
        price_key = f"s3_{storage_class.lower()}"
        price_per_gb = self.get_price(price_key, 0.023)
        return size_gb * price_per_gb * months
    
    def nat_gateway_cost(self, months: int = 1) -> float:
        """Calculate NAT gateway monthly cost."""
        return self.get_price("nat_gateway", 32.0) * months
    
    def ec2_instance_cost(self, instance_type: str, hours_per_month: int = 730) -> float:
        """Calculate EC2 instance cost."""
        hourly_rate = self.get_price(instance_type, 0.05)
        return hourly_rate * hours_per_month
