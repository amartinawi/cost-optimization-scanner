# Troubleshooting Guide

This comprehensive troubleshooting guide covers common issues, solutions, and best practices for the AWS Cost Optimization Scanner.

## Quick Diagnosis

### Common Error Patterns

| Error Type | Symptoms | Quick Fix |
|------------|----------|-----------|
| **Permission Issues** | `AccessDenied`, `UnauthorizedOperation` | Check IAM permissions |
| **API Throttling** | `Throttling`, `RequestLimitExceeded` | Enable fast mode or reduce concurrency |
| **Region Issues** | `InvalidRegion`, service not available | Verify region support |
| **Credential Issues** | `NoCredentialsError`, `InvalidAccessKeyId` | Configure AWS credentials |
| **Memory Issues** | `MemoryError`, slow performance | Use service filtering |

## Installation & Setup Issues

### 1. Python Dependencies

**Problem**: Import errors or missing dependencies
```bash
ModuleNotFoundError: No module named 'boto3'
```

**Solution**:
```bash
# Install all required dependencies
pip install -r requirements.txt

# Or install individually
pip install boto3>=1.34.0 botocore>=1.34.0 python-dateutil>=2.8.2

# For development
pip install pytest sphinx sphinx-rtd-theme
```

**Verification**:
```python
import boto3
import botocore
from dateutil import parser
print("✅ All dependencies installed correctly")
```

### 2. AWS Credentials Configuration

**Problem**: No AWS credentials configured
```
NoCredentialsError: Unable to locate credentials
```

**Solutions**:

**Option 1: AWS CLI Configuration**
```bash
aws configure
# Enter: Access Key ID, Secret Access Key, Region, Output format
```

**Option 2: Environment Variables**
```bash
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_DEFAULT_REGION=us-east-1
```

**Option 3: IAM Roles (EC2/Lambda)**
```python
# No configuration needed - uses instance/function role
optimizer = CostOptimizer('us-east-1')
```

**Option 4: AWS Profiles**
```bash
# Configure named profile
aws configure --profile production

# Use in scanner
optimizer = CostOptimizer('us-east-1', profile='production')
```

**Verification**:
```python
import boto3
try:
    sts = boto3.client('sts')
    identity = sts.get_caller_identity()
    print(f"✅ Credentials working. Account: {identity['Account']}")
except Exception as e:
    print(f"❌ Credential issue: {e}")
```

## Permission Issues

### 3. IAM Permission Errors

**Problem**: Access denied errors for specific services
```
ClientError: An error occurred (AccessDenied) when calling the DescribeInstances operation
```

**Diagnosis**:
```python
# Check which permissions are missing
optimizer = CostOptimizer('us-east-1')
results = optimizer.scan_region()

# Review permission issues
if optimizer.permission_issues:
    for issue in optimizer.permission_issues:
        print(f"Permission issue: {issue}")
```

**Solution**: Apply comprehensive IAM policy
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "cost-optimization-hub:ListRecommendations",
                "cost-optimization-hub:GetRecommendation",
                "compute-optimizer:GetEC2InstanceRecommendations",
                "compute-optimizer:GetEBSVolumeRecommendations",
                "ec2:DescribeInstances",
                "ec2:DescribeVolumes",
                "ec2:DescribeSnapshots",
                "rds:DescribeDBInstances",
                "s3:ListAllMyBuckets",
                "s3:GetBucketLocation",
                "dynamodb:ListTables",
                "lambda:ListFunctions",
                "cloudwatch:GetMetricStatistics",
                "cloudwatch:ListMetrics"
            ],
            "Resource": "*"
        }
    ]
}
```

**Minimal Permissions Testing**:
```python
def test_minimal_permissions():
    """Test with minimal permissions to identify required ones."""
    
    required_permissions = [
        ('ec2', 'describe_instances'),
        ('s3', 'list_buckets'),
        ('rds', 'describe_db_instances'),
        ('lambda', 'list_functions')
    ]
    
    for service, operation in required_permissions:
        try:
            client = boto3.client(service)
            getattr(client, operation)()
            print(f"✅ {service}:{operation}")
        except Exception as e:
            print(f"❌ {service}:{operation} - {e}")
```

### 4. Service-Specific Permission Issues

**Problem**: Specific services fail while others work

**EC2 Permissions**:
```json
{
    "Effect": "Allow",
    "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeVolumes", 
        "ec2:DescribeSnapshots",
        "ec2:DescribeImages",
        "ec2:DescribeAddresses"
    ],
    "Resource": "*"
}
```

**S3 Permissions**:
```json
{
    "Effect": "Allow", 
    "Action": [
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "s3:GetBucketLifecycleConfiguration",
        "s3:ListBucket"
    ],
    "Resource": "*"
}
```

**CloudWatch Permissions**:
```json
{
    "Effect": "Allow",
    "Action": [
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "cloudwatch:DescribeAlarms"
    ],
    "Resource": "*"
}
```

## Performance Issues

### 5. Slow Scan Performance

**Problem**: Scans taking too long (>30 minutes)

**Diagnosis**:
```python
import time
start_time = time.time()

optimizer = CostOptimizer('us-east-1')
results = optimizer.scan_region()

scan_duration = time.time() - start_time
print(f"Scan completed in {scan_duration:.2f} seconds")

# Check resource counts
for service, data in results['services'].items():
    count = data.get('resource_count', {})
    if isinstance(count, dict):
        total = sum(count.values()) if count else 0
    else:
        total = count
    print(f"{service}: {total} resources")
```

**Solutions**:

**Option 1: Enable Fast Mode**
```python
# Skip CloudWatch metrics for faster scanning
optimizer = CostOptimizer('us-east-1', fast_mode=True)
results = optimizer.scan_region()
```

**Option 2: Service Filtering**
```python
# Scan only specific services
results = optimizer.scan_region(scan_only=['s3', 'ec2', 'rds'])

# Skip resource-heavy services
results = optimizer.scan_region(skip_services=['s3', 'cloudwatch'])
```

**Option 3: Regional Optimization**
```python
# Scan fewer regions
regions = ['us-east-1', 'eu-west-1']  # Instead of all regions
for region in regions:
    optimizer = CostOptimizer(region)
    results = optimizer.scan_region()
```

### 6. Memory Issues

**Problem**: High memory usage or out-of-memory errors

**Diagnosis**:
```python
import psutil
import os

def monitor_memory():
    process = psutil.Process(os.getpid())
    memory_mb = process.memory_info().rss / 1024 / 1024
    print(f"Memory usage: {memory_mb:.2f} MB")

# Monitor during scan
monitor_memory()
optimizer = CostOptimizer('us-east-1')
monitor_memory()
results = optimizer.scan_region()
monitor_memory()
```

**Solutions**:

**Option 1: Service Filtering**
```python
# Process services in batches
service_groups = [
    ['ec2', 'ebs'],
    ['s3', 'efs'], 
    ['rds', 'dynamodb'],
    ['lambda', 'api_gateway']
]

all_results = {}
for group in service_groups:
    results = optimizer.scan_region(scan_only=group)
    all_results.update(results['services'])
```

**Option 2: Fast Mode**
```python
# Reduce memory usage by skipping CloudWatch metrics
optimizer = CostOptimizer('us-east-1', fast_mode=True)
```

## API Issues

### 7. API Throttling

**Problem**: Rate limiting errors
```
ClientError: An error occurred (Throttling) when calling the DescribeInstances operation
```

**Built-in Solution**: The scanner includes automatic retry logic
```python
# Retry configuration is automatic
retry_config = Config(
    retries={
        'max_attempts': 10,
        'mode': 'adaptive'  # Exponential backoff
    }
)
```

**Manual Throttling Management**:
```python
import time
from botocore.exceptions import ClientError

def scan_with_throttling_protection():
    """Scan with additional throttling protection."""
    
    max_retries = 3
    base_delay = 1
    
    for attempt in range(max_retries):
        try:
            optimizer = CostOptimizer('us-east-1')
            return optimizer.scan_region()
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'Throttling':
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                print(f"Throttling detected. Waiting {delay} seconds...")
                time.sleep(delay)
            else:
                raise
    
    raise Exception("Max retries exceeded due to throttling")
```

### 8. Regional Availability Issues

**Problem**: Service not available in region
```
ClientError: An error occurred (InvalidAction) when calling the ListClusters operation
```

**Diagnosis**:
```python
def check_service_availability(region, service_name):
    """Check if service is available in region."""
    
    try:
        session = boto3.Session()
        client = session.client(service_name, region_name=region)
        
        # Try a basic list operation
        if service_name == 'ecs':
            client.list_clusters()
        elif service_name == 'eks':
            client.list_clusters()
        elif service_name == 'redshift':
            client.describe_clusters()
            
        print(f"✅ {service_name} available in {region}")
        return True
        
    except Exception as e:
        print(f"❌ {service_name} not available in {region}: {e}")
        return False

# Test service availability
regions = ['us-east-1', 'eu-west-1', 'ap-southeast-1']
services = ['ecs', 'eks', 'redshift', 'lightsail']

for region in regions:
    print(f"\nTesting region: {region}")
    for service in services:
        check_service_availability(region, service)
```

**Solution**: Use region-appropriate scanning
```python
# Define service availability by region
REGIONAL_SERVICE_AVAILABILITY = {
    'us-east-1': ['all'],  # All services available
    'eu-west-1': ['all'],
    'ap-southeast-1': ['ec2', 'rds', 's3', 'lambda'],  # Limited services
    'us-gov-east-1': ['ec2', 'rds', 's3']  # GovCloud limitations
}

def scan_region_with_availability_check(region):
    """Scan region with service availability awareness."""
    
    available_services = REGIONAL_SERVICE_AVAILABILITY.get(region, ['ec2', 's3', 'rds'])
    
    if 'all' in available_services:
        # All services available
        return CostOptimizer(region).scan_region()
    else:
        # Limited services
        return CostOptimizer(region).scan_region(scan_only=available_services)
```

## Report Generation Issues

### 9. HTML Report Problems

**Problem**: Report generation fails or produces empty reports

**Diagnosis**:
```python
def diagnose_report_issues(scan_results):
    """Diagnose report generation issues."""
    
    print("=== Report Diagnosis ===")
    
    # Check scan results structure
    if not scan_results:
        print("❌ No scan results provided")
        return
    
    if 'services' not in scan_results:
        print("❌ No services data in results")
        return
    
    # Check service data
    services = scan_results['services']
    print(f"✅ Found {len(services)} services")
    
    total_recommendations = 0
    for service_name, service_data in services.items():
        rec_count = len(service_data.get('recommendations', []))
        total_recommendations += rec_count
        print(f"  {service_name}: {rec_count} recommendations")
    
    print(f"✅ Total recommendations: {total_recommendations}")
    
    # Check for common issues
    if total_recommendations == 0:
        print("⚠️  No recommendations found - this may result in empty report")
    
    return True

# Use diagnosis
results = optimizer.scan_region()
diagnose_report_issues(results)
```

**Solution**: Ensure proper data structure
```python
from html_report_generator import HTMLReportGenerator

def generate_report_with_error_handling(scan_results):
    """Generate report with comprehensive error handling."""
    
    try:
        # Validate results structure
        if not scan_results or 'services' not in scan_results:
            raise ValueError("Invalid scan results structure")
        
        # Generate report
        generator = HTMLReportGenerator(scan_results)
        report_path = generator.generate_html_report()
        
        # Verify report was created
        if os.path.exists(report_path):
            file_size = os.path.getsize(report_path)
            print(f"✅ Report generated: {report_path} ({file_size} bytes)")
            return report_path
        else:
            raise FileNotFoundError("Report file was not created")
            
    except Exception as e:
        print(f"❌ Report generation failed: {e}")
        
        # Create minimal report for debugging
        minimal_results = {
            'services': {'debug': {'recommendations': []}},
            'account_id': 'debug',
            'region': 'debug'
        }
        
        generator = HTMLReportGenerator(minimal_results)
        debug_report = generator.generate_html_report('debug_report.html')
        print(f"📝 Debug report created: {debug_report}")
        
        return None
```

### 10. Large Report Performance

**Problem**: Reports with 1000+ recommendations are slow to load

**Solution**: Implement pagination and filtering
```python
def generate_optimized_report(scan_results, max_recommendations_per_service=100):
    """Generate optimized report for large datasets."""
    
    # Limit recommendations per service
    optimized_results = scan_results.copy()
    
    for service_name, service_data in optimized_results['services'].items():
        recommendations = service_data.get('recommendations', [])
        
        if len(recommendations) > max_recommendations_per_service:
            # Sort by savings and take top N
            sorted_recs = sorted(
                recommendations,
                key=lambda x: x.get('estimated_monthly_savings', 0),
                reverse=True
            )
            
            service_data['recommendations'] = sorted_recs[:max_recommendations_per_service]
            service_data['truncated_count'] = len(recommendations) - max_recommendations_per_service
    
    # Generate report
    generator = HTMLReportGenerator(optimized_results)
    return generator.generate_html_report()
```

## Advanced Troubleshooting

### 11. Debug Mode

Enable comprehensive debugging:

```python
import logging

# Enable debug logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cost_optimizer_debug.log'),
        logging.StreamHandler()
    ]
)

# Enable boto3 debug logging
boto3.set_stream_logger('boto3', logging.DEBUG)
boto3.set_stream_logger('botocore', logging.DEBUG)

# Run scan with debug logging
optimizer = CostOptimizer('us-east-1')
results = optimizer.scan_region()
```

### 12. Network Connectivity Issues

**Problem**: Network timeouts or connection errors

**Diagnosis**:
```python
import requests
import socket

def test_aws_connectivity():
    """Test connectivity to AWS services."""
    
    endpoints = [
        'https://ec2.us-east-1.amazonaws.com',
        'https://s3.amazonaws.com',
        'https://rds.us-east-1.amazonaws.com'
    ]
    
    for endpoint in endpoints:
        try:
            response = requests.get(endpoint, timeout=10)
            print(f"✅ {endpoint}: {response.status_code}")
        except Exception as e:
            print(f"❌ {endpoint}: {e}")

def test_dns_resolution():
    """Test DNS resolution for AWS endpoints."""
    
    hostnames = [
        'ec2.us-east-1.amazonaws.com',
        's3.amazonaws.com',
        'sts.amazonaws.com'
    ]
    
    for hostname in hostnames:
        try:
            ip = socket.gethostbyname(hostname)
            print(f"✅ {hostname} -> {ip}")
        except Exception as e:
            print(f"❌ {hostname}: {e}")

# Run connectivity tests
test_aws_connectivity()
test_dns_resolution()
```

**Solutions**:
- Configure proxy settings if behind corporate firewall
- Verify security group rules for EC2 instances
- Check VPC endpoints for private subnet access

### 13. Cross-Account Access Issues

**Problem**: Scanning multiple accounts fails

**Solution**: Implement proper cross-account role assumption
```python
def scan_cross_account(account_id, role_name, region):
    """Scan another account using cross-account role."""
    
    try:
        # Assume role in target account
        sts = boto3.client('sts')
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        
        assumed_role = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"CostOptimization-{account_id}",
            DurationSeconds=3600  # 1 hour
        )
        
        # Create session with assumed role credentials
        session = boto3.Session(
            aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
            aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
            aws_session_token=assumed_role['Credentials']['SessionToken']
        )
        
        # Initialize optimizer with new session
        optimizer = CostOptimizer(region)
        optimizer.session = session
        
        # Reinitialize clients with new session
        optimizer._initialize_clients()
        
        return optimizer.scan_region()
        
    except Exception as e:
        print(f"Cross-account scan failed for {account_id}: {e}")
        return None
```

## Getting Help

### 14. Collecting Debug Information

When reporting issues, collect this information:

```python
def collect_debug_info():
    """Collect comprehensive debug information."""
    
    import sys
    import platform
    
    debug_info = {
        'python_version': sys.version,
        'platform': platform.platform(),
        'boto3_version': boto3.__version__,
        'botocore_version': botocore.__version__,
        'aws_region': os.getenv('AWS_DEFAULT_REGION', 'not_set'),
        'aws_profile': os.getenv('AWS_PROFILE', 'not_set')
    }
    
    # Test AWS connectivity
    try:
        sts = boto3.client('sts')
        identity = sts.get_caller_identity()
        debug_info['aws_account'] = identity['Account']
        debug_info['aws_user_arn'] = identity['Arn']
    except Exception as e:
        debug_info['aws_error'] = str(e)
    
    # Scanner version
    try:
        optimizer = CostOptimizer('us-east-1')
        debug_info['scanner_version'] = '2.5.9'
    except Exception as e:
        debug_info['scanner_error'] = str(e)
    
    print("=== Debug Information ===")
    for key, value in debug_info.items():
        print(f"{key}: {value}")
    
    return debug_info

# Collect debug info when reporting issues
debug_info = collect_debug_info()
```

### 15. Common Solutions Summary

| Issue | Quick Solution |
|-------|----------------|
| **Slow scans** | Use `fast_mode=True` and service filtering |
| **Permission errors** | Apply comprehensive IAM policy |
| **Memory issues** | Use service filtering and process in batches |
| **API throttling** | Built-in retry logic handles this automatically |
| **Empty reports** | Check scan results structure and recommendations |
| **Region errors** | Verify service availability in target region |
| **Credential issues** | Configure AWS CLI or use IAM roles |

### 16. Best Practices for Troubleshooting

1. **Start Simple**: Test with minimal configuration first
2. **Check Logs**: Enable debug logging for detailed information
3. **Isolate Issues**: Use service filtering to identify problematic services
4. **Verify Permissions**: Test IAM permissions incrementally
5. **Monitor Resources**: Check memory and CPU usage during scans
6. **Test Connectivity**: Verify network access to AWS services

For additional help, check the project's GitHub Issues or create a new issue with debug information collected using the `collect_debug_info()` function.
