# Contributing to AWS Cost Optimization Scanner

We welcome contributions to the AWS Cost Optimization Scanner! This guide will help you get started with contributing to this production-ready tool that analyzes 30 AWS services with 220+ cost optimization checks.

## 🚀 Getting Started

### Prerequisites
- Python 3.8+
- AWS CLI configured with appropriate permissions
- boto3 library
- Basic understanding of AWS services and cost optimization

### Development Setup

```bash
# Clone the repository
git clone https://github.com/aws-cost-optimizer/aws-cost-optimizer.git
cd aws-cost-optimizer

# Install dependencies
pip install -r requirements.txt

# Test with your AWS credentials
python3 cost_optimizer.py us-east-1 --profile your-profile
```

## 📋 How to Contribute

### 1. Reporting Issues
- Use GitHub Issues to report bugs or request features
- Include AWS region, Python version, and error messages
- Provide sample output or logs when possible
- Check existing issues before creating new ones

### 2. Feature Requests
- Describe the AWS service or optimization opportunity
- Explain the potential cost savings impact
- Provide AWS documentation references
- Consider implementation complexity

### 3. Code Contributions
- Fork the repository
- Create a feature branch (`git checkout -b feature/new-service`)
- Make your changes following our coding standards
- Test thoroughly with multiple AWS accounts/regions
- Submit a pull request with detailed description

## 🔧 Development Guidelines

### Code Structure

The project follows a modular architecture:

```
cost_optimizer.py           # Main engine (8,677 lines)
├── CostOptimizer class     # Core optimization logic
├── Service analysis methods # One per AWS service
├── Enhanced check methods  # Advanced optimization checks
└── Utility methods        # Helper functions

html_report_generator.py    # Report generation (3,699 lines)
├── HTMLReportGenerator    # Professional report creation with executive summary
├── Executive Summary Tab  # Interactive charts and key metrics dashboard
├── Chart.js Integration   # Cost distribution visualizations
├── Smart grouping logic   # Recommendation categorization
└── UI components         # Interactive elements
```

### Adding New AWS Services

To add support for a new AWS service:

1. **Add Service Client** (in `__init__` method):
```python
self.new_service = self.session.client('new-service', region_name=region, config=retry_config)
```

2. **Implement Analysis Method**:
```python
def get_enhanced_newservice_checks(self) -> Dict[str, Any]:
    """Get enhanced NewService cost optimization checks"""
    checks = {
        'optimization_category_1': [],
        'optimization_category_2': []
    }
    
    try:
        # Use pagination for scalability
        paginator = self.new_service.get_paginator('list_resources')
        for page in paginator.paginate():
            for resource in page.get('Resources', []):
                # Analyze resource for optimization opportunities
                pass
    except Exception as e:
        self.add_warning(f"Could not analyze NewService: {e}", "newservice")
    
    return {'recommendations': recommendations, 'checks': checks}
```

3. **Add to Service Filtering**:
```python
# In service_map dictionary
'newservice': ['newservice'],  # New service category
```

4. **Add to Main Scan Method**:
```python
# In scan_region method
if should_scan_service('newservice'):
    print("🔧 Scanning NewService optimization...")
    enhanced_newservice_checks = self.get_enhanced_newservice_checks()
else:
    enhanced_newservice_checks = {'recommendations': []}
```

5. **Add IAM Permissions**:
```json
"newservice:ListResources",
"newservice:DescribeResource"
```

### Coding Standards

#### Python Style
- Follow PEP 8 style guidelines
- Use type hints for method signatures
- Include comprehensive docstrings
- Handle exceptions gracefully with warnings

#### AWS Integration
- Always use pagination for list operations
- Implement proper retry logic with exponential backoff
- Handle IAM permission errors gracefully
- Use region-specific clients when needed

#### Cost Calculations
- Use regional pricing multipliers
- Include estimation disclaimers
- Base calculations on current AWS pricing
- Provide savings ranges rather than exact amounts

#### Error Handling
```python
try:
    # AWS API call
    response = self.service.describe_resources()
except ClientError as e:
    error_code = e.response['Error']['Code']
    if error_code == 'UnauthorizedOperation':
        self.add_permission_issue(f"Missing permission for describe_resources", "service")
    else:
        self.add_warning(f"Could not analyze service: {e}", "service")
except Exception as e:
    self.add_warning(f"Unexpected error: {e}", "service")
```

### Testing Guidelines

#### Manual Testing
- Test with multiple AWS accounts (small and large)
- Verify across different regions
- Test service filtering combinations
- Validate HTML report generation

#### Edge Cases
- Empty AWS accounts
- Accounts with 1000+ resources per service
- Cross-region resource dependencies
- API throttling scenarios
- Permission-restricted environments

## 📊 Adding New Optimization Checks

### Check Categories
1. **Idle Resources** - 100% cost elimination
2. **Rightsizing** - 20-50% cost reduction
3. **Reserved Instances** - 30-72% savings
4. **Storage Optimization** - 20-95% savings
5. **Architecture Optimization** - Variable savings

### Implementation Pattern
```python
def get_service_optimization_checks(self) -> Dict[str, Any]:
    checks = {
        'idle_resources': [],
        'rightsizing_opportunities': [],
        'reserved_instance_opportunities': [],
        'storage_optimization': [],
        'architecture_optimization': []
    }
    
    # Implement checks with CloudWatch integration where possible
    # Use intelligent gating to prevent false positives
    # Calculate regional pricing adjustments
    
    return {'recommendations': recommendations, 'checks': checks}
```

### CloudWatch Integration
For accurate recommendations, integrate CloudWatch metrics:

```python
# Example: CPU utilization analysis
cpu_response = self.cloudwatch.get_metric_statistics(
    Namespace='AWS/ServiceName',
    MetricName='CPUUtilization',
    Dimensions=[{'Name': 'ResourceId', 'Value': resource_id}],
    StartTime=start_time,
    EndTime=end_time,
    Period=3600,
    Statistics=['Average', 'Maximum']
)

if cpu_response['Datapoints']:
    avg_cpu = sum(dp['Average'] for dp in cpu_response['Datapoints']) / len(cpu_response['Datapoints'])
    # Only recommend if utilization is actually low
    if avg_cpu < 20:
        # Add rightsizing recommendation
```

## 🎨 HTML Report Contributions

### Adding New Service Tabs
1. Add service data extraction in `_get_detailed_recommendations`
2. Implement service-specific formatting
3. Add to tab generation logic
4. Include statistics calculation
5. Test responsive design

### UI Improvements
- Follow Material Design principles
- Ensure mobile compatibility
- Maintain consistent styling
- Test across different browsers

## 📝 Documentation Standards

### Code Documentation
- Include comprehensive docstrings for all methods
- Document parameters and return values
- Explain complex algorithms and business logic
- Include usage examples

### README Updates
- Keep service counts accurate
- Update feature lists with new capabilities
- Include relevant usage examples
- Maintain accurate IAM permissions

### Architecture Documentation
- Update component diagrams for new services
- Document new optimization categories
- Include performance characteristics
- Explain integration patterns

## 🧪 Testing Contributions

### Test Coverage Areas
- Service filtering logic
- Cost calculation accuracy
- Regional pricing multipliers
- Error handling scenarios
- HTML report generation

### Performance Testing
- Large account scenarios (1000+ resources)
- Cross-region analysis
- Memory usage optimization
- Scan duration benchmarks

## 📋 Pull Request Guidelines

### Before Submitting
- [ ] Code follows Python style guidelines
- [ ] All new services include proper error handling
- [ ] IAM permissions documented
- [ ] Regional pricing considerations included
- [ ] HTML report integration completed
- [ ] Documentation updated

### PR Description Template
```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New AWS service support
- [ ] New optimization check
- [ ] Documentation update
- [ ] Performance improvement

## Testing
- [ ] Tested with multiple AWS accounts
- [ ] Verified across different regions
- [ ] HTML report generation works
- [ ] Service filtering functions correctly

## Checklist
- [ ] Code follows style guidelines
- [ ] Self-review completed
- [ ] Documentation updated
- [ ] IAM permissions documented
```

## 🏷️ Release Process

### Version Numbering
- **Major** (X.0.0): Breaking changes or major new features
- **Minor** (X.Y.0): New services or significant enhancements
- **Patch** (X.Y.Z): Bug fixes and minor improvements

### Release Checklist
- [ ] Update version in all files
- [ ] Update CHANGELOG.md
- [ ] Test with multiple accounts
- [ ] Verify documentation accuracy
- [ ] Create GitHub release
- [ ] Update badges and links

## 🤝 Community Guidelines

### Code of Conduct
- Be respectful and inclusive
- Focus on constructive feedback
- Help newcomers get started
- Share knowledge and best practices

### Communication
- Use GitHub Issues for bug reports
- Use GitHub Discussions for questions
- Be clear and specific in communications
- Provide context and examples

## 📚 Resources

### AWS Documentation
- [AWS Cost Optimization](https://aws.amazon.com/aws-cost-management/)
- [AWS Well-Architected Cost Optimization](https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/)
- [AWS Pricing](https://aws.amazon.com/pricing/)

### Development Resources
- [boto3 Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)
- [AWS CLI Reference](https://docs.aws.amazon.com/cli/latest/reference/)
- [Python Style Guide](https://pep8.org/)

Thank you for contributing to the AWS Cost Optimization Scanner! Your contributions help the entire AWS community optimize their cloud costs effectively.
