#!/usr/bin/env python3
"""
Generate sample findings data for AWS Cost Optimizer dashboard demo.

Usage:
    python generate_sample_data.py [--count N] [--output findings-sample.json]
"""

import json
import random
from datetime import datetime, timedelta
from typing import List, Dict, Any

# Sample data generators
RESOURCE_TYPES = [
    "EBS Volume", "EC2 Instance", "S3 Bucket", "CloudWatch Logs",
    "RDS Instance", "NAT Gateway", "Auto Scaling Group", "ELB"
]

ISSUES = {
    "EBS Volume": [
        "Unattached for {days}+ days",
        "Old snapshot ({count} snapshots > 90 days)",
        "Low utilization (<10% IOPS)"
    ],
    "EC2 Instance": [
        "Idle - CPU <{cpu}% for {days} days",
        "Over-provisioned - using only {util}% of allocated resources",
        "Running outside business hours"
    ],
    "S3 Bucket": [
        "Bucket with {size}GB of infrequently accessed data",
        "Bucket with incomplete multipart uploads ({size}GB)",
        "No lifecycle policy configured"
    ],
    "CloudWatch Logs": [
        "Log group with {size}GB data, retention set to never expire",
        "Log group with {size}GB data, retention set to {days} days"
    ],
    "RDS Instance": [
        "RDS instance with low utilization (<{cpu}% CPU)",
        "RDS instance with no connections for {days} days"
    ],
    "NAT Gateway": [
        "NAT Gateway with no active connections for {days} days",
        "NAT Gateway in unused subnet"
    ]
}

ACTIONS = {
    "EBS Volume": ["delete", "snapshot", "resize"],
    "EC2 Instance": ["stop", "resize", "terminate"],
    "S3 Bucket": ["lifecycle", "cleanup", "archive"],
    "CloudWatch Logs": ["retention", "archive"],
    "RDS Instance": ["stop", "resize", "snapshot"],
    "NAT Gateway": ["delete"],
    "Auto Scaling Group": ["delete", "resize"],
    "ELB": ["delete"]
}

SEVERITIES = ["high", "medium", "low"]
REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]

def generate_finding(resource_type: str, index: int) -> Dict[str, Any]:
    """Generate a single finding."""
    resource_id = f"{resource_type.lower().replace(' ', '-')}-{random.randint(1000000000, 9999999999)}"

    # Generate issue
    if resource_type in ISSUES:
        issue_template = random.choice(ISSUES[resource_type])
        issue = issue_template.format(
            days=random.randint(7, 180),
            cpu=random.randint(5, 30),
            util=random.randint(10, 50),
            size=random.randint(1, 1000),
            count=random.randint(5, 50)
        )
    else:
        issue = f"Optimization opportunity for {resource_type}"

    # Generate costs
    base_cost = random.randint(10, 500)
    cost_monthly = round(base_cost * random.uniform(0.5, 2.0), 2)
    cost_annual = round(cost_monthly * 12, 2)

    # Generate action
    action = random.choice(ACTIONS.get(resource_type, ["review"]))

    # Generate tags
    envs = ["prod", "staging", "dev", "test"]
    teams = ["platform", "backend", "frontend", "data", "infra"]
    services = ["api", "web", "database", "cache", "worker"]

    tags = {}
    tags["env"] = random.choice(envs)
    if random.random() > 0.3:
        tags["team"] = random.choice(teams)
    if random.random() > 0.5:
        tags["service"] = random.choice(services)

    return {
        "id": resource_id,
        "type": resource_type,
        "issue": issue,
        "cost_monthly": cost_monthly,
        "cost_annual": cost_annual,
        "severity": random.choice(SEVERITIES),
        "action": action,
        "tags": tags,
        "region": random.choice(REGIONS),
        "account_id": "123456789012"
    }

def generate_findings(count: int = 20) -> Dict[str, Any]:
    """Generate multiple findings."""
    findings = []

    # Ensure variety in resource types
    types_to_generate = []
    for _ in range(count):
        types_to_generate.append(random.choice(RESOURCE_TYPES))

    for i, resource_type in enumerate(types_to_generate):
        findings.append(generate_finding(resource_type, i))

    # Calculate totals
    total_monthly = sum(f["cost_monthly"] for f in findings)
    total_annual = sum(f["cost_annual"] for f in findings)

    return {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_findings": len(findings),
            "potential_monthly_savings": round(total_monthly, 2),
            "potential_annual_savings": round(total_annual, 2)
        },
        "findings": findings
    }

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate sample findings data")
    parser.add_argument("--count", type=int, default=20, help="Number of findings to generate")
    parser.add_argument("--output", default="findings-sample.json", help="Output file")

    args = parser.parse_args()

    print(f"Generating {args.count} sample findings...")
    data = generate_findings(args.count)

    with open(args.output, 'w') as f:
        json.dump(data, f, indent=2)

    print(f"Sample data saved to {args.output}")
    print(f"Total potential savings: ${data['summary']['potential_annual_savings']:,.2f} annually")

if __name__ == "__main__":
    main()