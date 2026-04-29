# Quick Start Guide

Get up and running with the AWS Cost Optimization Scanner in 5 minutes.

## Prerequisites

- Python 3.8 or higher
- AWS CLI configured with appropriate permissions
- AWS account with resources to analyze

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/aws-cost-optimizer/aws-cost-optimizer.git
cd aws-cost-optimizer
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure AWS Credentials

Choose one of these methods:

**Option A: AWS CLI (Recommended)**
```bash
aws configure
```

**Option B: Environment Variables**
```bash
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_DEFAULT_REGION=us-east-1
```

**Option C: Named Profile**
```bash
aws configure --profile production
```

## Basic Usage

### 1. Run Your First Scan

```bash
# Basic scan (all services in us-east-1)
python3 cost_optimizer.py us-east-1

# With specific AWS profile
python3 cost_optimizer.py us-east-1 --profile production

# Fast mode for large S3 environments
python3 cost_optimizer.py us-east-1 --fast
```

### 2. Service Filtering

Target specific services for faster, focused analysis:

```bash
# Scan only storage services
python3 cost_optimizer.py us-east-1 --scan-only s3 --scan-only ebs

# Skip compute-heavy services
python3 cost_optimizer.py us-east-1 --skip-service ec2 --skip-service rds

# Storage optimization focus with fast mode
python3 cost_optimizer.py us-east-1 --fast --scan-only s3
```

### 3. View Results

The scanner generates an interactive HTML report:

```bash
# Report is automatically opened in your browser
# Or manually open: cost_optimization_report_[timestamp].html
```

## Understanding Your Results

### Report Structure

The HTML report contains multiple tabs:

- **📊 Executive Summary**: Interactive dashboard with cost savings charts and key metrics (appears first)
- **💰 Service Tabs**: Detailed recommendations for each AWS service
- **📈 Statistics**: Resource counts and optimization opportunities

### Executive Summary Features

The executive summary tab provides:

- **Key Metrics Dashboard**: Total savings, recommendations, and services scanned
- **Interactive Pie Chart**: Cost savings distribution by AWS service
- **Interactive Bar Chart**: Top services ranked by savings potential
- **Click-to-Filter**: Click chart segments to navigate to specific service tabs
- **AWS-Themed Styling**: Professional presentation for executive reporting
- **Dark Mode Support**: Toggle between light and dark themes with the button in top-right corner

### Dark Mode

The HTML reports include a dark mode toggle:

- **Toggle Button**: Click the moon/sun icon in the top-right corner to switch themes
- **Theme Persistence**: Your preference is remembered across sessions
- **Full Adaptation**: All charts, text, and UI elements adapt to the selected theme
- **Professional Dark Theme**: Reduces eye strain with dark backgrounds and light text

### Recommendation Types

| Icon | Type | Description |
|------|------|-------------|
| 🗑️ | **Idle Resources** | 100% cost elimination opportunities |
| 📏 | **Rightsizing** | 20-50% cost reduction through proper sizing |
| 💾 | **Storage Optimization** | 20-95% savings through lifecycle policies |
| 🔒 | **Reserved Instances** | 30-72% savings through commitments |

### Confidence Levels

- **High**: Based on CloudWatch metrics and usage data
- **Medium**: Based on configuration analysis and best practices  
- **Low**: Heuristic-based recommendations requiring validation

## Common Use Cases

### Enterprise Account Scan

```bash
# Comprehensive enterprise scan
python3 cost_optimizer.py us-east-1 --profile production

# Multi-region enterprise scan
for region in us-east-1 eu-west-1 ap-southeast-1; do
    python3 cost_optimizer.py $region --profile production
done
```

### Development Environment Optimization

```bash
# Focus on compute and storage for dev environments
python3 cost_optimizer.py us-east-1 \
    --scan-only ec2 --scan-only ebs --scan-only rds \
    --profile development
```

### Storage Cost Audit

```bash
# Comprehensive storage analysis
python3 cost_optimizer.py us-east-1 \
    --scan-only s3 --scan-only ebs --scan-only efs \
    --fast
```

## Next Steps

### 1. Review High-Impact Recommendations

Focus on recommendations with:
- High confidence levels
- Significant monthly savings (>$100)
- Idle resource elimination opportunities

### 2. Implement Quick Wins

Start with zero-risk optimizations:
- Delete unused resources (EIPs, volumes, snapshots)
- Enable S3 Intelligent-Tiering
- Clean up old AMIs and snapshots

### 3. Plan Larger Optimizations

For bigger savings:
- Analyze Reserved Instance opportunities
- Plan EC2 rightsizing based on CloudWatch data
- Implement S3 lifecycle policies

### 4. Set Up Regular Scanning

```bash
# Add to crontab for weekly scans
0 6 * * 0 /usr/bin/python3 /path/to/cost_optimizer.py us-east-1 --profile production
```

## Troubleshooting

### Common Issues

**Permission Errors**:
```bash
# Check IAM permissions
aws sts get-caller-identity

# Verify service access
aws ec2 describe-instances --max-items 1
```

**Slow Performance**:
```bash
# Use fast mode
python3 cost_optimizer.py us-east-1 --fast

# Or filter services
python3 cost_optimizer.py us-east-1 --scan-only s3 --scan-only ec2
```

**No Recommendations**:
- Verify resources exist in the scanned region
- Check that services are actually being used
- Review scan warnings in the output

### Getting Help

- 📖 **Full Documentation**: See `docs/` directory
- 🐛 **Issues**: Report bugs on GitHub
- 💬 **Discussions**: Join community discussions
- 📧 **Support**: Check troubleshooting guide

## Advanced Usage

Once you're comfortable with basic usage, explore:

- **{doc}`advanced_usage`** - Enterprise deployments and CI/CD integration
- **{doc}`tutorials/adding_services`** - Add new AWS services
- **{doc}`api_reference`** - Complete API documentation
- **{doc}`architecture`** - System architecture and design

## Sample Output

```
🚀 AWS Cost Optimization Scanner v2.5.9 - Production Ready
📍 Scanning region: us-east-1
👤 AWS profile: production

📊 SCAN RESULTS
┌─────────────────────┬─────────────┬──────────────────┬─────────────────────┐
│ Service             │ Resources   │ Recommendations  │ Est. Monthly Savings│
├─────────────────────┼─────────────┼──────────────────┼─────────────────────┤
│ EC2                 │ 45          │ 23               │ $1,250.00          │
│ EBS                 │ 67          │ 34               │ $890.00            │
│ S3                  │ 89          │ 156              │ $2,100.00          │
│ RDS                 │ 12          │ 8                │ $650.00            │
│ Lambda              │ 34          │ 12               │ $120.00            │
└─────────────────────┴─────────────┴──────────────────┴─────────────────────┘

💰 Total Potential Monthly Savings: $5,010.00
📄 Detailed report: cost_optimization_report_20240125_120000.html
```

You're now ready to start optimizing your AWS costs! 🚀
