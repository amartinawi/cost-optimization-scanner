# Advanced Usage Examples

This document provides comprehensive examples for enterprise deployments, CI/CD integration, and advanced configuration scenarios.

## Enterprise Deployment Scenarios

### Multi-Account Cost Optimization

```python
#!/usr/bin/env python3
"""
Multi-account cost optimization scanner for AWS Organizations.
Scans multiple accounts and generates consolidated reports.
"""

import boto3
from cost_optimizer import CostOptimizer
from html_report_generator import HTMLReportGenerator
import json
from datetime import datetime

class MultiAccountOptimizer:
    def __init__(self, organization_profile='org-master'):
        self.org_session = boto3.Session(profile_name=organization_profile)
        self.org_client = self.org_session.client('organizations')
        
    def get_organization_accounts(self):
        """Get all accounts in the organization."""
        accounts = []
        paginator = self.org_client.get_paginator('list_accounts')
        
        for page in paginator.paginate():
            for account in page['Accounts']:
                if account['Status'] == 'ACTIVE':
                    accounts.append({
                        'id': account['Id'],
                        'name': account['Name'],
                        'email': account['Email']
                    })
        return accounts
    
    def scan_account(self, account_id, account_name, region='us-east-1'):
        """Scan a single account for cost optimization opportunities."""
        try:
            # Assume role in target account
            sts = self.org_session.client('sts')
            role_arn = f"arn:aws:iam::{account_id}:role/CostOptimizationRole"
            
            assumed_role = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=f"CostOptimization-{account_id}"
            )
            
            # Create temporary credentials
            temp_session = boto3.Session(
                aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
                aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
                aws_session_token=assumed_role['Credentials']['SessionToken']
            )
            
            # Initialize optimizer with temporary credentials
            optimizer = CostOptimizer(region)
            optimizer.session = temp_session
            
            # Run scan
            results = optimizer.scan_region()
            results['account_info'] = {
                'id': account_id,
                'name': account_name,
                'scan_timestamp': datetime.utcnow().isoformat()
            }
            
            return results
            
        except Exception as e:
            print(f"Error scanning account {account_id} ({account_name}): {e}")
            return None
    
    def scan_organization(self, regions=['us-east-1', 'eu-west-1']):
        """Scan all accounts in the organization across multiple regions."""
        accounts = self.get_organization_accounts()
        all_results = []
        
        for account in accounts:
            print(f"Scanning account: {account['name']} ({account['id']})")
            
            for region in regions:
                print(f"  Region: {region}")
                results = self.scan_account(account['id'], account['name'], region)
                
                if results:
                    results['region'] = region
                    all_results.append(results)
        
        return all_results
    
    def generate_consolidated_report(self, all_results):
        """Generate consolidated report across all accounts and regions."""
        consolidated = {
            'scan_metadata': {
                'total_accounts': len(set(r['account_info']['id'] for r in all_results)),
                'total_regions': len(set(r['region'] for r in all_results)),
                'scan_timestamp': datetime.utcnow().isoformat()
            },
            'account_results': all_results,
            'summary': self._calculate_organization_summary(all_results)
        }
        
        # Generate HTML report
        generator = HTMLReportGenerator(consolidated)
        report_path = generator.generate_html_report('organization_cost_optimization_report.html')
        
        return report_path, consolidated

# Usage example
if __name__ == "__main__":
    optimizer = MultiAccountOptimizer('org-master')
    results = optimizer.scan_organization(['us-east-1', 'eu-west-1', 'ap-southeast-1'])
    report_path, data = optimizer.generate_consolidated_report(results)
    print(f"Organization report generated: {report_path}")
```

### Large-Scale Enterprise Configuration

```python
#!/usr/bin/env python3
"""
Enterprise-scale cost optimization with custom thresholds and filtering.
Optimized for accounts with 10,000+ resources.
"""

from cost_optimizer import CostOptimizer
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

class EnterpriseOptimizer:
    def __init__(self, config_file='enterprise_config.json'):
        with open(config_file, 'r') as f:
            self.config = json.load(f)
        
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('cost_optimization.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    def scan_regions_parallel(self, regions, profile='production'):
        """Scan multiple regions in parallel for faster processing."""
        results = {}
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            # Submit scan jobs for each region
            future_to_region = {
                executor.submit(self._scan_single_region, region, profile): region
                for region in regions
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    results[region] = future.result()
                    self.logger.info(f"Completed scan for region: {region}")
                except Exception as e:
                    self.logger.error(f"Error scanning region {region}: {e}")
                    results[region] = None
        
        return results
    
    def _scan_single_region(self, region, profile):
        """Scan a single region with enterprise configuration."""
        optimizer = CostOptimizer(region, profile, fast_mode=True)
        
        # Apply enterprise filtering based on configuration
        skip_services = self.config.get('skip_services', [])
        scan_only = self.config.get('scan_only', None)
        
        # Custom thresholds
        if 'thresholds' in self.config:
            self._apply_custom_thresholds(optimizer)
        
        return optimizer.scan_region(
            skip_services=skip_services,
            scan_only=scan_only
        )
    
    def _apply_custom_thresholds(self, optimizer):
        """Apply enterprise-specific optimization thresholds."""
        thresholds = self.config['thresholds']
        
        # Override default thresholds
        if 'old_snapshot_days' in thresholds:
            optimizer.OLD_SNAPSHOT_DAYS = thresholds['old_snapshot_days']
        
        if 'large_table_size_gb' in thresholds:
            optimizer.LARGE_TABLE_SIZE_GB = thresholds['large_table_size_gb']
        
        # Add more threshold overrides as needed

# Enterprise configuration file (enterprise_config.json)
enterprise_config = {
    "skip_services": ["lightsail", "workspaces"],  # Skip services not used
    "scan_only": null,  # Scan all services except skipped
    "thresholds": {
        "old_snapshot_days": 180,  # More conservative for enterprise
        "large_table_size_gb": 100,  # Higher threshold for enterprise
        "excessive_backup_retention_days": 90
    },
    "regions": [
        "us-east-1", "us-west-2", "eu-west-1", 
        "eu-central-1", "ap-southeast-1", "ap-northeast-1"
    ],
    "notification": {
        "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:cost-optimization-alerts",
        "email_recipients": ["finops@company.com", "devops@company.com"]
    }
}

# Save configuration
with open('enterprise_config.json', 'w') as f:
    json.dump(enterprise_config, f, indent=2)
```

## CI/CD Integration Examples

### GitHub Actions Workflow

```yaml
# .github/workflows/cost-optimization.yml
name: AWS Cost Optimization Scan

on:
  schedule:
    # Run weekly on Sundays at 6 AM UTC
    - cron: '0 6 * * 0'
  workflow_dispatch:  # Allow manual triggering
    inputs:
      regions:
        description: 'Comma-separated list of regions to scan'
        required: false
        default: 'us-east-1,eu-west-1'
      services:
        description: 'Comma-separated list of services to scan (optional)'
        required: false

jobs:
  cost-optimization:
    runs-on: ubuntu-latest
    
    steps:
    - name: Checkout code
      uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.9'
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
    
    - name: Configure AWS credentials
      uses: aws-actions/configure-aws-credentials@v2
      with:
        aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
        aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        aws-region: us-east-1
    
    - name: Run cost optimization scan
      run: |
        python3 scripts/ci_cost_scan.py \
          --regions "${{ github.event.inputs.regions || 'us-east-1,eu-west-1' }}" \
          --services "${{ github.event.inputs.services }}" \
          --output-dir reports/
    
    - name: Upload reports
      uses: actions/upload-artifact@v3
      with:
        name: cost-optimization-reports
        path: reports/
        retention-days: 30
    
    - name: Send Slack notification
      if: always()
      uses: 8398a7/action-slack@v3
      with:
        status: ${{ job.status }}
        channel: '#finops'
        webhook_url: ${{ secrets.SLACK_WEBHOOK }}
        fields: repo,message,commit,author,action,eventName,ref,workflow
```

### Jenkins Pipeline

```groovy
// Jenkinsfile
pipeline {
    agent any
    
    parameters {
        choice(
            name: 'ENVIRONMENT',
            choices: ['production', 'staging', 'development'],
            description: 'Environment to scan'
        )
        string(
            name: 'REGIONS',
            defaultValue: 'us-east-1,eu-west-1',
            description: 'Comma-separated list of regions'
        )
        booleanParam(
            name: 'FAST_MODE',
            defaultValue: false,
            description: 'Enable fast mode for large S3 environments'
        )
    }
    
    environment {
        AWS_DEFAULT_REGION = 'us-east-1'
        PYTHONPATH = "${WORKSPACE}"
    }
    
    stages {
        stage('Setup') {
            steps {
                sh '''
                    python3 -m venv venv
                    . venv/bin/activate
                    pip install -r requirements.txt
                '''
            }
        }
        
        stage('Cost Optimization Scan') {
            steps {
                withCredentials([
                    [$class: 'AmazonWebServicesCredentialsBinding', 
                     credentialsId: "aws-${params.ENVIRONMENT}"]
                ]) {
                    sh '''
                        . venv/bin/activate
                        python3 scripts/jenkins_cost_scan.py \
                            --environment ${ENVIRONMENT} \
                            --regions "${REGIONS}" \
                            --fast-mode ${FAST_MODE} \
                            --output-dir reports/
                    '''
                }
            }
        }
        
        stage('Generate Reports') {
            steps {
                sh '''
                    . venv/bin/activate
                    python3 scripts/generate_summary_report.py \
                        --input-dir reports/ \
                        --output reports/summary.html
                '''
            }
        }
        
        stage('Archive Results') {
            steps {
                archiveArtifacts artifacts: 'reports/**/*', fingerprint: true
                publishHTML([
                    allowMissing: false,
                    alwaysLinkToLastBuild: true,
                    keepAll: true,
                    reportDir: 'reports',
                    reportFiles: 'summary.html',
                    reportName: 'Cost Optimization Report'
                ])
            }
        }
        
        stage('Send Notifications') {
            when {
                anyOf {
                    expression { currentBuild.result == 'FAILURE' }
                    expression { env.BRANCH_NAME == 'main' }
                }
            }
            steps {
                script {
                    def reportUrl = "${BUILD_URL}Cost_Optimization_Report/"
                    def message = """
                        Cost Optimization Scan Complete
                        Environment: ${params.ENVIRONMENT}
                        Regions: ${params.REGIONS}
                        Report: ${reportUrl}
                    """
                    
                    slackSend(
                        channel: '#finops',
                        color: currentBuild.result == 'SUCCESS' ? 'good' : 'danger',
                        message: message
                    )
                }
            }
        }
    }
    
    post {
        always {
            cleanWs()
        }
    }
}
```

## Custom Threshold Configuration

### Dynamic Threshold Management

```python
#!/usr/bin/env python3
"""
Dynamic threshold configuration for different environments and use cases.
"""

import json
from dataclasses import dataclass
from typing import Dict, Any

@dataclass
class OptimizationThresholds:
    """Configuration class for optimization thresholds."""
    
    # Snapshot and AMI thresholds
    old_snapshot_days: int = 90
    old_ami_days: int = 90
    
    # Storage thresholds
    large_table_size_gb: int = 10
    small_efs_size_gb: float = 0.1
    large_efs_size_gb: int = 10
    efs_one_zone_min_size_gb: int = 1
    large_fsx_capacity_gb: int = 100
    
    # Backup thresholds
    excessive_backup_retention_days: int = 30
    multi_az_backup_retention_days: int = 7
    
    # Cost thresholds
    minimum_monthly_savings: float = 10.0
    high_confidence_threshold: float = 0.8
    medium_confidence_threshold: float = 0.5

class ThresholdManager:
    """Manages optimization thresholds for different environments."""
    
    def __init__(self, config_file: str = None):
        self.environments = {
            'development': OptimizationThresholds(
                old_snapshot_days=30,  # More aggressive cleanup
                old_ami_days=30,
                excessive_backup_retention_days=7,
                minimum_monthly_savings=5.0
            ),
            'staging': OptimizationThresholds(
                old_snapshot_days=60,
                old_ami_days=60,
                excessive_backup_retention_days=14,
                minimum_monthly_savings=10.0
            ),
            'production': OptimizationThresholds(
                old_snapshot_days=180,  # More conservative
                old_ami_days=180,
                excessive_backup_retention_days=90,
                minimum_monthly_savings=50.0,
                large_table_size_gb=100  # Higher threshold for prod
            )
        }
        
        if config_file:
            self.load_from_file(config_file)
    
    def get_thresholds(self, environment: str) -> OptimizationThresholds:
        """Get thresholds for a specific environment."""
        return self.environments.get(environment, self.environments['production'])
    
    def apply_to_optimizer(self, optimizer, environment: str):
        """Apply thresholds to a CostOptimizer instance."""
        thresholds = self.get_thresholds(environment)
        
        # Apply all threshold values
        for attr_name, value in thresholds.__dict__.items():
            if hasattr(optimizer, attr_name.upper()):
                setattr(optimizer, attr_name.upper(), value)
    
    def save_to_file(self, filename: str):
        """Save threshold configuration to JSON file."""
        config = {}
        for env_name, thresholds in self.environments.items():
            config[env_name] = thresholds.__dict__
        
        with open(filename, 'w') as f:
            json.dump(config, f, indent=2)
    
    def load_from_file(self, filename: str):
        """Load threshold configuration from JSON file."""
        with open(filename, 'r') as f:
            config = json.load(f)
        
        for env_name, threshold_dict in config.items():
            self.environments[env_name] = OptimizationThresholds(**threshold_dict)

# Usage example
def scan_with_custom_thresholds(region, environment, profile=None):
    """Scan with environment-specific thresholds."""
    from cost_optimizer import CostOptimizer
    
    # Initialize threshold manager
    threshold_mgr = ThresholdManager()
    
    # Create optimizer
    optimizer = CostOptimizer(region, profile)
    
    # Apply environment-specific thresholds
    threshold_mgr.apply_to_optimizer(optimizer, environment)
    
    # Run scan
    return optimizer.scan_region()

# Example usage
if __name__ == "__main__":
    # Scan development environment with aggressive thresholds
    dev_results = scan_with_custom_thresholds('us-east-1', 'development', 'dev-profile')
    
    # Scan production with conservative thresholds
    prod_results = scan_with_custom_thresholds('us-east-1', 'production', 'prod-profile')
```

## Monitoring and Alerting Integration

### CloudWatch Integration

```python
#!/usr/bin/env python3
"""
CloudWatch integration for cost optimization monitoring and alerting.
"""

import boto3
import json
from datetime import datetime, timedelta
from cost_optimizer import CostOptimizer

class CostOptimizationMonitor:
    """Monitors cost optimization metrics and sends alerts."""
    
    def __init__(self, region='us-east-1'):
        self.cloudwatch = boto3.client('cloudwatch', region_name=region)
        self.sns = boto3.client('sns', region_name=region)
        self.region = region
    
    def publish_metrics(self, scan_results: dict, namespace='CostOptimization'):
        """Publish cost optimization metrics to CloudWatch."""
        
        # Extract key metrics
        total_savings = scan_results.get('total_monthly_savings', 0)
        service_count = len(scan_results.get('services', {}))
        recommendation_count = sum(
            len(service.get('recommendations', []))
            for service in scan_results.get('services', {}).values()
        )
        
        # Publish metrics
        metrics = [
            {
                'MetricName': 'TotalMonthlySavings',
                'Value': total_savings,
                'Unit': 'None',
                'Dimensions': [
                    {'Name': 'Region', 'Value': self.region},
                    {'Name': 'AccountId', 'Value': scan_results.get('account_id', 'unknown')}
                ]
            },
            {
                'MetricName': 'RecommendationCount',
                'Value': recommendation_count,
                'Unit': 'Count',
                'Dimensions': [
                    {'Name': 'Region', 'Value': self.region},
                    {'Name': 'AccountId', 'Value': scan_results.get('account_id', 'unknown')}
                ]
            },
            {
                'MetricName': 'ServicesScanned',
                'Value': service_count,
                'Unit': 'Count',
                'Dimensions': [
                    {'Name': 'Region', 'Value': self.region},
                    {'Name': 'AccountId', 'Value': scan_results.get('account_id', 'unknown')}
                ]
            }
        ]
        
        # Publish to CloudWatch
        self.cloudwatch.put_metric_data(
            Namespace=namespace,
            MetricData=metrics
        )
        
        return metrics
    
    def check_savings_threshold(self, scan_results: dict, threshold: float = 1000.0):
        """Check if potential savings exceed threshold and send alert."""
        total_savings = scan_results.get('total_monthly_savings', 0)
        
        if total_savings > threshold:
            message = {
                'alert_type': 'high_savings_opportunity',
                'total_savings': total_savings,
                'threshold': threshold,
                'region': self.region,
                'account_id': scan_results.get('account_id'),
                'scan_timestamp': datetime.utcnow().isoformat(),
                'top_recommendations': self._get_top_recommendations(scan_results, 5)
            }
            
            self._send_alert(message)
            return True
        
        return False
    
    def _get_top_recommendations(self, scan_results: dict, limit: int = 5):
        """Get top cost optimization recommendations by savings."""
        all_recommendations = []
        
        for service_name, service_data in scan_results.get('services', {}).items():
            for rec in service_data.get('recommendations', []):
                if 'estimated_monthly_savings' in rec:
                    all_recommendations.append({
                        'service': service_name,
                        'description': rec.get('description', ''),
                        'savings': rec['estimated_monthly_savings']
                    })
        
        # Sort by savings and return top N
        all_recommendations.sort(key=lambda x: x['savings'], reverse=True)
        return all_recommendations[:limit]
    
    def _send_alert(self, message: dict):
        """Send alert via SNS."""
        topic_arn = 'arn:aws:sns:us-east-1:123456789012:cost-optimization-alerts'
        
        formatted_message = f"""
        🚨 High Cost Savings Opportunity Detected!
        
        💰 Total Potential Savings: ${message['total_savings']:.2f}/month
        📍 Region: {message['region']}
        🏢 Account: {message['account_id']}
        ⏰ Scan Time: {message['scan_timestamp']}
        
        🔝 Top Recommendations:
        """
        
        for i, rec in enumerate(message['top_recommendations'], 1):
            formatted_message += f"\n{i}. {rec['service']}: ${rec['savings']:.2f}/month - {rec['description']}"
        
        self.sns.publish(
            TopicArn=topic_arn,
            Subject='Cost Optimization Alert - High Savings Opportunity',
            Message=formatted_message
        )

# Usage example
def monitored_cost_scan(region, profile=None):
    """Run cost scan with monitoring and alerting."""
    
    # Run cost optimization scan
    optimizer = CostOptimizer(region, profile)
    results = optimizer.scan_region()
    
    # Initialize monitor
    monitor = CostOptimizationMonitor(region)
    
    # Publish metrics to CloudWatch
    monitor.publish_metrics(results)
    
    # Check for high savings opportunities
    monitor.check_savings_threshold(results, threshold=500.0)
    
    return results

if __name__ == "__main__":
    # Run monitored scan
    results = monitored_cost_scan('us-east-1', 'production')
    print(f"Scan complete. Total savings: ${results.get('total_monthly_savings', 0):.2f}/month")
```

These advanced usage examples demonstrate how to leverage the AWS Cost Optimization Scanner in enterprise environments with sophisticated requirements for automation, monitoring, and integration with existing DevOps workflows.
