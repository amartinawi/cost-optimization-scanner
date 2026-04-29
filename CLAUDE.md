# AI Agent Guidelines for AWS Cost Optimization Scanner

## Overview

This document provides guidelines for AI agents working on the AWS Cost Optimization Scanner project. The project is a production-ready tool analyzing 30 AWS services with 220+ cost optimization checks.

## 🔧 Technical Validation Requirements

### AWS Enhancement Validation
All AWS-level enhancements MUST be validated through multiple channels:

1. **AWS Documentation MCP**: Use AWS Doc MCP server to validate:
   - Service capabilities and limitations
   - API specifications and parameters
   - Regional availability
   - Pricing models and cost structures
   - Best practices and recommendations

2. **Technical Verification**: Before implementing any AWS service enhancement:
   ```
   1. Search AWS documentation for service specifications
   2. Validate API methods and parameters
   3. Confirm regional availability
   4. Verify pricing implications
   5. Check for service limitations or quotas
   ```

3. **Theoretical Validation**: Ensure all recommendations align with:
   - AWS Well-Architected Framework principles
   - AWS cost optimization best practices
   - Service-specific optimization patterns
   - Regional pricing variations

## 🛠️ Development Tools

### Context7 Integration
Use Context7 for code-related activities when needed:
- Code analysis and understanding
- Implementation planning
- Debugging and troubleshooting
- Performance optimization
- Code quality improvements

### Required Validation Workflow
```
AWS Enhancement Request
    ↓
AWS Doc MCP Validation
    ↓
Technical Feasibility Check
    ↓
Context7 Code Analysis (if needed)
    ↓
Implementation
    ↓
Testing & Validation
```

## 📋 Project Context

### Current State (v2.5.9)
- **30 AWS Services** with comprehensive coverage
- **220+ Cost Optimization Checks** across all services
- **Enterprise-scale** support (1000+ resources per service)
- **Global region** support with regional pricing
- **Service filtering** system for targeted scans
- **CloudWatch integration** for metric-backed analysis

### Key Files
- `cost_optimizer.py` (8,677 lines) - Main optimization engine
- `html_report_generator.py` (3,699 lines) - Report generation
- `requirements.txt` - Dependencies (boto3, botocore, python-dateutil)

## 🎯 Enhancement Guidelines

### Adding New AWS Services
1. **Validate with AWS Doc MCP**:
   - Confirm service exists and is generally available
   - Verify API methods for resource listing and analysis
   - Check regional availability
   - Understand pricing model

2. **Technical Implementation**:
   - Add service client initialization
   - Implement pagination for scalability
   - Add proper error handling
   - Include CloudWatch integration where applicable
   - Add to service filtering system

3. **Cost Analysis Requirements**:
   - Use regional pricing multipliers
   - Implement intelligent gating to prevent false positives
   - Base recommendations on actual usage metrics
   - Provide conservative savings estimates

### Optimization Check Development
1. **AWS Doc Validation**:
   - Verify optimization opportunity exists
   - Confirm cost impact potential
   - Validate against AWS best practices
   - Check for service-specific considerations

2. **Implementation Standards**:
   - Use CloudWatch metrics when available
   - Implement proper error handling
   - Add regional pricing considerations
   - Include confidence scoring

## 🔍 Validation Checklist

### Before Any AWS Enhancement
- [ ] AWS Doc MCP validation completed
- [ ] Service availability confirmed across regions
- [ ] API methods and parameters verified
- [ ] Pricing model understood
- [ ] Cost optimization potential validated
- [ ] Regional considerations documented

### Code Implementation
- [ ] Context7 analysis completed (if needed)
- [ ] Proper error handling implemented
- [ ] Pagination support added
- [ ] CloudWatch integration included
- [ ] Service filtering updated
- [ ] IAM permissions documented

### Testing Requirements
- [ ] Multiple AWS account testing
- [ ] Cross-region validation
- [ ] Large-scale resource testing (1000+)
- [ ] Error scenario testing
- [ ] Report generation validation

## 📚 Required Resources

### AWS Documentation Sources
- AWS Service Documentation
- AWS Pricing Documentation
- AWS Well-Architected Framework
- AWS Cost Optimization Hub
- AWS Compute Optimizer
- Regional service availability

### Development Resources
- boto3 API Reference
- AWS CLI Documentation
- CloudWatch Metrics Reference
- AWS IAM Policy Reference

## 🚫 Restrictions

### What NOT to Implement
- Write operations to AWS resources
- Automated remediation without explicit user consent
- Hardcoded pricing (always use regional multipliers)
- Recommendations without proper validation
- Features that require excessive IAM permissions

### Code Quality Standards
- Follow existing code patterns
- Maintain backward compatibility
- Use minimal dependencies
- Implement comprehensive error handling
- Include proper documentation

## 🔄 Continuous Validation

### Regular Checks
- AWS service updates and new features
- Pricing model changes
- Regional availability updates
- API deprecations or changes
- Best practice evolution

### Documentation Maintenance
- Keep AWS Doc references current
- Update pricing assumptions
- Maintain accuracy of service coverage
- Validate optimization recommendations

## 📞 Escalation

### When to Seek Additional Validation
- Complex multi-service optimizations
- New AWS service categories
- Significant architectural changes
- Performance optimization requirements
- Enterprise-specific features

### Validation Sources Priority
1. AWS Official Documentation (via AWS Doc MCP)
2. AWS Well-Architected Framework
3. AWS Cost Optimization Hub
4. AWS Compute Optimizer
5. Community best practices (with validation)

---

**Remember**: Every AWS enhancement must be technically sound, theoretically validated, and practically implementable. Use the available tools (AWS Doc MCP, Context7) to ensure the highest quality contributions to this production-ready cost optimization tool.


<claude-mem-context>
# Recent Activity

<!-- This section is auto-generated by claude-mem. Edit content outside the tags. -->

*No recent activity*
</claude-mem-context>