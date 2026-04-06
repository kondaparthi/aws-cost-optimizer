# AWS Cost Optimizer Dashboard

This folder contains the web dashboard for reviewing and approving cost optimization recommendations.

## Files

- `index.html` - Production dashboard (loads data from S3)
- `login.html` - Authentication page
- `demo/` - Demo folder with sample data and tools
  - `demo.html` - Demo dashboard with sample data
  - `findings-sample.json` - Sample cost optimization findings
  - `actions-sample.json` - Sample user action decisions
  - `findings-generated.json` - Additional generated sample data
  - `generate_sample_data.py` - Script to generate more sample data
- `README.md` - This documentation

## Demo Mode

To test the dashboard without AWS setup:

1. Open `demo/demo.html` in a web browser
2. The dashboard will load sample findings automatically
3. Click "KEEP" or "REMOVE" buttons to make decisions
4. Use filters to narrow down findings
5. Export results to CSV

### Generate More Sample Data

```bash
cd demo
python3 generate_sample_data.py --count 50 --output my-findings.json
```

This creates realistic sample findings across different AWS services with varying costs and severities.

## Sample Data

The demo includes 12 sample findings across different AWS services:

- **EBS Volumes**: Unattached storage
- **EC2 Instances**: Idle instances and over-provisioning
- **S3 Buckets**: Lifecycle optimization opportunities
- **CloudWatch Logs**: Retention policy tuning
- **RDS Instances**: Underutilized databases
- **NAT Gateways**: Unused network resources

## Production Setup

For production deployment:

1. Deploy via CloudFormation (see `../cloudformation/`)
2. Configure Cognito for authentication
3. Set up S3 buckets for findings and actions data
4. Update bucket references in `index.html`

## Features

- ✅ Filter by resource type, severity, and decision status
- ✅ Mark items to keep or remove
- ✅ Calculate potential savings
- ✅ Export decisions to CSV
- ✅ Responsive design for mobile/tablet
- ✅ Secure authentication (production)

## Data Format

Findings JSON structure:
```json
{
  "generated_at": "2024-04-05T14:30:00Z",
  "summary": {
    "total_findings": 12,
    "potential_monthly_savings": 2850.50,
    "potential_annual_savings": 34206.00
  },
  "findings": [
    {
      "id": "resource-id",
      "type": "AWS Service Type",
      "issue": "Description of the problem",
      "cost_monthly": 100.00,
      "cost_annual": 1200.00,
      "severity": "high|medium|low",
      "action": "recommended_action",
      "tags": {"key": "value"},
      "region": "us-east-1"
    }
  ]
}
```

Actions JSON structure:
```json
{
  "last_updated": "2024-04-05T15:00:00Z",
  "user_id": "user@example.com",
  "items": [
    {
      "id": "resource-id",
      "user_action": "keep|remove",
      "timestamp": "2024-04-05T15:00:00Z",
      "reason": "Optional reason"
    }
  ]
}
```