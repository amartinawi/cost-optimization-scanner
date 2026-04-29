# Examples Index

This section provides comprehensive examples for different use cases and scenarios.

## Basic Examples

### Simple Cost Optimization Scan

```python
from cost_optimizer import CostOptimizer
from html_report_generator import HTMLReportGenerator

# Initialize optimizer
optimizer = CostOptimizer('us-east-1')

# Run comprehensive scan
results = optimizer.scan_region()

# Generate report
generator = HTMLReportGenerator(results)
report_path = generator.generate_html_report()

print(f"Report generated: {report_path}")
print(f"Total potential savings: ${results.get('total_monthly_savings', 0):.2f}/month")
```

### Service-Specific Analysis

```python
# Storage optimization focus
storage_results = optimizer.scan_region(scan_only=['s3', 'ebs', 'efs'])

# Compute optimization focus  
compute_results = optimizer.scan_region(scan_only=['ec2', 'lambda', 'auto_scaling'])

# Database optimization focus
database_results = optimizer.scan_region(scan_only=['rds', 'dynamodb', 'elasticache'])
```

## Enterprise Examples

### Multi-Account Organization Scan

```python
import boto3
from concurrent.futures import ThreadPoolExecutor

class OrganizationScanner:
    def __init__(self, org_profile='org-master'):
        self.org_session = boto3.Session(profile_name=org_profile)
        self.org_client = self.org_session.client('organizations')
    
    def scan_all_accounts(self, regions=['us-east-1']):
        accounts = self._get_active_accounts()
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = []
            
            for account in accounts:
                for region in regions:
                    future = executor.submit(
                        self._scan_account, 
                        account['Id'], 
                        account['Name'], 
                        region
                    )
                    futures.append(future)
            
            results = []
            for future in futures:
                result = future.result()
                if result:
                    results.append(result)
        
        return self._consolidate_results(results)
    
    def _get_active_accounts(self):
        paginator = self.org_client.get_paginator('list_accounts')
        accounts = []
        
        for page in paginator.paginate():
            for account in page['Accounts']:
                if account['Status'] == 'ACTIVE':
                    accounts.append(account)
        
        return accounts

# Usage
scanner = OrganizationScanner('org-master')
org_results = scanner.scan_all_accounts(['us-east-1', 'eu-west-1'])
```

### Large-Scale Performance Optimization

```python
class HighPerformanceScanner:
    def __init__(self, region, profile=None):
        self.region = region
        self.profile = profile
    
    def scan_with_batching(self, batch_size=5):
        """Scan services in batches to manage memory usage."""
        
        all_services = [
            'ec2', 'ebs', 'rds', 's3', 'lambda', 'dynamodb',
            'efs', 'elasticache', 'opensearch', 'containers'
        ]
        
        # Process in batches
        batches = [all_services[i:i+batch_size] 
                  for i in range(0, len(all_services), batch_size)]
        
        consolidated_results = {'services': {}}
        
        for i, batch in enumerate(batches):
            print(f"Processing batch {i+1}/{len(batches)}: {batch}")
            
            optimizer = CostOptimizer(self.region, self.profile, fast_mode=True)
            batch_results = optimizer.scan_region(scan_only=batch)
            
            # Merge results
            consolidated_results['services'].update(batch_results['services'])
            
            # Update metadata from last batch
            if i == len(batches) - 1:
                consolidated_results.update({
                    k: v for k, v in batch_results.items() 
                    if k != 'services'
                })
        
        return consolidated_results

# Usage for large accounts
scanner = HighPerformanceScanner('us-east-1', 'production')
results = scanner.scan_with_batching(batch_size=3)
```

## CI/CD Integration Examples

### GitHub Actions Integration

```yaml
# .github/workflows/cost-optimization.yml
name: Weekly Cost Optimization Scan

on:
  schedule:
    - cron: '0 6 * * 1'  # Every Monday at 6 AM
  workflow_dispatch:

jobs:
  cost-scan:
    runs-on: ubuntu-latest
    
    strategy:
      matrix:
        region: [us-east-1, eu-west-1, ap-southeast-1]
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.9'
    
    - name: Install dependencies
      run: pip install -r requirements.txt
    
    - name: Configure AWS credentials
      uses: aws-actions/configure-aws-credentials@v2
      with:
        aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
        aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        aws-region: ${{ matrix.region }}
    
    - name: Run cost optimization scan
      run: |
        python3 cost_optimizer.py ${{ matrix.region }} \
          --output-dir reports/${{ matrix.region }}/
    
    - name: Upload reports
      uses: actions/upload-artifact@v3
      with:
        name: cost-reports-${{ matrix.region }}
        path: reports/${{ matrix.region }}/
```

### Jenkins Pipeline Integration

```groovy
pipeline {
    agent any
    
    parameters {
        choice(
            name: 'ENVIRONMENT',
            choices: ['production', 'staging', 'development'],
            description: 'Environment to scan'
        )
        booleanParam(
            name: 'SEND_ALERTS',
            defaultValue: true,
            description: 'Send alerts for high savings opportunities'
        )
    }
    
    stages {
        stage('Cost Optimization Scan') {
            parallel {
                stage('US East 1') {
                    steps {
                        script {
                            scanRegion('us-east-1', params.ENVIRONMENT)
                        }
                    }
                }
                stage('EU West 1') {
                    steps {
                        script {
                            scanRegion('eu-west-1', params.ENVIRONMENT)
                        }
                    }
                }
            }
        }
        
        stage('Generate Consolidated Report') {
            steps {
                script {
                    sh '''
                        python3 scripts/consolidate_reports.py \
                            --input-dir reports/ \
                            --output reports/consolidated_report.html
                    '''
                }
            }
        }
        
        stage('Send Notifications') {
            when {
                expression { params.SEND_ALERTS }
            }
            steps {
                script {
                    def savings = sh(
                        script: "python3 scripts/extract_total_savings.py reports/",
                        returnStdout: true
                    ).trim()
                    
                    if (savings.toFloat() > 1000) {
                        slackSend(
                            channel: '#finops',
                            color: 'warning',
                            message: """
                                🚨 High Cost Savings Opportunity!
                                Environment: ${params.ENVIRONMENT}
                                Total Potential Savings: \$${savings}/month
                                Report: ${BUILD_URL}artifact/reports/consolidated_report.html
                            """
                        )
                    }
                }
            }
        }
    }
    
    post {
        always {
            publishHTML([
                allowMissing: false,
                alwaysLinkToLastBuild: true,
                keepAll: true,
                reportDir: 'reports',
                reportFiles: 'consolidated_report.html',
                reportName: 'Cost Optimization Report'
            ])
        }
    }
}

def scanRegion(region, environment) {
    withCredentials([
        [$class: 'AmazonWebServicesCredentialsBinding', 
         credentialsId: "aws-${environment}"]
    ]) {
        sh """
            python3 cost_optimizer.py ${region} \
                --profile ${environment} \
                --output-dir reports/${region}/
        """
    }
}
```

## Custom Integration Examples

### Slack Integration

```python
import requests
import json

class SlackNotifier:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url
    
    def send_cost_alert(self, scan_results, threshold=500):
        """Send Slack alert for high savings opportunities."""
        
        total_savings = scan_results.get('total_monthly_savings', 0)
        
        if total_savings < threshold:
            return False
        
        # Get top recommendations
        top_recs = self._get_top_recommendations(scan_results, limit=5)
        
        message = {
            "text": f"🚨 Cost Optimization Alert: ${total_savings:.2f}/month potential savings!",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "💰 High Cost Savings Opportunity Detected!"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Total Savings:*\n${total_savings:.2f}/month"
                        },
                        {
                            "type": "mrkdwn", 
                            "text": f"*Account:*\n{scan_results.get('account_id', 'Unknown')}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Region:*\n{scan_results.get('region', 'Unknown')}"
                        }
                    ]
                }
            ]
        }
        
        # Add top recommendations
        if top_recs:
            rec_text = "\n".join([
                f"• {rec['service']}: ${rec['savings']:.2f}/month"
                for rec in top_recs
            ])
            
            message["blocks"].append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Top Recommendations:*\n{rec_text}"
                }
            })
        
        response = requests.post(self.webhook_url, json=message)
        return response.status_code == 200
    
    def _get_top_recommendations(self, scan_results, limit=5):
        """Extract top recommendations by savings."""
        all_recs = []
        
        for service_name, service_data in scan_results.get('services', {}).items():
            for rec in service_data.get('recommendations', []):
                if 'estimated_monthly_savings' in rec:
                    all_recs.append({
                        'service': service_name,
                        'savings': rec['estimated_monthly_savings'],
                        'description': rec.get('description', '')
                    })
        
        return sorted(all_recs, key=lambda x: x['savings'], reverse=True)[:limit]

# Usage
notifier = SlackNotifier('https://hooks.slack.com/services/YOUR/WEBHOOK/URL')
optimizer = CostOptimizer('us-east-1')
results = optimizer.scan_region()
notifier.send_cost_alert(results, threshold=1000)
```

### Email Reporting

```python
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

class EmailReporter:
    def __init__(self, smtp_server, smtp_port, username, password):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
    
    def send_cost_report(self, scan_results, recipients, attach_html=True):
        """Send cost optimization report via email."""
        
        # Generate HTML report
        generator = HTMLReportGenerator(scan_results)
        report_path = generator.generate_html_report()
        
        # Create email
        msg = MIMEMultipart()
        msg['From'] = self.username
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = f"AWS Cost Optimization Report - {scan_results.get('region', 'Unknown')}"
        
        # Email body
        body = self._create_email_body(scan_results)
        msg.attach(MIMEText(body, 'html'))
        
        # Attach HTML report
        if attach_html:
            with open(report_path, 'rb') as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
                encoders.encode_base64(part)
                part.add_header(
                    'Content-Disposition',
                    f'attachment; filename= {os.path.basename(report_path)}'
                )
                msg.attach(part)
        
        # Send email
        server = smtplib.SMTP(self.smtp_server, self.smtp_port)
        server.starttls()
        server.login(self.username, self.password)
        server.send_message(msg)
        server.quit()
        
        return True
    
    def _create_email_body(self, scan_results):
        """Create HTML email body with summary."""
        
        total_savings = scan_results.get('total_monthly_savings', 0)
        service_count = len(scan_results.get('services', {}))
        
        return f"""
        <html>
        <body>
            <h2>AWS Cost Optimization Report</h2>
            
            <h3>Summary</h3>
            <ul>
                <li><strong>Total Potential Savings:</strong> ${total_savings:.2f}/month</li>
                <li><strong>Services Analyzed:</strong> {service_count}</li>
                <li><strong>Region:</strong> {scan_results.get('region', 'Unknown')}</li>
                <li><strong>Account:</strong> {scan_results.get('account_id', 'Unknown')}</li>
            </ul>
            
            <h3>Next Steps</h3>
            <ol>
                <li>Review the attached detailed HTML report</li>
                <li>Prioritize high-confidence, high-savings recommendations</li>
                <li>Implement quick wins (idle resource cleanup)</li>
                <li>Plan larger optimizations (Reserved Instances, rightsizing)</li>
            </ol>
            
            <p><em>This report was generated automatically by the AWS Cost Optimization Scanner.</em></p>
        </body>
        </html>
        """

# Usage
reporter = EmailReporter('smtp.gmail.com', 587, 'your-email@gmail.com', 'your-password')
optimizer = CostOptimizer('us-east-1')
results = optimizer.scan_region()
reporter.send_cost_report(results, ['finops@company.com', 'devops@company.com'])
```

## Monitoring and Alerting Examples

### CloudWatch Custom Metrics

```python
import boto3
from datetime import datetime

class CloudWatchMetrics:
    def __init__(self, region='us-east-1'):
        self.cloudwatch = boto3.client('cloudwatch', region_name=region)
    
    def publish_cost_metrics(self, scan_results):
        """Publish cost optimization metrics to CloudWatch."""
        
        metrics = []
        
        # Total savings metric
        metrics.append({
            'MetricName': 'TotalPotentialSavings',
            'Value': scan_results.get('total_monthly_savings', 0),
            'Unit': 'None',
            'Timestamp': datetime.utcnow()
        })
        
        # Service-specific metrics
        for service_name, service_data in scan_results.get('services', {}).items():
            service_savings = sum(
                rec.get('estimated_monthly_savings', 0)
                for rec in service_data.get('recommendations', [])
            )
            
            metrics.append({
                'MetricName': 'ServicePotentialSavings',
                'Value': service_savings,
                'Unit': 'None',
                'Timestamp': datetime.utcnow(),
                'Dimensions': [
                    {'Name': 'ServiceName', 'Value': service_name}
                ]
            })
        
        # Publish metrics in batches (CloudWatch limit: 20 metrics per call)
        for i in range(0, len(metrics), 20):
            batch = metrics[i:i+20]
            self.cloudwatch.put_metric_data(
                Namespace='CostOptimization',
                MetricData=batch
            )
        
        return len(metrics)

# Usage
metrics = CloudWatchMetrics('us-east-1')
optimizer = CostOptimizer('us-east-1')
results = optimizer.scan_region()
metric_count = metrics.publish_cost_metrics(results)
print(f"Published {metric_count} metrics to CloudWatch")
```

These examples demonstrate the flexibility and power of the AWS Cost Optimization Scanner for various enterprise scenarios and integration patterns.
