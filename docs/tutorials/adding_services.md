# Adding New AWS Services Tutorial

This comprehensive tutorial guides you through adding new AWS services to the Cost Optimization Scanner, from research to implementation and testing.

## Overview

Adding a new AWS service involves:
1. **Research & Validation**: Understanding the service and cost optimization opportunities
2. **Service Integration**: Adding AWS API client and basic connectivity
3. **Optimization Logic**: Implementing cost optimization checks
4. **Cost Calculations**: Adding pricing models and savings estimates
5. **Report Integration**: Updating HTML report generation
6. **Testing & Validation**: Comprehensive testing across scenarios
7. **Documentation**: Updating all relevant documentation

## Step 1: Research & Validation

### 1.1 Service Analysis

Before implementing, thoroughly research the AWS service:

```python
# Example: Researching AWS App Mesh for cost optimization
"""
Service: AWS App Mesh
Cost Factors:
- Envoy proxy data plane costs
- Control plane costs (per mesh)
- Data transfer costs
- CloudWatch metrics costs

Optimization Opportunities:
- Unused meshes detection
- Over-provisioned proxy resources
- Excessive logging/metrics
- Regional optimization
"""
```

### 1.2 AWS Documentation Review

Use the AWS Documentation MCP to validate service capabilities:

```python
# Research checklist:
research_checklist = {
    "service_overview": "What does the service do?",
    "pricing_model": "How is the service billed?",
    "cost_factors": "What drives costs?",
    "optimization_opportunities": "Where can costs be reduced?",
    "api_methods": "What APIs are available for analysis?",
    "cloudwatch_metrics": "What metrics are available?",
    "regional_availability": "Which regions support the service?",
    "best_practices": "What are AWS recommended best practices?"
}
```

### 1.3 Cost Optimization Potential Assessment

Evaluate the potential impact:

```python
optimization_assessment = {
    "high_impact": [
        "Idle resource detection",
        "Rightsizing opportunities", 
        "Reserved capacity options"
    ],
    "medium_impact": [
        "Configuration optimization",
        "Regional cost differences",
        "Feature usage optimization"
    ],
    "low_impact": [
        "Minor configuration tweaks",
        "Logging optimization"
    ]
}
```

## Step 2: Service Integration

### 2.1 Add Service Client

Add the AWS service client to the `CostOptimizer.__init__` method:

```python
# In cost_optimizer.py, __init__ method
def __init__(self, region: str, profile: str = None, fast_mode: bool = False):
    # ... existing initialization code ...
    
    # Add new service client (example: App Mesh)
    self.appmesh = self.session.client('appmesh', region_name=region, config=retry_config)
    
    print("✅ App Mesh client initialized")
```

### 2.2 Add Service to Service Map

Update the service filtering system:

```python
# In scan_region method, update service_map
service_map = {
    # ... existing services ...
    'appmesh': ['appmesh'],  # Add new service
    'containers': ['containers', 'ecs', 'eks', 'ecr', 'appmesh'],  # Or add to existing category
}
```

### 2.3 Basic Connectivity Test

Create a basic method to test service connectivity:

```python
def get_appmesh_mesh_count(self) -> Dict[str, int]:
    """
    Get count of App Mesh resources for inventory.
    
    Returns:
        Dict[str, int]: Count of meshes and virtual services
    """
    try:
        # Test basic connectivity
        meshes_response = self.appmesh.list_meshes()
        mesh_count = len(meshes_response.get('meshes', []))
        
        # Count virtual services across all meshes
        virtual_service_count = 0
        for mesh in meshes_response.get('meshes', []):
            vs_response = self.appmesh.list_virtual_services(meshName=mesh['meshName'])
            virtual_service_count += len(vs_response.get('virtualServices', []))
        
        return {
            'meshes': mesh_count,
            'virtual_services': virtual_service_count,
            'total_resources': mesh_count + virtual_service_count
        }
        
    except Exception as e:
        self.add_warning(f"Could not retrieve App Mesh inventory: {e}", "appmesh")
        return {'meshes': 0, 'virtual_services': 0, 'total_resources': 0}
```

## Step 3: Optimization Logic Implementation

### 3.1 Create Main Analysis Method

Implement the core optimization analysis:

```python
def get_enhanced_appmesh_checks(self) -> Dict[str, Any]:
    """
    Get enhanced App Mesh cost optimization checks.
    
    Analyzes App Mesh resources for cost optimization opportunities including:
    - Unused meshes detection
    - Over-provisioned proxy resources
    - Excessive logging costs
    - Regional optimization opportunities
    
    Returns:
        Dict[str, Any]: Structured optimization recommendations
    """
    checks = {
        'unused_meshes': [],
        'logging_optimization': [],
        'regional_optimization': [],
        'proxy_optimization': []
    }
    
    recommendations = []
    
    try:
        # Get all meshes
        meshes_response = self.appmesh.list_meshes()
        
        for mesh in meshes_response.get('meshes', []):
            mesh_name = mesh['meshName']
            
            # Analyze each mesh
            mesh_analysis = self._analyze_mesh(mesh_name)
            
            # Check for unused meshes
            if mesh_analysis['is_unused']:
                unused_mesh = {
                    'mesh_name': mesh_name,
                    'reason': 'No virtual services or nodes found',
                    'estimated_monthly_savings': mesh_analysis['estimated_savings'],
                    'confidence': 'High'
                }
                checks['unused_meshes'].append(unused_mesh)
                recommendations.append({
                    'type': 'unused_mesh',
                    'resource': mesh_name,
                    'description': f"Unused App Mesh '{mesh_name}' with no active services",
                    'estimated_monthly_savings': mesh_analysis['estimated_savings'],
                    'confidence': 'High',
                    'action': f"Consider deleting unused mesh '{mesh_name}'"
                })
            
            # Check logging configuration
            logging_analysis = self._analyze_mesh_logging(mesh_name)
            if logging_analysis['optimization_opportunity']:
                checks['logging_optimization'].append(logging_analysis)
                recommendations.append({
                    'type': 'logging_optimization',
                    'resource': mesh_name,
                    'description': logging_analysis['description'],
                    'estimated_monthly_savings': logging_analysis['estimated_savings'],
                    'confidence': 'Medium',
                    'action': logging_analysis['recommended_action']
                })
        
        return {'recommendations': recommendations, 'checks': checks}
        
    except Exception as e:
        self.add_warning(f"Could not analyze App Mesh: {e}", "appmesh")
        return {'recommendations': [], 'checks': checks}

def _analyze_mesh(self, mesh_name: str) -> Dict[str, Any]:
    """Analyze individual mesh for optimization opportunities."""
    try:
        # Get mesh details
        mesh_response = self.appmesh.describe_mesh(meshName=mesh_name)
        
        # Count virtual services
        vs_response = self.appmesh.list_virtual_services(meshName=mesh_name)
        virtual_service_count = len(vs_response.get('virtualServices', []))
        
        # Count virtual nodes
        vn_response = self.appmesh.list_virtual_nodes(meshName=mesh_name)
        virtual_node_count = len(vn_response.get('virtualNodes', []))
        
        # Determine if mesh is unused
        is_unused = virtual_service_count == 0 and virtual_node_count == 0
        
        # Estimate savings for unused mesh
        estimated_savings = 0.0
        if is_unused:
            # Base mesh cost + estimated proxy costs
            estimated_savings = 10.0  # Base mesh cost per month
        
        return {
            'is_unused': is_unused,
            'virtual_service_count': virtual_service_count,
            'virtual_node_count': virtual_node_count,
            'estimated_savings': estimated_savings
        }
        
    except Exception as e:
        self.add_warning(f"Could not analyze mesh {mesh_name}: {e}", "appmesh")
        return {
            'is_unused': False,
            'virtual_service_count': 0,
            'virtual_node_count': 0,
            'estimated_savings': 0.0
        }

def _analyze_mesh_logging(self, mesh_name: str) -> Dict[str, Any]:
    """Analyze mesh logging configuration for cost optimization."""
    try:
        # This would analyze CloudWatch logs and metrics costs
        # Implementation depends on specific logging configuration
        
        return {
            'optimization_opportunity': False,
            'description': '',
            'estimated_savings': 0.0,
            'recommended_action': ''
        }
        
    except Exception as e:
        return {
            'optimization_opportunity': False,
            'description': f'Could not analyze logging: {e}',
            'estimated_savings': 0.0,
            'recommended_action': ''
        }
```

### 3.2 Add CloudWatch Integration (if applicable)

For services with CloudWatch metrics:

```python
def _get_appmesh_cloudwatch_metrics(self, mesh_name: str) -> Dict[str, Any]:
    """Get CloudWatch metrics for App Mesh analysis."""
    if self.fast_mode:
        return {}
    
    try:
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=14)
        
        # Example: Get request count metrics
        response = self.cloudwatch.get_metric_statistics(
            Namespace='AWS/AppMesh',
            MetricName='RequestCount',
            Dimensions=[
                {'Name': 'MeshName', 'Value': mesh_name}
            ],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=['Sum']
        )
        
        datapoints = response.get('Datapoints', [])
        if datapoints:
            total_requests = sum(dp['Sum'] for dp in datapoints)
            avg_requests_per_hour = total_requests / len(datapoints) if datapoints else 0
            
            return {
                'total_requests_14_days': total_requests,
                'avg_requests_per_hour': avg_requests_per_hour,
                'has_traffic': total_requests > 0
            }
        
        return {'has_traffic': False}
        
    except Exception as e:
        self.add_warning(f"Could not get CloudWatch metrics for mesh {mesh_name}: {e}", "appmesh")
        return {}
```

## Step 4: Cost Calculations

### 4.1 Add Pricing Constants

Add service-specific pricing information:

```python
# Add to class constants
class CostOptimizer:
    # ... existing constants ...
    
    # App Mesh pricing (US East 1 baseline)
    APPMESH_MESH_COST_PER_MONTH = 10.00  # Base mesh cost
    APPMESH_PROXY_COST_PER_HOUR = 0.043  # Per Envoy proxy hour
    APPMESH_REQUEST_COST_PER_MILLION = 0.50  # Per million requests
```

### 4.2 Implement Cost Estimation

```python
def _estimate_appmesh_cost(self, mesh_name: str, proxy_count: int = 1, 
                          monthly_requests: int = 0) -> float:
    """
    Estimate monthly App Mesh costs.
    
    Args:
        mesh_name: Name of the mesh
        proxy_count: Number of Envoy proxies
        monthly_requests: Monthly request volume
        
    Returns:
        float: Estimated monthly cost in USD
    """
    # Base mesh cost
    base_cost = self.APPMESH_MESH_COST_PER_MONTH
    
    # Proxy costs (24/7 operation)
    proxy_cost = proxy_count * self.APPMESH_PROXY_COST_PER_HOUR * 24 * 30
    
    # Request costs
    request_cost = (monthly_requests / 1_000_000) * self.APPMESH_REQUEST_COST_PER_MILLION
    
    total_cost = base_cost + proxy_cost + request_cost
    
    # Apply regional pricing multiplier
    return total_cost * self.pricing_multiplier
```

## Step 5: Report Integration

### 5.1 Update HTML Report Generator

Add the new service to the HTML report generator:

```python
# In html_report_generator.py, update service handling
def _get_service_content(self, service_key: str, service_data: Dict[str, Any]) -> str:
    """Generate HTML content for a service."""
    
    # Add App Mesh to service mapping
    if service_key == 'appmesh':
        return self._get_appmesh_content(service_data)
    
    # ... existing service handling ...

def _get_appmesh_content(self, appmesh_data: Dict[str, Any]) -> str:
    """Generate HTML content for App Mesh optimization results."""
    
    checks = appmesh_data.get('checks', {})
    
    content = """
    <div class="service-section">
        <h3>🕸️ App Mesh Optimization</h3>
    """
    
    # Unused meshes section
    unused_meshes = checks.get('unused_meshes', [])
    if unused_meshes:
        content += """
        <div class="check-category">
            <h4>🗑️ Unused Meshes</h4>
            <div class="recommendations">
        """
        
        for mesh in unused_meshes:
            content += f"""
            <div class="recommendation high-impact">
                <div class="rec-header">
                    <span class="rec-title">Unused Mesh: {mesh['mesh_name']}</span>
                    <span class="savings">${mesh['estimated_monthly_savings']:.2f}/month</span>
                </div>
                <div class="rec-details">
                    <p><strong>Issue:</strong> {mesh['reason']}</p>
                    <p><strong>Action:</strong> Consider deleting this unused mesh</p>
                    <p><strong>Confidence:</strong> {mesh['confidence']}</p>
                </div>
            </div>
            """
        
        content += "</div></div>"
    
    # Add other check categories...
    
    content += "</div>"
    return content
```

### 5.2 Update Service Statistics

```python
# Update statistics calculation to include new service
def _calculate_service_savings(self, service_key: str, service_data: Dict[str, Any]) -> float:
    """Calculate total savings for a service."""
    
    if service_key == 'appmesh':
        return self._calculate_appmesh_savings(service_data)
    
    # ... existing service calculations ...

def _calculate_appmesh_savings(self, appmesh_data: Dict[str, Any]) -> float:
    """Calculate total App Mesh savings."""
    total_savings = 0.0
    
    for recommendation in appmesh_data.get('recommendations', []):
        total_savings += recommendation.get('estimated_monthly_savings', 0.0)
    
    return total_savings
```

## Step 6: Integration with Main Scanner

### 6.1 Add to Main Scan Method

Update the `scan_region` method to include the new service:

```python
def scan_region(self, skip_services=None, scan_only=None) -> Dict[str, Any]:
    """Main scanning method - add new service here."""
    
    # ... existing scanning logic ...
    
    # Add App Mesh scanning
    if should_scan_service('appmesh'):
        print("🕸️ Scanning App Mesh optimization...")
        enhanced_appmesh_checks = self.get_enhanced_appmesh_checks()
        appmesh_count = self.get_appmesh_mesh_count()
    else:
        enhanced_appmesh_checks = {'recommendations': []}
        appmesh_count = {'meshes': 0, 'virtual_services': 0, 'total_resources': 0}
    
    # Add to results
    services['appmesh'] = {
        'service_name': 'App Mesh',
        'resource_count': appmesh_count,
        **enhanced_appmesh_checks
    }
    
    # ... rest of scanning logic ...
```

## Step 7: Testing & Validation

### 7.1 Unit Testing

Create comprehensive unit tests:

```python
# tests/test_appmesh.py
import unittest
from unittest.mock import Mock, patch
from cost_optimizer import CostOptimizer

class TestAppMeshOptimization(unittest.TestCase):
    
    def setUp(self):
        self.optimizer = CostOptimizer('us-east-1')
    
    @patch('boto3.Session')
    def test_appmesh_mesh_count(self, mock_session):
        """Test App Mesh mesh counting."""
        
        # Mock App Mesh response
        mock_appmesh = Mock()
        mock_appmesh.list_meshes.return_value = {
            'meshes': [
                {'meshName': 'test-mesh-1'},
                {'meshName': 'test-mesh-2'}
            ]
        }
        mock_appmesh.list_virtual_services.return_value = {
            'virtualServices': [{'virtualServiceName': 'test-service'}]
        }
        
        self.optimizer.appmesh = mock_appmesh
        
        # Test mesh count
        result = self.optimizer.get_appmesh_mesh_count()
        
        self.assertEqual(result['meshes'], 2)
        self.assertEqual(result['virtual_services'], 2)  # 1 per mesh
    
    @patch('boto3.Session')
    def test_unused_mesh_detection(self, mock_session):
        """Test detection of unused meshes."""
        
        # Mock responses for unused mesh
        mock_appmesh = Mock()
        mock_appmesh.list_meshes.return_value = {
            'meshes': [{'meshName': 'unused-mesh'}]
        }
        mock_appmesh.describe_mesh.return_value = {
            'mesh': {'meshName': 'unused-mesh'}
        }
        mock_appmesh.list_virtual_services.return_value = {'virtualServices': []}
        mock_appmesh.list_virtual_nodes.return_value = {'virtualNodes': []}
        
        self.optimizer.appmesh = mock_appmesh
        
        # Test unused mesh detection
        result = self.optimizer.get_enhanced_appmesh_checks()
        
        self.assertTrue(len(result['recommendations']) > 0)
        self.assertEqual(result['recommendations'][0]['type'], 'unused_mesh')
    
    def test_cost_estimation(self):
        """Test App Mesh cost estimation."""
        
        # Test cost calculation
        cost = self.optimizer._estimate_appmesh_cost(
            mesh_name='test-mesh',
            proxy_count=2,
            monthly_requests=1_000_000
        )
        
        # Should include base cost + proxy costs + request costs
        self.assertGreater(cost, 0)
        self.assertIsInstance(cost, float)

if __name__ == '__main__':
    unittest.main()
```

### 7.2 Integration Testing

Test with real AWS accounts:

```python
# integration_tests/test_appmesh_integration.py
import os
from cost_optimizer import CostOptimizer

def test_appmesh_integration():
    """Integration test with real AWS account."""
    
    # Skip if no AWS credentials
    if not os.getenv('AWS_PROFILE'):
        print("Skipping integration test - no AWS credentials")
        return
    
    optimizer = CostOptimizer('us-east-1', os.getenv('AWS_PROFILE'))
    
    try:
        # Test basic connectivity
        mesh_count = optimizer.get_appmesh_mesh_count()
        print(f"Found {mesh_count['meshes']} App Mesh meshes")
        
        # Test optimization analysis
        if mesh_count['meshes'] > 0:
            results = optimizer.get_enhanced_appmesh_checks()
            print(f"Generated {len(results['recommendations'])} recommendations")
        
        print("✅ App Mesh integration test passed")
        
    except Exception as e:
        print(f"❌ App Mesh integration test failed: {e}")

if __name__ == '__main__':
    test_appmesh_integration()
```

### 7.3 End-to-End Testing

Test complete workflow:

```python
def test_full_scan_with_appmesh():
    """Test full scan including App Mesh."""
    
    optimizer = CostOptimizer('us-east-1', 'test-profile')
    
    # Run scan with App Mesh included
    results = optimizer.scan_region(scan_only=['appmesh'])
    
    # Verify App Mesh is included
    assert 'appmesh' in results['services']
    
    # Generate report
    from html_report_generator import HTMLReportGenerator
    generator = HTMLReportGenerator(results)
    report_path = generator.generate_html_report()
    
    # Verify report contains App Mesh section
    with open(report_path, 'r') as f:
        report_content = f.read()
        assert 'App Mesh' in report_content
    
    print("✅ End-to-end test with App Mesh passed")
```

## Step 8: Documentation Updates

### 8.1 Update README.md

Add the new service to the service list:

```markdown
| Service | Key Optimizations |
|---------|-------------------|
| **App Mesh** | Unused meshes, proxy optimization, logging cost reduction |
```

### 8.2 Update API Documentation

Add docstrings and API documentation:

```python
def get_enhanced_appmesh_checks(self) -> Dict[str, Any]:
    """
    Get enhanced App Mesh cost optimization checks.
    
    Analyzes App Mesh resources for cost optimization opportunities including:
    
    - **Unused Meshes**: Detects meshes with no virtual services or nodes
    - **Proxy Optimization**: Identifies over-provisioned Envoy proxies
    - **Logging Optimization**: Analyzes CloudWatch logging costs
    - **Regional Optimization**: Compares costs across regions
    
    Returns:
        Dict[str, Any]: Optimization results containing:
            - recommendations: List of actionable recommendations
            - checks: Detailed analysis by category
            
    Example:
        >>> optimizer = CostOptimizer('us-east-1')
        >>> results = optimizer.get_enhanced_appmesh_checks()
        >>> print(f"Found {len(results['recommendations'])} recommendations")
        
    Note:
        Requires 'appmesh:ListMeshes', 'appmesh:DescribeMesh', 
        'appmesh:ListVirtualServices', and 'appmesh:ListVirtualNodes' permissions.
    """
```

### 8.3 Update IAM Permissions

Document required permissions:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "appmesh:ListMeshes",
                "appmesh:DescribeMesh",
                "appmesh:ListVirtualServices",
                "appmesh:ListVirtualNodes",
                "appmesh:ListVirtualRouters",
                "appmesh:ListVirtualGateways"
            ],
            "Resource": "*"
        }
    ]
}
```

## Step 9: Pull Request Checklist

Before submitting your new service implementation:

### 9.1 Code Quality Checklist

- [ ] All methods have comprehensive docstrings
- [ ] Type hints are used throughout
- [ ] Error handling is implemented with proper warnings
- [ ] Code follows existing patterns and conventions
- [ ] No hardcoded values (use class constants)
- [ ] Regional pricing multipliers are applied

### 9.2 Testing Checklist

- [ ] Unit tests cover all major methods
- [ ] Integration tests pass with real AWS accounts
- [ ] End-to-end testing includes report generation
- [ ] Edge cases are tested (empty responses, errors)
- [ ] Performance testing with large resource counts

### 9.3 Documentation Checklist

- [ ] README.md updated with new service
- [ ] API documentation includes all new methods
- [ ] IAM permissions documented
- [ ] Service filtering documentation updated
- [ ] Architecture documentation reflects new service

### 9.4 Integration Checklist

- [ ] Service added to main scan_region method
- [ ] Service filtering system updated
- [ ] HTML report generator handles new service
- [ ] Statistics calculation includes new service
- [ ] Service count methods implemented

## Step 10: Advanced Patterns

### 10.1 Multi-Resource Services

For services with multiple resource types:

```python
def get_enhanced_eks_checks(self) -> Dict[str, Any]:
    """Handle EKS with clusters, node groups, and add-ons."""
    
    checks = {
        'cluster_optimization': [],
        'nodegroup_optimization': [],
        'addon_optimization': []
    }
    
    # Analyze each resource type separately
    clusters = self._analyze_eks_clusters()
    nodegroups = self._analyze_eks_nodegroups()
    addons = self._analyze_eks_addons()
    
    # Combine results
    checks['cluster_optimization'] = clusters
    checks['nodegroup_optimization'] = nodegroups  
    checks['addon_optimization'] = addons
    
    return {'recommendations': self._combine_recommendations(checks), 'checks': checks}
```

### 10.2 Cross-Service Dependencies

For services that depend on others:

```python
def _analyze_cross_service_dependencies(self, primary_service: str) -> Dict[str, Any]:
    """Analyze dependencies between services for optimization."""
    
    dependencies = {
        'appmesh': ['ecs', 'eks'],  # App Mesh depends on container services
        'api_gateway': ['lambda'],   # API Gateway often uses Lambda
        'cloudfront': ['s3']        # CloudFront often uses S3
    }
    
    related_services = dependencies.get(primary_service, [])
    
    # Analyze related services for optimization opportunities
    cross_service_recommendations = []
    
    for service in related_services:
        # Check if related service has optimization opportunities
        # that could affect the primary service
        pass
    
    return cross_service_recommendations
```

This comprehensive tutorial provides everything needed to successfully add new AWS services to the Cost Optimization Scanner, ensuring consistency, quality, and maintainability.
