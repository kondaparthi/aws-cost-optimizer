# Demo Dashboard

This folder contains a fully functional demo of the AWS Cost Optimizer dashboard with realistic synthetic data.

## Quick Start

### Option 1: Direct Browser Open
```bash
# Navigate to this directory
cd dashboard/demo

# Open in your default browser
open demo.html
# or double-click demo.html in your file explorer
```

### Option 2: Local Web Server (Recommended)
```bash
# Navigate to this directory
cd dashboard/demo

# Start a local web server
python3 -m http.server 8000

# Open in your browser
open http://localhost:8000/demo.html
```

## What You'll See

The demo loads automatically and shows:
- **28 realistic AWS resource findings** across multiple services
- **Accurate cost calculations** based on real AWS pricing
- **Sample user decisions** already marked for demonstration
- **Interactive filtering** by resource type, severity, and status

## Sample Data Files

- `findings-realistic.json` - 28 comprehensive findings with authentic AWS resource data
- `actions-realistic.json` - Sample user decisions showing realistic optimization choices
- `findings-sample.json` - Original 12 hand-crafted sample findings (legacy)
- `actions-sample.json` - Original sample actions (legacy)

## Interactive Features

1. **Make Decisions**: Click "KEEP" or "REMOVE" buttons on each finding
2. **Filter Results**: Use dropdowns to filter by type, severity, or status
3. **Track Savings**: Watch real-time calculations of potential cost savings
4. **Export Data**: Download your decisions as CSV for analysis
5. **View Summary**: See total findings, monthly/annual savings, and action counts

## Data Realism

The synthetic data includes:
- **Authentic AWS ARNs** and resource identifiers
- **Real cost calculations** using current AWS pricing
- **Proper resource metadata** (regions, tags, configurations)
- **Diverse AWS services**: EBS, EC2, S3, RDS, NAT Gateway, CloudWatch, etc.
- **Realistic optimization scenarios** with accurate savings estimates

## Generate Custom Data

```bash
# Generate additional synthetic findings
python3 generate_sample_data.py --count 50 --output custom-findings.json

# The script creates realistic AWS resource findings with accurate costs
```

## Production vs Demo

| Feature | Demo | Production |
|---------|------|------------|
| Data Source | Local JSON files | S3 bucket |
| Authentication | None required | AWS Cognito |
| Persistence | Browser session only | Database storage |
| Real-time Updates | No | Yes (via Lambda) |

The demo provides the **exact same user interface** as the production dashboard!
