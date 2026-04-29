# API Reference

Complete API documentation for all classes and methods in the AWS Cost Optimization Scanner.

## Core Classes

```{eval-rst}
.. automodule:: cost_optimizer
   :members:
   :undoc-members:
   :show-inheritance:

.. automodule:: html_report_generator
   :members:
   :undoc-members:
   :show-inheritance:
```

## CostOptimizer Class

The main cost optimization engine that analyzes AWS resources across 30 services.

```{eval-rst}
.. autoclass:: cost_optimizer.CostOptimizer
   :members:
   :undoc-members:
   :show-inheritance:
```

### Core Methods

#### Initialization and Configuration

```{eval-rst}
.. automethod:: cost_optimizer.CostOptimizer.__init__
.. automethod:: cost_optimizer.CostOptimizer.get_regional_pricing_multiplier
.. automethod:: cost_optimizer.CostOptimizer.add_warning
.. automethod:: cost_optimizer.CostOptimizer.add_permission_issue
```

#### Main Scanning Methods

```{eval-rst}
.. automethod:: cost_optimizer.CostOptimizer.scan_region
```

### Service-Specific Analysis Methods

#### Compute Services

```{eval-rst}
.. automethod:: cost_optimizer.CostOptimizer.get_ec2_instance_count
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_ec2_checks
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_lambda_checks
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_auto_scaling_checks
```

#### Storage Services

```{eval-rst}
.. automethod:: cost_optimizer.CostOptimizer.get_ebs_volume_count
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_ebs_checks
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_s3_checks
.. automethod:: cost_optimizer.CostOptimizer.get_efs_file_system_count
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_efs_checks
```

#### Database Services

```{eval-rst}
.. automethod:: cost_optimizer.CostOptimizer.get_rds_instance_count
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_rds_checks
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_dynamodb_checks
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_elasticache_checks
.. automethod:: cost_optimizer.CostOptimizer.get_enhanced_opensearch_checks
```

## HTMLReportGenerator Class

Professional HTML report generator for cost optimization results with interactive executive summary dashboard.

**New in v2.6.0**: Executive Summary Tab with interactive charts showing cost savings distribution.

```{eval-rst}
.. autoclass:: html_report_generator.HTMLReportGenerator
   :members:
   :undoc-members:
   :show-inheritance:
```

### Report Generation Methods

```{eval-rst}
.. automethod:: html_report_generator.HTMLReportGenerator.__init__
.. automethod:: html_report_generator.HTMLReportGenerator.generate_html_report
.. automethod:: html_report_generator.HTMLReportGenerator._get_executive_summary_content
```

### Executive Summary Features

The HTML reports now include an executive summary tab with:

- **Interactive Pie Chart**: Cost savings distribution by AWS service
- **Interactive Bar Chart**: Top services ranked by savings potential  
- **Key Metrics Dashboard**: Total savings, recommendations, and services scanned
- **Click-to-Filter Navigation**: Click chart segments to navigate to service tabs
- **Professional AWS Styling**: Executive-ready presentation
- **Dark Mode Toggle**: Switch between light and dark themes with persistent preference

## Usage Examples

### Basic Usage

```python
from cost_optimizer import CostOptimizer
from html_report_generator import HTMLReportGenerator

# Initialize the optimizer
optimizer = CostOptimizer('us-east-1', profile='production')

# Run comprehensive scan
results = optimizer.scan_region()

# Generate HTML report
generator = HTMLReportGenerator(results)
report_path = generator.generate_html_report()
print(f"Report generated: {report_path}")
```

### Service Filtering

```python
# Scan only storage services
results = optimizer.scan_region(scan_only=['s3', 'ebs', 'efs'])

# Skip compute services
results = optimizer.scan_region(skip_services=['ec2', 'lambda'])

# Fast mode for large S3 environments
optimizer = CostOptimizer('us-east-1', fast_mode=True)
results = optimizer.scan_region(scan_only=['s3'])
```

### Error Handling

```python
try:
    optimizer = CostOptimizer('us-east-1')
    results = optimizer.scan_region()
except ClientError as e:
    print(f"AWS API Error: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")

# Check for warnings and permission issues
if optimizer.scan_warnings:
    print("Scan warnings:", optimizer.scan_warnings)
if optimizer.permission_issues:
    print("Permission issues:", optimizer.permission_issues)
```
