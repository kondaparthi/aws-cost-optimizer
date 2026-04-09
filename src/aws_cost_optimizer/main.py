"""
AWS Cost Optimizer - Main CLI entry point

Copyright (c) 2026 kondaparthi

Licensed under the MIT License.
"""

import sys
import json
from pathlib import Path
from typing import List, Optional

from src.aws_cost_optimizer.core import (
    ConfigLoader, AWSClient, StructuredLogger, SkipPolicy, DryRunMode
)
from src.aws_cost_optimizer.analyzers.ebs_analyzer import EBSAnalyzer


def run_analysis(config_path: str, regions: Optional[List[str]] = None, 
                 output_format: str = "json", output_dir: str = "./reports",
                 execute: bool = False):
    """
    Run cost analysis across specified regions and accounts.
    
    Args:
        config_path: Path to config YAML file
        regions: Regions to analyze (override config if provided)
        output_format: json, csv, or html
        output_dir: Output directory for reports
        execute: If True, allow write operations (default dry-run)
    """
    
    # Load config
    print(f"Loading config from {config_path}...")
    config = ConfigLoader.load(config_path)
    
    # Setup logging
    logger = StructuredLogger("aws-cost-optimizer", config.logging.get("level", "INFO"))
    
    # Parse accounts
    accounts = config.accounts if config.accounts else [{"id": None, "role_arn": None}]
    regions_to_scan = regions or config.regions
    
    logger.log_event("analysis_started", {
        "accounts": len(accounts),
        "regions": len(regions_to_scan),
        "execute": execute,
        "config_file": config_path
    })
    
    # Results aggregator
    all_results = []
    
    # =====================
    # Multi-account, multi-region loop
    # =====================
    for account in accounts:
        account_id = account.get("id")
        role_arn = account.get("role_arn")
        external_id = account.get("external_id")
        account_name = account.get("name", account_id or "local")
        
        for region in regions_to_scan:
            print(f"\nAnalyzing {account_name} / {region}...")
            
            # Create AWS client
            aws_client = AWSClient(
                region,
                logger,
                account_id=account_id,
                role_arn=role_arn,
                external_id=external_id,
            )
            
            # Create skip policy
            skip_policy = SkipPolicy(config.skip_policies, logger)
            
            # Run EBS analyzer (other analyzers would follow same pattern)
            ebs_analyzer = EBSAnalyzer(
                aws_client=aws_client,
                account_id=account_id or "local",
                region=region,
                skip_policy=skip_policy,
                logger=logger
            )
            
            result = ebs_analyzer.run(config.thresholds, dry_run=not execute)
            all_results.append(result.to_dict())
            
            # Print summary
            print(f"  Findings: {result.total_findings}")
            print(f"  Annual savings: ${result.total_potential_savings_annual:,.2f}")
    
    # =====================
    # Generate reports
    # =====================
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # JSON report
    json_report = {
        "metadata": {
            "total_results": len(all_results),
            "total_findings": sum(r["total_findings"] for r in all_results),
            "total_annual_savings": sum(r["total_potential_savings_annual"] for r in all_results)
        },
        "results": all_results
    }
    
    json_path = Path(output_dir) / "cost-analysis.json"
    with open(json_path, "w") as f:
        json.dump(json_report, f, indent=2, default=str)
    
    print(f"\n✓ Report saved: {json_path}")
    print(f"Total findings: {json_report['metadata']['total_findings']}")
    print(f"Potential annual savings: ${json_report['metadata']['total_annual_savings']:,.2f}")
    
    logger.log_event("analysis_completed", {
        "findings": json_report['metadata']['total_findings'],
        "annual_savings": json_report['metadata']['total_annual_savings']
    })


def run_scheduler(config_path: str, execute: bool = False, dry_run: bool = True):
    """
    Run scheduler to start/stop EC2/EMR instances based on tags.
    
    Args:
        config_path: Path to config YAML file
        execute: If True, perform actual start/stop operations
        dry_run: If True, preview only (default)
    """
    
    print(f"Loading config from {config_path}...")
    config = ConfigLoader.load(config_path)
    logger = StructuredLogger("aws-cost-optimizer-scheduler", config.logging.get("level", "INFO"))
    
    print("Scheduler not yet implemented. Coming in Phase 2.")
    print("See docs/SCHEDULER.md for planned functionality.")


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="AWS Cost Optimization Framework")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Analyze command
    analyze_parser = subparsers.add_parser("analyze", help="Run cost analysis")
    analyze_parser.add_argument("--config", required=True, help="Path to config YAML")
    analyze_parser.add_argument("--regions", help="Comma-separated regions (overrides config)")
    analyze_parser.add_argument("--output-format", default="json", help="json, csv, or html")
    analyze_parser.add_argument("--output-dir", default="./reports", help="Output directory")
    analyze_parser.add_argument("--execute", action="store_true", help="Execute changes (default is dry-run)")
    
    # Schedule command
    schedule_parser = subparsers.add_parser("schedule", help="Run EC2/EMR scheduler")
    schedule_parser.add_argument("--config", required=True, help="Path to config YAML")
    schedule_parser.add_argument("--execute", action="store_true", help="Execute changes (default is dry-run)")
    schedule_parser.add_argument("--dry-run", action="store_true", help="Preview only")
    
    args = parser.parse_args()
    
    if args.command == "analyze":
        regions = args.regions.split(",") if args.regions else None
        run_analysis(
            config_path=args.config,
            regions=regions,
            output_format=args.output_format,
            output_dir=args.output_dir,
            execute=args.execute
        )
    elif args.command == "schedule":
        run_scheduler(
            config_path=args.config,
            execute=args.execute,
            dry_run=args.dry_run
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
