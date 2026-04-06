# CLI Usage Guide

**Local command-line interface for the AWS Cost Optimizer Framework.**

This guide covers installation, local CLI commands for analysis and scheduling, and testing. For production deployment and automation, see the [Deployment Guide](DEPLOYMENT.md).

---

## Quick Start

### Install
```bash
git clone https://github.com/YOUR_GITHUB/aws-cost-optimizer.git
cd aws-cost-optimizer
pip install -r requirements.txt
```

### Local CLI Usage
The CLI implementation lives in `src/aws_cost_optimizer/main.py` and provides the `analyze` and `schedule` commands.
This local CLI is useful for ad-hoc analysis, report generation, and scheduler testing.
For production automation, prefer the CloudFormation/Lambda deployment path described below.

From the repository root, add `src` to `PYTHONPATH` before running the module:

```bash
export PYTHONPATH=src
python -m aws_cost_optimizer analyze \
  --config config/example-config.yaml \
  --regions us-east-1,us-west-2 \
  --output-format html \
  --output-dir ./reports
```

If you do not set `PYTHONPATH`, run the script directly:

```bash
python src/aws_cost_optimizer/main.py analyze \
  --config config/example-config.yaml \
  --regions us-east-1,us-west-2 \
  --output-format html \
  --output-dir ./reports
```

### Set Up Scheduler
```bash
# Test scheduler (no changes)
export PYTHONPATH=src
python -m aws_cost_optimizer schedule \
  --config config/example-config.yaml \
  --dry-run

# Or run directly if PYTHONPATH is not set:
python src/aws_cost_optimizer/main.py schedule \
  --config config/example-config.yaml \
  --dry-run

# Deploy to Lambda for nightly execution
bash scripts/deploy-to-lambda.sh
```

---

## Local CLI Notes
The local CLI lives in `src/aws_cost_optimizer/main.py` and provides the `analyze` and `schedule` commands. It is intended for development, testing, and ad-hoc runs.

### Analyze
```bash
export PYTHONPATH=src
python -m aws_cost_optimizer analyze \
  --config config/example-config.yaml \
  --regions us-east-1,us-west-2 \
  --output-format html \
  --output-dir ./reports
```

### Schedule
```bash
export PYTHONPATH=src
python -m aws_cost_optimizer schedule \
  --config config/example-config.yaml \
  --dry-run
```

If you do not set `PYTHONPATH`, run the module file directly:

```bash
python src/aws_cost_optimizer/main.py analyze --config config/example-config.yaml
```

---

## Testing

```bash
pytest -v
pytest --cov=src/aws_cost_optimizer --cov-report=html
pytest tests/unit/test_ebs_analyzer.py -v
```

---

For deployment and automation documentation, see the docs folder.