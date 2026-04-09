"""
PricingCache: Dynamic AWS pricing backed by the AWS Price List API and an S3
cache bucket.

Issue #9: Replaces hard-coded per-region pricing values that go stale every
quarter with a daily-refreshed cache.  A companion EventBridge rule can invoke
a lightweight Lambda (or call PricingCache.refresh()) once per day to keep
prices current.

Architecture
------------
1. On first access the cache tries to load prices from
   s3://<cache_bucket>/pricing-cache/prices.json.
2. If the S3 object is missing or older than CACHE_TTL_HOURS the AWS Price
   List API is queried and the results are written back to S3.
3. All cost calculations in the analyzers should call:
       pricing_cache.get_price(service, resource_type, region)
   instead of using hard-coded numbers.
4. Every findings report produced by the analysis Lambda includes a
   ``price_fetch_date`` field so downstream consumers know how fresh the
   pricing data is.

Copyright (c) 2026 kondaparthi
Licensed under the MIT License.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fallback prices (used when the API is unreachable)
# ---------------------------------------------------------------------------
FALLBACK_PRICING: Dict[str, Dict[str, float]] = {
    "ebs": {
        "gp3":      0.08,
        "gp2":      0.10,
        "io1":      0.125,
        "io2":      0.125,
        "st1":      0.045,
        "sc1":      0.015,
        "snapshot": 0.05,
    },
    "s3": {
        "standard": 0.023,
        "ia":       0.0125,
        "glacier":  0.004,
    },
    "ec2": {
        "t3.micro":   0.0104,
        "t3.small":   0.0208,
        "t3.medium":  0.0416,
        "t3.large":   0.0832,
        "t3.xlarge":  0.1664,
        "m5.large":   0.096,
        "m5.xlarge":  0.192,
        "c5.large":   0.085,
        "c5.xlarge":  0.17,
    },
}

# AWS Pricing API uses display names, not region codes
REGION_NAME_MAP: Dict[str, str] = {
    "us-east-1":      "US East (N. Virginia)",
    "us-east-2":      "US East (Ohio)",
    "us-west-1":      "US West (N. California)",
    "us-west-2":      "US West (Oregon)",
    "eu-west-1":      "EU (Ireland)",
    "eu-central-1":   "EU (Frankfurt)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
}


class PricingCache:
    """
    Dynamic pricing cache backed by the AWS Price List API and an S3 object.

    Usage
    -----
    In an analyzer::

        cache = PricingCache(s3_bucket=os.environ["REPORT_S3_BUCKET"],
                             region="us-east-1")
        price_per_gb_month = cache.get_price("ebs", "gp3", "us-east-1")
        price_per_hour     = cache.get_price("ec2", "t3.micro", "us-east-1")
        price_per_gb_month = cache.get_price("s3", "standard", "us-east-1")
    """

    CACHE_S3_KEY = "pricing-cache/prices.json"
    CACHE_TTL_HOURS = 24

    def __init__(self, s3_bucket: str, region: str = "us-east-1"):
        self.s3_bucket = s3_bucket
        self.region = region
        self._in_memory: Optional[Dict[str, Any]] = None
        # Pricing API endpoint is global (us-east-1)
        self._s3 = boto3.client("s3")
        self._pricing = boto3.client("pricing", region_name="us-east-1")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_price(self, service: str, resource_type: str, region: str) -> float:
        """
        Return the price for *service*/*resource_type* in *region*.

        Args:
            service:       "ebs", "s3", or "ec2"
            resource_type: volume type (gp3), storage class (standard), or
                           instance type (t3.micro)
            region:        AWS region code (e.g. "us-east-1")

        Returns:
            Price per unit:
            - EBS / S3 → per GB-month
            - EC2      → per hour
        """
        prices = self._load()
        price = prices.get(region, {}).get(service, {}).get(resource_type)
        if price is not None:
            return price

        # Fall back to global fallback table
        fallback = FALLBACK_PRICING.get(service, {}).get(resource_type)
        if fallback is not None:
            logger.warning(
                "Using fallback pricing for %s/%s/%s: $%.4f",
                service, resource_type, region, fallback,
            )
            return fallback

        logger.warning(
            "No pricing found for %s/%s/%s — defaulting to 0.0",
            service, resource_type, region,
        )
        return 0.0

    def price_fetch_date(self) -> str:
        """
        Return the ISO-8601 timestamp when prices were last refreshed.
        Included in findings.json for transparency.
        """
        data = self._load_raw()
        return data.get("fetch_date", "unknown")

    def refresh(self, save_to_s3: bool = True) -> Dict[str, Any]:
        """
        Fetch fresh prices from the AWS Price List API for all known regions.

        Args:
            save_to_s3: If True, write the result to the S3 cache key.

        Returns:
            The prices dict (region → service → type → price).
        """
        fetch_date = datetime.utcnow().isoformat()
        prices: Dict[str, Any] = {}

        for region_code, region_name in REGION_NAME_MAP.items():
            prices[region_code] = {
                "ebs": self._fetch_ebs(region_name),
                "s3":  self._fetch_s3(region_name),
                "ec2": self._fetch_ec2(region_name),
            }

        cache_doc = {"fetch_date": fetch_date, "prices": prices}
        self._in_memory = cache_doc

        if save_to_s3:
            try:
                self._s3.put_object(
                    Bucket=self.s3_bucket,
                    Key=self.CACHE_S3_KEY,
                    Body=json.dumps(cache_doc, indent=2),
                    ContentType="application/json",
                )
                logger.info(
                    "Pricing cache saved to s3://%s/%s",
                    self.s3_bucket, self.CACHE_S3_KEY,
                )
            except Exception as exc:
                logger.warning("Failed to save pricing cache to S3: %s", exc)

        return prices

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        """Return the prices portion of the cache, refreshing if needed."""
        return self._load_raw().get("prices", {})

    def _load_raw(self) -> Dict[str, Any]:
        """Load the full cache document, refreshing from API if stale/missing."""
        # 1. Check in-memory copy first
        if self._in_memory is not None:
            if self._is_fresh(self._in_memory.get("fetch_date")):
                return self._in_memory

        # 2. Try S3
        try:
            resp = self._s3.get_object(Bucket=self.s3_bucket, Key=self.CACHE_S3_KEY)
            doc = json.loads(resp["Body"].read().decode("utf-8"))
            if self._is_fresh(doc.get("fetch_date")):
                self._in_memory = doc
                return doc
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchKey":
                logger.warning("Failed to load pricing cache from S3: %s", exc)
        except Exception as exc:
            logger.warning("Failed to load pricing cache from S3: %s", exc)

        # 3. Cache is missing or stale — refresh from API
        self.refresh(save_to_s3=True)
        return self._in_memory or {}

    def _is_fresh(self, fetch_date_str: Optional[str]) -> bool:
        """Return True if the cache was fetched within CACHE_TTL_HOURS."""
        if not fetch_date_str:
            return False
        try:
            fetch_date = datetime.fromisoformat(fetch_date_str)
            return datetime.utcnow() - fetch_date < timedelta(hours=self.CACHE_TTL_HOURS)
        except ValueError:
            return False

    def _extract_price(self, response: Dict, unit: str) -> Optional[float]:
        """Parse the first matching USD price from a get_products response."""
        if not response.get("PriceList"):
            return None
        try:
            pl = json.loads(response["PriceList"][0])
            for term in pl.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    if dim.get("unit") == unit:
                        usd = dim["pricePerUnit"].get("USD", "0")
                        if usd and float(usd) > 0:
                            return float(usd)
        except (ValueError, KeyError, IndexError):
            pass
        return None

    # ------------------------------------------------------------------
    # Per-service price fetchers
    # ------------------------------------------------------------------

    def _fetch_ebs(self, region_name: str) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        vol_types = {
            "gp3":  "General Purpose SSD (gp3)",
            "gp2":  "General Purpose SSD (gp2)",
            "io1":  "Provisioned IOPS SSD (io1)",
            "io2":  "Provisioned IOPS SSD (io2)",
            "st1":  "Throughput Optimized HDD",
            "sc1":  "Cold HDD",
        }
        for key, display_name in vol_types.items():
            try:
                resp = self._pricing.get_products(
                    ServiceCode="AmazonEC2",
                    Filters=[
                        {"Type": "TERM_MATCH", "Field": "volumeType",  "Value": display_name},
                        {"Type": "TERM_MATCH", "Field": "location",    "Value": region_name},
                    ],
                    MaxResults=1,
                )
                p = self._extract_price(resp, unit="GB-Mo")
                prices[key] = p if p is not None else FALLBACK_PRICING["ebs"].get(key, 0.0)
            except Exception as exc:
                logger.warning("EBS %s price fetch failed (%s): %s", key, region_name, exc)
                prices[key] = FALLBACK_PRICING["ebs"].get(key, 0.0)

        # Snapshot price
        try:
            resp = self._pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
                    {"Type": "TERM_MATCH", "Field": "location",      "Value": region_name},
                ],
                MaxResults=1,
            )
            p = self._extract_price(resp, unit="GB-Mo")
            prices["snapshot"] = p if p is not None else FALLBACK_PRICING["ebs"]["snapshot"]
        except Exception as exc:
            logger.warning("Snapshot price fetch failed (%s): %s", region_name, exc)
            prices["snapshot"] = FALLBACK_PRICING["ebs"]["snapshot"]

        return prices

    def _fetch_s3(self, region_name: str) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        cls_map = {
            "standard": "General Purpose",
            "ia":       "Infrequent Access",
            "glacier":  "Amazon Glacier",
        }
        for key, display_name in cls_map.items():
            try:
                resp = self._pricing.get_products(
                    ServiceCode="AmazonS3",
                    Filters=[
                        {"Type": "TERM_MATCH", "Field": "storageClass", "Value": display_name},
                        {"Type": "TERM_MATCH", "Field": "location",     "Value": region_name},
                    ],
                    MaxResults=1,
                )
                p = self._extract_price(resp, unit="GB-Mo")
                prices[key] = p if p is not None else FALLBACK_PRICING["s3"].get(key, 0.0)
            except Exception as exc:
                logger.warning("S3 %s price fetch failed (%s): %s", key, region_name, exc)
                prices[key] = FALLBACK_PRICING["s3"].get(key, 0.0)
        return prices

    def _fetch_ec2(self, region_name: str) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        for instance_type in FALLBACK_PRICING["ec2"]:
            try:
                resp = self._pricing.get_products(
                    ServiceCode="AmazonEC2",
                    Filters=[
                        {"Type": "TERM_MATCH", "Field": "instanceType",    "Value": instance_type},
                        {"Type": "TERM_MATCH", "Field": "location",        "Value": region_name},
                        {"Type": "TERM_MATCH", "Field": "tenancy",         "Value": "Shared"},
                        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                        {"Type": "TERM_MATCH", "Field": "preInstalledSw",  "Value": "NA"},
                        {"Type": "TERM_MATCH", "Field": "capacitystatus",  "Value": "Used"},
                    ],
                    MaxResults=1,
                )
                p = self._extract_price(resp, unit="Hrs")
                prices[instance_type] = (
                    p if p is not None else FALLBACK_PRICING["ec2"].get(instance_type, 0.1)
                )
            except Exception as exc:
                logger.warning("EC2 %s price fetch failed (%s): %s", instance_type, region_name, exc)
                prices[instance_type] = FALLBACK_PRICING["ec2"].get(instance_type, 0.1)
        return prices
