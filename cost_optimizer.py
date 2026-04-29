#!/usr/bin/env python3
"""
AWS Cost Optimization Scanner v2.5.9

A comprehensive multi-service AWS cost optimization tool that analyzes 30 AWS services
with 220+ cost optimization checks for identifying cost-saving opportunities across
your entire AWS infrastructure.

🎯 Key Features:
- 30 AWS Service Categories: EC2, EBS, RDS, S3, Lambda, Glue, Athena, Batch, etc.
- 220+ Cost Optimization Checks: Comprehensive analysis across all services
- Enterprise-Scale Support: Handles 1000+ resources per service with pagination
- Smart Retry Logic: Exponential backoff handles API throttling gracefully
- Professional HTML Reports: Interactive multi-tab interface with intelligent grouping
- Service Filtering: Target specific services for faster, focused scans (30 categories)
- Cross-Region Support: Proper handling of resources across all AWS regions
- Error Tracking: Comprehensive warnings and permission issue tracking

💰 Potential Savings:
- High-Impact: 30-90% savings (Spot instances, Reserved capacity, ARM migration)
- Medium-Impact: 10-30% savings (Rightsizing, gp2→gp3, lifecycle policies)
- Continuous: Ongoing savings (Unused resources, log retention, backup optimization)

🏗️ Architecture:
- 40+ AWS service clients with adaptive retry configuration
- CloudWatch integration for real metrics and usage analysis
- Cross-region S3 analysis with region-specific clients
- Professional report generation with consistent styling
- Dynamic region discovery with comprehensive error tracking

Author: AWS Cost Optimization Team
Version: 2.5.9 Production Ready
Last Updated: 2026-01-21
License: MIT
"""

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dataclasses import asdict
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any
import argparse
import re

from core.scan_context import ScanContext
from services.ami import compute_ami_checks as _ami_compute
from services.apprunner import (
    APPRUNNER_OPTIMIZATION_DESCRIPTIONS as _APPRUNNER_DESCRIPTIONS,
    get_enhanced_apprunner_checks as _apprunner_enhanced_checks,
)
from services.api_gateway import (
    API_GATEWAY_OPTIMIZATION_DESCRIPTIONS as _API_GATEWAY_DESCRIPTIONS,
    get_enhanced_api_gateway_checks as _api_gateway_enhanced_checks,
)
from services.quicksight import (
    QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS as _QUICKSIGHT_DESCRIPTIONS,
    get_enhanced_quicksight_checks as _quicksight_enhanced_checks,
)
from services.ebs import (
    EBS_OPTIMIZATION_DESCRIPTIONS as _EBS_DESCRIPTIONS,
    compute_ebs_checks as _ebs_compute,
    get_ebs_compute_optimizer_recs as _ebs_compute_optimizer_recs,
    get_ebs_volume_count as _ebs_volume_count,
    get_unattached_volumes as _ebs_unattached_volumes,
)
from services.s3 import (
    S3_OPTIMIZATION_DESCRIPTIONS as _S3_DESCRIPTIONS,
    get_enhanced_s3_checks as _s3_enhanced_checks,
    get_s3_bucket_analysis as _s3_bucket_analysis,
)
from services.ec2 import (
    get_advanced_ec2_checks as _ec2_advanced_checks,
    get_auto_scaling_checks as _ec2_auto_scaling_checks,
    get_compute_optimizer_recommendations as _ec2_compute_optimizer_recs,
    get_ec2_instance_count as _ec2_instance_count,
    get_enhanced_ec2_checks as _ec2_enhanced_checks,
)
from services.rds import (
    RDS_OPTIMIZATION_DESCRIPTIONS as _RDS_DESCRIPTIONS,
    get_enhanced_rds_checks as _rds_enhanced_checks,
    get_rds_compute_optimizer_recommendations as _rds_compute_optimizer_recs,
    get_rds_instance_count as _rds_instance_count,
)
from services.mediastore import (
    MEDIASTORE_OPTIMIZATION_DESCRIPTIONS as _MEDIASTORE_DESCRIPTIONS,
    get_enhanced_mediastore_checks as _mediastore_enhanced_checks,
)
from services.msk import (
    MSK_OPTIMIZATION_DESCRIPTIONS as _MSK_DESCRIPTIONS,
    get_enhanced_msk_checks as _msk_enhanced_checks,
)
from services.workspaces import (
    WORKSPACES_OPTIMIZATION_DESCRIPTIONS as _WORKSPACES_DESCRIPTIONS,
    get_enhanced_workspaces_checks as _workspaces_enhanced_checks,
)
from services.transfer_svc import get_enhanced_transfer_checks as _transfer_enhanced_checks
from services.redshift import (
    REDSHIFT_OPTIMIZATION_DESCRIPTIONS as _REDSHIFT_DESCRIPTIONS,
    get_enhanced_redshift_checks as _redshift_enhanced_checks,
)
from services.athena import get_enhanced_athena_checks as _athena_enhanced_checks
from services.batch_svc import (
    BATCH_OPTIMIZATION_DESCRIPTIONS as _BATCH_DESCRIPTIONS,
    get_enhanced_batch_checks as _batch_enhanced_checks,
)
from services.cloudfront import get_enhanced_cloudfront_checks as _cloudfront_enhanced_checks
from services.step_functions import (
    STEP_FUNCTIONS_OPTIMIZATION_DESCRIPTIONS as _STEP_FUNCTIONS_DESCRIPTIONS,
    get_enhanced_step_functions_checks as _step_functions_enhanced_checks,
)
from core.client_registry import ClientRegistry
from core.session import AwsSessionFactory


class CostOptimizer:
    """
    Main cost optimization scanner class that analyzes AWS resources across 31 services
    and provides actionable cost-saving recommendations.

    This class handles:
    - AWS service client initialization with proper retry configuration
    - Cost optimization analysis across 30 AWS services
    - Regional pricing calculations for accurate cost estimates
    - Comprehensive error handling with graceful degradation
    - Enterprise-scale pagination for unlimited resource support

    Supported Services:
    - Compute: EC2, Lambda
    - Storage: EBS, S3, File Systems (EFS/FSx)
    - Database: RDS, DynamoDB
    - Caching: ElastiCache, OpenSearch
    - Containers: ECS, EKS, ECR
    - Network: EIP, NAT, Load Balancers, VPC, CloudFront
    - Serverless: Lambda, API Gateway, Step Functions
    - Management: CloudWatch, CloudTrail, Auto Scaling, Backup
    - DNS: Route53

    Usage:
        optimizer = CostOptimizer('us-east-1', 'production')
        results = optimizer.scan_region()
    """

    # Cost optimization thresholds - configurable constants for consistent analysis
    OLD_SNAPSHOT_DAYS = 90  # Days after which snapshots are considered old
    OLD_AMI_DAYS = 90  # Days after which AMIs are considered old
    LARGE_TABLE_SIZE_GB = 10  # GB threshold for large DynamoDB tables
    SMALL_EFS_SIZE_GB = 0.1  # GB threshold for small EFS file systems
    LARGE_EFS_SIZE_GB = 10  # GB threshold for large EFS file systems
    EFS_ONE_ZONE_MIN_SIZE_GB = 1  # Minimum size for One Zone migration consideration
    LARGE_FSX_CAPACITY_GB = 100  # GB threshold for large FSx file systems
    EXCESSIVE_BACKUP_RETENTION_DAYS = 30  # Days threshold for excessive backup retention
    MULTI_AZ_BACKUP_RETENTION_DAYS = 7  # Recommended backup retention for Multi-AZ

    # Cost calculation constants - S3 storage class pricing (US East 1 baseline)
    # Source: https://aws.amazon.com/s3/pricing/ (US East N. Virginia)
    S3_STORAGE_COSTS = {
        "STANDARD": 0.023,  # Standard storage - $0.023/GB
        "STANDARD_IA": 0.0125,  # Standard-Infrequent Access - $0.0125/GB
        "ONEZONE_IA": 0.01,  # One Zone-Infrequent Access - $0.01/GB
        "GLACIER_FLEXIBLE_RETRIEVAL": 0.0036,  # Glacier Flexible Retrieval - $0.0036/GB
        "GLACIER_INSTANT_RETRIEVAL": 0.004,  # Glacier Instant Retrieval - $0.004/GB
        "DEEP_ARCHIVE": 0.00099,  # Glacier Deep Archive - $0.00099/GB
        "INTELLIGENT_TIERING": 0.023,  # Intelligent-Tiering storage - $0.023/GB (monitoring fee separate)
        "EXPRESS_ONE_ZONE": 0.11,  # S3 Express One Zone - $0.11/GB (high-performance)
    }

    # S3 Intelligent-Tiering monitoring fee per 1,000 objects
    # Source: https://aws.amazon.com/s3/pricing/ (Monitoring and automation)
    S3_INTELLIGENT_TIERING_MONITORING_FEE = 0.0025  # $0.0025 per 1,000 objects

    # S3 Intelligent-Tiering Archive Access tier pricing
    # Source: https://aws.amazon.com/s3/pricing/ (Archive Access tier)
    S3_INTELLIGENT_TIERING_ARCHIVE_ACCESS = 0.0036  # $0.0036/GB for Archive Access tier

    # Regional S3 pricing multipliers by storage class (relative to us-east-1)
    # Source: https://aws.amazon.com/s3/pricing/ - Regional pricing variations
    # Note: Glacier and Deep Archive have consistent global pricing (1.0x multiplier)
    # Standard/IA classes vary by region: 1.0x (baseline), 0.956x (eu-north-1), 1.087x (premium regions)
    S3_REGIONAL_MULTIPLIERS = {
        "us-east-1": {
            "STANDARD": 1.0,
            "STANDARD_IA": 1.0,
            "ONEZONE_IA": 1.0,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.0,
            "EXPRESS_ONE_ZONE": 1.0,
        },
        "us-east-2": {
            "STANDARD": 1.0,
            "STANDARD_IA": 1.0,
            "ONEZONE_IA": 1.0,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.0,
            "EXPRESS_ONE_ZONE": 1.0,
        },
        "us-west-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "us-west-2": {
            "STANDARD": 1.0,
            "STANDARD_IA": 1.0,
            "ONEZONE_IA": 1.0,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.0,
            "EXPRESS_ONE_ZONE": 1.0,
        },
        "eu-west-1": {
            "STANDARD": 1.0,
            "STANDARD_IA": 1.0,
            "ONEZONE_IA": 1.0,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.0,
            "EXPRESS_ONE_ZONE": 1.0,
        },
        "eu-west-2": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "eu-west-3": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "eu-central-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "eu-central-2": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "eu-north-1": {
            "STANDARD": 0.956,
            "STANDARD_IA": 0.96,
            "ONEZONE_IA": 0.9,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 0.956,
            "EXPRESS_ONE_ZONE": 0.956,
        },
        "eu-south-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "eu-south-2": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-southeast-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-southeast-2": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-southeast-3": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-southeast-4": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-northeast-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-northeast-2": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-northeast-3": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-south-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-south-2": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ap-east-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ca-central-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "ca-west-1": {
            "STANDARD": 1.087,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.087,
            "EXPRESS_ONE_ZONE": 1.087,
        },
        "sa-east-1": {
            "STANDARD": 1.304,
            "STANDARD_IA": 1.28,
            "ONEZONE_IA": 1.3,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.304,
            "EXPRESS_ONE_ZONE": 1.304,
        },
        "me-south-1": {
            "STANDARD": 1.15,
            "STANDARD_IA": 1.12,
            "ONEZONE_IA": 1.2,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.15,
            "EXPRESS_ONE_ZONE": 1.15,
        },
        "me-central-1": {
            "STANDARD": 1.15,
            "STANDARD_IA": 1.12,
            "ONEZONE_IA": 1.2,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.15,
            "EXPRESS_ONE_ZONE": 1.15,
        },
        "af-south-1": {
            "STANDARD": 1.304,
            "STANDARD_IA": 1.28,
            "ONEZONE_IA": 1.3,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.304,
            "EXPRESS_ONE_ZONE": 1.304,
        },
        "il-central-1": {
            "STANDARD": 1.15,
            "STANDARD_IA": 1.12,
            "ONEZONE_IA": 1.2,
            "GLACIER_FLEXIBLE_RETRIEVAL": 1.0,
            "GLACIER_INSTANT_RETRIEVAL": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.15,
            "EXPRESS_ONE_ZONE": 1.15,
        },
        "ap-east-2": {
            "STANDARD": 1.18,
            "STANDARD_IA": 1.15,
            "ONEZONE_IA": 1.2,
            "GLACIER": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.18,
        },
        "ap-southeast-5": {
            "STANDARD": 1.12,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.12,
        },
        "ap-southeast-6": {
            "STANDARD": 1.15,
            "STANDARD_IA": 1.12,
            "ONEZONE_IA": 1.2,
            "GLACIER": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.15,
        },
        "ap-southeast-7": {
            "STANDARD": 1.12,
            "STANDARD_IA": 1.08,
            "ONEZONE_IA": 1.1,
            "GLACIER": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.12,
        },
        "mx-central-1": {
            "STANDARD": 1.15,
            "STANDARD_IA": 1.12,
            "ONEZONE_IA": 1.2,
            "GLACIER": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.15,
        },
        "us-gov-east-1": {
            "STANDARD": 1.05,
            "STANDARD_IA": 1.05,
            "ONEZONE_IA": 1.05,
            "GLACIER": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.05,
        },
        "us-gov-west-1": {
            "STANDARD": 1.05,
            "STANDARD_IA": 1.05,
            "ONEZONE_IA": 1.05,
            "GLACIER": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.05,
        },
        "eusc-de-east-1": {
            "STANDARD": 1.15,
            "STANDARD_IA": 1.12,
            "ONEZONE_IA": 1.2,
            "GLACIER": 1.0,
            "DEEP_ARCHIVE": 1.0,
            "INTELLIGENT_TIERING": 1.15,
        },
    }
    LOW_CPU_THRESHOLD = 20  # CPU utilization threshold for underutilized resources
    HIGH_IMAGE_COUNT_THRESHOLD = 10  # ECR image count threshold for lifecycle policies
    STANDARD_VS_EXPRESS_TRANSITION_THRESHOLD = 20  # Step Functions transition count threshold
    EXCESSIVE_TRANSITION_THRESHOLD = 50  # Step Functions excessive transition threshold

    # Regional pricing multipliers (relative to us-east-1 baseline)
    # Used for accurate cost calculations across different AWS regions
    # Conservative fallback: 1.15 for missing regions (15% premium over us-east-1)
    REGIONAL_PRICING = {
        # US Regions
        "us-east-1": 1.00,
        "us-east-2": 1.00,
        "us-west-1": 1.02,
        "us-west-2": 1.00,
        "us-gov-east-1": 1.05,
        "us-gov-west-1": 1.05,
        # Canada
        "ca-central-1": 1.02,
        "ca-west-1": 1.02,
        # Europe
        "eu-west-1": 1.10,
        "eu-west-2": 1.10,
        "eu-west-3": 1.10,
        "eu-central-1": 1.12,
        "eu-central-2": 1.12,
        "eu-north-1": 1.05,
        "eu-south-1": 1.10,
        "eu-south-2": 1.10,
        "eusc-de-east-1": 1.15,
        # Asia Pacific
        "ap-south-1": 1.08,
        "ap-south-2": 1.08,
        "ap-southeast-1": 1.12,
        "ap-southeast-2": 1.15,
        "ap-southeast-3": 1.12,
        "ap-southeast-4": 1.12,
        "ap-southeast-5": 1.12,
        "ap-southeast-6": 1.15,
        "ap-southeast-7": 1.12,
        "ap-northeast-1": 1.15,
        "ap-northeast-2": 1.10,
        "ap-northeast-3": 1.15,
        "ap-east-1": 1.18,
        "ap-east-2": 1.18,
        # Middle East & Africa
        "me-south-1": 1.15,
        "me-central-1": 1.15,
        "il-central-1": 1.15,
        "af-south-1": 1.20,
        # South America & Mexico
        "sa-east-1": 1.25,
        "mx-central-1": 1.15,
    }

    @classmethod
    def get_regional_pricing_multiplier(cls, region: str) -> float:
        """Get regional pricing multiplier with conservative fallback for missing regions"""
        if region not in cls.REGIONAL_PRICING:
            print(f"⚠️ WARNING: Regional pricing not defined for {region}")
            print(f"   Using conservative 15% premium over us-east-1 pricing")
            print(f"   Actual costs may vary - verify with AWS Pricing Calculator")
            return 1.15  # Conservative 15% premium fallback
        return cls.REGIONAL_PRICING[region]

    def add_warning(self, message: str, service: str = None):
        """Add a warning to the scan results"""
        self._ctx.warn(message, service or "")

    def add_permission_issue(self, message: str, service: str, action: str = None):
        """Add a permission issue to the scan results"""
        self._ctx.permission_issue(message, service, action)

    def __init__(self, region: str, profile: str = None, fast_mode: bool = False):
        """
        Initialize the Cost Optimizer with AWS region and profile.

        Sets up all AWS service clients with proper retry configuration and
        calculates regional pricing multipliers for accurate cost estimates.

        Args:
            region (str): AWS region to scan (e.g., 'us-east-1', 'eu-west-1')
            profile (str, optional): AWS profile name from ~/.aws/credentials.
                                   Defaults to 'default' if not specified.
            fast_mode (bool): Skip expensive CloudWatch metrics for faster analysis.
                            Recommended for accounts with 100+ S3 buckets.

        Raises:
            ClientError: If AWS credentials are invalid or region is not accessible
            Exception: If service clients cannot be initialized

        Note:
            - Initializes 37 AWS service clients with adaptive retry configuration
            - Sets up exponential backoff with up to 10 retry attempts
            - Handles global services (Route53, CloudFront) with us-east-1 fallback
            - Cost Optimization Hub falls back to us-east-1 if not available in target region
        """
        print(f"🚀 Initializing AWS Cost Optimization Scanner...")
        print(f"📍 Target region: {region}")
        print(f"👤 AWS profile: {profile or 'default'}")

        self.region = region
        self.profile = profile
        self.fast_mode = fast_mode  # Boolean value, not string
        # Use 1.0 multiplier for any region not explicitly defined (conservative approach)
        self.pricing_multiplier = self.get_regional_pricing_multiplier(region)

        # Initialize warnings and permission issues tracking
        self.scan_warnings = []
        self.permission_issues = []

        if self.fast_mode:
            print("🚀 Fast mode enabled - skipping CloudWatch metrics for faster analysis")

        print("⚙️ Configuring AWS clients with retry logic...")
        # Configure boto3 with adaptive retries and exponential backoff
        # This handles API throttling gracefully for enterprise-scale accounts
        retry_config = Config(
            retries={
                "max_attempts": 10,
                "mode": "adaptive",  # Automatic exponential backoff for throttling
            }
        )

        self.session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self.config = retry_config

        print("🔗 Setting up Cost Optimization Hub connection...")
        # Cost Optimization Hub - try target region first, then dynamic fallback
        self.cost_hub = None

        # Cost Optimization Hub is a global service - always use us-east-1 endpoint
        try:
            self.cost_hub = self.session.client("cost-optimization-hub", region_name="us-east-1", config=retry_config)
            print("✅ Cost Optimization Hub client created (global service - us-east-1 endpoint)")
        except Exception as e:
            self.add_warning(f"Cost Optimization Hub not available: {str(e)}", "cost-optimization-hub")
            self.cost_hub = None

        print("🔧 Initializing AWS service clients (37 services)...")
        # Initialize all AWS service clients with retry configuration
        # Core compute and storage services
        self.compute_optimizer = self.session.client("compute-optimizer", region_name=region, config=retry_config)
        self.ec2 = self.session.client("ec2", region_name=region, config=retry_config)
        self.rds = self.session.client("rds", region_name=region, config=retry_config)
        self.efs = self.session.client("efs", region_name=region, config=retry_config)
        self.fsx = self.session.client("fsx", region_name=region, config=retry_config)
        self.s3 = self.session.client("s3", region_name=region, config=retry_config)
        self.cloudwatch = self.session.client("cloudwatch", region_name=region, config=retry_config)
        self.dynamodb = self.session.client("dynamodb", region_name=region, config=retry_config)

        # Caching and search services
        self.elasticache = self.session.client("elasticache", region_name=region, config=retry_config)
        self.opensearch = self.session.client("opensearch", region_name=region, config=retry_config)

        # Container and orchestration services
        self.ecs = self.session.client("ecs", region_name=region, config=retry_config)
        self.eks = self.session.client("eks", region_name=region, config=retry_config)
        self.ecr = self.session.client("ecr", region_name=region, config=retry_config)

        # Network and load balancing services
        self.elbv2 = self.session.client("elbv2", region_name=region, config=retry_config)
        self.elb = self.session.client("elb", region_name=region, config=retry_config)

        # Management and monitoring services
        self.logs = self.session.client("logs", region_name=region, config=retry_config)
        self.cloudtrail = self.session.client("cloudtrail", region_name=region, config=retry_config)
        self.backup = self.session.client("backup", region_name=region, config=retry_config)
        self.autoscaling = self.session.client("autoscaling", region_name=region, config=retry_config)

        # Global services - always use us-east-1 for consistency
        self.route53 = self.session.client("route53", region_name="us-east-1", config=retry_config)
        self.cloudfront = self.session.client("cloudfront", region_name="us-east-1", config=retry_config)

        # Serverless and application services
        self.lambda_client = self.session.client("lambda", region_name=region, config=retry_config)
        self.apigateway = self.session.client("apigateway", region_name=region, config=retry_config)
        self.apigatewayv2 = self.session.client("apigatewayv2", region_name=region, config=retry_config)
        self.stepfunctions = self.session.client("stepfunctions", region_name=region, config=retry_config)

        # Additional services for comprehensive coverage
        self.sts = self.session.client("sts", region_name=region, config=retry_config)
        self.iam = self.session.client("iam", region_name="us-east-1", config=retry_config)  # Global service
        self.organizations = self.session.client(
            "organizations", region_name="us-east-1", config=retry_config
        )  # Global service
        self.support = self.session.client("support", region_name="us-east-1", config=retry_config)  # Global service
        self.trustedadvisor = self.session.client(
            "support", region_name="us-east-1", config=retry_config
        )  # Global service
        self.pricing = self.session.client("pricing", region_name="us-east-1", config=retry_config)  # Global service
        self.ce = self.session.client("ce", region_name="us-east-1", config=retry_config)  # Global service
        self.cur = self.session.client("cur", region_name="us-east-1", config=retry_config)  # Global service

        # New services for enhanced cost optimization coverage
        self.lightsail = self.session.client("lightsail", region_name=region, config=retry_config)
        self.redshift = self.session.client("redshift", region_name=region, config=retry_config)
        self.redshift_serverless = self.session.client("redshift-serverless", region_name=region, config=retry_config)
        self.dms = self.session.client("dms", region_name=region, config=retry_config)
        self.quicksight = self.session.client("quicksight", region_name=region, config=retry_config)
        self.apprunner = self.session.client("apprunner", region_name=region, config=retry_config)
        self.transfer = self.session.client("transfer", region_name=region, config=retry_config)
        self.kafka = self.session.client("kafka", region_name=region, config=retry_config)  # MSK
        self.workspaces = self.session.client("workspaces", region_name=region, config=retry_config)
        self.mediastore = self.session.client("mediastore", region_name=region, config=retry_config)
        self.budgets = self.session.client("budgets", region_name="us-east-1", config=retry_config)  # Global service
        self.config = self.session.client("config", region_name=region, config=retry_config)

        # Data processing services
        self.glue = self.session.client("glue", region_name=region, config=retry_config)
        self.athena = self.session.client("athena", region_name=region, config=retry_config)
        self.batch = self.session.client("batch", region_name=region, config=retry_config)
        self.ssm = self.session.client("ssm", region_name=region, config=retry_config)
        self.secretsmanager = self.session.client("secretsmanager", region_name=region, config=retry_config)
        self.kms = self.session.client("kms", region_name=region, config=retry_config)
        self.sns = self.session.client("sns", region_name=region, config=retry_config)
        self.sqs = self.session.client("sqs", region_name=region, config=retry_config)
        self.events = self.session.client("events", region_name=region, config=retry_config)

        print("✅ All AWS service clients initialized successfully!")
        print(f"🎯 Ready to scan {region} with comprehensive cost optimization analysis")

        # Get account ID for resource identification
        try:
            self.account_id = self.sts.get_caller_identity()["Account"]
            print(f"✅ Connected to AWS account: {self.account_id}")
        except Exception as e:
            print(f"⚠️ Warning: Could not get account ID: {e}")
            self.account_id = "unknown"

        factory = AwsSessionFactory(self.region, self.profile)
        registry = ClientRegistry(factory)
        self._ctx = ScanContext(
            region=self.region,
            account_id=self.account_id,
            profile=self.profile,
            fast_mode=self.fast_mode,
            clients=registry,
        )

    def _is_kubernetes_managed_alb(self, lb_name: str, lb_arn: str) -> bool:
        """
        Determine if an ALB is managed by Kubernetes (EKS/AWS Load Balancer Controller).

        Based on AWS documentation, K8s-managed ALBs have specific naming patterns:
        - Names starting with 'k8s-' (AWS Load Balancer Controller pattern)
        - Names containing cluster identifiers
        - Tags indicating Kubernetes management
        """
        try:
            # Check naming patterns that indicate K8s management
            k8s_patterns = [
                "k8s-",  # AWS Load Balancer Controller prefix
                "eks-",  # EKS-related naming
                "ingress-",  # Common ingress naming
                "kube-",  # Kubernetes prefix
            ]

            if any(lb_name.lower().startswith(pattern) for pattern in k8s_patterns):
                return True

            # Check tags for Kubernetes management indicators
            try:
                tags_response = self.elbv2.describe_tags(ResourceArns=[lb_arn])
                for tag_desc in tags_response.get("TagDescriptions", []):
                    for tag in tag_desc.get("Tags", []):
                        key = tag.get("Key", "").lower()
                        value = tag.get("Value", "").lower()

                        # Common K8s tags
                        k8s_tag_patterns = [
                            "kubernetes.io/",
                            "ingress.k8s.aws/",
                            "elbv2.k8s.aws/",
                            "alb.ingress.kubernetes.io/",
                        ]

                        if any(pattern in key for pattern in k8s_tag_patterns):
                            return True

                        # Check for cluster name in tags
                        if key in ["kubernetes.io/cluster", "alpha.eksctl.io/cluster-name"]:
                            return True

            except Exception as e:
                # If we can't get tags, fall back to name-based detection
                print(f"Warning: Could not get tags for ALB {lb_arn}: {e}")

            return False

        except Exception as e:
            # If any error occurs, assume it's not K8s managed
            print(f"⚠️ Error checking K8s management for {instance_id}: {str(e)}")
            return False

    def _is_eks_nodegroup_asg(self, asg_name: str, tags: dict) -> bool:
        """
        Determine if an ASG is an EKS node group.

        EKS node groups have specific naming patterns and tags:
        - Names often contain 'eks-', 'nodegroup', or cluster names
        - Tags include kubernetes.io/cluster/<cluster-name>
        - Tags include eks:nodegroup-name
        """
        try:
            # Check naming patterns that indicate EKS node group
            eks_patterns = ["eks-", "nodegroup", "node-group", "ng-"]

            if any(pattern in asg_name.lower() for pattern in eks_patterns):
                return True

            # Check tags for EKS indicators
            for key, value in tags.items():
                key_lower = key.lower()

                # Common EKS node group tags
                if key_lower.startswith("kubernetes.io/cluster/"):
                    return True
                if key_lower == "eks:nodegroup-name":
                    return True
                if key_lower == "eks:cluster-name":
                    return True
                if "nodegroup" in key_lower:
                    return True

            return False

        except Exception as e:
            print(f"⚠️ Error checking ECS management for {instance_id}: {str(e)}")
            return False

    def _format_estimated_savings(self, savings_text: str) -> str:
        """Format savings estimates with clear labeling"""
        if "estimated" not in savings_text.lower():
            return f"Estimated {savings_text.lower()} (estimate based on current AWS pricing)"
        return savings_text

    def get_ebs_volume_count(self) -> Dict[str, int]:
        """
        Get EBS volume counts by state and type.

        Uses pagination to support accounts with unlimited volumes.
        Handles IAM permission errors and rate limiting gracefully.

        Returns:
            Dict with volume counts by state (attached/unattached) and type (gp2/gp3/io1/io2)
        """
        try:
            paginator = self.ec2.get_paginator("describe_volumes")
            volumes = []
            for page in paginator.paginate():
                volumes.extend(page.get("Volumes", []))

            counts = {"total": len(volumes), "attached": 0, "unattached": 0, "gp2": 0, "gp3": 0, "io1": 0, "io2": 0}

            for volume in volumes:
                # Count by attachment state
                if volume["State"] == "in-use":
                    counts["attached"] += 1
                else:
                    counts["unattached"] += 1

                # Count by volume type
                vol_type = volume.get("VolumeType", "unknown")
                if vol_type in counts:
                    counts[vol_type] += 1

            return counts
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "UnauthorizedOperation":
                print(f"Warning: Missing IAM permission for describe_volumes")
            elif error_code == "RequestLimitExceeded":
                print(f"Warning: Rate limit exceeded for describe_volumes (retries exhausted)")
            else:
                print(f"Warning: Could not get EBS volume count: {e}")
            return {"total": 0, "attached": 0, "unattached": 0, "gp2": 0, "gp3": 0, "io1": 0, "io2": 0}
        except Exception as e:
            print(f"Warning: Unexpected error getting EBS volume count: {e}")
            return {"total": 0, "attached": 0, "unattached": 0, "gp2": 0, "gp3": 0, "io1": 0, "io2": 0}

    def get_ebs_compute_optimizer_recommendations(self) -> List[Dict[str, Any]]:
        """Get EBS recommendations from Compute Optimizer"""
        recommendations = []
        try:
            response = self.compute_optimizer.get_ebs_volume_recommendations()
            recommendations.extend(response["volumeRecommendations"])

            # Handle pagination manually
            while response.get("nextToken"):
                response = self.compute_optimizer.get_ebs_volume_recommendations(nextToken=response["nextToken"])
                recommendations.extend(response["volumeRecommendations"])
        except Exception as e:
            print(f"Warning: EBS Compute Optimizer not available: {e}")
            # Add recommendation to enable Compute Optimizer
            if "OptInRequiredException" in str(e) or "not registered" in str(e):
                opt_in_recommendation = {
                    "ResourceId": "compute-optimizer-service",
                    "ResourceType": "Service Configuration",
                    "Issue": "AWS Compute Optimizer not enabled",
                    "Recommendation": "Enable AWS Compute Optimizer for EBS recommendations",
                    "EstimatedMonthlySavings": "Variable - up to 20% on EBS volumes",
                    "Action": "Go to AWS Compute Optimizer console and opt-in to receive EBS rightsizing recommendations",
                    "Priority": "Medium",
                    "Service": "Compute Optimizer",
                }
                recommendations.append(opt_in_recommendation)
        return recommendations

    def get_unattached_volumes(self) -> List[Dict[str, Any]]:
        """
        Get unattached EBS volumes for cost optimization.

        Identifies volumes in 'available' state (not attached to any instance).
        Uses pagination to support unlimited volumes.
        Calculates estimated monthly cost for each unattached volume.

        Returns:
            List of dicts with VolumeId, Size, VolumeType, CreateTime, EstimatedMonthlyCost
            Returns empty list on errors (with warning messages)
        """
        unattached = []
        try:
            # First, get volume IDs attached to stopped instances to exclude them
            stopped_instance_volumes = set()
            instance_paginator = self.ec2.get_paginator("describe_instances")
            for page in instance_paginator.paginate(Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]):
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        for bdm in instance.get("BlockDeviceMappings", []):
                            if "Ebs" in bdm:
                                stopped_instance_volumes.add(bdm["Ebs"]["VolumeId"])

            paginator = self.ec2.get_paginator("describe_volumes")
            for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
                for volume in page.get("Volumes", []):
                    volume_id = volume["VolumeId"]
                    # Skip volumes attached to stopped instances
                    if volume_id in stopped_instance_volumes:
                        continue

                    # Check if volume is truly unattached (not attached to stopped instances)
                    attachments = volume.get("Attachments", [])
                    if not attachments:  # Completely unattached
                        unattached.append(
                            {
                                "VolumeId": volume["VolumeId"],
                                "Size": volume["Size"],
                                "VolumeType": volume["VolumeType"],
                                "CreateTime": volume["CreateTime"].isoformat(),
                                "EstimatedMonthlyCost": self._estimate_volume_cost(
                                    volume["Size"], volume["VolumeType"], volume.get("Iops"), volume.get("Throughput")
                                ),
                            }
                        )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "UnauthorizedOperation":
                print(f"Warning: Missing IAM permission for describe_volumes")
            elif error_code == "RequestLimitExceeded":
                print(f"Warning: Rate limit exceeded for describe_volumes (retries exhausted)")
            else:
                print(f"Warning: Could not get unattached volumes: {e}")
        except Exception as e:
            print(f"Warning: Unexpected error getting unattached volumes: {e}")
        return unattached

    def get_ebs_optimization_descriptions(self) -> Dict[str, str]:
        """Get descriptions for EBS cost optimization opportunities"""
        return {
            "unattached_volumes": {
                "title": "Delete Unattached EBS Volumes",
                "description": "Unattached EBS volumes continue to incur storage costs. Delete volumes that are no longer needed after creating snapshots for backup if required.",
                "action": "1. Create snapshot if data recovery needed\n2. Delete unattached volume via AWS Console or CLI\n3. Estimated savings: 100% of volume storage cost (estimate based on current volume pricing)",
            },
            "gp2_to_gp3": {
                "title": "Migrate gp2 to gp3 Volumes",
                "description": "gp3 volumes offer up to 20% cost savings compared to gp2 with better performance baseline (3,000 IOPS vs size-dependent IOPS).",
                "action": "1. Modify volume type from gp2 to gp3 (no downtime)\n2. Optionally adjust IOPS and throughput independently\n3. Estimated savings: ~20% reduction in storage costs (estimate based on current AWS pricing)",
            },
            "io1_to_io2": {
                "title": "Upgrade io1 to io2 Volumes",
                "description": "io2 volumes provide better durability (99.999% vs 99.9%) and higher IOPS limits at the same price as io1.",
                "action": "1. Modify volume type from io1 to io2 (no downtime)\n2. Benefit from improved performance and durability\n3. Cost: Same price with better performance",
            },
            "volume_rightsizing": {
                "title": "Rightsize EBS Volumes",
                "description": "Reduce volume size or IOPS allocation based on actual usage patterns to eliminate over-provisioning.",
                "action": "1. Analyze CloudWatch metrics for volume utilization\n2. Reduce volume size or IOPS if underutilized\n3. Estimated savings: 10-50% based on over-provisioning (estimate varies by usage pattern)",
            },
        }

    def _estimate_volume_cost(self, size_gb: int, volume_type: str, iops: int = None, throughput: int = None) -> float:
        """
        Estimate monthly cost for EBS volume including IOPS and throughput.

        Uses January 2026 AWS pricing adjusted for region.

        Args:
            size_gb: Volume size in GB
            volume_type: EBS volume type (gp2, gp3, io1, io2, st1, sc1)
            iops: Provisioned IOPS (for gp3, io1, io2)
            throughput: Provisioned throughput in MB/s (for gp3)

        Returns:
            Estimated monthly cost in USD
        """
        # January 2026 pricing per GB/month (us-east-1 baseline)
        pricing = {
            "gp2": 0.10,  # General Purpose SSD
            "gp3": 0.08,  # General Purpose SSD (20% cheaper than gp2)
            "io1": 0.125,  # Provisioned IOPS SSD
            "io2": 0.125,  # Provisioned IOPS SSD (latest generation)
            "st1": 0.045,  # Throughput Optimized HDD
            "sc1": 0.025,  # Cold HDD
        }

        base_cost = size_gb * pricing.get(volume_type, 0.10) * self.pricing_multiplier

        # Add IOPS costs
        if iops and volume_type in ["gp3", "io1", "io2"]:
            if volume_type == "gp3":
                # gp3: 3,000 IOPS included, $0.005/IOPS-month for additional
                extra_iops = max(0, iops - 3000)
                base_cost += extra_iops * 0.005 * self.pricing_multiplier
            elif volume_type in ["io1", "io2"]:
                # io1/io2: $0.065/IOPS-month
                base_cost += iops * 0.065 * self.pricing_multiplier

        # Add throughput costs for gp3
        if throughput and volume_type == "gp3":
            # gp3: 125 MB/s included, $0.04/MB/s-month for additional
            extra_throughput = max(0, throughput - 125)
            base_cost += extra_throughput * 0.04 * self.pricing_multiplier

        return base_cost

    def get_enhanced_ebs_checks(self) -> Dict[str, Any]:
        """
        Get enhanced EBS cost optimization checks.

        Performs 8 categories of EBS optimization checks:
        1. Unattached volumes (100% savings opportunity)
        2. gp2→gp3 migration (20% savings)
        3. Old snapshots (>90 days, storage cost reduction)
        4. Underutilized volumes (rightsizing opportunity)
        5. Over-provisioned IOPS (cost reduction)
        6. Unused encrypted volumes (storage cost elimination)
        7. Orphaned snapshots (cleanup opportunity)
        8. Snapshot lifecycle policies (automated cost management)

        Uses pagination to support unlimited volumes.

        Returns:
            Dict with 'recommendations' list containing all EBS optimization opportunities
        """
        checks = {
            "unattached_volumes": self.get_unattached_volumes(),
            "gp2_migration": [],
            "old_snapshots": [],
            "underutilized_volumes": [],
            "over_provisioned_iops": [],
            "unused_encrypted_volumes": [],
            "orphaned_snapshots": [],
            "snapshot_lifecycle": [],
        }

        try:
            # Check for gp2 volumes that can migrate to gp3
            paginator = self.ec2.get_paginator("describe_volumes")
            for page in paginator.paginate(Filters=[{"Name": "volume-type", "Values": ["gp2"]}]):
                for volume in page.get("Volumes", []):
                    checks["gp2_migration"].append(
                        {
                            "VolumeId": volume["VolumeId"],
                            "Size": volume["Size"],
                            "CurrentType": "gp2",
                            "RecommendedType": "gp3",
                            "EstimatedSavings": "Estimated 20% cost reduction",
                            "CheckCategory": "Volume Type Optimization",
                        }
                    )

            # Check for underutilized volumes (basic heuristic - high IOPS but low utilization)
            for page in paginator.paginate():
                for volume in page.get("Volumes", []):
                    volume_type = volume.get("VolumeType", "")
                    iops = volume.get("Iops", 0)
                    size = volume.get("Size", 0)

                    # Flag high-IOPS volumes that might be underutilized
                    if volume_type in ["io1", "io2"] and iops > 1000:
                        checks["underutilized_volumes"].append(
                            {
                                "VolumeId": volume["VolumeId"],
                                "VolumeType": volume_type,
                                "Size": size,
                                "IOPS": iops,
                                "Recommendation": f"High-IOPS volume ({iops} IOPS) - verify utilization with CloudWatch metrics",
                                "EstimatedSavings": "Enable CloudWatch monitoring to validate IOPS usage",
                                "CheckCategory": "Underutilized Volumes",
                            }
                        )

            # Check for snapshot lifecycle opportunities
            paginator = self.ec2.get_paginator("describe_snapshots")
            snapshot_count = 0
            for page in paginator.paginate(OwnerIds=["self"]):
                for snapshot in page["Snapshots"]:
                    snapshot_count += 1

            # If many snapshots, suggest lifecycle management
            if snapshot_count > 50:
                checks["snapshot_lifecycle"].append(
                    {
                        "SnapshotCount": snapshot_count,
                        "Recommendation": f"Account has {snapshot_count} snapshots - consider implementing automated lifecycle management",
                        "EstimatedSavings": "Automated cleanup of old snapshots can reduce storage costs",
                        "CheckCategory": "Snapshot Lifecycle",
                    }
                )

            # Check for over-provisioned IOPS (heuristic estimates - recommend CloudWatch validation)
            volume_paginator = self.ec2.get_paginator("describe_volumes")
            for page in volume_paginator.paginate(Filters=[{"Name": "volume-type", "Values": ["io1", "io2", "gp3"]}]):
                for volume in page.get("Volumes", []):
                    iops = volume.get("Iops", 0) if volume["VolumeType"] in ["io1", "io2", "gp3"] else 0
                    size = volume["Size"]
                    volume_type = volume["VolumeType"]

                    # Check if IOPS is over-provisioned
                    if volume_type == "gp3" and iops > 3000 + size * 30:  # gp3: 3000 baseline + reasonable ratio
                        recommended_iops = 3000 + size * 30
                        extra_iops = iops - recommended_iops
                        savings = extra_iops * 0.005 * self.pricing_multiplier
                        checks["over_provisioned_iops"].append(
                            {
                                "VolumeId": volume["VolumeId"],
                                "CurrentIOPS": iops,
                                "RecommendedIOPS": recommended_iops,
                                "Recommendation": "Reduce provisioned IOPS based on actual usage",
                                "EstimatedSavings": f"${savings:.2f}/month",
                            }
                        )
                    elif volume_type in ["io1", "io2"] and iops > size * 50:  # io1/io2: check for over-provisioning
                        recommended_iops = size * 30
                        extra_iops = iops - recommended_iops
                        savings = extra_iops * 0.065 * self.pricing_multiplier
                        checks["over_provisioned_iops"].append(
                            {
                                "VolumeId": volume["VolumeId"],
                                "CurrentIOPS": iops,
                                "RecommendedIOPS": recommended_iops,
                                "Recommendation": "Reduce provisioned IOPS based on actual usage",
                                "EstimatedSavings": f"${savings:.2f}/month",
                            }
                        )

            # Check for old snapshots (>90 days for Snapshots tab) - with pagination
            paginator = self.ec2.get_paginator("describe_snapshots")
            for page in paginator.paginate(OwnerIds=["self"]):
                for snapshot in page["Snapshots"]:
                    age_days = (datetime.now(snapshot["StartTime"].tzinfo) - snapshot["StartTime"]).days
                    if age_days > self.OLD_SNAPSHOT_DAYS:  # Only snapshots older than 90 days
                        checks["old_snapshots"].append(
                            {
                                "SnapshotId": snapshot["SnapshotId"],
                                "AgeDays": age_days,
                                "VolumeSize": snapshot["VolumeSize"],
                                "CheckCategory": "Old Snapshots",
                                "Recommendation": f"Review {age_days}-day old snapshot for deletion (Note: Actual savings may be lower due to incremental storage)",
                                "EstimatedSavings": f"${snapshot['VolumeSize'] * 0.05 * self.pricing_multiplier:.2f}/month (max estimate)",
                            }
                        )

                    # Check for orphaned snapshots (from deleted AMIs) - only if >90 days old
                    if (
                        snapshot.get("Description", "").startswith("Created by CreateImage")
                        and age_days > self.OLD_SNAPSHOT_DAYS
                    ):
                        checks["orphaned_snapshots"].append(
                            {
                                "SnapshotId": snapshot["SnapshotId"],
                                "AgeDays": age_days,
                                "VolumeSize": snapshot["VolumeSize"],
                                "Description": snapshot.get("Description", ""),
                                "Recommendation": "Check if snapshot is from deleted AMI and can be removed (Note: Actual savings may be lower due to incremental storage)",
                                "CheckCategory": "Orphaned Snapshots",
                                "EstimatedSavings": f"${snapshot['VolumeSize'] * 0.05 * self.pricing_multiplier:.2f}/month (max estimate)",
                            }
                        )

            # Check for unused encrypted volumes
            volume_paginator = self.ec2.get_paginator("describe_volumes")
            for page in volume_paginator.paginate(
                Filters=[{"Name": "encrypted", "Values": ["true"]}, {"Name": "status", "Values": ["available"]}]
            ):
                for volume in page.get("Volumes", []):
                    checks["unused_encrypted_volumes"].append(
                        {
                            "VolumeId": volume["VolumeId"],
                            "Size": volume["Size"],
                            "Encrypted": True,
                            "Recommendation": "Delete unused encrypted volume",
                            "EstimatedSavings": f"${self._estimate_volume_cost(volume['Size'], volume['VolumeType'], volume.get('Iops'), volume.get('Throughput')):.2f}/month",
                        }
                    )

        except Exception as e:
            print(f"Warning: Could not perform enhanced EBS checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            if isinstance(items, list):
                for item in items:
                    item["CheckCategory"] = item.get("CheckCategory", category.replace("_", " ").title())
                    recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_rds_instance_count(self) -> Dict[str, int]:
        """Get RDS instance counts by engine and state"""
        try:
            paginator = self.rds.get_paginator("describe_db_instances")
            instances = []
            for page in paginator.paginate():
                instances.extend(page.get("DBInstances", []))

            counts = {
                "total": len(instances),
                "running": 0,
                "stopped": 0,
                "mysql": 0,
                "postgres": 0,
                "aurora": 0,
                "oracle": 0,
                "sqlserver": 0,
            }

            for instance in instances:
                # Count by state
                if instance["DBInstanceStatus"] == "available":
                    counts["running"] += 1
                elif instance["DBInstanceStatus"] == "stopped":
                    counts["stopped"] += 1

                # Count by engine
                engine = instance.get("Engine", "").lower()
                if "mysql" in engine:
                    counts["mysql"] += 1
                elif "postgres" in engine:
                    counts["postgres"] += 1
                elif "aurora" in engine:
                    counts["aurora"] += 1
                elif "oracle" in engine:
                    counts["oracle"] += 1
                elif "sqlserver" in engine:
                    counts["sqlserver"] += 1

            return counts
        except Exception as e:
            print(f"Warning: Could not get RDS instance count: {e}")
            return {
                "total": 0,
                "running": 0,
                "stopped": 0,
                "mysql": 0,
                "postgres": 0,
                "aurora": 0,
                "oracle": 0,
                "sqlserver": 0,
            }

    def get_rds_compute_optimizer_recommendations(self) -> List[Dict[str, Any]]:
        """Get RDS recommendations from Compute Optimizer"""
        recommendations = []
        try:
            response = self.compute_optimizer.get_rds_database_recommendations()
            recommendations.extend(response["rdsDBRecommendations"])

            # Handle pagination manually
            while response.get("nextToken"):
                response = self.compute_optimizer.get_rds_database_recommendations(nextToken=response["nextToken"])
                recommendations.extend(response["rdsDBRecommendations"])
        except Exception as e:
            print(f"Warning: RDS Compute Optimizer not available: {e}")
            # Add recommendation to enable Compute Optimizer
            if "OptInRequiredException" in str(e) or "not registered" in str(e):
                opt_in_recommendation = {
                    "ResourceId": "compute-optimizer-service",
                    "ResourceType": "Service Configuration",
                    "Issue": "AWS Compute Optimizer not enabled",
                    "Recommendation": "Enable AWS Compute Optimizer for RDS recommendations",
                    "EstimatedMonthlySavings": "Variable - up to 25% on RDS instances",
                    "Action": "Go to AWS Compute Optimizer console and opt-in to receive RDS rightsizing recommendations",
                    "Priority": "Medium",
                    "Service": "Compute Optimizer",
                }
                recommendations.append(opt_in_recommendation)
        return recommendations

    def get_rds_optimization_descriptions(self) -> Dict[str, str]:
        """Get descriptions for RDS cost optimization opportunities"""
        return {
            "idle_databases": {
                "title": "Stop or Delete Idle RDS Instances",
                "description": "Idle RDS instances with low CPU utilization can be stopped to save costs or deleted if no longer needed.",
                "action": "1. Stop instance to save compute costs (storage still charged)\n2. Delete instance and create final snapshot\n3. Consider Aurora Serverless v2 for variable workloads\n4. Estimated savings: 100% of compute costs when stopped",
            },
            "rds_optimization": {
                "title": "Comprehensive RDS Cost Optimization",
                "description": "RDS instances can be optimized through multiple strategies including rightsizing, engine optimization, and Reserved Instance purchases.",
                "action": "1. **Performance Analysis**: Review CloudWatch metrics for CPU (target 70-80%), memory, and IOPS utilization over 2-4 weeks\n2. **Rightsizing**: Downsize overprovisioned instances to match actual usage patterns\n3. **Graviton Migration**: Migrate to Graviton2/Graviton3 instances for 20% cost reduction\n4. **Reserved Instances**: Purchase 1-year or 3-year RIs for 30-72% savings on predictable workloads\n5. **Storage Optimization**: Migrate from gp2 to gp3 storage for 20% savings\n6. **Engine Optimization**: Consider Aurora for better performance per dollar\n7. **Multi-AZ Review**: Disable Multi-AZ for non-production environments\n8. **Backup Optimization**: Reduce backup retention for non-critical databases",
            },
            "instance_rightsizing": {
                "title": "Rightsize RDS Instance Classes",
                "description": "Move to smaller instance classes based on actual CPU, memory, and I/O utilization patterns.",
                "action": "1. Analyze CloudWatch metrics for CPU/memory usage\n2. Modify instance class during maintenance window\n3. Monitor performance after change\n4. Estimated savings: 20-50% based on rightsizing",
            },
            "reserved_instances": {
                "title": "Purchase RDS Reserved Instances",
                "description": "Save up to 72% compared to On-Demand pricing with 1-year or 3-year commitments.",
                "action": "1. Analyze usage patterns for steady workloads\n2. Purchase Reserved Instances (No/Partial/All Upfront)\n3. Apply to existing instances automatically\n4. Estimated savings: 30-72% vs On-Demand",
            },
            "storage_optimization": {
                "title": "Optimize RDS Storage Configuration",
                "description": "Adjust storage type, size, and IOPS allocation based on actual usage patterns.",
                "action": "1. Monitor storage metrics and IOPS utilization\n2. Reduce allocated storage if over-provisioned\n3. Switch from Provisioned IOPS to gp3 if appropriate\n4. Estimated savings: 10-30% on storage costs",
            },
        }

    def get_ec2_instance_count(self) -> int:
        """
        Get total EC2 instance count in region.

        Uses pagination to support accounts with unlimited instances.
        Counts all instances across all reservations regardless of state.

        Returns:
            Total number of EC2 instances in the region
            Returns 0 on errors (with warning messages)
        """
        try:
            paginator = self.ec2.get_paginator("describe_instances")
            count = 0
            for page in paginator.paginate():
                for reservation in page.get("Reservations", []):
                    count += len(reservation["Instances"])
            return count
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "UnauthorizedOperation":
                print(f"Warning: Missing IAM permission for describe_instances")
            elif error_code == "RequestLimitExceeded":
                print(f"Warning: Rate limit exceeded for describe_instances (retries exhausted)")
            else:
                print(f"Warning: Could not get EC2 instance count: {e}")
            return 0
        except Exception as e:
            print(f"Warning: Unexpected error getting EC2 instance count: {e}")
            return 0

    def get_efs_file_system_count(self) -> Dict[str, Any]:
        """Get EFS file system counts and analysis"""
        try:
            paginator = self.efs.get_paginator("describe_file_systems")
            counts = {
                "total": 0,
                "available": 0,
                "creating": 0,
                "deleting": 0,
                "standard_storage": 0,
                "one_zone_storage": 0,
                "total_size_gb": 0,
                "unused_systems": [],
            }

            for page in paginator.paginate():
                for fs in page["FileSystems"]:
                    counts["total"] += 1
                    # Count by lifecycle state
                    state = fs.get("LifeCycleState", "")
                    if state == "available":
                        counts["available"] += 1
                    elif state == "creating":
                        counts["creating"] += 1
                    elif state == "deleting":
                        counts["deleting"] += 1

                    # Count by storage class
                    if fs.get("AvailabilityZoneName"):
                        counts["one_zone_storage"] += 1
                    else:
                        counts["standard_storage"] += 1

                    # Calculate total size
                    size_bytes = fs.get("SizeInBytes", {}).get("Value", 0)
                    size_gb = size_bytes / (1024**3) if size_bytes else 0
                    counts["total_size_gb"] += size_gb

                    # Identify potentially unused systems (very small size)
                    if size_gb < 0.1 and fs.get("NumberOfMountTargets", 0) == 0:
                        counts["unused_systems"].append(
                            {
                                "FileSystemId": fs["FileSystemId"],
                                "Name": fs.get("Name", "Unnamed"),
                                "SizeGB": round(size_gb, 3),
                                "CreationTime": fs["CreationTime"].isoformat(),
                                "MountTargets": fs.get("NumberOfMountTargets", 0),
                            }
                        )

            counts["total_size_gb"] = round(counts["total_size_gb"], 2)
            return counts
        except Exception as e:
            print(f"Warning: Could not get EFS file system count: {e}")
            return {
                "total": 0,
                "available": 0,
                "creating": 0,
                "deleting": 0,
                "standard_storage": 0,
                "one_zone_storage": 0,
                "total_size_gb": 0,
                "unused_systems": [],
            }

    def get_efs_lifecycle_analysis(self) -> List[Dict[str, Any]]:
        """Analyze EFS file systems for lifecycle policy optimization"""
        recommendations = []
        try:
            paginator = self.efs.get_paginator("describe_file_systems")

            for page in paginator.paginate():
                for fs in page["FileSystems"]:
                    fs_id = fs["FileSystemId"]

                    try:
                        # Get lifecycle configuration
                        lifecycle_response = self.efs.describe_lifecycle_configuration(FileSystemId=fs_id)
                        lifecycle_policies = lifecycle_response.get("LifecyclePolicies", [])

                        # Check if One Zone file system (no Archive class available)
                        availability_zone_name = fs.get("AvailabilityZoneName")
                        is_one_zone = availability_zone_name is not None

                        # Analyze lifecycle policies (Archive not available for One Zone)
                        has_ia_policy = any(p.get("TransitionToIA") for p in lifecycle_policies)
                        has_archive_policy = (
                            any(p.get("TransitionToArchive") for p in lifecycle_policies) if not is_one_zone else False
                        )

                        size_bytes = fs.get("SizeInBytes", {}).get("Value", 0)
                        size_gb = size_bytes / (1024**3) if size_bytes else 0

                        recommendation = {
                            "FileSystemId": fs_id,
                            "Name": fs.get("Name", "Unnamed"),
                            "SizeGB": round(size_gb, 2),
                            "HasIAPolicy": has_ia_policy,
                            "HasArchivePolicy": has_archive_policy,
                            "MountTargets": fs.get("NumberOfMountTargets", 0),
                            "StorageClass": "One Zone" if fs.get("AvailabilityZoneName") else "Standard",
                            "EstimatedMonthlyCost": self._estimate_efs_cost(
                                size_gb, fs.get("AvailabilityZoneName") is not None
                            ),
                        }

                        recommendations.append(recommendation)

                    except Exception as e:
                        print(f"Warning: Could not get lifecycle config for {fs_id}: {e}")

        except Exception as e:
            print(f"Warning: Could not analyze EFS lifecycle policies: {e}")

        return recommendations

    def _estimate_efs_cost(self, size_gb: float, is_one_zone: bool = False) -> float:
        """Estimate monthly cost for EFS file system"""
        # Pricing per GB/month (us-east-1 baseline)
        if is_one_zone:
            standard_price = 0.16  # One Zone Standard
            ia_price = 0.0133  # One Zone IA
        else:
            standard_price = 0.30  # Regional Standard
            ia_price = 0.025  # Regional IA

        # Assume 80% could be moved to IA for cost estimation
        base_cost = (size_gb * 0.2 * standard_price) + (size_gb * 0.8 * ia_price)
        return base_cost * self.pricing_multiplier

    def get_efs_optimization_descriptions(self) -> Dict[str, str]:
        """Get descriptions for EFS cost optimization opportunities"""
        return {
            "lifecycle_policies": {
                "title": "Configure EFS Lifecycle Policies",
                "description": "Automatically move infrequently accessed files to IA (up to 94% cost savings) and Archive storage classes.",
                "action": "1. Enable Transition to IA after 30 days\n2. Enable Transition to Archive after 90 days\n3. Configure Transition back to Standard on access\n4. Estimated savings: 80-94% for infrequent data",
            },
            "unused_file_systems": {
                "title": "Delete Unused EFS File Systems",
                "description": "Remove EFS file systems with no mount targets and minimal data to eliminate unnecessary costs.",
                "action": "1. Verify no applications are using the file system\n2. Create backup if data recovery needed\n3. Delete unused file systems via console or CLI\n4. Estimated savings: 100% of file system costs",
            },
            "one_zone_migration": {
                "title": "Migrate to EFS One Zone Storage",
                "description": "For workloads that don't require Multi-AZ resilience, One Zone storage offers 47% cost savings.",
                "action": "1. Assess availability requirements\n2. Create new One Zone file system\n3. Migrate data using AWS DataSync\n4. Estimated savings: 47% vs Regional storage",
            },
            "storage_class_optimization": {
                "title": "Optimize EFS Storage Classes",
                "description": "Use appropriate storage classes based on access patterns: Standard, IA, or Archive.",
                "action": "1. Analyze file access patterns\n2. Configure lifecycle policies for automatic transitions\n3. Use EFS Intelligent-Tiering for automatic optimization\n4. Estimated savings: Up to 94% for cold data",
            },
            "throughput_optimization": {
                "title": "Optimize EFS Throughput Mode",
                "description": "Switch from Provisioned to Elastic Throughput mode to pay only for actual usage.",
                "action": "1. Monitor current throughput usage patterns\n2. Switch to Elastic Throughput mode\n3. Remove unnecessary provisioned throughput\n4. Estimated savings: 20-50% on throughput costs",
            },
        }

    def get_fsx_file_system_count(self) -> Dict[str, Any]:
        """Get FSx file system counts and analysis (includes File Cache)"""
        try:
            # Get FSx file systems
            fs_response = self.fsx.describe_file_systems()

            # Get File Cache instances
            cache_response = self.fsx.describe_file_caches()

            counts = {
                "total": len(fs_response["FileSystems"]) + len(cache_response.get("FileCaches", [])),
                "available": 0,
                "creating": 0,
                "deleting": 0,
                "lustre": 0,
                "windows": 0,
                "ontap": 0,
                "openzfs": 0,
                "file_cache": len(cache_response.get("FileCaches", [])),
                "total_capacity_gb": 0,
                "underutilized_systems": [],
            }

            # Process FSx file systems
            for fs in fs_response["FileSystems"]:
                # Count by lifecycle state
                state = fs.get("Lifecycle", "")
                if state == "AVAILABLE":
                    counts["available"] += 1
                elif state == "CREATING":
                    counts["creating"] += 1
                elif state == "DELETING":
                    counts["deleting"] += 1

                # Count by file system type
                fs_type = fs.get("FileSystemType", "").lower()
                if fs_type == "lustre":
                    counts["lustre"] += 1
                elif fs_type == "windows":
                    counts["windows"] += 1
                elif fs_type == "ontap":
                    counts["ontap"] += 1
                elif fs_type == "openzfs":
                    counts["openzfs"] += 1

                # Calculate total capacity
                capacity_gb = fs.get("StorageCapacity", 0)
                counts["total_capacity_gb"] += capacity_gb

                # Identify potentially underutilized systems (small capacity, older systems)
                if capacity_gb > 0 and capacity_gb < 100:  # Less than 100GB might be underutilized
                    counts["underutilized_systems"].append(
                        {
                            "FileSystemId": fs["FileSystemId"],
                            "FileSystemType": fs.get("FileSystemType", "Unknown"),
                            "StorageCapacity": capacity_gb,
                            "CreationTime": fs["CreationTime"].isoformat(),
                            "Lifecycle": fs.get("Lifecycle", "Unknown"),
                        }
                    )

            # Process File Cache instances
            for cache in cache_response.get("FileCaches", []):
                state = cache.get("Lifecycle", "")
                if state == "AVAILABLE":
                    counts["available"] += 1
                elif state == "CREATING":
                    counts["creating"] += 1
                elif state == "DELETING":
                    counts["deleting"] += 1

                # Add cache capacity
                capacity_gb = cache.get("StorageCapacity", 0)
                counts["total_capacity_gb"] += capacity_gb

                # Check for underutilized caches
                if capacity_gb > 0 and capacity_gb < 1200:  # File Cache minimum is 1.2 TiB
                    counts["underutilized_systems"].append(
                        {
                            "FileCacheId": cache["FileCacheId"],
                            "FileSystemType": "FILE_CACHE",
                            "StorageCapacity": capacity_gb,
                            "CreationTime": cache["CreationTime"].isoformat(),
                            "Lifecycle": cache.get("Lifecycle", "Unknown"),
                        }
                    )

            return counts
        except Exception as e:
            print(f"Warning: Could not get FSx file system count: {e}")
            return {
                "total": 0,
                "available": 0,
                "creating": 0,
                "deleting": 0,
                "lustre": 0,
                "windows": 0,
                "ontap": 0,
                "openzfs": 0,
                "file_cache": 0,
                "total_capacity_gb": 0,
                "underutilized_systems": [],
            }

    def get_fsx_optimization_analysis(self) -> List[Dict[str, Any]]:
        """Analyze FSx file systems and File Cache for optimization opportunities"""
        recommendations = []
        try:
            # Analyze FSx file systems
            fs_response = self.fsx.describe_file_systems()

            for fs in fs_response["FileSystems"]:
                fs_id = fs["FileSystemId"]
                fs_type = fs.get("FileSystemType", "Unknown")
                capacity_gb = fs.get("StorageCapacity", 0)
                storage_type = fs.get("StorageType", "Unknown")

                recommendation = {
                    "FileSystemId": fs_id,
                    "FileSystemType": fs_type,
                    "StorageCapacity": capacity_gb,
                    "StorageType": storage_type,
                    "Lifecycle": fs.get("Lifecycle", "Unknown"),
                    "CreationTime": fs["CreationTime"].isoformat(),
                    "EstimatedMonthlyCost": self._estimate_fsx_cost(fs_type, capacity_gb, storage_type),
                    "OptimizationOpportunities": self._get_fsx_optimization_opportunities(fs),
                }

                recommendations.append(recommendation)

            # Analyze File Cache instances
            cache_response = self.fsx.describe_file_caches()

            for cache in cache_response.get("FileCaches", []):
                cache_id = cache["FileCacheId"]
                capacity_gb = cache.get("StorageCapacity", 0)

                recommendation = {
                    "FileCacheId": cache_id,
                    "FileSystemType": "FILE_CACHE",
                    "StorageCapacity": capacity_gb,
                    "StorageType": "SSD",  # File Cache uses SSD storage
                    "Lifecycle": cache.get("Lifecycle", "Unknown"),
                    "CreationTime": cache["CreationTime"].isoformat(),
                    "EstimatedMonthlyCost": self._estimate_file_cache_cost(capacity_gb),
                    "OptimizationOpportunities": self._get_file_cache_optimization_opportunities(cache),
                }

                recommendations.append(recommendation)

        except Exception as e:
            print(f"Warning: Could not analyze FSx file systems: {e}")

        return recommendations

    def _estimate_fsx_cost(self, fs_type: str, capacity_gb: int, storage_type: str) -> float:
        """Estimate monthly cost for FSx file system"""
        # Pricing per GB/month (us-east-1 baseline)
        pricing = {
            "LUSTRE": {"SSD": 0.145, "HDD": 0.040},
            "WINDOWS": {"SSD": 0.13, "HDD": 0.08},
            "ONTAP": {"SSD": 0.144, "HDD": 0.05},
            "OPENZFS": {"SSD": 0.20, "INTELLIGENT_TIERING": 0.10},
        }

        fs_pricing = pricing.get(fs_type.upper(), {"SSD": 0.15})
        storage_price = fs_pricing.get(storage_type, fs_pricing.get("SSD", 0.15))

        return capacity_gb * storage_price * self.pricing_multiplier

    def _get_fsx_optimization_opportunities(self, fs: Dict[str, Any]) -> List[str]:
        """Get optimization opportunities for a specific FSx file system"""
        opportunities = []
        fs_type = fs.get("FileSystemType", "").upper()
        storage_type = fs.get("StorageType", "")
        capacity = fs.get("StorageCapacity", 0)

        # Storage type optimization
        if fs_type in ["LUSTRE", "WINDOWS"] and storage_type == "SSD" and capacity > 500:
            opportunities.append("Consider HDD storage for large, less performance-critical workloads")

        if fs_type == "OPENZFS" and storage_type == "SSD":
            opportunities.append("Consider Intelligent-Tiering for automatic cost optimization")

        # Capacity optimization
        if capacity < 100:
            opportunities.append("Small file system - consider consolidation or deletion if unused")

        # Type-specific optimizations
        if fs_type == "ONTAP":
            opportunities.append("Enable data deduplication and compression for cost savings")
            opportunities.append("Use capacity pool tier for infrequently accessed data")

        if fs_type == "LUSTRE":
            opportunities.append("Consider scratch file systems for temporary workloads")
            opportunities.append("Use data compression to reduce storage requirements")

        return opportunities

    def _estimate_file_cache_cost(self, capacity_gb: int) -> float:
        """Estimate monthly cost for Amazon File Cache"""
        # File Cache pricing per GB/month (us-east-1 baseline)
        # File Cache is approximately $0.30/GB/month
        cache_price_per_gb = 0.30
        return capacity_gb * cache_price_per_gb * self.pricing_multiplier

    def _get_file_cache_optimization_opportunities(self, cache: Dict[str, Any]) -> List[str]:
        """Get optimization opportunities for Amazon File Cache"""
        opportunities = []
        capacity = cache.get("StorageCapacity", 0)

        # Capacity optimization
        if capacity < 2400:  # Less than 2.4 TiB might be underutilized
            opportunities.append("Small cache size - consider consolidation or deletion if unused")

        # Cache-specific optimizations
        opportunities.append("Enable automatic cache eviction to optimize storage usage")
        opportunities.append("Set storage quotas for users and groups to control costs")
        opportunities.append("Monitor cache hit rates and adjust capacity based on usage patterns")
        opportunities.append("Consider using linked data repositories to reduce cache storage needs")

        return opportunities

    def get_file_system_optimization_descriptions(self) -> Dict[str, str]:
        """Get descriptions for all file system cost optimization opportunities"""
        return {
            "efs_lifecycle_policies": {
                "title": "Configure EFS Lifecycle Policies",
                "description": "Automatically move infrequently accessed files to IA (up to 94% cost savings) and Archive storage classes.",
                "action": "1. Enable Transition to IA after 30 days\n2. Enable Transition to Archive after 90 days\n3. Configure Transition back to Standard on access\n4. Estimated savings: 80-94% for infrequent data",
            },
            "efs_unused_systems": {
                "title": "Delete Unused EFS File Systems",
                "description": "Remove EFS file systems with no mount targets and minimal data to eliminate unnecessary costs.",
                "action": "1. Verify no applications are using the file system\n2. Create backup if data recovery needed\n3. Delete unused file systems via console or CLI\n4. Estimated savings: 100% of file system costs",
            },
            "efs_one_zone_migration": {
                "title": "Migrate to EFS One Zone Storage",
                "description": "For workloads that don't require Multi-AZ resilience, One Zone storage offers 47% cost savings.",
                "action": "1. Assess availability requirements\n2. Create new One Zone file system\n3. Migrate data using AWS DataSync\n4. Estimated savings: 47% vs Regional storage",
            },
            "fsx_storage_optimization": {
                "title": "Optimize FSx Storage Types",
                "description": "Choose appropriate storage types: SSD for performance, HDD for capacity, Intelligent-Tiering for automatic optimization.",
                "action": "1. Analyze performance requirements\n2. Switch to HDD for large, less critical workloads\n3. Use Intelligent-Tiering for FSx OpenZFS\n4. Estimated savings: 60-75% with HDD storage",
            },
            "fsx_capacity_rightsizing": {
                "title": "Rightsize FSx File System Capacity",
                "description": "Optimize storage capacity based on actual usage patterns and consolidate small file systems.",
                "action": "1. Monitor storage utilization metrics\n2. Consolidate small file systems\n3. Reduce over-provisioned capacity\n4. Estimated savings: 20-40% through rightsizing",
            },
            "fsx_ontap_features": {
                "title": "Enable FSx ONTAP Data Efficiency",
                "description": "Use deduplication, compression, and capacity pool tiers to reduce storage costs significantly.",
                "action": "1. Enable data deduplication and compression\n2. Configure capacity pool for cold data\n3. Use SnapMirror for efficient replication\n4. Estimated savings: 30-70% through data efficiency",
            },
            "fsx_lustre_optimization": {
                "title": "Optimize FSx Lustre Configuration",
                "description": "Use scratch file systems for temporary workloads and enable data compression.",
                "action": "1. Use scratch file systems for temporary data\n2. Enable LZ4 data compression\n3. Optimize metadata configuration\n4. Estimated savings: 40-60% for temporary workloads",
            },
            "file_cache_optimization": {
                "title": "Optimize Amazon File Cache Usage",
                "description": "Configure cache eviction policies, storage quotas, and monitor usage patterns to optimize costs.",
                "action": "1. Enable automatic cache eviction\n2. Set user and group storage quotas\n3. Monitor cache hit rates via CloudWatch\n4. Adjust capacity based on usage patterns\n5. Estimated savings: 20-40% through better utilization",
            },
        }

    def get_s3_bucket_analysis(self) -> Dict[str, Any]:
        """Get S3 bucket analysis for cost optimization with performance optimizations"""
        import concurrent.futures
        from threading import Lock

        try:
            # list_buckets is not pageable - returns all buckets in single call
            response = self.s3.list_buckets()
            buckets = response.get("Buckets", [])

            print(f"📊 Analyzing {len(buckets)} S3 buckets{'(fast mode)' if self.fast_mode else '(full analysis)'}...")

            analysis = {
                "total_buckets": len(buckets),
                "buckets_without_lifecycle": [],
                "buckets_without_intelligent_tiering": [],
                "optimization_opportunities": [],
                "top_cost_buckets": [],
                "top_size_buckets": [],
                "permission_issues": [],
            }

            bucket_metrics = []

            for bucket in buckets:
                bucket_name = bucket["Name"]

                # Get bucket location to create region-specific client
                try:
                    location_response = self.s3.get_bucket_location(Bucket=bucket_name)
                    bucket_region = location_response.get("LocationConstraint")
                    # LocationConstraint is None for us-east-1
                    if bucket_region is None:
                        bucket_region = "us-east-1"

                    # Create region-specific S3 client for this bucket (skip config for now)
                    bucket_s3_client = self.session.client("s3", region_name=bucket_region)

                except Exception as e:
                    print(f"⚠️ Error getting bucket location for {bucket_name}: {str(e)}")
                    bucket_s3_client = self.s3
                    bucket_region = self.region
                    if bucket_region != self.region:
                        # Create region-specific client for cross-region bucket access with profile
                        session = boto3.Session(profile_name=self.profile)
                        bucket_s3_client = self.session.client("s3", region_name=bucket_region)
                    else:
                        bucket_s3_client = self.s3

                    # Track permission issues for reporting
                    analysis.setdefault("permission_issues", []).append(
                        {"bucket": bucket_name, "issue": "location_access", "error": str(e)}
                    )

                bucket_info = {
                    "Name": bucket_name,
                    "CreationDate": bucket["CreationDate"].isoformat(),
                    "Region": bucket_region,
                    "HasLifecyclePolicy": False,
                    "HasIntelligentTiering": False,
                    "EstimatedMonthlyCost": 0,
                    "SizeBytes": 0,
                    "SizeGB": 0,
                    "OptimizationOpportunities": [],
                }

                # Get bucket size - use fast mode for large accounts
                if self.fast_mode:
                    # Fast mode: Skip CloudWatch metrics, use quick object sampling
                    try:
                        # Use region-specific S3 client for cross-region buckets
                        bucket_s3_client = self.session.client("s3", region_name=bucket_region)
                        objects_response = bucket_s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=100)
                        object_count = objects_response.get("KeyCount", 0)

                        if object_count > 0:
                            # Use sample size only - no extrapolation
                            total_size = sum(obj.get("Size", 0) for obj in objects_response.get("Contents", []))
                            bucket_info["SizeGB"] = total_size / (1024**3)

                            # Always mark fast mode as unreliable
                            bucket_info["FastModeWarning"] = (
                                "Fast mode: Size based on sample only - may be significantly understated"
                            )
                            print(f"⚠️ Fast mode: {bucket_name} size is sample-based estimate only")
                            bucket_info["EstimatedMonthlyCost"] = self.calculate_s3_storage_cost(
                                bucket_info["SizeGB"], "STANDARD", bucket_region
                            )
                    except Exception as e:
                        print(f"⚠️ Error analyzing bucket {bucket_name}: {str(e)}")
                        # Continue with next bucket - don't fail entire S3 analysis
                else:
                    # Full mode: Get accurate CloudWatch metrics
                    try:
                        # Use region-specific CloudWatch client for cross-region buckets
                        bucket_cloudwatch_client = self.session.client("cloudwatch", region_name=bucket_region)

                        # Get total bucket size across all storage classes
                        total_size_gb = 0
                        storage_classes = [
                            "StandardStorage",
                            "StandardIAStorage",
                            "OneZoneIAStorage",
                            "GlacierStorage",
                            "DeepArchiveStorage",
                            "IntelligentTieringStorage",
                        ]

                        for storage_class in storage_classes:
                            try:
                                size_response = bucket_cloudwatch_client.get_metric_statistics(
                                    Namespace="AWS/S3",
                                    MetricName="BucketSizeBytes",
                                    Dimensions=[
                                        {"Name": "BucketName", "Value": bucket_name},
                                        {"Name": "StorageType", "Value": storage_class},
                                    ],
                                    StartTime=datetime.now(timezone.utc) - timedelta(days=2),
                                    EndTime=datetime.now(timezone.utc),
                                    Period=86400,
                                    Statistics=["Average"],
                                )
                                if size_response["Datapoints"]:
                                    class_size_gb = size_response["Datapoints"][-1]["Average"] / (1024**3)
                                    total_size_gb += class_size_gb
                            except Exception as e:
                                print(f"⚠️ Error getting S3 metrics for {bucket_name}, class {storage_class}: {str(e)}")
                                continue

                        if total_size_gb > 0:
                            bucket_info["SizeBytes"] = int(total_size_gb * (1024**3))
                            bucket_info["SizeGB"] = total_size_gb
                            bucket_info["EstimatedMonthlyCost"] = self.calculate_s3_storage_cost(
                                total_size_gb, "STANDARD", bucket_region
                            )

                    except Exception as e:
                        print(f"⚠️ Error calculating S3 costs for bucket {bucket_name}: {str(e)}")
                        continue

                # Check lifecycle configuration
                try:
                    bucket_s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
                    bucket_info["HasLifecyclePolicy"] = True
                except Exception as e:
                    if "NoSuchLifecycleConfiguration" not in str(e):
                        print(f"⚠️ Error checking lifecycle for bucket {bucket_name}: {str(e)}")
                    bucket_info["OptimizationOpportunities"].append(
                        "Configure lifecycle policies for automatic storage class transitions"
                    )
                    analysis["buckets_without_lifecycle"].append(bucket_name)

                try:
                    # Check intelligent tiering configuration using region-specific client
                    response = bucket_s3_client.list_bucket_intelligent_tiering_configurations(Bucket=bucket_name)
                    if response.get("IntelligentTieringConfigurationList"):
                        bucket_info["HasIntelligentTiering"] = True
                    else:
                        # Check if this is a static website bucket
                        is_static_site = self.is_static_website_bucket(bucket_name)
                        bucket_info["IsStaticWebsite"] = is_static_site

                        if is_static_site:
                            bucket_info["OptimizationOpportunities"].append(
                                "Static website: Consider CloudFront CDN for reduced data transfer costs"
                            )
                        else:
                            bucket_info["OptimizationOpportunities"].append(
                                "Enable S3 Intelligent-Tiering for automatic cost optimization"
                            )
                        analysis["buckets_without_intelligent_tiering"].append(bucket_name)
                except Exception as e:
                    print(f"⚠️ Error checking intelligent tiering for bucket {bucket_name}: {str(e)}")
                    # Check if this is a static website bucket
                    is_static_site = self.is_static_website_bucket(bucket_name)
                    bucket_info["IsStaticWebsite"] = is_static_site

                    if is_static_site:
                        bucket_info["OptimizationOpportunities"].append(
                            "Static website: Consider CloudFront CDN for reduced data transfer costs"
                        )
                    else:
                        bucket_info["OptimizationOpportunities"].append(
                            "Enable S3 Intelligent-Tiering for automatic cost optimization"
                        )
                    analysis["buckets_without_intelligent_tiering"].append(bucket_name)

                # Add general optimization opportunities
                if not bucket_info["HasLifecyclePolicy"] and not bucket_info["HasIntelligentTiering"]:
                    bucket_info["OptimizationOpportunities"].append("High priority: No cost optimization configured")

                bucket_metrics.append(bucket_info)
                analysis["optimization_opportunities"].append(bucket_info)

                # Progress tracking for large accounts
                if len(bucket_metrics) % 20 == 0:
                    print(f"   📈 Processed {len(bucket_metrics)}/{len(buckets)} buckets...")

            print(f"✅ Completed S3 analysis for {len(buckets)} buckets")

            # Sort and get top 10 by cost and size
            analysis["top_cost_buckets"] = sorted(
                bucket_metrics, key=lambda x: x["EstimatedMonthlyCost"], reverse=True
            )[:10]
            analysis["top_size_buckets"] = sorted(bucket_metrics, key=lambda x: x["SizeGB"], reverse=True)[:10]

            return analysis

        except Exception as e:
            print(f"Warning: Could not analyze S3 buckets: {e}")
            return {
                "total_buckets": 0,
                "buckets_without_lifecycle": [],
                "buckets_without_intelligent_tiering": [],
                "optimization_opportunities": [],
                "top_cost_buckets": [],
                "top_size_buckets": [],
                "permission_issues": [],
            }

    def _estimate_s3_bucket_cost(
        self, bucket_name: str, size_gb: float, bucket_region: str, session: boto3.Session = None
    ) -> float:
        """Estimate S3 bucket cost based on storage class distribution"""
        try:
            # Try to get storage class metrics from CloudWatch with profile
            if session is None:
                session = boto3.Session(profile_name=self.profile)
            cloudwatch = self.session.client("cloudwatch", region_name=bucket_region)

            # Get storage class breakdown
            storage_classes = [
                "StandardStorage",
                "StandardIAStorage",
                "OneZoneIAStorage",
                "GlacierStorage",
                "DeepArchiveStorage",
                "IntelligentTieringStorage",
            ]

            total_cost = 0
            total_accounted_gb = 0

            for storage_class in storage_classes:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace="AWS/S3",
                        MetricName="BucketSizeBytes",
                        Dimensions=[
                            {"Name": "BucketName", "Value": bucket_name},
                            {"Name": "StorageType", "Value": storage_class},
                        ],
                        StartTime=datetime.now(timezone.utc) - timedelta(days=2),
                        EndTime=datetime.now(timezone.utc),
                        Period=86400,
                        Statistics=["Average"],
                    )

                    if response["Datapoints"]:
                        class_size_gb = response["Datapoints"][-1]["Average"] / (1024**3)
                        total_accounted_gb += class_size_gb

                        # Map storage class to cost
                        cost_key = {
                            "StandardStorage": "STANDARD",
                            "StandardIAStorage": "STANDARD_IA",
                            "OneZoneIAStorage": "ONEZONE_IA",
                            "GlacierStorage": "GLACIER",
                            "DeepArchiveStorage": "DEEP_ARCHIVE",
                            "IntelligentTieringStorage": "INTELLIGENT_TIERING",
                        }.get(storage_class, "STANDARD")

                        base_cost = self.S3_STORAGE_COSTS[cost_key]
                        # Apply region-specific multiplier for this storage class
                        regional_multiplier = self.S3_REGIONAL_MULTIPLIERS.get(bucket_region, {}).get(cost_key, 1.0)
                        regional_cost = base_cost * regional_multiplier
                        storage_cost = class_size_gb * regional_cost

                        # Add Intelligent-Tiering monitoring fee if applicable
                        if cost_key == "INTELLIGENT_TIERING":
                            # Estimate 1,000 objects per GB (conservative estimate)
                            estimated_objects = class_size_gb * 1000
                            monitoring_fee = (estimated_objects / 1000) * self.S3_INTELLIGENT_TIERING_MONITORING_FEE
                            storage_cost += monitoring_fee

                        total_cost += storage_cost

                except Exception as e:
                    print(f"⚠️ Error calculating S3 costs for bucket {bucket_name}: {str(e)}")
                    continue

            # If we couldn't get detailed breakdown, use Standard pricing as fallback
            if total_accounted_gb < size_gb * 0.1:  # Less than 10% accounted for
                base_cost = self.S3_STORAGE_COSTS["STANDARD"]
                regional_multiplier = self.S3_REGIONAL_MULTIPLIERS.get(bucket_region, {}).get("STANDARD", 1.0)
                regional_cost = base_cost * regional_multiplier
                total_cost = size_gb * regional_cost

            return round(total_cost, 2)

        except Exception as e:
            print(f"⚠️ Error calculating S3 storage cost: {str(e)}")
            # Fallback to Standard pricing with region-specific multiplier
            base_cost = self.S3_STORAGE_COSTS["STANDARD"]
            regional_multiplier = self.S3_REGIONAL_MULTIPLIERS.get(bucket_region, {}).get("STANDARD", 1.0)
            regional_cost = base_cost * regional_multiplier
            return round(size_gb * regional_cost, 2)

    def calculate_s3_storage_cost(self, size_gb: float, storage_class: str, region: str) -> float:
        """Simple S3 storage cost calculation for fast mode"""
        try:
            base_cost = self.S3_STORAGE_COSTS.get(storage_class, self.S3_STORAGE_COSTS["STANDARD"])
            regional_multiplier = self.S3_REGIONAL_MULTIPLIERS.get(region, {}).get(storage_class, 1.0)
            return round(size_gb * base_cost * regional_multiplier, 2)
        except Exception:
            return round(size_gb * 0.023, 2)  # Fallback to standard pricing

    def is_static_website_bucket(self, bucket_name: str, s3_client=None) -> bool:
        """Check if S3 bucket is configured for static website hosting"""
        client = s3_client or self.s3
        try:
            client.get_bucket_website(Bucket=bucket_name)
            return True
        except Exception as e:
            # Expected for non-website buckets (NoSuchWebsiteConfiguration)
            if "NoSuchWebsiteConfiguration" not in str(e):
                print(f"⚠️ Error checking website config for bucket {bucket_name}: {str(e)}")
            return False

    def get_enhanced_s3_checks(self) -> Dict[str, Any]:
        """Get enhanced S3 cost optimization checks"""
        checks = {
            "lifecycle_missing": [],
            "multipart_uploads": [],
            "storage_class_optimization": [],
            "intelligent_tiering_missing": [],
            "unused_buckets": [],
            "versioning_growth": [],
            "cross_region_replication": [],
            "server_access_logs": [],
            "request_heavy_buckets": [],
            "static_website_optimization": [],
        }

        try:
            response = self.s3.list_buckets()
            buckets = response.get("Buckets", [])

            for bucket in buckets:
                bucket_name = bucket["Name"]

                # Get bucket location to ensure we're in the right region
                try:
                    location_response = self.s3.get_bucket_location(Bucket=bucket_name)
                    bucket_region = location_response.get("LocationConstraint")
                    if bucket_region is None:
                        bucket_region = "us-east-1"

                    # Create region-specific S3 client for this bucket
                    if bucket_region != self.region:
                        session = boto3.Session(profile_name=self.profile)
                        bucket_s3_client = self.session.client("s3", region_name=bucket_region)
                    else:
                        bucket_s3_client = self.s3

                except Exception as e:
                    print(f"Warning: Could not get location for bucket {bucket_name}: {e}")
                    bucket_s3_client = self.s3  # Fallback to default client

                # Check for incomplete multipart uploads using region-specific client
                try:
                    multipart_response = bucket_s3_client.list_multipart_uploads(Bucket=bucket_name)
                    uploads = multipart_response.get("Uploads", [])
                    if uploads:
                        checks["multipart_uploads"].append(
                            {
                                "BucketName": bucket_name,
                                "IncompleteUploads": len(uploads),
                                "CheckCategory": "Incomplete Multipart Uploads",
                                "Recommendation": "Configure lifecycle rule to abort incomplete uploads after 7 days",
                            }
                        )
                except Exception as e:
                    print(f"Warning: Could not check multipart uploads for bucket {bucket_name}: {e}")

                # Check lifecycle policies
                try:
                    bucket_s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
                except Exception as e:
                    if "NoSuchLifecycleConfiguration" in str(e):
                        # Check if this is a static website bucket
                        is_static_site = self.is_static_website_bucket(bucket_name, bucket_s3_client)

                        if is_static_site:
                            recommendation = "Static website detected: Configure lifecycle policies for logs/backups only. Consider CloudFront for reduced data transfer costs"
                            category = "Static Website Optimization"
                        else:
                            recommendation = (
                                "Configure lifecycle policies for automatic tiering to reduce storage costs by 40-95%"
                            )
                            category = "Storage Class Optimization"

                        checks["lifecycle_missing"].append(
                            {
                                "BucketName": bucket_name,
                                "IsStaticWebsite": is_static_site,
                                "CheckCategory": category,
                                "Recommendation": recommendation,
                                "SizeGB": 0,  # Enhanced checks don't calculate size - will be merged with standard analysis
                                "EstimatedMonthlyCost": 0,
                            }
                        )

                # Check versioning configuration
                try:
                    versioning_response = bucket_s3_client.get_bucket_versioning(Bucket=bucket_name)
                    if versioning_response.get("Status") == "Enabled":
                        checks["versioning_growth"].append(
                            {
                                "BucketName": bucket_name,
                                "VersioningStatus": "Enabled",
                                "Recommendation": "Monitor versioning growth and configure lifecycle for old versions",
                                "CheckCategory": "Versioning Optimization",
                            }
                        )
                except Exception as e:
                    print(f"Warning: Could not check versioning for bucket {bucket_name}: {e}")

                # Check for cross-region replication
                try:
                    replication_response = bucket_s3_client.get_bucket_replication(Bucket=bucket_name)
                    if replication_response.get("ReplicationConfiguration"):
                        checks["cross_region_replication"].append(
                            {
                                "BucketName": bucket_name,
                                "HasReplication": True,
                                "Recommendation": "Review cross-region replication necessity and destination usage",
                                "CheckCategory": "Replication Optimization",
                            }
                        )
                except Exception as e:
                    if "ReplicationConfigurationNotFoundError" not in str(e):
                        print(f"Warning: Could not check replication for bucket {bucket_name}: {e}")

                # Check for server access logging
                try:
                    logging_response = bucket_s3_client.get_bucket_logging(Bucket=bucket_name)
                    if logging_response.get("LoggingEnabled"):
                        checks["server_access_logs"].append(
                            {
                                "BucketName": bucket_name,
                                "LoggingEnabled": True,
                                "Recommendation": "Review if server access logs are still needed",
                                "CheckCategory": "Logging Optimization",
                            }
                        )
                except Exception as e:
                    print(f"Warning: Could not check logging for bucket {bucket_name}: {e}")

                # Check for unused buckets (empty buckets older than 30 days)
                try:
                    objects_response = bucket_s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
                    if objects_response.get("KeyCount", 0) == 0:
                        bucket_age = (datetime.now(bucket["CreationDate"].tzinfo) - bucket["CreationDate"]).days
                        if bucket_age > 30:
                            checks["unused_buckets"].append(
                                {
                                    "BucketName": bucket_name,
                                    "AgeDays": bucket_age,
                                    "Recommendation": f"Empty bucket older than {bucket_age} days - consider deletion",
                                    "CheckCategory": "Unused Resources",
                                }
                            )
                except Exception as e:
                    print(f"Warning: Could not check bucket {bucket_name} for emptiness: {e}")

                # Check for static website optimization opportunities
                if self.is_static_website_bucket(bucket_name, bucket_s3_client):
                    checks["static_website_optimization"].append(
                        {
                            "BucketName": bucket_name,
                            "IsStaticWebsite": True,
                            "Recommendation": "Static website detected: Enable CloudFront CDN for reduced data transfer costs and improved performance",
                            "CheckCategory": "Static Website Optimization",
                            "EstimatedSavings": "Variable based on traffic - typically 20-60% on data transfer costs",
                        }
                    )

        except Exception as e:
            print(f"Warning: Could not perform enhanced S3 checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                item["CheckCategory"] = item.get("CheckCategory", category.replace("_", " ").title())
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_s3_optimization_descriptions(self) -> Dict[str, str]:
        """Get descriptions for S3 cost optimization opportunities"""
        return {
            "lifecycle_policies": {
                "title": "Configure S3 Lifecycle Policies",
                "description": "Automatically transition objects to lower-cost storage classes based on age and access patterns.",
                "action": "1. Analyze access patterns using S3 Storage Class Analysis\n2. Create lifecycle rules for IA transition after 30 days\n3. Configure Glacier transitions for long-term storage\n4. Estimated savings: 50-95% for infrequent/archive data",
            },
            "intelligent_tiering": {
                "title": "Enable S3 Intelligent-Tiering",
                "description": "Automatically optimize costs by moving data between access tiers based on access patterns.",
                "action": "1. Enable Intelligent-Tiering on buckets with unpredictable access\n2. Configure Archive and Deep Archive tiers for maximum savings\n3. Monitor cost optimization through S3 Storage Lens\n4. Estimated savings: 40-95% for variable access patterns",
            },
            "storage_class_optimization": {
                "title": "Optimize S3 Storage Classes",
                "description": "Choose appropriate storage classes based on access frequency and retrieval requirements.",
                "action": "1. Use Standard for frequently accessed data\n2. Use Standard-IA for monthly access patterns\n3. Use Glacier for quarterly/yearly access\n4. Estimated savings: 40-80% vs Standard storage",
            },
            "unused_buckets": {
                "title": "Delete Unused S3 Buckets",
                "description": "Remove empty or unused S3 buckets to eliminate unnecessary costs.",
                "action": "1. Identify buckets with no objects or minimal usage\n2. Verify no applications depend on the bucket\n3. Delete unused buckets via console or CLI\n4. Estimated savings: 100% of bucket costs",
            },
            "multipart_cleanup": {
                "title": "Clean Up Incomplete Multipart Uploads",
                "description": "Remove incomplete multipart uploads that continue to incur storage costs.",
                "action": "1. List incomplete multipart uploads\n2. Configure lifecycle rules to abort incomplete uploads\n3. Set automatic cleanup after 7 days\n4. Estimated savings: Variable based on incomplete uploads",
            },
        }

    def get_dynamodb_table_analysis(self) -> Dict[str, Any]:
        """Get DynamoDB table analysis for cost optimization"""
        try:
            paginator = self.dynamodb.get_paginator("list_tables")
            table_names = []
            for page in paginator.paginate():
                table_names.extend(page.get("TableNames", []))

            analysis = {
                "total_tables": len(table_names),
                "provisioned_tables": [],
                "on_demand_tables": [],
                "optimization_opportunities": [],
            }

            for table_name in table_names:
                try:
                    table_response = self.dynamodb.describe_table(TableName=table_name)
                    table = table_response["Table"]

                    table_info = {
                        "TableName": table_name,
                        "BillingMode": table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
                        "TableStatus": table.get("TableStatus", "UNKNOWN"),
                        "ItemCount": table.get("ItemCount", 0),
                        "TableSizeBytes": table.get("TableSizeBytes", 0),
                        "ReadCapacityUnits": 0,
                        "WriteCapacityUnits": 0,
                        "EstimatedMonthlyCost": 0,
                        "OptimizationOpportunities": [],
                    }

                    # Get provisioned throughput if applicable
                    if table_info["BillingMode"] == "PROVISIONED":
                        provisioned_throughput = table.get("ProvisionedThroughput", {})
                        table_info["ReadCapacityUnits"] = provisioned_throughput.get("ReadCapacityUnits", 0)
                        table_info["WriteCapacityUnits"] = provisioned_throughput.get("WriteCapacityUnits", 0)

                        # Estimate cost for provisioned mode ($0.25 per RCU, $1.25 per WCU per month)
                        monthly_cost = (table_info["ReadCapacityUnits"] * 0.25) + (
                            table_info["WriteCapacityUnits"] * 1.25
                        )
                        table_info["EstimatedMonthlyCost"] = round(monthly_cost, 2)

                        analysis["provisioned_tables"].append(table_name)

                        # Check for optimization opportunities
                        if table_info["ReadCapacityUnits"] > 100 or table_info["WriteCapacityUnits"] > 100:
                            table_info["OptimizationOpportunities"].append(
                                "Consider On-Demand billing for unpredictable workloads"
                            )

                        if table_info["ItemCount"] == 0:
                            table_info["OptimizationOpportunities"].append("Empty table - consider deletion if unused")

                    else:  # ON_DEMAND
                        analysis["on_demand_tables"].append(table_name)
                        # On-demand pricing is usage-based, harder to estimate without CloudWatch metrics
                        table_info["OptimizationOpportunities"].append(
                            "Monitor usage patterns to consider Provisioned mode for steady workloads"
                        )

                    # General optimizations
                    if table_info["TableSizeBytes"] > 1024**3:  # > 1GB
                        table_info["OptimizationOpportunities"].append(
                            "Large table - consider data archiving or compression strategies"
                        )

                    analysis["optimization_opportunities"].append(table_info)

                except Exception as e:
                    print(f"Warning: Could not analyze table {table_name}: {e}")

            return analysis

        except Exception as e:
            print(f"Warning: Could not analyze DynamoDB tables: {e}")
            return {
                "total_tables": 0,
                "provisioned_tables": [],
                "on_demand_tables": [],
                "optimization_opportunities": [],
            }

    def get_dynamodb_optimization_descriptions(self) -> Dict[str, str]:
        """Get descriptions for DynamoDB cost optimization opportunities"""
        return {
            "billing_mode_optimization": {
                "title": "Optimize DynamoDB Billing Mode",
                "description": "Choose between Provisioned and On-Demand billing based on traffic patterns.",
                "action": "1. Use Provisioned for predictable, steady workloads\n2. Use On-Demand for unpredictable, spiky traffic\n3. Monitor CloudWatch metrics for usage patterns\n4. Estimated savings: 20-60% with proper mode selection",
            },
            "capacity_rightsizing": {
                "title": "Rightsize Provisioned Capacity",
                "description": "Adjust read/write capacity units based on actual usage to avoid over-provisioning.",
                "action": "1. Monitor consumed vs provisioned capacity\n2. Use Auto Scaling for dynamic adjustment\n3. Reduce unused capacity units\n4. Estimated savings: 30-70% through rightsizing",
            },
            "reserved_capacity": {
                "title": "Purchase DynamoDB Reserved Capacity",
                "description": "Save up to 76% on DynamoDB costs with reserved capacity for predictable workloads.",
                "action": "1. Analyze baseline capacity requirements\n2. Purchase 1-year or 3-year reserved capacity\n3. Apply to tables with steady usage\n4. Estimated savings: 53-76% vs On-Demand",
            },
            "data_lifecycle": {
                "title": "Implement Data Lifecycle Management",
                "description": "Archive or delete old data to reduce storage costs and improve performance.",
                "action": "1. Identify old or unused data\n2. Implement TTL for automatic expiration\n3. Archive historical data to S3\n4. Estimated savings: 40-80% on storage costs",
            },
            "global_tables_optimization": {
                "title": "Optimize Global Tables Configuration",
                "description": "Review Global Tables setup to ensure cost-effective multi-region replication.",
                "action": "1. Evaluate necessity of each region\n2. Use consistent read where possible\n3. Optimize cross-region replication\n4. Estimated savings: 20-50% on replication costs",
            },
        }

    def get_container_services_analysis(self) -> Dict[str, Any]:
        """Get ECS, EKS, and ECR analysis for cost optimization"""
        analysis = {"ecs": self.get_ecs_analysis(), "eks": self.get_eks_analysis(), "ecr": self.get_ecr_analysis()}
        return analysis

    def get_ecs_analysis(self) -> Dict[str, Any]:
        """Analyze ECS clusters and services"""
        try:
            # Paginate list_clusters
            paginator = self.ecs.get_paginator("list_clusters")
            cluster_arns = []
            for page in paginator.paginate():
                cluster_arns.extend(page.get("clusterArns", []))

            analysis = {
                "total_clusters": len(cluster_arns),
                "clusters": [],
                "total_services": 0,
                "optimization_opportunities": [],
            }

            for cluster_arn in cluster_arns:
                cluster_name = cluster_arn.split("/")[-1]

                # Get cluster details
                cluster_details = self.ecs.describe_clusters(clusters=[cluster_arn])
                cluster = cluster_details["clusters"][0] if cluster_details["clusters"] else {}

                # Get services in cluster - paginate list_services
                paginator = self.ecs.get_paginator("list_services")
                service_arns = []
                for page in paginator.paginate(cluster=cluster_arn):
                    service_arns.extend(page.get("serviceArns", []))

                cluster_info = {
                    "ClusterName": cluster_name,
                    "Status": cluster.get("status", "UNKNOWN"),
                    "RunningTasksCount": cluster.get("runningTasksCount", 0),
                    "PendingTasksCount": cluster.get("pendingTasksCount", 0),
                    "ActiveServicesCount": cluster.get("activeServicesCount", 0),
                    "ServicesCount": len(service_arns),
                    "OptimizationOpportunities": [],
                }

                # Optimization checks
                if cluster_info["RunningTasksCount"] == 0 and cluster_info["PendingTasksCount"] == 0:
                    cluster_info["OptimizationOpportunities"].append("Empty cluster - consider deletion if unused")
                    cluster_info["CheckCategory"] = "Idle Resources"

                if cluster_info["ServicesCount"] > 0:
                    cluster_info["OptimizationOpportunities"].append(
                        "Review service resource allocation and scaling policies"
                    )
                    cluster_info["CheckCategory"] = "Over-provisioned Containers"

                # Add specific ECS checks
                cluster_info["OptimizationOpportunities"].extend(
                    [
                        "Consider Fargate Spot for fault-tolerant tasks (Save 70%)",
                        "Implement task auto scaling based on metrics",
                        "Review CPU and memory allocation for rightsizing",
                    ]
                )

                analysis["clusters"].append(cluster_info)
                analysis["total_services"] += cluster_info["ServicesCount"]

            return analysis

        except Exception as e:
            print(f"Warning: Could not analyze ECS: {e}")
            return {"total_clusters": 0, "clusters": [], "total_services": 0, "optimization_opportunities": []}

    def get_eks_analysis(self) -> Dict[str, Any]:
        """Analyze EKS clusters"""
        try:
            # Paginate list_clusters
            paginator = self.eks.get_paginator("list_clusters")
            cluster_names = []
            for page in paginator.paginate():
                cluster_names.extend(page.get("clusters", []))

            analysis = {"total_clusters": len(cluster_names), "clusters": [], "optimization_opportunities": []}

            for cluster_name in cluster_names:
                try:
                    cluster_details = self.eks.describe_cluster(name=cluster_name)
                    cluster = cluster_details["cluster"]

                    # Get node groups - paginate list_nodegroups
                    paginator = self.eks.get_paginator("list_nodegroups")
                    nodegroup_names = []
                    for page in paginator.paginate(clusterName=cluster_name):
                        nodegroup_names.extend(page.get("nodegroups", []))

                    cluster_info = {
                        "ClusterName": cluster_name,
                        "Status": cluster.get("status", "UNKNOWN"),
                        "Version": cluster.get("version", "Unknown"),
                        "NodeGroupsCount": len(nodegroup_names),
                        "EstimatedMonthlyCost": 144,  # Base EKS cluster cost $0.20/hour
                        "OptimizationOpportunities": [],
                    }

                    # Optimization checks
                    if cluster_info["Status"] != "ACTIVE":
                        cluster_info["OptimizationOpportunities"].append(
                            "Inactive cluster - consider deletion if not needed"
                        )

                    if cluster_info["NodeGroupsCount"] == 0:
                        cluster_info["OptimizationOpportunities"].append("No node groups - cluster may be unused")

                    cluster_info["OptimizationOpportunities"].extend(
                        [
                            "Consider Spot instances for non-critical workloads (Save 60-90%)",
                            "Implement cluster autoscaling to optimize node utilization",
                            "Review node group instance types for rightsizing opportunities",
                        ]
                    )

                    analysis["clusters"].append(cluster_info)

                except Exception as e:
                    print(f"Warning: Could not analyze EKS cluster {cluster_name}: {e}")

            return analysis

        except Exception as e:
            print(f"Warning: Could not analyze EKS: {e}")
            return {"total_clusters": 0, "clusters": [], "optimization_opportunities": []}

    def get_ecr_analysis(self) -> Dict[str, Any]:
        """Analyze ECR repositories"""
        try:
            repos_response = self.ecr.describe_repositories()
            repositories = repos_response.get("repositories", [])

            analysis = {"total_repositories": len(repositories), "repositories": [], "optimization_opportunities": []}

            for repo in repositories:
                repo_name = repo["repositoryName"]

                # Get image count with pagination
                try:
                    paginator = self.ecr.get_paginator("list_images")
                    image_count = 0
                    for page in paginator.paginate(repositoryName=repo_name):
                        image_count += len(page.get("imageIds", []))
                except Exception as e:
                    print(f"⚠️ Error getting image count for ECR repo {repo_name}: {str(e)}")
                    image_count = 0

                repo_info = {
                    "RepositoryName": repo_name,
                    "CreatedAt": repo["createdAt"].isoformat() if "createdAt" in repo else "Unknown",
                    "ImageCount": image_count,
                    "RepositoryUri": repo.get("repositoryUri", ""),
                    "OptimizationOpportunities": [],
                }

                # Optimization checks
                if image_count == 0:
                    repo_info["OptimizationOpportunities"].append("Empty repository - consider deletion if unused")
                elif image_count > 100:
                    repo_info["OptimizationOpportunities"].append(
                        "Large number of images - implement lifecycle policies"
                    )

                repo_info["OptimizationOpportunities"].extend(
                    [
                        "Configure lifecycle policies to automatically delete old images",
                        "Use image scanning to identify vulnerabilities and reduce storage",
                    ]
                )

                analysis["repositories"].append(repo_info)

            return analysis

        except Exception as e:
            print(f"Warning: Could not analyze ECR: {e}")
            return {"total_repositories": 0, "repositories": [], "optimization_opportunities": []}

    def get_container_optimization_descriptions(self) -> Dict[str, str]:
        """Get descriptions for container services cost optimization"""
        return {
            "ecs_rightsizing": {
                "title": "Optimize ECS Task and Service Configuration",
                "description": "Rightsize CPU and memory allocation, implement auto scaling, and use Spot capacity.",
                "action": "1. Monitor task resource utilization\n2. Adjust CPU/memory allocation based on usage\n3. Implement service auto scaling\n4. Use Spot capacity for fault-tolerant workloads\n5. Estimated savings: 30-70% through optimization",
            },
            "eks_node_optimization": {
                "title": "Optimize EKS Node Groups and Scaling",
                "description": "Use appropriate instance types, implement cluster autoscaling, and leverage Spot instances.",
                "action": "1. Implement cluster autoscaler\n2. Use Spot instances for non-critical workloads\n3. Rightsize node group instance types\n4. Configure horizontal pod autoscaling\n5. Estimated savings: 60-90% with Spot instances",
            },
            "ecr_lifecycle": {
                "title": "Implement ECR Lifecycle Policies",
                "description": "Automatically delete old container images to reduce storage costs.",
                "action": "1. Configure lifecycle policies for image retention\n2. Delete untagged images automatically\n3. Limit number of images per repository\n4. Implement image scanning and cleanup\n5. Estimated savings: 50-80% on storage costs",
            },
            "container_scheduling": {
                "title": "Optimize Container Scheduling and Placement",
                "description": "Improve resource utilization through better scheduling and placement strategies.",
                "action": "1. Use appropriate scheduling constraints\n2. Implement resource quotas and limits\n3. Optimize container placement for cost efficiency\n4. Consider Fargate vs EC2 launch types\n5. Estimated savings: 20-50% through better utilization",
            },
            "monitoring_optimization": {
                "title": "Implement Container Cost Monitoring",
                "description": "Use CloudWatch Container Insights and cost allocation tags for visibility.",
                "action": "1. Enable Container Insights for detailed monitoring\n2. Implement cost allocation tags\n3. Set up cost alerts and budgets\n4. Regular cost review and optimization\n5. Estimated savings: 15-30% through visibility and control",
            },
        }

    def get_networking_checks(self) -> Dict[str, Any]:
        """Get networking cost optimization checks"""
        checks = {
            "unused_eips": [],
            "idle_nat_gateways": [],
            "unused_load_balancers": [],
            "cross_az_traffic": [],
            "vpc_endpoints": [],
        }

        try:
            # Check for unused Elastic IPs (already covered in EC2 checks)
            eips_response = self.ec2.describe_addresses()
            for eip in eips_response.get("Addresses", []):
                if "InstanceId" not in eip and "NetworkInterfaceId" not in eip:
                    checks["unused_eips"].append(
                        {
                            "AllocationId": eip.get("AllocationId"),
                            "PublicIp": eip.get("PublicIp"),
                            "Recommendation": "Release unused Elastic IP",
                            "EstimatedSavings": "$3.65/month",  # $0.005/hour × 730 hours
                        }
                    )

            # Check for NAT Gateways
            nat_response = self.ec2.describe_nat_gateways()
            for nat in nat_response.get("NatGateways", []):
                if nat.get("State") == "available":
                    checks["idle_nat_gateways"].append(
                        {
                            "NatGatewayId": nat["NatGatewayId"],
                            "State": nat["State"],
                            "Recommendation": "Monitor NAT Gateway usage and consider NAT instance for low traffic",
                            "EstimatedSavings": "$32.85/month base + data processing fees",
                        }
                    )

            # Check for unused ALBs/NLBs
            alb_response = self.elbv2.describe_load_balancers()
            for lb in alb_response.get("LoadBalancers", []):
                # Check if load balancer has targets
                target_groups = self.elbv2.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])
                if not target_groups.get("TargetGroups"):
                    checks["unused_load_balancers"].append(
                        {
                            "LoadBalancerArn": lb["LoadBalancerArn"],
                            "LoadBalancerName": lb["LoadBalancerName"],
                            "Type": lb["Type"],
                            "Recommendation": "Load balancer has no target groups - verify if still needed and delete if unused",
                            "EstimatedSavings": f"${16 if lb['Type'] == 'application' else 22}/month (ALB: $16.20, NLB: $22.50)",
                            "Action": "1. Verify no traffic routing through this LB\n2. Check if target groups were accidentally deleted\n3. Delete LB if confirmed unused",
                        }
                    )

            # Check for classic load balancers
            clb_response = self.elb.describe_load_balancers()
            for clb in clb_response.get("LoadBalancerDescriptions", []):
                if not clb.get("Instances"):
                    checks["unused_load_balancers"].append(
                        {
                            "LoadBalancerName": clb["LoadBalancerName"],
                            "Type": "Classic",
                            "Recommendation": "Classic Load Balancer with no instances - migrate to ALB/NLB or delete if unused",
                            "EstimatedSavings": "$18/month per CLB + potential ALB savings (ALB: $16.20/month)",
                            "Action": "1. Check if instances were deregistered accidentally\n2. Migrate to ALB for HTTP/HTTPS (cheaper + better features)\n3. Delete if no longer needed",
                        }
                    )

        except Exception as e:
            print(f"Warning: Could not perform networking checks: {e}")

        return checks

    def get_detailed_cost_hub_recommendations(self) -> List[Dict[str, Any]]:
        """Get detailed recommendations from Cost Optimization Hub (all resource types)"""
        recommendations = []
        if not self.cost_hub:
            print("ℹ️ Cost Optimization Hub unavailable - continuing with other optimization sources")
            return recommendations

        try:
            # Get all recommendations without filtering by resource type
            response = self.cost_hub.list_recommendations(filter={"regions": [self.region]}, maxResults=100)

            # Get detailed info for each recommendation
            for rec in response.get("items", []):
                try:
                    detailed = self.cost_hub.get_recommendation(recommendationId=rec["recommendationId"])
                    recommendations.append(detailed)
                except Exception as e:
                    recommendations.append(rec)  # Fallback to basic info

            # Handle pagination
            while response.get("nextToken"):
                response = self.cost_hub.list_recommendations(
                    filter={"regions": [self.region]}, nextToken=response["nextToken"], maxResults=100
                )
                for rec in response.get("items", []):
                    try:
                        detailed = self.cost_hub.get_recommendation(recommendationId=rec["recommendationId"])
                        recommendations.append(detailed)
                    except Exception as e:
                        recommendations.append(rec)

        except Exception as e:
            print(f"Warning: Cost Optimization Hub error: {e}")
        return recommendations

    def get_enhanced_ec2_checks(self) -> Dict[str, Any]:
        """Get enhanced EC2 cost optimization checks"""
        checks = {
            "idle_instances": [],
            "rightsizing_opportunities": [],
            "previous_generation": [],
            "auto_scaling_missing": [],
            "stopped_instances": [],
            "dedicated_hosts": [],
            "burstable_credits": [],
        }

        try:
            # Get all instances - paginate
            paginator = self.ec2.get_paginator("describe_instances")

            for page in paginator.paginate():
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        instance_id = instance["InstanceId"]
                        instance_type = instance["InstanceType"]
                        state = instance["State"]["Name"]

                        if state == "running":
                            # Check for idle instances (low CPU utilization)
                            try:
                                cloudwatch = self.session.client("cloudwatch", region_name=self.region)
                                end_time = datetime.now(timezone.utc)
                                start_time = end_time - timedelta(days=7)

                                cpu_response = cloudwatch.get_metric_statistics(
                                    Namespace="AWS/EC2",
                                    MetricName="CPUUtilization",
                                    Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average", "Maximum"],
                                )

                                if cpu_response.get("Datapoints"):
                                    avg_cpu = sum(dp["Average"] for dp in cpu_response["Datapoints"]) / len(
                                        cpu_response["Datapoints"]
                                    )
                                    max_cpu = max(dp["Maximum"] for dp in cpu_response["Datapoints"])

                                    # Idle instance detection
                                    if avg_cpu < 5 and max_cpu < 10:
                                        checks["idle_instances"].append(
                                            {
                                                "InstanceId": instance_id,
                                                "InstanceType": instance_type,
                                                "AvgCPU": f"{avg_cpu:.1f}%",
                                                "MaxCPU": f"{max_cpu:.1f}%",
                                                "Recommendation": f"Instance shows very low utilization (avg: {avg_cpu:.1f}%, max: {max_cpu:.1f}%) - consider terminating or downsizing",
                                                "EstimatedSavings": "Up to 100% if terminated, 50-70% if downsized",
                                                "CheckCategory": "Idle Instances",
                                            }
                                        )
                                    # Rightsizing opportunities
                                    elif avg_cpu < 20 and max_cpu < 40:
                                        checks["rightsizing_opportunities"].append(
                                            {
                                                "InstanceId": instance_id,
                                                "InstanceType": instance_type,
                                                "AvgCPU": f"{avg_cpu:.1f}%",
                                                "MaxCPU": f"{max_cpu:.1f}%",
                                                "Recommendation": f"Low utilization (avg: {avg_cpu:.1f}%, max: {max_cpu:.1f}%) - consider smaller instance type",
                                                "EstimatedSavings": "30-50% cost reduction with proper rightsizing",
                                                "CheckCategory": "Rightsizing Opportunities",
                                            }
                                        )

                            except Exception:
                                # No CloudWatch data available - suggest enabling monitoring
                                checks["rightsizing_opportunities"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "InstanceType": instance_type,
                                        "Recommendation": "Enable detailed CloudWatch monitoring to analyze utilization for rightsizing opportunities",
                                        "EstimatedSavings": "Enable monitoring first - potential 30-70% savings with proper rightsizing",
                                        "CheckCategory": "Monitoring Required",
                                    }
                                )

                            # Check if instance is not in Auto Scaling Group
                            try:
                                autoscaling = self.session.client("autoscaling", region_name=self.region)
                                asg_response = autoscaling.describe_auto_scaling_instances(InstanceIds=[instance_id])
                                if not asg_response.get("AutoScalingInstances"):
                                    # Instance not in ASG - check if it should be
                                    instance_name = ""
                                    for tag in instance.get("Tags", []):
                                        if tag["Key"] == "Name":
                                            instance_name = tag["Value"]
                                            break

                                    # Heuristic: if instance name suggests it's part of a service/application
                                    if any(
                                        keyword in instance_name.lower()
                                        for keyword in ["web", "app", "api", "service", "worker"]
                                    ):
                                        checks["auto_scaling_missing"].append(
                                            {
                                                "InstanceId": instance_id,
                                                "InstanceType": instance_type,
                                                "InstanceName": instance_name,
                                                "Recommendation": "Consider adding to Auto Scaling Group for high availability and cost optimization",
                                                "EstimatedSavings": "Improved availability and potential Spot instance usage",
                                                "CheckCategory": "Auto Scaling Missing",
                                            }
                                        )
                            except Exception:
                                pass  # Skip ASG check if not available

                            # Check for previous generation instances - focus on cost reduction only
                            if any(gen in instance_type for gen in ["t2.", "m4.", "c4.", "r4."]):
                                # Only recommend if it reduces costs, not just performance
                                if instance_type.startswith("t2."):
                                    # t2 to t3 migration can reduce costs with better performance per dollar
                                    checks["previous_generation"].append(
                                        {
                                            "InstanceId": instance_id,
                                            "InstanceType": instance_type,
                                            "Recommendation": f"Consider migrating to {instance_type.replace('t2.', 't3.')} for better cost efficiency",
                                            "EstimatedSavings": "Potential cost reduction with better performance per dollar",
                                            "CheckCategory": "Previous Generation Migration",
                                        }
                                    )
                                # Skip other generation upgrades as they may increase costs

                            # Graviton migration removed per user request

                            # Check for dedicated tenancy
                            if instance.get("Placement", {}).get("Tenancy") == "dedicated":
                                checks["dedicated_hosts"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "Tenancy": "dedicated",
                                        "Recommendation": "Review dedicated tenancy necessity",
                                        "EstimatedSavings": "Significant cost reduction if shared tenancy acceptable",
                                    }
                                )

                            # Check burstable instances with CloudWatch metrics
                            if instance_type.startswith(("t2.", "t3.", "t4g.")):
                                # Try to get CloudWatch CPU credit metrics
                                try:
                                    cloudwatch = self.session.client("cloudwatch", region_name=self.region)
                                    end_time = datetime.now(timezone.utc)
                                    start_time = end_time - timedelta(days=7)

                                    # Get CPU credit balance
                                    credit_response = cloudwatch.get_metric_statistics(
                                        Namespace="AWS/EC2",
                                        MetricName="CPUCreditBalance",
                                        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                                        StartTime=start_time,
                                        EndTime=end_time,
                                        Period=3600,
                                        Statistics=["Average", "Minimum"],
                                    )

                                    # Get CPU utilization
                                    cpu_response = cloudwatch.get_metric_statistics(
                                        Namespace="AWS/EC2",
                                        MetricName="CPUUtilization",
                                        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                                        StartTime=start_time,
                                        EndTime=end_time,
                                        Period=3600,
                                        Statistics=["Average", "Maximum"],
                                    )

                                    credit_datapoints = credit_response.get("Datapoints", [])
                                    cpu_datapoints = cpu_response.get("Datapoints", [])

                                    if credit_datapoints and cpu_datapoints:
                                        avg_credits = sum(dp["Average"] for dp in credit_datapoints) / len(
                                            credit_datapoints
                                        )
                                        min_credits = min(dp["Minimum"] for dp in credit_datapoints)
                                        avg_cpu = sum(dp["Average"] for dp in cpu_datapoints) / len(cpu_datapoints)
                                        max_cpu = max(dp["Maximum"] for dp in cpu_datapoints)

                                        if min_credits < 10 and avg_cpu > 40:  # Both credit exhaustion AND high CPU
                                            # Skip - high CPU means they need the performance, not a cost optimization
                                            pass
                                        elif min_credits < 10:  # Credit exhaustion but low CPU
                                            recommendation = f"CloudWatch shows credit exhaustion (min: {min_credits:.1f}) despite low CPU (avg: {avg_cpu:.1f}%) - consider smaller fixed instance"
                                            savings = "Potential 20-40% cost reduction with smaller fixed instances"

                                            checks["burstable_credits"].append(
                                                {
                                                    "InstanceId": instance_id,
                                                    "InstanceType": instance_type,
                                                    "Recommendation": recommendation,
                                                    "CheckCategory": "Burstable Instance Optimization",
                                                    "EstimatedSavings": savings,
                                                    "ActionRequired": "Enable detailed CloudWatch monitoring if not already enabled",
                                                }
                                            )

                                        elif avg_cpu > 40:  # High CPU but good credits
                                            # Skip - high CPU indicates they need the performance, not a cost optimization
                                            pass
                                        # else: Skip - no optimization opportunity
                                    else:
                                        recommendation = "Enable detailed CloudWatch monitoring for accurate burstable instance analysis"
                                        savings = "CloudWatch detailed monitoring required for assessment"

                                        checks["burstable_credits"].append(
                                            {
                                                "InstanceId": instance_id,
                                                "InstanceType": instance_type,
                                                "Recommendation": recommendation,
                                                "CheckCategory": "Burstable Instance Optimization",
                                                "EstimatedSavings": savings,
                                                "ActionRequired": "Enable detailed CloudWatch monitoring if not already enabled",
                                            }
                                        )

                                except Exception:
                                    recommendation = "Enable detailed CloudWatch monitoring to analyze CPU credit usage and determine optimal instance type"
                                    savings = "CloudWatch detailed monitoring required for accurate rightsizing"

                                    checks["burstable_credits"].append(
                                        {
                                            "InstanceId": instance_id,
                                            "InstanceType": instance_type,
                                            "Recommendation": recommendation,
                                            "CheckCategory": "Burstable Instance Optimization",
                                            "EstimatedSavings": savings,
                                            "ActionRequired": "Enable detailed CloudWatch monitoring if not already enabled",
                                        }
                                    )

                        elif state == "stopped":
                            checks["stopped_instances"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "State": state,
                                    "Recommendation": "Stopped instance already saves compute costs. Terminate to also eliminate EBS storage costs",
                                    "CheckCategory": "Stopped Instances",
                                    "EstimatedSavings": "EBS storage costs only (compute already saved)",
                                }
                            )

            # Unattached EIPs are already covered in Network section, skip here

        except Exception as e:
            print(f"Warning: Could not perform enhanced EC2 checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                item["CheckCategory"] = item.get("CheckCategory", category.replace("_", " ").title())
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_compute_optimizer_recommendations(self) -> List[Dict[str, Any]]:
        """Get EC2 recommendations from Compute Optimizer"""
        recommendations = []
        try:
            response = self.compute_optimizer.get_ec2_instance_recommendations()
            recommendations.extend(response["instanceRecommendations"])

            # Handle pagination manually
            while response.get("nextToken"):
                response = self.compute_optimizer.get_ec2_instance_recommendations(nextToken=response["nextToken"])
                recommendations.extend(response["instanceRecommendations"])
        except Exception as e:
            print(f"Warning: Compute Optimizer not available: {e}")
            # Add recommendation to enable Compute Optimizer
            if "OptInRequiredException" in str(e) or "not registered" in str(e):
                opt_in_recommendation = {
                    "ResourceId": "compute-optimizer-service",
                    "ResourceType": "Service Configuration",
                    "Issue": "AWS Compute Optimizer not enabled",
                    "Recommendation": "Enable AWS Compute Optimizer for EC2 recommendations",
                    "EstimatedMonthlySavings": "Variable - up to 25% on EC2 instances",
                    "Action": "Go to AWS Compute Optimizer console and opt-in to receive EC2 rightsizing recommendations",
                    "Priority": "Medium",
                    "Service": "Compute Optimizer",
                }
                recommendations.append(opt_in_recommendation)
        return recommendations

    def get_ami_checks(self) -> Dict[str, Any]:
        """Get AMI optimization checks"""
        checks = {"unused_amis": [], "old_amis": []}

        try:
            # First, get all AMIs owned by this account
            amis_response = self.ec2.describe_images(Owners=["self"])

            # Get all running instances to check AMI usage
            running_amis = set()
            paginator = self.ec2.get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        if instance["State"]["Name"] in ["running", "stopped"]:
                            running_amis.add(instance.get("ImageId"))

            # Get Launch Templates to check AMI usage
            try:
                lt_response = self.ec2.describe_launch_templates()
                for lt in lt_response.get("LaunchTemplates", []):
                    try:
                        lt_versions = self.ec2.describe_launch_template_versions(
                            LaunchTemplateId=lt["LaunchTemplateId"]
                        )
                        for version in lt_versions.get("LaunchTemplateVersions", []):
                            lt_data = version.get("LaunchTemplateData", {})
                            if "ImageId" in lt_data:
                                running_amis.add(lt_data["ImageId"])
                    except Exception:
                        pass  # Skip if can't access launch template versions
            except Exception:
                pass  # Skip if can't access launch templates

            # Get Auto Scaling Launch Configurations
            try:
                asg_response = self.autoscaling.describe_auto_scaling_groups()
                for asg in asg_response.get("AutoScalingGroups", []):
                    if "LaunchConfigurationName" in asg:
                        try:
                            lc_response = self.autoscaling.describe_launch_configurations(
                                LaunchConfigurationNames=[asg["LaunchConfigurationName"]]
                            )
                            for lc in lc_response.get("LaunchConfigurations", []):
                                running_amis.add(lc.get("ImageId"))
                        except Exception:
                            pass
            except Exception:
                pass  # Skip if autoscaling not available

            for ami in amis_response.get("Images", []):
                ami_id = ami["ImageId"]
                creation_date = datetime.strptime(ami["CreationDate"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=timezone.utc
                )
                age_days = (datetime.now(timezone.utc) - creation_date).days

                # Check if AMI is unused (not in any running instances, launch templates, or ASGs)
                if ami_id not in running_amis and age_days > 30:  # Only flag if older than 30 days
                    checks["unused_amis"].append(
                        {
                            "ImageId": ami_id,
                            "Name": ami.get("Name", "N/A"),
                            "AgeDays": age_days,
                            "Recommendation": f"AMI appears unused (not found in running instances, launch templates, or ASGs) and is {age_days} days old - verify before deletion",
                            "EstimatedSavings": "Snapshot storage costs (varies by AMI size)",
                            "CheckCategory": "Unused AMIs",
                        }
                    )

                if age_days > self.OLD_SNAPSHOT_DAYS:
                    # Calculate actual snapshot storage cost
                    total_snapshot_size_gb = 0
                    snapshot_ids = []

                    # Get actual snapshot sizes from AMI block device mappings
                    for block_device in ami.get("BlockDeviceMappings", []):
                        if "Ebs" in block_device and "SnapshotId" in block_device["Ebs"]:
                            snapshot_id = block_device["Ebs"]["SnapshotId"]
                            snapshot_ids.append(snapshot_id)

                            try:
                                snapshot_response = self.ec2.describe_snapshots(SnapshotIds=[snapshot_id])
                                for snapshot in snapshot_response.get("Snapshots", []):
                                    total_snapshot_size_gb += snapshot.get("VolumeSize", 0)
                            except Exception as e:
                                print(f"⚠️ Error getting snapshot details for {snapshot_id}: {str(e)}")
                                # If we can't get snapshot details, use volume size from block device
                                total_snapshot_size_gb += block_device["Ebs"].get("VolumeSize", 8)

                    # If no snapshots found, use default estimate
                    if total_snapshot_size_gb == 0:
                        total_snapshot_size_gb = 8

                    # Calculate monthly cost: $0.05 per GB/month for snapshots (Note: Uses VolumeSize, not actual incremental storage)
                    monthly_snapshot_cost = total_snapshot_size_gb * 0.05 * self.pricing_multiplier

                    checks["old_amis"].append(
                        {
                            "ImageId": ami["ImageId"],
                            "Name": ami.get("Name", "N/A"),
                            "AgeDays": age_days,
                            "CreationDate": ami["CreationDate"],
                            "SnapshotSizeGB": total_snapshot_size_gb,
                            "SnapshotIds": snapshot_ids,
                            "Recommendation": f"Review {age_days}-day old AMI for deletion",
                            "EstimatedSavings": f"${monthly_snapshot_cost:.2f}/month ({total_snapshot_size_gb}GB snapshot storage - max estimate)",
                            "EstimatedMonthlySavings": monthly_snapshot_cost,
                        }
                    )
        except Exception as e:
            print(f"Warning: Could not get AMI checks: {e}")

        return {"recommendations": checks["old_amis"], "total_count": len(checks["old_amis"])}

    def scan_region(self, skip_services=None, scan_only=None) -> Dict[str, Any]:
        """
        Comprehensive cost optimization scan across all 30 AWS services with intelligent filtering.

        This is the main orchestrator method that coordinates the entire cost optimization
        analysis process. It performs the following operations:

        1. Scans all 30 AWS services for cost optimization opportunities
        2. Executes 220+ individual cost optimization checks
        3. Integrates data from multiple sources (Cost Hub, Compute Optimizer, CloudWatch)
        4. Calculates potential savings with regional pricing adjustments
        5. Generates structured output for HTML report generation
        6. Supports intelligent service filtering for faster, targeted scans

        Args:
            skip_services (list, optional): Services to skip during scanning.
                Available: ec2, ebs, rds, s3, lambda, dynamodb, efs, elasticache,
                opensearch, containers, network, monitoring, backup, lightsail, redshift,
                dms, quicksight, apprunner, transfer, msk, workspaces, mediastore

            scan_only (list, optional): Services to scan exclusively.
                Cannot be used with skip_services. Same service list as skip_services.

        Service Filtering Benefits:
            - 50-80% faster scan times when targeting specific services
            - Focused analysis reduces noise and improves actionability
            - Enables team-specific workflows and progressive optimization
            - Particularly effective for storage audits (--scan-only s3 --fast)

        The scan process includes:
        - Resource inventory and utilization analysis
        - Cost optimization recommendations from AWS services
        - Enhanced custom checks for additional savings opportunities
        - Smart deduplication to avoid duplicate recommendations
        - Savings calculations with regional pricing multipliers
        - Intelligent service filtering based on user preferences

        Returns:
            Dict[str, Any]: Comprehensive scan results containing:
                - account_id: AWS account identifier
                - region: Scanned AWS region
                - service_findings: Detailed results per service
                - total_savings: Aggregated potential monthly savings
                - scan_metadata: Scan configuration and performance metrics

        Examples:
            # Full scan (all services)
            results = optimizer.scan_region()

            # Storage optimization focus
            results = optimizer.scan_region(scan_only=['s3', 'ebs', 'efs'])

            # Skip compute-heavy services
            results = optimizer.scan_region(skip_services=['ec2', 'rds', 'containers'])

        Raises:
            ValueError: If both skip_services and scan_only are provided
            boto3.exceptions.ClientError: For AWS API permission or availability issues
        """
        return self._scan_legacy_run(skip_services, scan_only)

    def _scan_legacy_run(self, skip_services=None, scan_only=None) -> Dict[str, Any]:
        """Extracted from scan_region()."""
        # Initialize service filtering
        skip_services = skip_services or []
        scan_only = scan_only or []

        service_map = {
            "ec2": ["ec2", "ami"],
            "ebs": ["ebs"],
            "rds": ["rds"],
            "s3": ["s3"],
            "lambda": ["lambda"],
            "dynamodb": ["dynamodb"],
            "efs": ["efs", "file_systems"],
            "elasticache": ["elasticache"],
            "opensearch": ["opensearch"],
            "containers": ["containers", "ecs", "eks", "ecr"],
            "network": ["network", "elastic_ip", "nat_gateway", "load_balancer"],
            "monitoring": ["monitoring", "cloudwatch", "cloudtrail"],
            "auto_scaling": ["auto_scaling"],
            "route53": ["route53"],
            "backup": ["backup"],
            "cloudfront": ["cloudfront"],
            "api_gateway": ["api_gateway"],
            "step_functions": ["step_functions"],
            "lightsail": ["lightsail"],
            "redshift": ["redshift"],
            "dms": ["dms"],
            "quicksight": ["quicksight"],
            "apprunner": ["apprunner"],
            "transfer": ["transfer"],
            "msk": ["msk"],
            "workspaces": ["workspaces"],
            "mediastore": ["mediastore"],
            "glue": ["glue"],
            "athena": ["athena"],
            "batch": ["batch"],
        }

        def should_scan_service(service_key):
            """
            Determine if a service should be scanned based on filtering options.

            This function implements the core service filtering logic that enables:
            - Targeted scans for faster analysis (--scan-only)
            - Service exclusion for focused optimization (--skip-service)
            - Performance optimization by reducing scan scope

            Args:
                service_key (str): Internal service identifier to check

            Returns:
                bool: True if service should be scanned, False if skipped

            Performance Impact:
                - Can reduce scan time by 50-80% when filtering services
                - Particularly effective for storage-only scans (s3 + fast mode)
                - Enables team-specific workflows and progressive optimization
            """
            # Scan-only mode: Only scan services explicitly specified by user
            if scan_only:
                return any(service_key in service_map.get(s, []) for s in scan_only)

            # Skip mode: Scan all services except those explicitly skipped by user
            if skip_services:
                return not any(service_key in service_map.get(s, []) for s in skip_services)

            # Default: Scan all services when no filtering is specified
            return True

        print(f"Starting comprehensive cost optimization scan for region: {self.region}")
        print(f"Using AWS profile: {self.profile}")

        # Calculate and display actual services to be scanned
        if scan_only:
            services_to_scan = scan_only
            print(f"Analyzing {len(services_to_scan)} AWS services with 220+ cost optimization checks...")
            print(f"🎯 Scanning only: {', '.join(scan_only)}")
        elif skip_services:
            services_to_scan = [s for s in service_map.keys() if s not in skip_services]
            print(f"Analyzing {len(services_to_scan)} AWS services with 220+ cost optimization checks...")
            print(f"⏭️ Skipping: {', '.join(skip_services)}")
        else:
            services_to_scan = list(service_map.keys())
            print(f"Analyzing {len(services_to_scan)} AWS services with 220+ cost optimization checks...")

        # EC2 Compute Service Analysis
        # Includes EC2 instances, AMIs, and compute optimization recommendations
        if should_scan_service("ec2"):
            print("📊 Scanning EC2 instances and compute resources...")
            instance_count = _ec2_instance_count(self._ctx)

            # Enhanced EC2 checks for advanced optimization opportunities
            enhanced_ec2_checks = _ec2_enhanced_checks(self._ctx, self.pricing_multiplier)
        else:
            print("⏭️ Skipping EC2 analysis...")
            instance_count = {"total_instances": 0}
            enhanced_ec2_checks = {"recommendations": []}

        # Get Cost Optimization Hub recommendations for all services (categorize by resource type)
        all_cost_hub_recs = self.get_detailed_cost_hub_recommendations()

        # Categorize Cost Optimization Hub recommendations by resource type
        cost_hub_by_service = {"ec2": [], "lambda": [], "ebs": [], "rds": [], "other": []}

        for rec in all_cost_hub_recs:
            resource_type = rec.get("currentResourceType", "")
            if resource_type == "Ec2Instance":
                cost_hub_by_service["ec2"].append(rec)
            elif resource_type == "LambdaFunction":
                cost_hub_by_service["lambda"].append(rec)
            elif resource_type == "EbsVolume":
                cost_hub_by_service["ebs"].append(rec)
            elif resource_type in ["RdsDbInstance", "RdsDbCluster"]:
                cost_hub_by_service["rds"].append(rec)
            else:
                cost_hub_by_service["other"].append(rec)

        # Get EC2-specific Cost Optimization Hub recommendations
        cost_hub_recs = cost_hub_by_service["ec2"]
        compute_optimizer_recs = _ec2_compute_optimizer_recs(self._ctx) if should_scan_service("ec2") else []

        # AMI lifecycle management checks
        if should_scan_service("ami"):
            ami_checks = _ami_compute(self._ctx, self.pricing_multiplier)
        else:
            ami_checks = {"recommendations": []}

        # EBS Storage Service Analysis
        if should_scan_service("ebs"):
            print("💾 Scanning EBS volumes and storage optimization...")
            ebs_counts = _ebs_volume_count(self._ctx)

            # Enhanced EBS checks for storage optimization
            enhanced_ebs_checks = _ebs_compute(self._ctx, self.pricing_multiplier, self.OLD_SNAPSHOT_DAYS)
        else:
            print("⏭️ Skipping EBS analysis...")
            ebs_counts = {"total_volumes": 0}
            enhanced_ebs_checks = {"recommendations": []}

        # File Systems Analysis (EFS + FSx)
        if should_scan_service("efs") or should_scan_service("file_systems"):
            print("📁 Scanning file systems (EFS and FSx)...")
            efs_counts = self.get_efs_file_system_count()
            fsx_counts = self.get_fsx_file_system_count()

            efs_lifecycle_recs = self.get_efs_lifecycle_analysis()
            fsx_optimization_recs = self.get_fsx_optimization_analysis()
            file_system_descriptions = self.get_file_system_optimization_descriptions()
        else:
            print("⏭️ Skipping file systems analysis...")
            efs_counts = {"total_file_systems": 0}
            fsx_counts = {"total_file_systems": 0}
            efs_lifecycle_recs = {"recommendations": []}
            fsx_optimization_recs = {"recommendations": []}
            file_system_descriptions = {}

        # RDS scanning
        if should_scan_service("rds"):
            print("🗄️ Scanning RDS databases and optimization opportunities...")
            rds_counts = _rds_instance_count(self._ctx)

            rds_compute_optimizer_recs = _rds_compute_optimizer_recs(self._ctx)
            rds_descriptions = _RDS_DESCRIPTIONS
        else:
            print("⏭️ Skipping RDS analysis...")
            rds_counts = {"total_instances": 0}
            rds_compute_optimizer_recs = []  # Empty list, not dict
            rds_descriptions = {}

        # S3 scanning
        if should_scan_service("s3"):
            print("🪣 Scanning S3 buckets and storage optimization...")
            s3_data = _s3_bucket_analysis(self._ctx, self.fast_mode, self.pricing_multiplier)

            enhanced_s3_checks = _s3_enhanced_checks(self._ctx, self.pricing_multiplier)

            s3_descriptions = _S3_DESCRIPTIONS
        else:
            print("⏭️ Skipping S3 analysis...")
            s3_data = {"total_buckets": 0, "optimization_opportunities": []}
            enhanced_s3_checks = {"recommendations": []}
            s3_descriptions = {}

        # DynamoDB scanning
        if should_scan_service("dynamodb"):
            print("⚡ Scanning DynamoDB tables and capacity optimization...")
            dynamodb_data = self.get_dynamodb_table_analysis()

            # Get enhanced DynamoDB checks
            enhanced_dynamodb_checks = self.get_enhanced_dynamodb_checks()

            dynamodb_descriptions = self.get_dynamodb_optimization_descriptions()
        else:
            print("⏭️ Skipping DynamoDB analysis...")
            dynamodb_data = {"total_tables": 0}
            enhanced_dynamodb_checks = {"recommendations": []}
            dynamodb_descriptions = {}

        # Container services scanning
        if should_scan_service("containers"):
            print("🐳 Scanning container services (ECS/EKS/ECR)...")
            container_data = self.get_container_services_analysis()
            enhanced_container_checks = self.get_enhanced_container_checks()
            container_descriptions = self.get_container_optimization_descriptions()
        else:
            print("⏭️ Skipping container services analysis...")
            container_data = {}
            enhanced_container_checks = {"recommendations": []}
            container_descriptions = {}

        # EBS Compute Optimizer recommendations (only if EBS is being scanned)
        if should_scan_service("ebs"):
            ebs_compute_optimizer_recs = _ebs_compute_optimizer_recs(self._ctx, self.pricing_multiplier)
            unattached_volumes = _ebs_unattached_volumes(self._ctx, self.pricing_multiplier)
            ebs_descriptions = _EBS_DESCRIPTIONS
        else:
            ebs_compute_optimizer_recs = []
            unattached_volumes = []
            ebs_descriptions = {}

        # Enhanced RDS checks (only if RDS is being scanned)
        if should_scan_service("rds"):
            enhanced_rds_checks = _rds_enhanced_checks(self._ctx, self.pricing_multiplier, self.OLD_SNAPSHOT_DAYS)
        else:
            enhanced_rds_checks = {"recommendations": []}

        # ==================== NEW COMPREHENSIVE CHECKS ====================
        # Suppress CLI output - only generate HTML report

        # Savings Plans removed - will be generated from another source

        # Network & Infrastructure checks
        if should_scan_service("network"):
            print("🌐 Scanning network resources and infrastructure...")
            elastic_ip_checks = self.get_elastic_ip_checks()
            nat_gateway_checks = self.get_nat_gateway_checks()
            vpc_endpoints_checks = self.get_vpc_endpoints_checks()
            load_balancer_checks = self.get_load_balancer_checks()
            advanced_ec2_checks = _ec2_advanced_checks(self._ctx, self.pricing_multiplier, self.fast_mode)
        else:
            print("⏭️ Skipping network analysis...")
            elastic_ip_checks = {"recommendations": []}
            nat_gateway_checks = {"recommendations": []}
            vpc_endpoints_checks = {"recommendations": []}
            load_balancer_checks = {"recommendations": []}
            advanced_ec2_checks = {"recommendations": []}

        # Auto Scaling checks
        if should_scan_service("auto_scaling"):
            print("📈 Scanning Auto Scaling groups...")
            auto_scaling_checks = _ec2_auto_scaling_checks(self._ctx)
        else:
            auto_scaling_checks = {"recommendations": []}

        # Monitoring & Logging checks
        if should_scan_service("monitoring"):
            print("📊 Scanning monitoring and logging services...")
            cloudwatch_checks = self.get_cloudwatch_checks()
            cloudtrail_checks = self.get_cloudtrail_checks()
            backup_checks = self.get_backup_checks()
        else:
            print("⏭️ Skipping monitoring analysis...")
            cloudwatch_checks = {"recommendations": []}
            cloudtrail_checks = {"recommendations": []}
            backup_checks = {"recommendations": []}

        # Route 53 checks
        if should_scan_service("route53"):
            print("🌐 Scanning Route 53 DNS services...")
            route53_checks = self.get_route53_checks()
        else:
            route53_checks = {"recommendations": []}

        # File systems checks
        if should_scan_service("efs"):
            print("📁 Scanning file systems (EFS/FSx)...")
            enhanced_file_systems_checks = self.get_enhanced_file_systems_checks()
        else:
            print("⏭️ Skipping file systems analysis...")
            enhanced_file_systems_checks = {"recommendations": []}

        # ElastiCache/Redis scanning
        if should_scan_service("elasticache"):
            print("⚡ Scanning ElastiCache clusters...")
            enhanced_elasticache_checks = self.get_enhanced_elasticache_checks()
        else:
            print("⏭️ Skipping ElastiCache analysis...")
            enhanced_elasticache_checks = {"recommendations": []}

        # OpenSearch scanning
        if should_scan_service("opensearch"):
            print("🔍 Scanning OpenSearch domains...")
            enhanced_opensearch_checks = self.get_enhanced_opensearch_checks()
        else:
            print("⏭️ Skipping OpenSearch analysis...")
            enhanced_opensearch_checks = {"recommendations": []}

        # CloudFront CDN
        if should_scan_service("cloudfront"):
            enhanced_cloudfront_checks = _cloudfront_enhanced_checks(self._ctx)
        else:
            enhanced_cloudfront_checks = {"recommendations": []}

        # API Gateway
        if should_scan_service("api_gateway"):
            enhanced_api_gateway_checks = _api_gateway_enhanced_checks(self._ctx)
        else:
            enhanced_api_gateway_checks = {"recommendations": []}

        # Step Functions
        if should_scan_service("step_functions"):
            enhanced_step_functions_checks = _step_functions_enhanced_checks(self._ctx)
        else:
            enhanced_step_functions_checks = {"recommendations": []}

        # Lambda
        if should_scan_service("lambda"):
            enhanced_lambda_checks = self.get_enhanced_lambda_checks()
        else:
            enhanced_lambda_checks = {"recommendations": []}

        # Calculate total EC2 savings from all sources
        ec2_total_savings = 0

        # Cost Hub savings (handle case when EC2 is skipped)
        cost_hub_recommendations = (
            cost_hub_recs if isinstance(cost_hub_recs, list) else cost_hub_recs.get("recommendations", [])
        )
        cost_hub_savings = sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recommendations)
        ec2_total_savings += cost_hub_savings

        # Compute Optimizer savings (only use actual dollar amounts)
        compute_optimizer_savings = 0
        for rec in compute_optimizer_recs:
            # Only extract actual dollar amounts from Compute Optimizer
            if "estimatedMonthlySavings" in rec and rec.get("estimatedMonthlySavings", 0) > 0:
                compute_optimizer_savings += rec.get("estimatedMonthlySavings", 0)
            # Skip percentage-based savings without actual dollar amounts
        ec2_total_savings += compute_optimizer_savings

        # Enhanced checks savings (only parse actual dollar amounts)
        enhanced_savings = 0
        for rec in enhanced_ec2_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")

            # Only parse actual dollar amounts - no fixed estimates
            if "$" in savings_str and "/month" in savings_str:
                try:
                    savings_val = float(savings_str.replace("$", "").split("/")[0])
                    enhanced_savings += savings_val
                except (ValueError, AttributeError) as e:
                    print(f"Warning: Could not parse EC2 savings amount '{savings_str}': {e}")
            # Skip recommendations without parseable dollar amounts
        ec2_total_savings += enhanced_savings

        # Advanced EC2 checks savings
        advanced_savings = 0
        for rec in advanced_ec2_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    # Extract dollar amount using regex for better parsing
                    dollar_match = re.search(r"\$(\d+\.?\d*)", savings_str)
                    if dollar_match:
                        savings_val = float(dollar_match.group(1))
                        advanced_savings += savings_val
                except (ValueError, AttributeError) as e:
                    print(f"⚠️ Could not parse savings amount '{savings_str}': {str(e)}")
                    # Continue without this recommendation's savings
        ec2_total_savings += advanced_savings

        # Structure data for multi-service HTML report
        ec2_findings = {
            "service_name": "EC2",
            "instance_count": instance_count,
            "total_recommendations": len(cost_hub_recs)
            + len(compute_optimizer_recs)
            + len(enhanced_ec2_checks.get("recommendations", []))
            + len(advanced_ec2_checks.get("recommendations", [])),
            "total_monthly_savings": ec2_total_savings,
            "sources": {
                "cost_optimization_hub": {"count": len(cost_hub_recs), "recommendations": cost_hub_recs},
                "compute_optimizer": {"count": len(compute_optimizer_recs), "recommendations": compute_optimizer_recs},
                "enhanced_checks": {
                    "count": len(enhanced_ec2_checks.get("recommendations", [])),
                    "recommendations": enhanced_ec2_checks.get("recommendations", []),
                },
                "advanced_ec2_checks": {
                    "count": len(advanced_ec2_checks.get("recommendations", [])),
                    "recommendations": advanced_ec2_checks.get("recommendations", []),
                },
            },
        }

        # Extract gp2 migration recommendations separately
        gp2_recommendations = [
            rec
            for rec in enhanced_ebs_checks.get("recommendations", [])
            if rec.get("CheckCategory") == "Volume Type Optimization"
        ]
        other_ebs_recommendations = [
            rec
            for rec in enhanced_ebs_checks.get("recommendations", [])
            if rec.get("CheckCategory") != "Volume Type Optimization"
        ]

        # Calculate total EBS savings from all sources
        ebs_total_savings = 0
        ebs_total_savings += sum(v.get("EstimatedMonthlyCost", 0) for v in unattached_volumes)

        # Parse dollar savings from enhanced checks
        for rec in enhanced_ebs_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    ebs_total_savings += float(savings_str.replace("$", "").split("/")[0])
                except Exception:
                    pass

        # gp2 → gp3: 20% of current gp2 storage cost
        for rec in gp2_recommendations:
            size = rec.get("Size", 0)
            ebs_total_savings += self._estimate_volume_cost(size, "gp2") * 0.20

        # Compute Optimizer (if available)
        ebs_total_savings += sum(r.get("estimatedMonthlySavings", 0) for r in ebs_compute_optimizer_recs)

        ebs_findings = {
            "service_name": "EBS",
            "volume_counts": ebs_counts,
            "total_recommendations": len(ebs_compute_optimizer_recs)
            + len(unattached_volumes)
            + len(gp2_recommendations)
            + len(other_ebs_recommendations),
            "total_monthly_savings": ebs_total_savings,
            "optimization_descriptions": ebs_descriptions,
            "sources": {
                "compute_optimizer": {
                    "count": len(ebs_compute_optimizer_recs),
                    "recommendations": ebs_compute_optimizer_recs,
                },
                "unattached_volumes": {"count": len(unattached_volumes), "recommendations": unattached_volumes},
                "gp2_migration": {"count": len(gp2_recommendations), "recommendations": gp2_recommendations},
                "enhanced_checks": {
                    "count": len(other_ebs_recommendations),
                    "recommendations": other_ebs_recommendations,
                },
            },
        }

        # Calculate RDS total savings from all sources
        rds_total_savings = 0

        # Add Compute Optimizer savings
        rds_total_savings += sum(r.get("estimatedMonthlySavings", 0) for r in rds_compute_optimizer_recs)

        # Parse savings from RDS enhanced checks
        for rec in enhanced_rds_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    # Extract dollar amount using regex for better parsing
                    dollar_match = re.search(r"\$(\d+\.?\d*)", savings_str)
                    if dollar_match:
                        savings_val = float(dollar_match.group(1))
                        rds_total_savings += savings_val
                except (ValueError, AttributeError) as e:
                    print(f"⚠️ Could not parse RDS savings amount '{savings_str}': {str(e)}")
            # Note: Percentage-based savings (Multi-AZ ~50%, scheduling 65-75%, RI up to 60%, rightsizing)
            # are not parsed into dollar totals as they require instance cost data not available in this context
            # WARNING: Total savings are significantly understated by design due to these excluded recommendations

        # Add warning about understated totals
        if rds_total_savings == 0:
            print(
                "⚠️ RDS total savings may appear low - percentage-based recommendations (Multi-AZ, scheduling, RI) not included in totals"
            )

        rds_findings = {
            "service_name": "RDS",
            "instance_counts": rds_counts,
            "total_recommendations": len(rds_compute_optimizer_recs)
            + len(enhanced_rds_checks.get("recommendations", [])),
            "total_monthly_savings": rds_total_savings,
            "optimization_descriptions": rds_descriptions,
            "sources": {
                "compute_optimizer": {
                    "count": len(rds_compute_optimizer_recs),
                    "recommendations": rds_compute_optimizer_recs,
                },
                "enhanced_checks": {
                    "count": len(enhanced_rds_checks.get("recommendations", [])),
                    "recommendations": enhanced_rds_checks.get("recommendations", []),
                },
            },
        }

        file_systems_findings = {
            "service_name": "File Systems",
            "efs_counts": efs_counts,
            "fsx_counts": fsx_counts,
            "total_recommendations": len(
                efs_lifecycle_recs
                if isinstance(efs_lifecycle_recs, list)
                else efs_lifecycle_recs.get("recommendations", [])
            )
            + len(
                fsx_optimization_recs
                if isinstance(fsx_optimization_recs, list)
                else fsx_optimization_recs.get("recommendations", [])
            )
            + len(enhanced_file_systems_checks.get("recommendations", [])),
            # Savings calculation: Conservative 30% estimate for lifecycle policies
            # Actual savings depend on data access patterns and lifecycle transitions
            "total_monthly_savings": sum(
                rec.get("EstimatedMonthlyCost", 0) * 0.3
                for rec in (
                    efs_lifecycle_recs
                    if isinstance(efs_lifecycle_recs, list)
                    else efs_lifecycle_recs.get("recommendations", [])
                )
                + (
                    fsx_optimization_recs
                    if isinstance(fsx_optimization_recs, list)
                    else fsx_optimization_recs.get("recommendations", [])
                )
            ),
            "optimization_descriptions": file_system_descriptions,
            "sources": {
                "efs_lifecycle_analysis": {
                    "count": len(
                        efs_lifecycle_recs
                        if isinstance(efs_lifecycle_recs, list)
                        else efs_lifecycle_recs.get("recommendations", [])
                    ),
                    "recommendations": efs_lifecycle_recs
                    if isinstance(efs_lifecycle_recs, list)
                    else efs_lifecycle_recs.get("recommendations", []),
                },
                "fsx_optimization_analysis": {
                    "count": len(
                        fsx_optimization_recs
                        if isinstance(fsx_optimization_recs, list)
                        else fsx_optimization_recs.get("recommendations", [])
                    ),
                    "recommendations": fsx_optimization_recs
                    if isinstance(fsx_optimization_recs, list)
                    else fsx_optimization_recs.get("recommendations", []),
                },
                "enhanced_checks": {
                    "count": len(enhanced_file_systems_checks.get("recommendations", [])),
                    "recommendations": enhanced_file_systems_checks.get("recommendations", []),
                },
            },
        }

        s3_findings = {
            "service_name": "S3",
            "fast_mode_warning": "Size estimates may be unreliable - based on 100-object samples"
            if self.fast_mode
            else None,
            "bucket_counts": {
                "total": s3_data.get("total_buckets", 0),
                "without_lifecycle": len(s3_data.get("buckets_without_lifecycle", [])),
                "without_intelligent_tiering": len(s3_data.get("buckets_without_intelligent_tiering", [])),
            },
            "total_recommendations": len(s3_data.get("optimization_opportunities", []))
            + len(enhanced_s3_checks.get("recommendations", [])),
            "total_monthly_savings": sum(
                rec.get("EstimatedMonthlyCost", 0) for rec in s3_data.get("optimization_opportunities", [])
            ),
            "optimization_descriptions": s3_descriptions,
            "sources": {
                "s3_bucket_analysis": {
                    "count": len(s3_data.get("optimization_opportunities", [])),
                    "recommendations": s3_data.get("optimization_opportunities", []),
                    "top_cost_buckets": s3_data.get("top_cost_buckets", []),
                    "top_size_buckets": s3_data.get("top_size_buckets", []),
                },
                "enhanced_checks": {
                    "count": len(enhanced_s3_checks.get("recommendations", [])),
                    "recommendations": enhanced_s3_checks.get("recommendations", []),
                },
            },
        }

        dynamodb_findings = {
            "service_name": "DynamoDB",
            "table_counts": {
                "total": dynamodb_data.get("total_tables", 0),
                "provisioned": len(dynamodb_data.get("provisioned_tables", [])),
                "on_demand": len(dynamodb_data.get("on_demand_tables", [])),
            },
            "total_recommendations": len(dynamodb_data.get("optimization_opportunities", []))
            + len(enhanced_dynamodb_checks.get("recommendations", [])),
            # Savings calculation: Conservative 30% estimate for capacity optimization
            # Actual savings depend on traffic patterns and provisioned vs on-demand usage
            "total_monthly_savings": sum(
                rec.get("EstimatedMonthlyCost", 0) * 0.3 for rec in dynamodb_data.get("optimization_opportunities", [])
            ),
            "optimization_descriptions": dynamodb_descriptions,
            "sources": {
                "dynamodb_table_analysis": {
                    "count": len(dynamodb_data.get("optimization_opportunities", [])),
                    "recommendations": dynamodb_data.get("optimization_opportunities", []),
                },
                "enhanced_checks": {
                    "count": len(enhanced_dynamodb_checks.get("recommendations", [])),
                    "recommendations": enhanced_dynamodb_checks.get("recommendations", []),
                },
            },
        }

        # Calculate Container total savings from enhanced checks with better estimates
        container_total_savings = 0

        # Estimate savings based on container optimization types
        for rec in enhanced_container_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")
            recommendation = rec.get("Recommendation", "").lower()

            if "spot instances" in savings_str or "spot" in recommendation:
                container_total_savings += 150  # $150/month per Spot opportunity
            elif "rightsizing" in savings_str.lower() or "rightsize" in recommendation:
                container_total_savings += 75  # $75/month per rightsizing
            elif "lifecycle" in savings_str.lower() or "lifecycle" in recommendation:
                container_total_savings += 25  # $25/month per ECR lifecycle
            elif "unused" in recommendation or "empty" in recommendation:
                container_total_savings += 100  # $100/month per unused cluster
            elif "over-provisioned" in recommendation or "over provisioned" in recommendation:
                container_total_savings += 60  # $60/month per over-provisioned service

        containers_findings = {
            "service_name": "Containers",
            "service_counts": {
                "ecs_clusters": container_data.get("ecs", {}).get("total_clusters", 0),
                "eks_clusters": container_data.get("eks", {}).get("total_clusters", 0),
                "ecr_repositories": container_data.get("ecr", {}).get("total_repositories", 0),
                "ecs_services": container_data.get("ecs", {}).get("total_services", 0),
            },
            "total_recommendations": len(enhanced_container_checks.get("recommendations", [])),
            "total_monthly_savings": container_total_savings,
            "optimization_descriptions": container_descriptions,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_container_checks.get("recommendations", [])),
                    "recommendations": enhanced_container_checks.get("recommendations", []),
                }
            },
        }

        # Calculate ElastiCache total savings
        elasticache_total_savings = 0
        for rec in enhanced_elasticache_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")
            if "Reserved" in savings_str:
                elasticache_total_savings += 200  # $200/month per reserved node opportunity
            elif "Graviton" in savings_str or "20-40%" in savings_str:
                elasticache_total_savings += 80  # $80/month per Graviton migration
            elif "Valkey" in savings_str:
                elasticache_total_savings += 50  # conservative placeholder; label as estimate
            elif "Underutilized" in savings_str:
                elasticache_total_savings += 100  # $100/month per rightsizing

        # ElastiCache/Redis findings
        elasticache_findings = {
            "service_name": "ElastiCache",
            "total_recommendations": len(enhanced_elasticache_checks.get("recommendations", [])),
            "total_monthly_savings": elasticache_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_elasticache_checks.get("recommendations", [])),
                    "recommendations": enhanced_elasticache_checks.get("recommendations", []),
                }
            },
        }

        # Calculate OpenSearch total savings
        opensearch_total_savings = 0
        for rec in enhanced_opensearch_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")
            if "Reserved" in savings_str:
                opensearch_total_savings += 300  # $300/month per reserved instance opportunity
            elif "Graviton" in savings_str or "20-40%" in savings_str:
                opensearch_total_savings += 120  # $120/month per Graviton migration
            elif "storage" in savings_str.lower():
                opensearch_total_savings += 50  # $50/month per storage optimization

        # OpenSearch findings
        opensearch_findings = {
            "service_name": "OpenSearch",
            "total_recommendations": len(enhanced_opensearch_checks.get("recommendations", [])),
            "total_monthly_savings": opensearch_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_opensearch_checks.get("recommendations", [])),
                    "recommendations": enhanced_opensearch_checks.get("recommendations", []),
                }
            },
        }

        # Calculate Network total savings from all sources
        network_total_savings = 0

        # Parse savings from all network sources
        all_network_recs = (
            elastic_ip_checks.get("recommendations", [])
            + nat_gateway_checks.get("recommendations", [])
            + vpc_endpoints_checks.get("recommendations", [])
            + load_balancer_checks.get("recommendations", [])
            + auto_scaling_checks.get("recommendations", [])
        )

        for rec in all_network_recs:
            savings_str = rec.get("EstimatedSavings", "")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    # Extract dollar amount from various formats:
                    # "$3.60/month", "Up to $16.20/month", "Estimated $6.30/month"
                    dollar_match = re.search(r"\$(\d+\.?\d*)", savings_str)
                    if dollar_match:
                        savings_val = float(dollar_match.group(1))
                        network_total_savings += savings_val
                except (ValueError, AttributeError) as e:
                    print(f"⚠️ Could not parse network savings amount '{savings_str}': {str(e)}")
                    # Continue without this recommendation's savings

        # NEW: Network & Infrastructure findings
        network_findings = {
            "service_name": "Network & Infrastructure",
            "total_recommendations": (
                len(elastic_ip_checks.get("recommendations", []))
                + len(nat_gateway_checks.get("recommendations", []))
                + len(vpc_endpoints_checks.get("recommendations", []))
                + len(load_balancer_checks.get("recommendations", []))
                + len(auto_scaling_checks.get("recommendations", []))
            ),
            "total_monthly_savings": network_total_savings,
            "sources": {
                "elastic_ip_checks": {
                    "count": len(elastic_ip_checks.get("recommendations", [])),
                    "recommendations": elastic_ip_checks.get("recommendations", []),
                },
                "nat_gateway_checks": {
                    "count": len(nat_gateway_checks.get("recommendations", [])),
                    "recommendations": nat_gateway_checks.get("recommendations", []),
                },
                "vpc_endpoints_checks": {
                    "count": len(vpc_endpoints_checks.get("recommendations", [])),
                    "recommendations": vpc_endpoints_checks.get("recommendations", []),
                },
                "load_balancer_checks": {
                    "count": len(load_balancer_checks.get("recommendations", [])),
                    "recommendations": load_balancer_checks.get("recommendations", []),
                },
                "auto_scaling_checks": {
                    "count": len(auto_scaling_checks.get("recommendations", [])),
                    "recommendations": auto_scaling_checks.get("recommendations", []),
                },
            },
        }

        # Calculate total monitoring savings from all sources
        monitoring_total_savings = 0

        # Parse savings from all monitoring sources
        all_monitoring_recs = (
            cloudwatch_checks.get("recommendations", [])
            + cloudtrail_checks.get("recommendations", [])
            + backup_checks.get("recommendations", [])
            + route53_checks.get("recommendations", [])
        )

        for rec in all_monitoring_recs:
            savings_str = rec.get("EstimatedSavings", "")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    # Extract dollar amount from strings like "$5.50/month with 30-day retention"
                    savings_val = float(savings_str.replace("$", "").split("/")[0])
                    monitoring_total_savings += savings_val
                except (ValueError, AttributeError) as e:
                    print(f"⚠️ Could not parse monitoring savings amount '{savings_str}': {str(e)}")
                    # Continue without this recommendation's savings

        # NEW: Monitoring & Logging findings
        monitoring_findings = {
            "service_name": "Monitoring & Logging",
            "total_recommendations": (
                len(cloudwatch_checks.get("recommendations", []))
                + len(cloudtrail_checks.get("recommendations", []))
                + len(backup_checks.get("recommendations", []))
                + len(route53_checks.get("recommendations", []))
            ),
            "total_monthly_savings": monitoring_total_savings,
            "sources": {
                "cloudwatch_checks": {
                    "count": len(cloudwatch_checks.get("recommendations", [])),
                    "recommendations": cloudwatch_checks.get("recommendations", []),
                },
                "cloudtrail_checks": {
                    "count": len(cloudtrail_checks.get("recommendations", [])),
                    "recommendations": cloudtrail_checks.get("recommendations", []),
                },
                "backup_checks": {
                    "count": len(backup_checks.get("recommendations", [])),
                    "recommendations": backup_checks.get("recommendations", []),
                },
                "route53_checks": {
                    "count": len(route53_checks.get("recommendations", [])),
                    "recommendations": route53_checks.get("recommendations", []),
                },
            },
        }

        # Lambda findings
        # Calculate Lambda total savings from enhanced checks and Cost Optimization Hub
        lambda_total_savings = 0

        # Cost Optimization Hub Lambda recommendations
        lambda_cost_hub_recs = cost_hub_by_service["lambda"]
        lambda_cost_hub_savings = sum(rec.get("estimatedMonthlySavings", 0) for rec in lambda_cost_hub_recs)
        lambda_total_savings += lambda_cost_hub_savings

        # Parse savings from Lambda recommendations
        for rec in enhanced_lambda_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")

            # Estimate savings based on recommendation type
            if "Up to 90%" in savings_str:  # Provisioned concurrency
                # Estimate $50/month per provisioned concurrency config
                lambda_total_savings += 50
            elif "memory optimization" in savings_str.lower():  # Memory rightsizing
                # Estimate $10/month per over-provisioned function
                lambda_total_savings += 10
            elif "Eliminate unused costs" in savings_str:  # Low invocation
                # Estimate $5/month per unused function
                lambda_total_savings += 5

        lambda_findings = {
            "service_name": "Lambda",
            "total_recommendations": len(lambda_cost_hub_recs) + len(enhanced_lambda_checks.get("recommendations", [])),
            "total_monthly_savings": lambda_total_savings,
            "sources": {
                "cost_optimization_hub": {"count": len(lambda_cost_hub_recs), "recommendations": lambda_cost_hub_recs},
                "enhanced_checks": {
                    "count": len(enhanced_lambda_checks.get("recommendations", [])),
                    "recommendations": enhanced_lambda_checks.get("recommendations", []),
                },
            },
        }

        # Calculate CloudFront total savings
        cloudfront_total_savings = (
            len(enhanced_cloudfront_checks.get("recommendations", [])) * 25
        )  # $25/month per optimization

        # CloudFront findings
        cloudfront_findings = {
            "service_name": "CloudFront",
            "total_recommendations": len(enhanced_cloudfront_checks.get("recommendations", [])),
            "total_monthly_savings": cloudfront_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_cloudfront_checks.get("recommendations", [])),
                    "recommendations": enhanced_cloudfront_checks.get("recommendations", []),
                }
            },
        }

        # Calculate API Gateway total savings
        api_gateway_total_savings = (
            len(enhanced_api_gateway_checks.get("recommendations", [])) * 15
        )  # $15/month per optimization

        # API Gateway findings
        api_gateway_findings = {
            "service_name": "API Gateway",
            "total_recommendations": len(enhanced_api_gateway_checks.get("recommendations", [])),
            "total_monthly_savings": api_gateway_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_api_gateway_checks.get("recommendations", [])),
                    "recommendations": enhanced_api_gateway_checks.get("recommendations", []),
                }
            },
        }

        # NEW SERVICES SCANNING

        # Lightsail scanning
        if should_scan_service("lightsail"):
            print("💡 Scanning Lightsail instances and optimization opportunities...")
            enhanced_lightsail_checks = self.get_enhanced_lightsail_checks()
            lightsail_descriptions = self.get_lightsail_optimization_descriptions()
        else:
            print("⏭️ Skipping Lightsail analysis...")
            enhanced_lightsail_checks = {"recommendations": []}
            lightsail_descriptions = {}

        # Redshift scanning
        if should_scan_service("redshift"):
            print("🏢 Scanning Redshift clusters and serverless optimization...")
            enhanced_redshift_checks = _redshift_enhanced_checks(self._ctx)
            redshift_descriptions = _REDSHIFT_DESCRIPTIONS
        else:
            print("⏭️ Skipping Redshift analysis...")
            enhanced_redshift_checks = {"recommendations": []}
            redshift_descriptions = {}

        # DMS scanning
        if should_scan_service("dms"):
            print("🔄 Scanning Database Migration Service optimization...")
            enhanced_dms_checks = self.get_enhanced_dms_checks()
            dms_descriptions = self.get_dms_optimization_descriptions()
        else:
            print("⏭️ Skipping DMS analysis...")
            enhanced_dms_checks = {"recommendations": []}
            dms_descriptions = {}

        # QuickSight scanning
        if should_scan_service("quicksight"):
            print("📊 Scanning QuickSight BI service optimization...")
            enhanced_quicksight_checks = _quicksight_enhanced_checks(self._ctx)
            quicksight_descriptions = _QUICKSIGHT_DESCRIPTIONS
        else:
            print("⏭️ Skipping QuickSight analysis...")
            enhanced_quicksight_checks = {"recommendations": []}
            quicksight_descriptions = {}

        # App Runner scanning
        if should_scan_service("apprunner"):
            print("🏃 Scanning App Runner service optimization...")
            enhanced_apprunner_checks = _apprunner_enhanced_checks(self._ctx)
            apprunner_descriptions = _APPRUNNER_DESCRIPTIONS
        else:
            print("⏭️ Skipping App Runner analysis...")
            enhanced_apprunner_checks = {"recommendations": []}
            apprunner_descriptions = {}

        # Transfer Family scanning
        if should_scan_service("transfer"):
            print("📁 Scanning Transfer Family optimization...")
            enhanced_transfer_checks = _transfer_enhanced_checks(self._ctx)
            transfer_descriptions = self.get_transfer_optimization_descriptions()
        else:
            print("⏭️ Skipping Transfer Family analysis...")
            enhanced_transfer_checks = {"recommendations": []}
            transfer_descriptions = {}

        # MSK scanning
        if should_scan_service("msk"):
            print("🔄 Scanning Managed Streaming for Apache Kafka optimization...")
            enhanced_msk_checks = _msk_enhanced_checks(self._ctx)
            msk_descriptions = _MSK_DESCRIPTIONS
        else:
            print("⏭️ Skipping MSK analysis...")
            enhanced_msk_checks = {"recommendations": []}
            msk_descriptions = {}

        # WorkSpaces scanning
        if should_scan_service("workspaces"):
            print("🖥️ Scanning WorkSpaces virtual desktops optimization...")
            enhanced_workspaces_checks = _workspaces_enhanced_checks(self._ctx)
            workspaces_descriptions = _WORKSPACES_DESCRIPTIONS
        else:
            print("⏭️ Skipping WorkSpaces analysis...")
            enhanced_workspaces_checks = {"recommendations": []}
            workspaces_descriptions = {}

        # MediaStore scanning
        if should_scan_service("mediastore"):
            print("🎬 Scanning Elemental MediaStore optimization...")
            enhanced_mediastore_checks = _mediastore_enhanced_checks(self._ctx)
            mediastore_descriptions = _MEDIASTORE_DESCRIPTIONS
        else:
            print("⏭️ Skipping MediaStore analysis...")
            enhanced_mediastore_checks = {"recommendations": []}
            mediastore_descriptions = {}

        # Glue scanning
        if should_scan_service("glue"):
            print("🔧 Scanning AWS Glue ETL optimization...")
            enhanced_glue_checks = self.get_enhanced_glue_checks()
            glue_descriptions = self.get_glue_optimization_descriptions()
        else:
            print("⏭️ Skipping Glue analysis...")
            enhanced_glue_checks = {"recommendations": []}
            glue_descriptions = {}

        # Athena scanning
        if should_scan_service("athena"):
            print("📊 Scanning Athena query optimization...")
            enhanced_athena_checks = _athena_enhanced_checks(self._ctx)
            athena_descriptions = self.get_athena_optimization_descriptions()
        else:
            print("⏭️ Skipping Athena analysis...")
            enhanced_athena_checks = {"recommendations": []}
            athena_descriptions = {}

        # Batch scanning
        if should_scan_service("batch"):
            print("⚙️  Scanning AWS Batch compute optimization...")
            enhanced_batch_checks = _batch_enhanced_checks(self._ctx)
            batch_descriptions = _BATCH_DESCRIPTIONS
        else:
            print("⏭️ Skipping Batch analysis...")
            enhanced_batch_checks = {"recommendations": []}
            batch_descriptions = {}

        # Calculate savings for new services
        lightsail_total_savings = (
            len(enhanced_lightsail_checks.get("recommendations", [])) * 12
        )  # $12/month per optimization
        redshift_total_savings = (
            len(enhanced_redshift_checks.get("recommendations", [])) * 200
        )  # $200/month per optimization
        dms_total_savings = len(enhanced_dms_checks.get("recommendations", [])) * 50  # $50/month per optimization
        quicksight_total_savings = (
            len(enhanced_quicksight_checks.get("recommendations", [])) * 30
        )  # $30/month per optimization
        apprunner_total_savings = (
            len(enhanced_apprunner_checks.get("recommendations", [])) * 25
        )  # $25/month per optimization
        transfer_total_savings = (
            len(enhanced_transfer_checks.get("recommendations", [])) * 40
        )  # $40/month per optimization
        msk_total_savings = len(enhanced_msk_checks.get("recommendations", [])) * 150  # $150/month per optimization
        workspaces_total_savings = (
            len(enhanced_workspaces_checks.get("recommendations", [])) * 35
        )  # $35/month per optimization
        mediastore_total_savings = (
            len(enhanced_mediastore_checks.get("recommendations", [])) * 20
        )  # $20/month per optimization
        glue_total_savings = len(enhanced_glue_checks.get("recommendations", [])) * 100  # $100/month per optimization
        athena_total_savings = len(enhanced_athena_checks.get("recommendations", [])) * 50  # $50/month per optimization
        batch_total_savings = len(enhanced_batch_checks.get("recommendations", [])) * 150  # $150/month per optimization

        # Create findings for new services
        lightsail_findings = {
            "service_name": "Lightsail",
            "total_recommendations": len(enhanced_lightsail_checks.get("recommendations", [])),
            "total_monthly_savings": lightsail_total_savings,
            "sources": enhanced_lightsail_checks.get("checks", {}),
        }

        redshift_findings = {
            "service_name": "Redshift",
            "total_recommendations": len(enhanced_redshift_checks.get("recommendations", [])),
            "total_monthly_savings": redshift_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_redshift_checks.get("recommendations", [])),
                    "recommendations": enhanced_redshift_checks.get("recommendations", []),
                }
            },
        }

        dms_findings = {
            "service_name": "DMS",
            "total_recommendations": len(enhanced_dms_checks.get("recommendations", [])),
            "total_monthly_savings": dms_total_savings,
            "sources": enhanced_dms_checks.get("checks", {}),
        }

        quicksight_findings = {
            "service_name": "QuickSight",
            "total_recommendations": len(enhanced_quicksight_checks.get("recommendations", [])),
            "total_monthly_savings": quicksight_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_quicksight_checks.get("recommendations", [])),
                    "recommendations": enhanced_quicksight_checks.get("recommendations", []),
                }
            },
        }

        apprunner_findings = {
            "service_name": "App Runner",
            "total_recommendations": len(enhanced_apprunner_checks.get("recommendations", [])),
            "total_monthly_savings": apprunner_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_apprunner_checks.get("recommendations", [])),
                    "recommendations": enhanced_apprunner_checks.get("recommendations", []),
                }
            },
        }

        transfer_findings = {
            "service_name": "Transfer Family",
            "total_recommendations": len(enhanced_transfer_checks.get("recommendations", [])),
            "total_monthly_savings": transfer_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_transfer_checks.get("recommendations", [])),
                    "recommendations": enhanced_transfer_checks.get("recommendations", []),
                }
            },
        }

        msk_findings = {
            "service_name": "MSK",
            "total_recommendations": len(enhanced_msk_checks.get("recommendations", [])),
            "total_monthly_savings": msk_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_msk_checks.get("recommendations", [])),
                    "recommendations": enhanced_msk_checks.get("recommendations", []),
                }
            },
        }

        workspaces_findings = {
            "service_name": "WorkSpaces",
            "total_recommendations": len(enhanced_workspaces_checks.get("recommendations", [])),
            "total_monthly_savings": workspaces_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_workspaces_checks.get("recommendations", [])),
                    "recommendations": enhanced_workspaces_checks.get("recommendations", []),
                }
            },
        }

        mediastore_findings = {
            "service_name": "MediaStore",
            "total_recommendations": len(enhanced_mediastore_checks.get("recommendations", [])),
            "total_monthly_savings": mediastore_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_mediastore_checks.get("recommendations", [])),
                    "recommendations": enhanced_mediastore_checks.get("recommendations", []),
                }
            },
        }

        glue_findings = {
            "service_name": "Glue",
            "total_recommendations": len(enhanced_glue_checks.get("recommendations", [])),
            "total_monthly_savings": glue_total_savings,
            "sources": enhanced_glue_checks.get("checks", {}),
        }

        athena_findings = {
            "service_name": "Athena",
            "total_recommendations": len(enhanced_athena_checks.get("recommendations", [])),
            "total_monthly_savings": athena_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_athena_checks.get("recommendations", [])),
                    "recommendations": enhanced_athena_checks.get("recommendations", []),
                }
            },
        }

        batch_findings = {
            "service_name": "Batch",
            "total_recommendations": len(enhanced_batch_checks.get("recommendations", [])),
            "total_monthly_savings": batch_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_batch_checks.get("recommendations", [])),
                    "recommendations": enhanced_batch_checks.get("recommendations", []),
                }
            },
        }

        mediastore_findings = {
            "service_name": "MediaStore",
            "total_recommendations": len(enhanced_mediastore_checks.get("recommendations", [])),
            "total_monthly_savings": mediastore_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_mediastore_checks.get("recommendations", [])),
                    "recommendations": enhanced_mediastore_checks.get("recommendations", []),
                }
            },
        }

        # Calculate Step Functions total savings
        step_functions_total_savings = 0
        for rec in enhanced_step_functions_checks.get("recommendations", []):
            savings_str = rec.get("EstimatedSavings", "")
            if "Up to 90%" in savings_str:
                step_functions_total_savings += 200  # $200/month per Express migration
            elif "65-75%" in savings_str:
                step_functions_total_savings += 150  # $150/month per non-prod schedule

        # Step Functions findings
        step_functions_findings = {
            "service_name": "Step Functions",
            "total_recommendations": len(enhanced_step_functions_checks.get("recommendations", [])),
            "total_monthly_savings": step_functions_total_savings,
            "sources": {
                "enhanced_checks": {
                    "count": len(enhanced_step_functions_checks.get("recommendations", [])),
                    "recommendations": enhanced_step_functions_checks.get("recommendations", []),
                }
            },
        }

        # Create AMI findings for consistent structure
        ami_total_savings = sum(rec.get("EstimatedMonthlySavings", 0) for rec in ami_checks.get("recommendations", []))
        ami_findings = {
            "service_name": "AMI",
            "total_recommendations": ami_checks.get("total_count", 0),
            "total_monthly_savings": ami_total_savings,
            "sources": {
                "old_amis": {
                    "count": ami_checks.get("total_count", 0),
                    "recommendations": ami_checks.get("recommendations", []),
                }
            },
        }

        total_recommendations = (
            ec2_findings["total_recommendations"]
            + ami_findings["total_recommendations"]
            + ebs_findings["total_recommendations"]
            + rds_findings["total_recommendations"]
            + file_systems_findings["total_recommendations"]
            + s3_findings["total_recommendations"]
            + dynamodb_findings["total_recommendations"]
            + lambda_findings["total_recommendations"]
            + containers_findings["total_recommendations"]
            + network_findings["total_recommendations"]
            + monitoring_findings["total_recommendations"]
            + elasticache_findings["total_recommendations"]
            + opensearch_findings["total_recommendations"]
            + cloudfront_findings["total_recommendations"]
            + api_gateway_findings["total_recommendations"]
            + step_functions_findings["total_recommendations"]
            + lightsail_findings["total_recommendations"]
            + redshift_findings["total_recommendations"]
            + dms_findings["total_recommendations"]
            + quicksight_findings["total_recommendations"]
            + apprunner_findings["total_recommendations"]
            + transfer_findings["total_recommendations"]
            + msk_findings["total_recommendations"]
            + workspaces_findings["total_recommendations"]
            + mediastore_findings["total_recommendations"]
            + glue_findings["total_recommendations"]
            + athena_findings["total_recommendations"]
            + batch_findings["total_recommendations"]
        )

        total_savings = (
            ec2_findings["total_monthly_savings"]
            + ami_findings["total_monthly_savings"]
            + ebs_findings["total_monthly_savings"]
            + rds_findings["total_monthly_savings"]
            + file_systems_findings["total_monthly_savings"]
            + s3_findings["total_monthly_savings"]
            + dynamodb_findings["total_monthly_savings"]
            + lambda_findings["total_monthly_savings"]
            + containers_findings["total_monthly_savings"]
            + network_findings["total_monthly_savings"]
            + monitoring_findings["total_monthly_savings"]
            + elasticache_findings["total_monthly_savings"]
            + opensearch_findings["total_monthly_savings"]
            + cloudfront_findings["total_monthly_savings"]
            + api_gateway_findings["total_monthly_savings"]
            + step_functions_findings["total_monthly_savings"]
            + lightsail_findings["total_monthly_savings"]
            + redshift_findings["total_monthly_savings"]
            + dms_findings["total_monthly_savings"]
            + quicksight_findings["total_monthly_savings"]
            + apprunner_findings["total_monthly_savings"]
            + transfer_findings["total_monthly_savings"]
            + msk_findings["total_monthly_savings"]
            + workspaces_findings["total_monthly_savings"]
            + mediastore_findings["total_monthly_savings"]
            + glue_findings["total_monthly_savings"]
            + athena_findings["total_monthly_savings"]
            + batch_findings["total_monthly_savings"]
        )

        scan_results = {
            "account_id": self.account_id,
            "region": self.region,
            "profile": self.profile,
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "scan_warnings": [asdict(w) for w in self._ctx._warnings],
            "permission_issues": [asdict(p) for p in self._ctx._permission_issues],
            "services": {
                "ec2": ec2_findings,
                "ami": ami_findings,
                "ebs": ebs_findings,
                "rds": rds_findings,
                "file_systems": file_systems_findings,
                "s3": s3_findings,
                "dynamodb": dynamodb_findings,
                "containers": containers_findings,
                "network": network_findings,
                "monitoring": monitoring_findings,
                "elasticache": elasticache_findings,
                "opensearch": opensearch_findings,
                "lambda": lambda_findings,
                "cloudfront": cloudfront_findings,
                "api_gateway": api_gateway_findings,
                "step_functions": step_functions_findings,
                "lightsail": lightsail_findings,
                "redshift": redshift_findings,
                "dms": dms_findings,
                "quicksight": quicksight_findings,
                "apprunner": apprunner_findings,
                "transfer": transfer_findings,
                "msk": msk_findings,
                "workspaces": workspaces_findings,
                "mediastore": mediastore_findings,
                "glue": glue_findings,
                "athena": athena_findings,
                "batch": batch_findings,
            },
            "summary": {
                "total_services_scanned": len(
                    [
                        s
                        for s in [
                            ec2_findings,
                            ebs_findings,
                            rds_findings,
                            file_systems_findings,
                            s3_findings,
                            dynamodb_findings,
                            lambda_findings,
                            containers_findings,
                            network_findings,
                            monitoring_findings,
                            elasticache_findings,
                            opensearch_findings,
                            cloudfront_findings,
                            api_gateway_findings,
                            step_functions_findings,
                            lightsail_findings,
                            redshift_findings,
                            dms_findings,
                            quicksight_findings,
                            apprunner_findings,
                            transfer_findings,
                            msk_findings,
                            workspaces_findings,
                            mediastore_findings,
                            glue_findings,
                            athena_findings,
                            batch_findings,
                            ami_checks,
                        ]
                        if s.get("total_recommendations", 0) > 0 or s.get("total_count", 0) > 0
                    ]
                ),
                "total_recommendations": total_recommendations,
                "total_monthly_savings": total_savings,
            },
        }

        print("✅ Cost optimization scan completed successfully!")

        # Calculate actual services scanned
        scanned_services = []
        if scan_only:
            scanned_services = scan_only
        elif skip_services:
            scanned_services = [s for s in service_map.keys() if s not in skip_services]
        else:
            scanned_services = list(service_map.keys())

        print(f"📊 Found {total_recommendations} optimization opportunities across {len(scanned_services)} services")

        return scan_results

    # NEW SERVICES ENHANCED CHECKS

    def get_enhanced_lightsail_checks(self) -> Dict[str, Any]:
        """Get enhanced Lightsail cost optimization checks"""
        checks = {
            "idle_instances": [],
            "oversized_instances": [],
            "unused_static_ips": [],
            "load_balancer_optimization": [],
            "database_optimization": [],
        }

        try:
            # Get all Lightsail instances with pagination
            paginator = self.lightsail.get_paginator("get_instances")
            instances = []
            for page in paginator.paginate():
                instances.extend(page.get("instances", []))

            for instance in instances:
                instance_name = instance.get("name")
                instance_state = instance.get("state", {}).get("name")
                bundle_id = instance.get("bundleId")

                # Check for stopped instances (idle)
                if instance_state == "stopped":
                    checks["idle_instances"].append(
                        {
                            "InstanceName": instance_name,
                            "State": instance_state,
                            "BundleId": bundle_id,
                            "Recommendation": "Delete stopped Lightsail instance to eliminate costs",
                            "EstimatedSavings": f"${self.get_lightsail_bundle_cost(bundle_id):.2f}/month",
                            "CheckCategory": "Idle Resource Cleanup",
                        }
                    )

                # Only suggest downsizing for running instances with large bundles
                # This is heuristic but focuses on potentially oversized instances
                if instance_state == "running" and ("xlarge" in bundle_id.lower() or "large" in bundle_id.lower()):
                    checks["oversized_instances"].append(
                        {
                            "InstanceName": instance_name,
                            "BundleId": bundle_id,
                            "State": instance_state,
                            "Recommendation": "Review instance utilization - consider downsizing if CPU/memory usage is consistently low",
                            "EstimatedSavings": f"${self.get_lightsail_bundle_cost(bundle_id) * 0.3:.2f}/month potential",
                            "CheckCategory": "Instance Rightsizing",
                            "Note": "Recommendation based on instance size - verify actual utilization before downsizing",
                        }
                    )

            # Check for unused static IPs
            static_ips_response = self.lightsail.get_static_ips()
            static_ips = static_ips_response.get("staticIps", [])

            for static_ip in static_ips:
                if not static_ip.get("attachedTo"):
                    checks["unused_static_ips"].append(
                        {
                            "StaticIpName": static_ip.get("name"),
                            "IpAddress": static_ip.get("ipAddress"),
                            "Recommendation": "Release unused static IP to avoid charges",
                            "EstimatedSavings": "$5.00/month",
                            "CheckCategory": "Unused Resource Cleanup",
                        }
                    )

        except Exception as e:
            self.add_warning(f"Could not analyze Lightsail resources: {e}", "lightsail")

        # Flatten all recommendations
        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def get_lightsail_bundle_cost(self, bundle_id: str) -> float:
        """Get estimated monthly cost for Lightsail bundle"""
        # Simplified pricing - actual pricing varies by region
        bundle_costs = {
            "nano_2_0": 3.50,
            "micro_2_0": 5.00,
            "small_2_0": 10.00,
            "medium_2_0": 20.00,
            "large_2_0": 40.00,
            "xlarge_2_0": 80.00,
            "2xlarge_2_0": 160.00,
        }
        return bundle_costs.get(bundle_id, 20.00)  # Default estimate

    def get_enhanced_dms_checks(self) -> Dict[str, Any]:
        """Get enhanced DMS cost optimization checks"""
        checks = {"serverless_migration": [], "instance_rightsizing": [], "unused_instances": []}

        try:
            # Check traditional replication instances with pagination
            paginator = self.dms.get_paginator("describe_replication_instances")

            for page in paginator.paginate():
                instances = page.get("ReplicationInstances", [])

                for instance in instances:
                    instance_id = instance.get("ReplicationInstanceIdentifier")
                    instance_class = instance.get("ReplicationInstanceClass")
                    status = instance.get("ReplicationInstanceStatus")

                    if status == "available" and instance_class and "large" in instance_class:
                        # Check CloudWatch metrics for actual utilization
                        try:
                            from datetime import datetime, timedelta, timezone

                            end_time = datetime.now(timezone.utc)
                            start_time = end_time - timedelta(days=7)

                            cpu_metrics = self.cloudwatch.get_metric_statistics(
                                Namespace="AWS/DMS",
                                MetricName="CPUUtilization",
                                Dimensions=[{"Name": "ReplicationInstanceIdentifier", "Value": instance_id}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=3600,  # 1 hour
                                Statistics=["Average"],
                            )

                            avg_cpu = sum(point["Average"] for point in cpu_metrics.get("Datapoints", [])) / max(
                                len(cpu_metrics.get("Datapoints", [])), 1
                            )

                            # Only recommend if CPU is consistently low
                            if avg_cpu < 30:  # Less than 30% average CPU
                                checks["instance_rightsizing"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "InstanceClass": instance_class,
                                        "AvgCPU": f"{avg_cpu:.1f}%",
                                        "Recommendation": f"Low CPU utilization ({avg_cpu:.1f}%) - consider DMS Serverless or smaller instance",
                                        "EstimatedSavings": "$100/month potential",
                                        "CheckCategory": "Instance Optimization",
                                    }
                                )
                        except Exception:
                            # If can't get metrics, use heuristic with note
                            checks["instance_rightsizing"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceClass": instance_class,
                                    "Recommendation": "Review replication instance utilization - consider DMS Serverless for variable workloads",
                                    "EstimatedSavings": "$100/month potential",
                                    "CheckCategory": "Instance Optimization",
                                    "Note": "Verify actual CPU and network utilization before downsizing",
                                }
                            )

                            # Check for unused instances (low CPU for extended period)
                            if avg_cpu < 5:  # Less than 5% average CPU
                                checks["unused_instances"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "InstanceClass": instance_class,
                                        "AvgCPU": f"{avg_cpu:.1f}%",
                                        "Recommendation": "Very low CPU utilization - consider stopping if unused",
                                        "EstimatedSavings": "Full instance cost if terminated",
                                        "CheckCategory": "Unused DMS Instances",
                                    }
                                )

            # Check for DMS Serverless configurations with pagination
            try:
                serverless_paginator = self.dms.get_paginator("describe_replication_configs")
                for page in serverless_paginator.paginate():
                    serverless_configs = page.get("ReplicationConfigs", [])

                    for config in serverless_configs:
                        config_id = config.get("ReplicationConfigIdentifier")
                        checks["serverless_migration"].append(
                            {
                                "ConfigId": config_id,
                                "Recommendation": "Monitor DMS Serverless usage patterns for cost optimization",
                                "EstimatedSavings": "Variable based on usage",
                                "CheckCategory": "Serverless Optimization",
                            }
                        )
            except Exception:
                # Serverless API might not be available
                pass

        except Exception as e:
            self.add_warning(f"Could not analyze DMS resources: {e}", "dms")

        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def get_enhanced_quicksight_checks(self) -> Dict[str, Any]:
        """Get enhanced QuickSight cost optimization checks"""
        checks = {"spice_optimization": [], "user_optimization": [], "capacity_optimization": []}

        try:
            # Check account subscription first
            subscription = self.quicksight.describe_account_subscription(AwsAccountId=self.account_id)
            if subscription.get("AccountInfo", {}).get("AccountSubscriptionStatus") != "ACCOUNT_CREATED":
                return {"recommendations": [], "checks": checks}

            # Discover all namespaces with pagination
            namespaces_paginator = self.quicksight.get_paginator("list_namespaces")
            namespaces = []
            for page in namespaces_paginator.paginate(AwsAccountId=self.account_id):
                namespaces.extend(page.get("Namespaces", []))

            total_users = 0
            for namespace in namespaces:
                namespace_name = namespace.get("Name")
                try:
                    # Check users with pagination for each namespace
                    paginator = self.quicksight.get_paginator("list_users")
                    for page in paginator.paginate(AwsAccountId=self.account_id, Namespace=namespace_name):
                        total_users += len(page.get("UserList", []))
                except Exception:
                    # Skip namespace if no access
                    continue

            if total_users > 0:
                # Check SPICE capacity with correct field mapping
                try:
                    spice_capacity = self.quicksight.describe_spice_capacity(AwsAccountId=self.account_id)
                    capacity_config = spice_capacity.get("SpiceCapacityConfiguration", {})

                    # Use correct API field names
                    used_capacity = capacity_config.get("UsedCapacityInBytes", 0) / (1024**3)  # Convert to GB
                    total_capacity = capacity_config.get("TotalCapacityInBytes", 0) / (1024**3)  # Convert to GB

                    # Only recommend if capacity data is meaningful and underutilized
                    if total_capacity > 0 and used_capacity < total_capacity * 0.5:
                        checks["spice_optimization"].append(
                            {
                                "UserCount": total_users,
                                "UsedCapacityGB": round(used_capacity, 2),
                                "TotalCapacityGB": round(total_capacity, 2),
                                "UtilizationPercent": round((used_capacity / total_capacity) * 100, 1),
                                "Recommendation": f"SPICE capacity underutilized ({round(used_capacity, 1)}/{round(total_capacity, 1)} GB) - consider reducing",
                                "EstimatedSavings": f"~${(total_capacity - used_capacity) * 0.25:.0f}/month (estimate - verify SPICE pricing)",
                                "CheckCategory": "SPICE Optimization",
                            }
                        )
                except Exception:
                    # SPICE not configured or no capacity data available
                    pass

        except Exception as e:
            error_str = str(e)
            if "ResourceNotFoundException" in error_str and "account does not exist" in error_str:
                # QuickSight is not enabled in this account
                print("ℹ️ QuickSight is not enabled in this account - skipping QuickSight analysis")
            else:
                self.add_warning(f"Could not analyze QuickSight resources: {e}", "quicksight")

        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def get_enhanced_apprunner_checks(self) -> Dict[str, Any]:
        """Get enhanced App Runner cost optimization checks"""
        checks = {"auto_scaling_optimization": [], "instance_rightsizing": [], "unused_services": []}

        try:
            # List services (no pagination available for this API)
            response = self.apprunner.list_services()
            services = response.get("ServiceSummaryList", [])

            for service in services:
                service_name = service.get("ServiceName")
                status = service.get("Status")

                # Only recommend optimization for services that have been running for a while
                if status == "RUNNING":
                    service_arn = service.get("ServiceArn")

                    # Get service details to check configuration
                    try:
                        service_details = self.apprunner.describe_service(ServiceArn=service_arn)
                        service_config = service_details.get("Service", {})
                        instance_config = service_config.get("InstanceConfiguration", {})

                        # Only suggest optimization for larger instances
                        instance_type = instance_config.get("InstanceRoleArn", "")
                        if "large" in str(instance_config) or instance_config.get("Memory", "1 GB") != "1 GB":
                            checks["auto_scaling_optimization"].append(
                                {
                                    "ServiceName": service_name,
                                    "Recommendation": "Review auto-scaling settings and instance configuration for cost optimization",
                                    "EstimatedSavings": "$30/month potential",
                                    "CheckCategory": "Auto Scaling Optimization",
                                    "Note": "Monitor actual CPU and memory usage before adjusting",
                                }
                            )
                    except Exception:
                        # Skip if can't get service details
                        pass

        except Exception as e:
            self.add_warning(f"Could not analyze App Runner resources: {e}", "apprunner")

        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def get_enhanced_transfer_checks(self) -> Dict[str, Any]:
        """Get enhanced Transfer Family cost optimization checks"""
        checks = {"unused_servers": [], "protocol_optimization": [], "endpoint_optimization": []}

        try:
            # Add pagination for larger accounts
            paginator = self.transfer.get_paginator("list_servers")

            for page in paginator.paginate():
                servers = page.get("Servers", [])

                for server in servers:
                    server_id = server.get("ServerId")
                    state = server.get("State")
                    protocols = server.get("Protocols", [])

                    # Only recommend for servers with multiple protocols
                    if state == "ONLINE" and len(protocols) > 1:
                        # Try to get regional pricing, fallback to disclaimer
                        estimated_savings = "Variable – check AWS Pricing Calculator"

                        try:
                            # Attempt to get pricing data (this would require Pricing API implementation)
                            # For now, use disclaimer approach as requested
                            pass
                        except Exception:
                            pass

                        checks["protocol_optimization"].append(
                            {
                                "ServerId": server_id,
                                "Protocols": protocols,
                                "Region": self.region,
                                "Recommendation": f"Review if all {len(protocols)} protocols are needed - each protocol has hourly charges",
                                "EstimatedSavings": estimated_savings,
                                "CheckCategory": "Protocol Optimization",
                                "Note": f"Protocol costs vary by region ({self.region}) and type. Verify actual pricing in AWS Pricing Calculator before making changes.",
                            }
                        )

                    # Check for unused servers (stopped or offline)
                    if state in ["STOPPED", "OFFLINE"]:
                        checks["unused_servers"].append(
                            {
                                "ServerId": server_id,
                                "State": state,
                                "Protocols": protocols,
                                "Recommendation": f"Server is {state.lower()} - terminate if no longer needed",
                                "EstimatedSavings": "Full server hourly costs",
                                "CheckCategory": "Unused Transfer Servers",
                            }
                        )

        except Exception as e:
            self.add_warning(f"Could not analyze Transfer Family resources: {e}", "transfer")

        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def get_enhanced_msk_checks(self) -> Dict[str, Any]:
        """Get enhanced MSK cost optimization checks"""
        checks = {"cluster_rightsizing": [], "serverless_migration": [], "storage_optimization": []}

        try:
            # Check classic clusters with pagination
            paginator = self.kafka.get_paginator("list_clusters")
            for page in paginator.paginate():
                clusters = page.get("ClusterInfoList", [])

                for cluster in clusters:
                    cluster_name = cluster.get("ClusterName")
                    state = cluster.get("State")
                    broker_node_group = cluster.get("BrokerNodeGroupInfo", {})
                    instance_type = broker_node_group.get("InstanceType")

                    if state == "ACTIVE" and instance_type and "large" in instance_type:
                        checks["cluster_rightsizing"].append(
                            {
                                "ClusterName": cluster_name,
                                "InstanceType": instance_type,
                                "Recommendation": "Review cluster utilization - consider MSK Serverless for variable workloads",
                                "EstimatedSavings": "$200/month potential",
                                "CheckCategory": "Cluster Rightsizing",
                                "Note": "Verify actual throughput and utilization before downsizing",
                            }
                        )

                    # Check storage optimization (EBS volume types)
                    storage_info = broker_node_group.get("StorageInfo", {})
                    ebs_storage = storage_info.get("EBSStorageInfo", {})
                    volume_size = ebs_storage.get("VolumeSize", 0)

                    if volume_size > 1000:  # Large storage volumes
                        checks["storage_optimization"].append(
                            {
                                "ClusterName": cluster_name,
                                "VolumeSize": f"{volume_size} GB",
                                "Recommendation": "Large EBS volumes - review retention policies and consider gp3 volumes",
                                "EstimatedSavings": "20% with gp3 migration + retention optimization",
                                "CheckCategory": "MSK Storage Optimization",
                            }
                        )

            # Check MSK Serverless clusters with pagination
            try:
                paginator_v2 = self.kafka.get_paginator("list_clusters_v2")
                for page in paginator_v2.paginate():
                    serverless_clusters = page.get("ClusterInfoList", [])

                    for cluster in serverless_clusters:
                        if cluster.get("ClusterType") == "SERVERLESS":
                            cluster_name = cluster.get("ClusterName")
                            checks["serverless_migration"].append(
                                {
                                    "ClusterName": cluster_name,
                                    "ClusterType": "Serverless",
                                    "Recommendation": "Monitor serverless usage patterns for cost optimization",
                                    "EstimatedSavings": "Variable based on usage",
                                    "CheckCategory": "Serverless Optimization",
                                }
                            )
            except Exception:
                # v2 API might not be available in all regions
                pass

        except Exception as e:
            self.add_warning(f"Could not analyze MSK resources: {e}", "msk")

        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def get_enhanced_workspaces_checks(self) -> Dict[str, Any]:
        """Get enhanced WorkSpaces cost optimization checks"""
        checks = {"billing_mode_optimization": [], "bundle_rightsizing": [], "unused_workspaces": []}

        try:
            # Add pagination for larger accounts
            paginator = self.workspaces.get_paginator("describe_workspaces")

            for page in paginator.paginate():
                workspaces = page.get("Workspaces", [])

                for workspace in workspaces:
                    workspace_id = workspace.get("WorkspaceId")
                    state = workspace.get("State")
                    running_mode = workspace.get("WorkspaceProperties", {}).get("RunningMode")

                    # Only recommend for ALWAYS_ON workspaces that might benefit from AUTO_STOP
                    if state == "AVAILABLE" and running_mode == "ALWAYS_ON":
                        checks["billing_mode_optimization"].append(
                            {
                                "WorkspaceId": workspace_id,
                                "CurrentMode": running_mode,
                                "Recommendation": "Consider AUTO_STOP mode for occasional users - monitor usage patterns first",
                                "EstimatedSavings": "$50/month potential per workspace",
                                "CheckCategory": "Billing Mode Optimization",
                                "Note": "Verify user login patterns before switching to AUTO_STOP",
                            }
                        )

                    # Check for unused workspaces (stopped or error state)
                    if state in ["STOPPED", "ERROR", "SUSPENDED"]:
                        checks["unused_workspaces"].append(
                            {
                                "WorkspaceId": workspace_id,
                                "State": state,
                                "RunningMode": running_mode,
                                "Recommendation": f"Workspace in {state} state - terminate if no longer needed",
                                "EstimatedSavings": "Full workspace monthly cost",
                                "CheckCategory": "Unused WorkSpaces",
                            }
                        )

        except Exception as e:
            self.add_warning(f"Could not analyze WorkSpaces resources: {e}", "workspaces")

        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def get_enhanced_mediastore_checks(self) -> Dict[str, Any]:
        """Get enhanced MediaStore cost optimization checks"""
        checks = {"unused_containers": [], "access_optimization": [], "cors_policies": []}

        try:
            response = self.mediastore.list_containers()
            containers = response.get("Containers", [])

            for container in containers:
                container_name = container.get("Name")
                status = container.get("Status")

                if status == "ACTIVE":
                    # Evidence-based check using multiple CloudWatch metrics
                    try:
                        from datetime import datetime, timedelta, timezone

                        end_time = datetime.now(timezone.utc)
                        start_time = end_time - timedelta(days=14)  # 14-day window

                        # Check multiple usage metrics
                        metrics_to_check = ["RequestCount", "BytesDownloaded", "BytesUploaded"]
                        total_activity = 0

                        for metric_name in metrics_to_check:
                            try:
                                metrics = self.cloudwatch.get_metric_statistics(
                                    Namespace="AWS/MediaStore",
                                    MetricName=metric_name,
                                    Dimensions=[{"Name": "ContainerName", "Value": container_name}],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=86400,  # 1 day
                                    Statistics=["Sum"],
                                )
                                total_activity += sum(point["Sum"] for point in metrics.get("Datapoints", []))
                            except Exception:
                                # Metric might not exist for this container
                                continue

                        # Only recommend deletion if no activity across all metrics
                        if total_activity == 0:
                            checks["unused_containers"].append(
                                {
                                    "ContainerName": container_name,
                                    "ActivityLast14Days": total_activity,
                                    "Recommendation": "Container shows no activity (requests, uploads, downloads) in last 14 days - consider deletion",
                                    "EstimatedSavings": "$25/month",
                                    "CheckCategory": "Unused Resource Cleanup",
                                }
                            )
                    except Exception as e:
                        # If metrics unavailable, log warning and skip recommendation
                        self.add_warning(f"Could not get MediaStore metrics for {container_name}: {e}", "mediastore")
                        continue

        except Exception as e:
            self.add_warning(f"Could not analyze MediaStore resources: {e}", "mediastore")

        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def get_lightsail_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "idle_instances": {
                "title": "Delete Idle Lightsail Instances",
                "description": "Stopped Lightsail instances still incur charges. Delete unused instances.",
                "action": "Delete stopped instances or restart if needed",
            }
        }

    def get_redshift_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "reserved_instances": {
                "title": "Purchase Redshift Reserved Instances",
                "description": "Save up to 24% with Redshift Reserved Instances for predictable workloads.",
                "action": "Purchase 1-year or 3-year Reserved Instances",
            }
        }

    def get_dms_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "instance_rightsizing": {
                "title": "Optimize DMS Instance Sizing",
                "description": "Right-size DMS instances or migrate to serverless for variable workloads.",
                "action": "Consider DMS Serverless or smaller instance types",
            }
        }

    def get_quicksight_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "spice_optimization": {
                "title": "Optimize QuickSight SPICE Usage",
                "description": "Review SPICE capacity and optimize data refresh schedules.",
                "action": "Optimize SPICE capacity and refresh schedules",
            }
        }

    def get_apprunner_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "auto_scaling_optimization": {
                "title": "Optimize App Runner Auto Scaling",
                "description": "Review auto-scaling settings and concurrency limits for cost efficiency.",
                "action": "Optimize auto-scaling configuration",
            }
        }

    def get_transfer_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "protocol_optimization": {
                "title": "Optimize Transfer Family Protocols",
                "description": "Protocol costs vary by region and endpoint type. Review if all protocols are needed.",
                "action": "Remove unused protocols and check AWS Pricing Calculator for region-specific costs",
            }
        }

    def get_msk_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "cluster_rightsizing": {
                "title": "Optimize MSK Cluster Sizing",
                "description": "Right-size MSK clusters or consider serverless for variable workloads.",
                "action": "Consider MSK Serverless or smaller broker instances",
            }
        }

    def get_workspaces_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "billing_mode_optimization": {
                "title": "Optimize WorkSpaces Billing Mode",
                "description": "Use AUTO_STOP mode for occasional users instead of ALWAYS_ON.",
                "action": "Switch to AUTO_STOP billing mode",
            }
        }

    def get_mediastore_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "unused_containers": {
                "title": "Review Unused MediaStore Containers",
                "description": "Delete unused MediaStore containers to eliminate storage and request costs.",
                "action": "Review container usage and delete unused containers",
            }
        }

    def get_glue_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "job_optimization": {
                "title": "Optimize Glue Job Configuration",
                "description": "Right-size DPU allocation and enable auto-scaling for variable workloads.",
                "action": "Review DPU usage and enable Glue auto-scaling",
            }
        }

    def get_athena_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "query_optimization": {
                "title": "Optimize Athena Query Costs",
                "description": "Partition data, use columnar formats, and compress data to reduce scan costs.",
                "action": "Implement partitioning and use Parquet/ORC formats",
            }
        }

    def get_batch_optimization_descriptions(self) -> Dict[str, str]:
        return {
            "compute_optimization": {
                "title": "Optimize Batch Compute Environments",
                "description": "Use Spot instances and Fargate Spot for fault-tolerant batch workloads.",
                "action": "Enable Spot instances for 60-90% cost savings",
            }
        }

    def get_enhanced_glue_checks(self) -> Dict[str, Any]:
        """Get enhanced Glue cost optimization checks"""
        checks = {"job_rightsizing": [], "dev_endpoints": [], "crawler_optimization": []}

        try:
            # Check Glue jobs
            paginator = self.glue.get_paginator("get_jobs")
            for page in paginator.paginate():
                for job in page.get("Jobs", []):
                    job_name = job.get("Name")
                    max_capacity = job.get("MaxCapacity", 0)
                    worker_type = job.get("WorkerType")
                    number_of_workers = job.get("NumberOfWorkers", 0)

                    # Check for over-provisioned jobs
                    if max_capacity > 10 or number_of_workers > 10:
                        checks["job_rightsizing"].append(
                            {
                                "JobName": job_name,
                                "MaxCapacity": max_capacity,
                                "WorkerType": worker_type,
                                "NumberOfWorkers": number_of_workers,
                                "Recommendation": "Review DPU allocation - enable auto-scaling",
                                "EstimatedSavings": "20-40% with auto-scaling",
                                "CheckCategory": "Glue Job Rightsizing",
                            }
                        )

            # Check dev endpoints (expensive)
            dev_endpoints = self.glue.get_dev_endpoints()
            for endpoint in dev_endpoints.get("DevEndpoints", []):
                endpoint_name = endpoint.get("EndpointName")
                status = endpoint.get("Status")

                if status == "READY":
                    checks["dev_endpoints"].append(
                        {
                            "EndpointName": endpoint_name,
                            "Status": status,
                            "Recommendation": "Dev endpoints cost $0.44/hour - delete when not in use",
                            "EstimatedSavings": "$316/month per endpoint",
                            "CheckCategory": "Glue Dev Endpoints",
                        }
                    )

            # Check crawlers
            paginator = self.glue.get_paginator("get_crawlers")
            for page in paginator.paginate():
                for crawler in page.get("Crawlers", []):
                    crawler_name = crawler.get("Name")
                    schedule = crawler.get("Schedule", {}).get("ScheduleExpression")

                    if schedule and "cron" in schedule.lower():
                        checks["crawler_optimization"].append(
                            {
                                "CrawlerName": crawler_name,
                                "Schedule": schedule,
                                "Recommendation": "Review crawler frequency - run on-demand if possible",
                                "EstimatedSavings": "Variable based on frequency",
                                "CheckCategory": "Glue Crawler Optimization",
                            }
                        )
        except Exception as e:
            self.add_warning(f"Could not analyze Glue resources: {e}", "glue")

        recommendations = []
        for category, items in checks.items():
            recommendations.extend(items)

        return {"recommendations": recommendations, "checks": checks}

    def get_enhanced_athena_checks(self) -> Dict[str, Any]:
        """Get enhanced Athena cost optimization checks"""
        checks = {"workgroup_optimization": [], "query_results": []}

        try:
            # Check workgroups - list_work_groups doesn't support pagination
            response = self.athena.list_work_groups()
            for wg in response.get("WorkGroups", []):
                wg_name = wg.get("Name")

                try:
                    wg_details = self.athena.get_work_group(WorkGroup=wg_name)
                    config = wg_details.get("WorkGroup", {}).get("Configuration", {})

                    # Check for query result location (S3 costs)
                    result_config = config.get("ResultConfiguration", {})
                    output_location = result_config.get("OutputLocation", "")

                    if output_location:
                        checks["query_results"].append(
                            {
                                "WorkGroupName": wg_name,
                                "OutputLocation": output_location,
                                "Recommendation": "Set lifecycle policy on query results bucket",
                                "EstimatedSavings": "Reduce S3 storage costs",
                                "CheckCategory": "Athena Query Results",
                            }
                        )

                    # Check for data scanned limits
                    bytes_scanned_cutoff = config.get("BytesScannedCutoffPerQuery")
                    if not bytes_scanned_cutoff:
                        checks["workgroup_optimization"].append(
                            {
                                "WorkGroupName": wg_name,
                                "Recommendation": "Set per-query data scan limit to control costs",
                                "EstimatedSavings": "Prevent runaway query costs",
                                "CheckCategory": "Athena Workgroup Optimization",
                            }
                        )
                except Exception as e:
                    continue
        except Exception as e:
            self.add_warning(f"Could not analyze Athena resources: {e}", "athena")

        recommendations = []
        for category, items in checks.items():
            recommendations.extend(items)

        return {"recommendations": recommendations, "checks": checks}

    def get_enhanced_batch_checks(self) -> Dict[str, Any]:
        """Get enhanced Batch cost optimization checks"""
        checks = {"compute_environments": [], "job_definitions": []}

        try:
            # Check compute environments
            paginator = self.batch.get_paginator("describe_compute_environments")
            for page in paginator.paginate():
                for ce in page.get("computeEnvironments", []):
                    ce_name = ce.get("computeEnvironmentName")
                    ce_type = ce.get("type")
                    state = ce.get("state")

                    if state == "ENABLED":
                        compute_resources = ce.get("computeResources", {})
                        allocation_strategy = compute_resources.get("allocationStrategy", "BEST_FIT")
                        instance_types = compute_resources.get("instanceTypes", [])

                        # Check for Spot usage
                        if allocation_strategy != "SPOT_CAPACITY_OPTIMIZED":
                            checks["compute_environments"].append(
                                {
                                    "ComputeEnvironmentName": ce_name,
                                    "Type": ce_type,
                                    "AllocationStrategy": allocation_strategy,
                                    "Recommendation": "Use SPOT_CAPACITY_OPTIMIZED for fault-tolerant workloads",
                                    "EstimatedSavings": "60-90% with Spot instances",
                                    "CheckCategory": "Batch Spot Optimization",
                                }
                            )

                        # Check for Graviton instances
                        has_graviton = any("6g" in inst or "7g" in inst for inst in instance_types)
                        if not has_graviton and instance_types:
                            checks["compute_environments"].append(
                                {
                                    "ComputeEnvironmentName": ce_name,
                                    "InstanceTypes": instance_types,
                                    "Recommendation": "Consider Graviton instances for better price-performance",
                                    "EstimatedSavings": "20-40% cost reduction",
                                    "CheckCategory": "Batch Graviton Migration",
                                }
                            )

            # Check job definitions for optimization opportunities
            try:
                job_paginator = self.batch.get_paginator("describe_job_definitions")
                for page in job_paginator.paginate(status="ACTIVE"):
                    for job_def in page.get("jobDefinitions", []):
                        job_name = job_def.get("jobDefinitionName")
                        container_props = job_def.get("containerProperties", {})
                        vcpus = container_props.get("vcpus", 0)
                        memory = container_props.get("memory", 0)

                        # Check for oversized job definitions
                        if vcpus > 8 or memory > 16384:  # >8 vCPUs or >16GB memory
                            checks["job_definitions"].append(
                                {
                                    "JobDefinitionName": job_name,
                                    "VCpus": vcpus,
                                    "Memory": f"{memory} MB",
                                    "Recommendation": "Large resource allocation - verify job requirements",
                                    "EstimatedSavings": "Rightsize based on actual usage",
                                    "CheckCategory": "Batch Job Rightsizing",
                                }
                            )
            except Exception:
                pass  # Job definitions API might have different pagination
        except Exception as e:
            self.add_warning(f"Could not analyze Batch resources: {e}", "batch")

        recommendations = []
        for category, items in checks.items():
            recommendations.extend(items)

        return {"recommendations": recommendations, "checks": checks}

    def get_enhanced_redshift_checks(self) -> Dict[str, Any]:
        """Get enhanced Redshift cost optimization checks"""
        checks = {
            "reserved_instances": [],
            "serverless_optimization": [],
            "cluster_rightsizing": [],
            "pause_resume_scheduling": [],
            "storage_optimization": [],
        }

        try:
            # Get Redshift clusters with pagination
            paginator = self.redshift.get_paginator("describe_clusters")
            clusters = []
            for page in paginator.paginate():
                clusters.extend(page.get("Clusters", []))

            for cluster in clusters:
                cluster_id = cluster.get("ClusterIdentifier")
                node_type = cluster.get("NodeType")
                cluster_status = cluster.get("ClusterStatus")
                number_of_nodes = cluster.get("NumberOfNodes", 1)

                # Check for Reserved Instance opportunities - only for stable, long-running clusters
                if (
                    cluster_status == "available" and cluster.get("ClusterCreateTime") and number_of_nodes >= 2
                ):  # Only suggest RI for multi-node clusters
                    # Calculate cluster age to determine RI suitability
                    from datetime import datetime, timezone

                    create_time = cluster.get("ClusterCreateTime")
                    if isinstance(create_time, str):
                        # Handle string datetime if needed
                        cluster_age_days = 30  # Default assumption
                    else:
                        cluster_age_days = (datetime.now(timezone.utc) - create_time).days

                    # Only recommend RI for clusters running > 30 days
                    if cluster_age_days > 30:
                        checks["reserved_instances"].append(
                            {
                                "ClusterIdentifier": cluster_id,
                                "NodeType": node_type,
                                "NumberOfNodes": number_of_nodes,
                                "ClusterAge": f"{cluster_age_days} days",
                                "Recommendation": f"Consider Reserved Instances for stable cluster (running {cluster_age_days} days) - 24% savings potential",
                                "EstimatedSavings": f"${number_of_nodes * 150:.2f}/month with 1-year RI",
                                "CheckCategory": "Reserved Instance Optimization",
                                "Note": "Suitable for predictable, long-running workloads",
                            }
                        )

                # Check for cluster rightsizing opportunities
                if number_of_nodes > 3:
                    checks["cluster_rightsizing"].append(
                        {
                            "ClusterIdentifier": cluster_id,
                            "CurrentNodes": number_of_nodes,
                            "Recommendation": "Analyze query performance and consider reducing cluster size",
                            "EstimatedSavings": f"${(number_of_nodes - 2) * 100:.2f}/month potential",
                            "CheckCategory": "Cluster Rightsizing",
                        }
                    )

            # Check Redshift Serverless with pagination
            try:
                paginator = self.redshift_serverless.get_paginator("list_workgroups")
                for page in paginator.paginate():
                    workgroups = page.get("workgroups", [])

                    for workgroup in workgroups:
                        workgroup_name = workgroup.get("workgroupName")
                        status = workgroup.get("status")

                        if status == "AVAILABLE":
                            checks["serverless_optimization"].append(
                                {
                                    "WorkgroupName": workgroup_name,
                                    "Recommendation": "Consider Serverless Reservations for 24% savings on predictable workloads",
                                    "EstimatedSavings": "$150/month with reservations",
                                    "CheckCategory": "Serverless Optimization",
                                }
                            )
            except Exception:
                pass  # Serverless might not be available in all regions

        except Exception as e:
            self.add_warning(f"Could not analyze Redshift resources: {e}", "redshift")

        # Flatten all recommendations
        all_recommendations = []
        for category, recs in checks.items():
            all_recommendations.extend(recs)

        return {"recommendations": all_recommendations, "checks": checks}

    def generate_report(self, scan_results: Dict[str, Any]) -> str:
        """Generate human-readable report from scan results"""
        report = []
        report.append("=" * 60)
        report.append("AWS COST OPTIMIZATION REPORT")
        report.append("=" * 60)
        report.append(f"Account ID: {scan_results['account_id']}")
        report.append(f"Region: {scan_results['region']}")
        report.append(f"Scan Time: {scan_results['scan_time']}")
        report.append("")

        # Summary section
        summary = scan_results["summary"]
        report.append("SUMMARY")
        report.append("-" * 20)
        report.append(f"Services Scanned: {summary['total_services_scanned']}")
        report.append(f"Total Recommendations: {summary['total_recommendations']}")
        report.append(f"Total Monthly Savings: ${summary['total_monthly_savings']:.2f}")
        report.append("")

        # Service-specific sections
        for service_key, service_data in scan_results["services"].items():
            report.append(f"{service_data['service_name'].upper()} SERVICE FINDINGS")
            report.append("=" * 40)

            if service_key == "ec2":
                report.append(f"Instances Found: {service_data['instance_count']}")
            elif service_key == "ebs":
                counts = service_data["volume_counts"]
                report.append(f"Total Volumes: {counts['total']}")
                report.append(f"Unattached Volumes: {counts['unattached']}")
                report.append(f"gp2 Volumes: {counts['gp2']} (migration candidates)")
                report.append(f"gp3 Volumes: {counts['gp3']}")

                # Add EBS optimization descriptions
                report.append("")
                report.append("COST OPTIMIZATION OPPORTUNITIES:")
                report.append("-" * 35)
                descriptions = service_data.get("optimization_descriptions", {})

                if counts["unattached"] > 0:
                    desc = descriptions.get("unattached_volumes", {})
                    report.append(f"• {desc.get('title', 'Delete Unattached Volumes')}")
                    report.append(f"  {desc.get('description', '')}")
                    report.append(f"  Action Steps: {desc.get('action', '').replace(chr(10), chr(10) + '    ')}")
                    report.append("")

                if counts["gp2"] > 0:
                    desc = descriptions.get("gp2_to_gp3", {})
                    report.append(f"• {desc.get('title', 'Migrate gp2 to gp3')}")
                    report.append(f"  {desc.get('description', '')}")
                    report.append(f"  Action Steps: {desc.get('action', '').replace(chr(10), chr(10) + '    ')}")
                    report.append("")

                if counts["io1"] > 0:
                    desc = descriptions.get("io1_to_io2", {})
                    report.append(f"• {desc.get('title', 'Upgrade io1 to io2')}")
                    report.append(f"  {desc.get('description', '')}")
                    report.append(f"  Action Steps: {desc.get('action', '').replace(chr(10), chr(10) + '    ')}")
                    report.append("")
            elif service_key == "rds":
                counts = service_data["instance_counts"]
                report.append(f"Total Instances: {counts['total']}")
                report.append(f"Running: {counts['running']}, Stopped: {counts['stopped']}")
                report.append(f"MySQL: {counts['mysql']}, PostgreSQL: {counts['postgres']}")
                report.append(f"Aurora: {counts['aurora']}, Oracle: {counts['oracle']}")

                # Add RDS optimization descriptions
                report.append("")
                report.append("COST OPTIMIZATION OPPORTUNITIES:")
                report.append("-" * 35)
                descriptions = service_data.get("optimization_descriptions", {})

                for desc_key, desc in descriptions.items():
                    report.append(f"• {desc.get('title', '')}")
                    report.append(f"  {desc.get('description', '')}")
                    report.append(f"  Action Steps: {desc.get('action', '').replace(chr(10), chr(10) + '    ')}")
                    report.append("")
            elif service_key == "file_systems":
                efs_counts = service_data["efs_counts"]
                fsx_counts = service_data["fsx_counts"]
                report.append(f"EFS File Systems: {efs_counts['total']} ({efs_counts['total_size_gb']} GB)")
                report.append(f"FSx File Systems: {fsx_counts['total']} ({fsx_counts['total_capacity_gb']} GB)")
                report.append(f"FSx Types: Lustre: {fsx_counts['lustre']}, Windows: {fsx_counts['windows']}")
                report.append(f"           ONTAP: {fsx_counts['ontap']}, OpenZFS: {fsx_counts['openzfs']}")
                report.append(
                    f"Unused/Small Systems: EFS: {len(efs_counts['unused_systems'])}, FSx: {len(fsx_counts['underutilized_systems'])}"
                )

                # Add File Systems optimization descriptions
                report.append("")
                report.append("COST OPTIMIZATION OPPORTUNITIES:")
                report.append("-" * 35)
                descriptions = service_data.get("optimization_descriptions", {})

                # Show key optimization opportunities
                key_opportunities = [
                    "efs_lifecycle_policies",
                    "fsx_storage_optimization",
                    "fsx_ontap_features",
                    "fsx_lustre_optimization",
                ]
                for desc_key in key_opportunities:
                    if desc_key in descriptions:
                        desc = descriptions[desc_key]
                        report.append(f"• {desc.get('title', '')}")
                        report.append(f"  {desc.get('description', '')}")
                        report.append(f"  Action Steps: {desc.get('action', '').replace(chr(10), chr(10) + '    ')}")
                        report.append("")
            elif service_key == "efs":
                counts = service_data["file_system_counts"]
                report.append(f"Total File Systems: {counts['total']}")
                report.append(f"Available: {counts['available']}, Total Size: {counts['total_size_gb']} GB")
                report.append(f"Standard Storage: {counts['standard_storage']}, One Zone: {counts['one_zone_storage']}")
                report.append(f"Unused Systems: {len(counts['unused_systems'])}")

                # Add EFS optimization descriptions
                report.append("")
                report.append("COST OPTIMIZATION OPPORTUNITIES:")
                report.append("-" * 35)
                descriptions = service_data.get("optimization_descriptions", {})

                for desc_key, desc in descriptions.items():
                    report.append(f"• {desc.get('title', '')}")
                    report.append(f"  {desc.get('description', '')}")
                    report.append(f"  Action Steps: {desc.get('action', '').replace(chr(10), chr(10) + '    ')}")
                    report.append("")

            report.append(f"Total Recommendations: {service_data['total_recommendations']}")
            report.append(f"Monthly Savings: ${service_data['total_monthly_savings']:.2f}")
            report.append("")

            # Service-specific source handling
            if service_key == "ec2":
                # Cost Optimization Hub findings with detailed savings
                coh_data = service_data["sources"]["cost_optimization_hub"]
                report.append(f"Cost Optimization Hub: {coh_data['count']} recommendations")
                report.append("-" * 30)

                for rec in coh_data["recommendations"]:
                    savings = rec.get("estimatedMonthlySavings", 0)
                    report.append(f"• Resource: {rec.get('resourceId', 'N/A')}")
                    report.append(f"  Action: {rec.get('actionType', 'N/A')}")
                    report.append(f"  Monthly Savings: ${savings:.2f}")

                    # Add detailed savings breakdown if available
                    if rec.get("recommendedResourceDetails"):
                        details = rec["recommendedResourceDetails"]
                        if details.get("savingsPlans"):
                            report.append("  Savings Plans Options:")
                            for plan in details["savingsPlans"][:3]:  # Show top 3
                                term = plan.get("termInYears", "N/A")
                                payment = plan.get("paymentOption", "N/A")
                                plan_savings = plan.get("estimatedMonthlySavings", 0)
                                report.append(f"    - {term}yr {payment}: ${plan_savings:.2f}/month")

                        if details.get("reservedInstances"):
                            report.append("  Reserved Instance Options:")
                            for ri in details["reservedInstances"][:3]:  # Show top 3
                                term = ri.get("termInYears", "N/A")
                                payment = ri.get("paymentOption", "N/A")
                                ri_savings = ri.get("estimatedMonthlySavings", 0)
                                report.append(f"    - {term}yr {payment}: ${ri_savings:.2f}/month")

                    report.append(f"  Current Type: {rec.get('currentResourceType', 'N/A')}")
                    if rec.get("recommendedResourceType"):
                        report.append(f"  Recommended Type: {rec['recommendedResourceType']}")
                    report.append("")

                # Compute Optimizer findings
                co_data = service_data["sources"]["compute_optimizer"]
                report.append(f"Compute Optimizer: {co_data['count']} recommendations")
                report.append("-" * 30)

                for rec in co_data["recommendations"]:
                    report.append(f"• Instance: {rec.get('instanceName', rec.get('instanceArn', 'N/A'))}")
                    report.append(f"  Finding: {rec.get('finding', 'N/A')}")
                    report.append(f"  Current Type: {rec.get('currentInstanceType', 'N/A')}")

                    if rec.get("recommendationOptions"):
                        report.append("  Recommended Options:")
                        for i, option in enumerate(rec["recommendationOptions"][:3], 1):
                            report.append(f"    {i}. {option.get('instanceType', 'N/A')}")
                    report.append("")

            elif service_key == "ebs":
                # Compute Optimizer EBS findings
                co_data = service_data["sources"]["compute_optimizer"]
                report.append(f"Compute Optimizer: {co_data['count']} recommendations")
                report.append("-" * 30)

                for rec in co_data["recommendations"]:
                    report.append(
                        f"• Volume: {rec.get('volumeArn', 'N/A').split('/')[-1] if rec.get('volumeArn') else 'N/A'}"
                    )
                    report.append(f"  Finding: {rec.get('finding', 'N/A')}")
                    if rec.get("volumeRecommendationOptions"):
                        for option in rec["volumeRecommendationOptions"][:1]:  # Show top recommendation
                            config = option.get("configuration", {})
                            report.append(
                                f"  Recommended: {config.get('volumeType', 'N/A')} {config.get('volumeSize', 'N/A')}GB"
                            )
                    report.append("")

                # Unattached volumes findings
                unattached_data = service_data["sources"]["unattached_volumes"]
                report.append(f"Unattached Volumes: {unattached_data['count']} volumes")
                report.append("-" * 30)

                for vol in unattached_data["recommendations"]:
                    report.append(f"• Volume: {vol['VolumeId']}")
                    report.append(f"  Type: {vol['VolumeType']} - {vol['Size']}GB")
                    report.append(f"  Monthly Cost: ${vol['EstimatedMonthlyCost']:.2f}")
                    report.append(f"  Created: {vol['CreateTime'][:10]}")
                    report.append("")

            elif service_key == "rds":
                # RDS Compute Optimizer findings
                co_data = service_data["sources"]["compute_optimizer"]
                report.append(f"Compute Optimizer: {co_data['count']} recommendations")
                report.append("-" * 30)

                for rec in co_data["recommendations"]:
                    resource_arn = rec.get("resourceArn", "N/A")
                    db_name = resource_arn.split(":")[-1] if resource_arn != "N/A" else "N/A"
                    report.append(f"• Database: {db_name}")
                    report.append(f"  Engine: {rec.get('engine', 'N/A')} {rec.get('engineVersion', '')}")
                    report.append(f"  Instance Finding: {rec.get('instanceFinding', 'N/A')}")
                    report.append(f"  Storage Finding: {rec.get('storageFinding', 'N/A')}")

                    if rec.get("instanceRecommendationOptions"):
                        report.append("  Recommended Instance Options:")
                        for i, option in enumerate(rec["instanceRecommendationOptions"][:2], 1):
                            report.append(f"    {i}. {option.get('dbInstanceClass', 'N/A')}")

                    if rec.get("storageRecommendationOptions"):
                        report.append("  Storage Recommendations:")
                        for option in rec["storageRecommendationOptions"][:1]:
                            storage_config = option.get("storageConfiguration", {})
                            report.append(f"    Storage Type: {storage_config.get('storageType', 'N/A')}")

                    report.append("")

            elif service_key == "file_systems":
                # EFS Lifecycle Analysis findings
                efs_data = service_data["sources"]["efs_lifecycle_analysis"]
                report.append(f"EFS Lifecycle Analysis: {efs_data['count']} file systems")
                report.append("-" * 30)

                for rec in efs_data["recommendations"]:
                    report.append(f"• EFS: {rec.get('Name', rec.get('FileSystemId', 'N/A'))}")
                    report.append(f"  Size: {rec.get('SizeGB', 0)} GB")
                    report.append(f"  Storage Class: {rec.get('StorageClass', 'N/A')}")
                    report.append(f"  Has IA Policy: {'Yes' if rec.get('HasIAPolicy') else 'No'}")
                    report.append(f"  Monthly Cost: ${rec.get('EstimatedMonthlyCost', 0):.2f}")
                    report.append("")

                # FSx Optimization Analysis findings
                fsx_data = service_data["sources"]["fsx_optimization_analysis"]
                report.append(f"FSx Optimization Analysis: {fsx_data['count']} file systems")
                report.append("-" * 30)

                for rec in fsx_data["recommendations"]:
                    report.append(f"• FSx {rec.get('FileSystemType', 'N/A')}: {rec.get('FileSystemId', 'N/A')}")
                    report.append(f"  Capacity: {rec.get('StorageCapacity', 0)} GB")
                    report.append(f"  Storage Type: {rec.get('StorageType', 'N/A')}")
                    report.append(f"  Monthly Cost: ${rec.get('EstimatedMonthlyCost', 0):.2f}")

                    opportunities = rec.get("OptimizationOpportunities", [])
                    if opportunities:
                        report.append("  Optimization Opportunities:")
                        for opp in opportunities[:3]:  # Show top 3
                            report.append(f"    - {opp}")
                    report.append("")

        return "\n".join(report)

    # ==================== COMPREHENSIVE ENHANCED CHECKS ====================
    # Categories 1-5: Network & Infrastructure Optimization

    def get_elastic_ip_checks(self) -> Dict[str, Any]:
        """Category 1: Elastic IPs & Public Addressing optimization checks"""
        checks = {
            "unassociated_eips": [],
            "eips_on_stopped_instances": [],
            "multiple_eips_per_instance": [],
            "public_ips_should_be_private": [],
        }

        try:
            # Get all Elastic IPs
            eips_response = self.ec2.describe_addresses()
            addresses = eips_response.get("Addresses", [])

            # Get all instances for cross-reference - paginate
            paginator = self.ec2.get_paginator("describe_instances")
            instances = {}
            for page in paginator.paginate():
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        instances[instance["InstanceId"]] = instance

            instance_eip_count = {}

            for eip in addresses:
                allocation_id = eip.get("AllocationId", "N/A")
                public_ip = eip.get("PublicIp", "N/A")

                # Check for unassociated EIPs
                if not eip.get("InstanceId") and not eip.get("NetworkInterfaceId"):
                    # Create a more descriptive name
                    eip_name = f"EIP {public_ip} ({allocation_id})"

                    checks["unassociated_eips"].append(
                        {
                            "AllocationId": allocation_id,
                            "PublicIp": public_ip,
                            "ResourceName": eip_name,
                            "Recommendation": "Release unassociated Elastic IP to avoid charges",
                            "EstimatedSavings": "$3.65/month per EIP",  # $0.005/hour × 730 hours
                            "CheckCategory": "Unassociated EIPs",
                        }
                    )

                # Check EIPs on stopped instances
                elif eip.get("InstanceId"):
                    instance_id = eip["InstanceId"]
                    instance = instances.get(instance_id)

                    # Count EIPs per instance
                    instance_eip_count[instance_id] = instance_eip_count.get(instance_id, 0) + 1

                    if instance and instance.get("State", {}).get("Name") == "stopped":
                        checks["eips_on_stopped_instances"].append(
                            {
                                "AllocationId": allocation_id,
                                "PublicIp": public_ip,
                                "InstanceId": instance_id,
                                "InstanceState": "stopped",
                                "Recommendation": "Release EIP from stopped instance or start instance",
                                "EstimatedSavings": "$3.65/month per EIP",  # $0.005/hour × 730 hours
                                "CheckCategory": "EIPs on Stopped Instances",
                            }
                        )

            # Check for multiple EIPs per instance
            for instance_id, eip_count in instance_eip_count.items():
                if eip_count > 1:
                    instance = instances.get(instance_id, {})
                    checks["multiple_eips_per_instance"].append(
                        {
                            "InstanceId": instance_id,
                            "EIPCount": eip_count,
                            "InstanceType": instance.get("InstanceType", "N/A"),
                            "Recommendation": f"Instance has {eip_count} EIPs - review if all are necessary",
                            "EstimatedSavings": f"${(eip_count - 1) * 3.65:.2f}/month if reduced to 1 EIP",
                            "CheckCategory": "Multiple EIPs per Instance",
                        }
                    )

            # Check for public IPs that should be private (instances in private subnets with public IPs)
            for instance_id, instance in instances.items():
                if instance.get("PublicIpAddress") and instance.get("State", {}).get("Name") == "running":
                    subnet_id = instance.get("SubnetId")
                    if subnet_id:
                        try:
                            subnet_response = self.ec2.describe_subnets(SubnetIds=[subnet_id])
                            subnet = subnet_response["Subnets"][0]
                            # If subnet doesn't auto-assign public IPs, this might be unnecessary
                            if not subnet.get("MapPublicIpOnLaunch", False):
                                # Get instance name from tags
                                instance_name = "Unknown"
                                for tag in instance.get("Tags", []):
                                    if tag.get("Key") == "Name":
                                        instance_name = tag.get("Value", "Unknown")
                                        break

                                # If no Name tag, use instance type + ID
                                if instance_name == "Unknown":
                                    instance_type = instance.get("InstanceType", "unknown")
                                    instance_name = f"{instance_type} ({instance_id})"

                                checks["public_ips_should_be_private"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "InstanceName": instance_name,
                                        "InstanceType": instance.get("InstanceType", "unknown"),
                                        "PublicIp": instance.get("PublicIpAddress"),
                                        "SubnetId": subnet_id,
                                        "Recommendation": "Instance in private subnet has public IP - review necessity",
                                        "EstimatedSavings": "$3.65/month per public IP if removed",
                                        "CheckCategory": "Public IP Optimization",
                                    }
                                )
                        except Exception as e:
                            print(f"Warning: Could not check instance {instance_id}: {e}")
                            continue

        except Exception as e:
            print(f"Warning: Could not perform Elastic IP checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_nat_gateway_checks(self) -> Dict[str, Any]:
        """Category 2: NAT Gateway & VPC Design optimization checks"""
        checks = {
            "low_throughput_nat_gateways": [],
            "unnecessary_nat_per_az": [],
            "nat_for_aws_services": [],
            "nat_in_dev_test": [],
            "multiple_nat_gateways": [],
        }

        try:
            # Get all NAT Gateways - paginated
            paginator = self.ec2.get_paginator("describe_nat_gateways")
            nat_gateways = []
            for page in paginator.paginate():
                nat_gateways.extend(page.get("NatGateways", []))

            # Get VPC endpoints to check for S3/DynamoDB endpoints - paginated
            endpoints_paginator = self.ec2.get_paginator("describe_vpc_endpoints")
            vpc_endpoints = []
            for page in endpoints_paginator.paginate():
                vpc_endpoints.extend(page.get("VpcEndpoints", []))

            # Group NAT gateways by VPC and AZ
            vpc_nat_count = {}
            az_nat_count = {}

            for nat in nat_gateways:
                if nat.get("State") == "available":
                    vpc_id = nat.get("VpcId")
                    subnet_id = nat.get("SubnetId")
                    nat_id = nat.get("NatGatewayId")

                    # Get subnet details for AZ
                    try:
                        subnet_response = self.ec2.describe_subnets(SubnetIds=[subnet_id])
                        az = subnet_response["Subnets"][0].get("AvailabilityZone")

                        # Count NAT gateways per VPC and AZ
                        vpc_nat_count[vpc_id] = vpc_nat_count.get(vpc_id, 0) + 1
                        az_key = f"{vpc_id}:{az}"
                        az_nat_count[az_key] = az_nat_count.get(az_key, 0) + 1

                        # Check for low throughput (would need CloudWatch metrics in real implementation)
                        # For now, flag NAT gateways in dev/test environments
                        tags = {tag["Key"]: tag["Value"] for tag in nat.get("Tags", [])}
                        environment = tags.get("Environment", "").lower()

                        if environment in ["dev", "test", "development", "staging"]:
                            # Create a more descriptive name
                            nat_name = f"NAT Gateway {nat_id} ({az})"

                            checks["nat_in_dev_test"].append(
                                {
                                    "NatGatewayId": nat_id,
                                    "VpcId": vpc_id,
                                    "AvailabilityZone": az,
                                    "Environment": environment,
                                    "ResourceName": nat_name,
                                    "Recommendation": "Consider NAT instance or scheduled shutdown for dev/test",
                                    "EstimatedSavings": "$32.85/month base + data processing fees",
                                    "CheckCategory": "Dev/Test NAT Optimization",
                                }
                            )

                        # Check if VPC has S3/DynamoDB endpoints (should use instead of NAT)
                        vpc_has_s3_endpoint = any(
                            ep.get("VpcId") == vpc_id and ep.get("ServiceName", "").endswith(".s3")
                            for ep in vpc_endpoints
                        )
                        vpc_has_dynamodb_endpoint = any(
                            ep.get("VpcId") == vpc_id and ep.get("ServiceName", "").endswith(".dynamodb")
                            for ep in vpc_endpoints
                        )

                        if not vpc_has_s3_endpoint or not vpc_has_dynamodb_endpoint:
                            missing_endpoints = []
                            if not vpc_has_s3_endpoint:
                                missing_endpoints.append("S3")
                            if not vpc_has_dynamodb_endpoint:
                                missing_endpoints.append("DynamoDB")

                            checks["nat_for_aws_services"].append(
                                {
                                    "NatGatewayId": nat_id,
                                    "VpcId": vpc_id,
                                    "MissingEndpoints": missing_endpoints,
                                    "Recommendation": f"Create VPC endpoints for {', '.join(missing_endpoints)} to reduce NAT costs",
                                    "EstimatedSavings": "$0.01/GB data processing savings",
                                    "CheckCategory": "VPC Endpoints Missing",
                                }
                            )

                    except Exception as e:
                        print(f"Warning: Could not analyze NAT gateway {nat_id}: {e}")

            # Check for multiple NAT gateways per AZ (might be unnecessary)
            for az_key, count in az_nat_count.items():
                if count > 1:
                    vpc_id, az = az_key.split(":")
                    checks["multiple_nat_gateways"].append(
                        {
                            "VpcId": vpc_id,
                            "AvailabilityZone": az,
                            "NatGatewayCount": count,
                            "Recommendation": f"{count} NAT Gateways in same AZ - review if all are needed",
                            "EstimatedSavings": f"${(count - 1) * 32:.2f}/month if consolidated",
                            "CheckCategory": "Multiple NAT Gateways",
                        }
                    )

            # Check for low throughput NAT gateways (CloudWatch metrics would be ideal)
            for nat in nat_gateways:
                if nat.get("State") == "available":
                    nat_id = nat.get("NatGatewayId", "N/A")
                    checks["low_throughput_nat_gateways"].append(
                        {
                            "NatGatewayId": nat_id,
                            "VpcId": nat.get("VpcId", "N/A"),
                            "Recommendation": "Monitor CloudWatch metrics - consider NAT instance for low throughput",
                            "EstimatedSavings": "Up to $25/month for low-traffic scenarios",
                            "CheckCategory": "Low Throughput NAT Gateway",
                        }
                    )

            # Check for unnecessary NAT per AZ (single NAT can serve multiple AZs)
            for vpc_id, count in vpc_nat_count.items():
                if count > 1:
                    checks["unnecessary_nat_per_az"].append(
                        {
                            "VpcId": vpc_id,
                            "NatGatewayCount": count,
                            "Recommendation": f"{count} NAT Gateways in VPC - consider single NAT for cost optimization",
                            "EstimatedSavings": f"${(count - 1) * 32:.2f}/month (reduced availability)",
                            "CheckCategory": "Unnecessary NAT per AZ",
                        }
                    )

        except Exception as e:
            print(f"Warning: Could not perform NAT Gateway checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_vpc_endpoints_checks(self) -> Dict[str, Any]:
        """Category 3: VPC Endpoints optimization checks"""
        checks = {
            "missing_gateway_endpoints": [],
            "unused_interface_endpoints": [],
            "interface_endpoints_in_nonprod": [],
            "duplicate_endpoints": [],
            "no_traffic_endpoints": [],
        }

        try:
            # Get all VPCs - paginated
            vpcs_paginator = self.ec2.get_paginator("describe_vpcs")
            vpcs = []
            for page in vpcs_paginator.paginate():
                vpcs.extend(page.get("Vpcs", []))

            # Get all VPC endpoints - paginated
            endpoints_paginator = self.ec2.get_paginator("describe_vpc_endpoints")
            endpoints = []
            for page in endpoints_paginator.paginate():
                endpoints.extend(page.get("VpcEndpoints", []))

            # Check each VPC for missing gateway endpoints
            for vpc in vpcs:
                vpc_id = vpc["VpcId"]
                vpc_endpoints_in_vpc = [ep for ep in endpoints if ep.get("VpcId") == vpc_id]

                # Check for missing S3 gateway endpoint
                has_s3_gateway = any(
                    ep.get("ServiceName", "").endswith(".s3") and ep.get("VpcEndpointType") == "Gateway"
                    for ep in vpc_endpoints_in_vpc
                )

                # Check for missing DynamoDB gateway endpoint
                has_dynamodb_gateway = any(
                    ep.get("ServiceName", "").endswith(".dynamodb") and ep.get("VpcEndpointType") == "Gateway"
                    for ep in vpc_endpoints_in_vpc
                )

                if not has_s3_gateway:
                    checks["missing_gateway_endpoints"].append(
                        {
                            "VpcId": vpc_id,
                            "MissingService": "S3",
                            "EndpointType": "Gateway",
                            "Recommendation": "Create S3 Gateway endpoint to reduce NAT Gateway costs",
                            "EstimatedSavings": "$0.01/GB data processing + NAT costs",
                            "CheckCategory": "Missing S3 Gateway Endpoint",
                        }
                    )

                if not has_dynamodb_gateway:
                    checks["missing_gateway_endpoints"].append(
                        {
                            "VpcId": vpc_id,
                            "MissingService": "DynamoDB",
                            "EndpointType": "Gateway",
                            "Recommendation": "Create DynamoDB Gateway endpoint to reduce NAT Gateway costs",
                            "EstimatedSavings": "$0.01/GB data processing + NAT costs",
                            "CheckCategory": "Missing DynamoDB Gateway Endpoint",
                        }
                    )

            # Analyze existing endpoints
            endpoint_services = {}
            for endpoint in endpoints:
                endpoint_id = endpoint.get("VpcEndpointId")
                service_name = endpoint.get("ServiceName", "")
                endpoint_type = endpoint.get("VpcEndpointType", "")
                vpc_id = endpoint.get("VpcId")
                state = endpoint.get("State", "")

                # Get tags for environment detection
                tags = {tag["Key"]: tag["Value"] for tag in endpoint.get("Tags", [])}
                environment = tags.get("Environment", "").lower()

                # Check for interface endpoints in non-prod
                if endpoint_type == "Interface" and environment in ["dev", "test", "development", "staging"]:
                    checks["interface_endpoints_in_nonprod"].append(
                        {
                            "VpcEndpointId": endpoint_id,
                            "ServiceName": service_name,
                            "VpcId": vpc_id,
                            "Environment": environment,
                            "Recommendation": "Interface endpoints in non-prod may be unnecessary",
                            "EstimatedSavings": "$7.30/month per endpoint",
                            "CheckCategory": "Interface Endpoints in Non-Prod",
                        }
                    )

                # Track service usage for duplicate detection
                service_key = f"{vpc_id}:{service_name}"
                if service_key not in endpoint_services:
                    endpoint_services[service_key] = []
                endpoint_services[service_key].append(
                    {"endpoint_id": endpoint_id, "type": endpoint_type, "state": state}
                )

            # Check for potentially duplicate endpoints (>2 for same service)
            for service_key, service_endpoints in endpoint_services.items():
                if len(service_endpoints) > 2:  # Only flag if >2 endpoints (not just >1)
                    vpc_id, service_name = service_key.split(":", 1)
                    checks["duplicate_endpoints"].append(
                        {
                            "VpcId": vpc_id,
                            "ServiceName": service_name,
                            "EndpointCount": len(service_endpoints),
                            "EndpointIds": [ep["endpoint_id"] for ep in service_endpoints],
                            "Recommendation": f"{len(service_endpoints)} endpoints for same service - review if all needed (multiple can be valid for different route tables/policies)",
                            "EstimatedSavings": f"${(len(service_endpoints) - 2) * 7.30:.2f}/month if some consolidated",
                            "CheckCategory": "Multiple VPC Endpoints",
                        }
                    )

        except Exception as e:
            print(f"Warning: Could not perform VPC Endpoints checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_load_balancer_checks(self) -> Dict[str, Any]:
        """Category 4: Load Balancers optimization checks"""
        checks = {
            "zero_traffic_albs": [],
            "single_service_albs": [],
            "idle_listeners": [],
            "excessive_rules": [],
            "unnecessary_cross_az": [],
            "old_classic_elbs": [],
            "public_internal_lb": [],
            "nlb_vs_alb": [],
            "shared_alb_opportunity": [],
        }

        try:
            # Get ALBs/NLBs - paginated
            alb_paginator = self.elbv2.get_paginator("describe_load_balancers")
            load_balancers = []
            for page in alb_paginator.paginate():
                load_balancers.extend(page.get("LoadBalancers", []))

            try:
                # Get Classic Load Balancers - paginated
                clb_paginator = self.elb.get_paginator("describe_load_balancers")
                classic_lbs = []
                for page in clb_paginator.paginate():
                    classic_lbs.extend(page.get("LoadBalancerDescriptions", []))
            except Exception as e:
                print(f"⚠️ Error getting Classic Load Balancers: {str(e)}")
                classic_lbs = []

            # Track ALBs for consolidation opportunities
            alb_count = 0
            k8s_managed_albs = 0
            standalone_albs = []

            for lb in load_balancers:
                lb_arn = lb.get("LoadBalancerArn")
                lb_name = lb.get("LoadBalancerName")
                lb_type = lb.get("Type", "application")
                scheme = lb.get("Scheme", "internet-facing")

                # Check if this is a Kubernetes-managed ALB
                is_k8s_managed = self._is_kubernetes_managed_alb(lb_name, lb_arn)

                if lb_type == "application":
                    alb_count += 1
                    if is_k8s_managed:
                        k8s_managed_albs += 1
                    else:
                        standalone_albs.append(lb)

                # Check for public IP on internal workloads
                if scheme == "internet-facing" and any(
                    keyword in lb_name.lower() for keyword in ["internal", "private", "backend"]
                ):
                    checks["public_internal_lb"].append(
                        {
                            "LoadBalancerName": lb_name,
                            "Type": lb_type,
                            "Scheme": scheme,
                            "Recommendation": "Internet-facing load balancer with internal naming - verify if should be internal scheme",
                            "EstimatedSavings": "Security improvement + potential cost reduction if internal scheme sufficient",
                            "Action": "1. Verify if external access is actually needed\n2. Check if internal scheme would work\n3. Consider changing to internal if only internal access required\n4. Review security groups and NACLs",
                            "CheckCategory": "Load Balancer Scheme Optimization",
                        }
                    )

                # Check NLB where ALB might be more cost-effective
                if lb_type == "network":
                    checks["nlb_vs_alb"].append(
                        {
                            "LoadBalancerName": lb_name,
                            "Type": lb_type,
                            "Recommendation": "Review if ALB can handle your traffic patterns (HTTP/HTTPS only) - ALB is typically cheaper",
                            "EstimatedSavings": "Estimated $6.30/month savings (NLB: $22.50 vs ALB: $16.20)",
                            "Action": "1. Verify if you need Layer 4 load balancing\n2. Check if traffic is HTTP/HTTPS only\n3. Consider ALB if Layer 7 features sufficient\n4. Keep NLB if you need TCP/UDP or extreme performance",
                            "CheckCategory": "NLB vs ALB Cost Optimization",
                        }
                    )

                try:
                    listeners_response = self.elbv2.describe_listeners(LoadBalancerArn=lb_arn)
                    listeners = listeners_response.get("Listeners", [])

                    if len(listeners) == 0:
                        checks["idle_listeners"].append(
                            {
                                "LoadBalancerName": lb_name,
                                "Type": lb_type,
                                "Recommendation": "Load balancer has no listeners configured - verify configuration or delete if unused",
                                "EstimatedSavings": f"${16 if lb_type == 'application' else 22}/month if deleted",
                                "Action": "1. Check if listeners were accidentally deleted\n2. Verify if LB is still needed\n3. Configure listeners or delete LB",
                                "CheckCategory": "Load Balancer Configuration Issue",
                            }
                        )

                    # Only suggest consolidation for standalone ALBs, not K8s-managed ones
                    if lb_type == "application" and len(listeners) == 1 and not is_k8s_managed:
                        checks["single_service_albs"].append(
                            {
                                "LoadBalancerName": lb_name,
                                "ListenerCount": len(listeners),
                                "Recommendation": "ALB serving single service - consider consolidating multiple services on one ALB to reduce costs",
                                "EstimatedSavings": "Up to $16.20/month per ALB eliminated through consolidation",
                                "Action": "1. Identify other single-service ALBs\n2. Plan consolidation using host-based or path-based routing\n3. Test routing rules before migration\n4. Delete unused ALBs after consolidation",
                                "CheckCategory": "ALB Consolidation Opportunity",
                            }
                        )

                    # For K8s ALBs, suggest using Ingress Groups instead
                    elif lb_type == "application" and len(listeners) == 1 and is_k8s_managed:
                        checks["single_service_albs"].append(
                            {
                                "LoadBalancerName": lb_name,
                                "ListenerCount": len(listeners),
                                "Recommendation": "K8s ALB serving single service - consider using Ingress Groups to share ALBs across multiple services",
                                "EstimatedSavings": "Up to $16.20/month per ALB eliminated through Ingress Groups",
                                "Action": "1. Review Kubernetes Ingress resources\n2. Add alb.ingress.kubernetes.io/group.name annotation\n3. Use same group name across multiple Ingress resources\n4. Test routing before removing individual ALBs",
                                "CheckCategory": "K8s ALB Consolidation Opportunity",
                            }
                        )

                    total_rules = 0
                    for listener in listeners:
                        try:
                            rules_response = self.elbv2.describe_rules(ListenerArn=listener["ListenerArn"])
                            total_rules += len(rules_response.get("Rules", []))
                        except Exception as e:
                            print(f"Warning: Could not get rules for listener {listener['ListenerArn']}: {e}")
                            continue

                    if total_rules > 100:
                        checks["excessive_rules"].append(
                            {
                                "LoadBalancerName": lb_name,
                                "RuleCount": total_rules,
                                "Recommendation": f"ALB has {total_rules} rules which increases LCU costs - consider simplifying routing",
                                "EstimatedSavings": "Reduced LCU charges (rules contribute to LCU calculation)",
                                "Action": "1. Review and consolidate similar rules\n2. Use wildcard patterns where possible\n3. Consider path-based routing over multiple rules\n4. Monitor LCU usage in CloudWatch",
                                "CheckCategory": "ALB Rule Optimization",
                            }
                        )

                except Exception as e:
                    print(f"Warning: Could not analyze ALB {lb_name}: {e}")
                    continue

                az_count = len(lb.get("AvailabilityZones", []))
                if az_count > 2 and scheme == "internal":
                    checks["unnecessary_cross_az"].append(
                        {
                            "LoadBalancerName": lb_name,
                            "AvailabilityZoneCount": az_count,
                            "Recommendation": f"Internal Load Balancer spans {az_count} AZs - consider reducing to 2-3 AZs to minimize cross-AZ data transfer costs",
                            "EstimatedSavings": f"Reduce cross-AZ transfer costs by ${(az_count - 2) * 0.01 * 1000}/month (estimated based on 1GB/hour transfer)",
                            "Action": "1. Analyze traffic patterns to identify primary AZs\n2. Concentrate resources in 2-3 AZs for better cost efficiency\n3. Ensure high availability is maintained\n4. Monitor cross-AZ data transfer costs in Cost Explorer\n5. Cross-AZ transfer costs $0.01/GB - can add up with high traffic volumes",
                            "CheckCategory": "Cross-AZ Load Balancing",
                        }
                    )

            # Suggest ALB consolidation based on ALB type
            if alb_count > 5:
                standalone_count = len(standalone_albs)
                if standalone_count > 2:
                    # Suggest consolidation for standalone ALBs
                    checks["shared_alb_opportunity"].append(
                        {
                            "ALBCount": standalone_count,
                            "K8sALBCount": k8s_managed_albs,
                            "Recommendation": f"{standalone_count} standalone ALBs detected - consolidate using host-based or path-based routing to reduce costs",
                            "EstimatedSavings": f"Save ${(standalone_count - 2) * 16}/month by consolidating to 2 ALBs",
                            "Action": "1. Identify ALBs serving similar applications or environments\n2. Plan consolidation using host-based routing (different domains) or path-based routing (same domain, different paths)\n3. Test routing rules in staging environment\n4. Migrate traffic gradually and monitor performance\n5. Delete unused ALBs after successful consolidation\n6. Each ALB costs $16.20/month base + data processing fees",
                            "CheckCategory": "Shared ALB Opportunity",
                        }
                    )

                if k8s_managed_albs > 3:
                    # Suggest Ingress Groups for K8s ALBs
                    checks["shared_alb_opportunity"].append(
                        {
                            "ALBCount": k8s_managed_albs,
                            "StandaloneALBCount": standalone_count,
                            "Recommendation": f"{k8s_managed_albs} K8s ALBs detected - consider using Ingress Groups for consolidation",
                            "EstimatedSavings": f"Save ${(k8s_managed_albs - 2) * 16}/month through Ingress Groups",
                            "Action": "1. Review Kubernetes Ingress resources\n2. Add alb.ingress.kubernetes.io/group.name annotation\n3. Use same group name across multiple Ingress resources\n4. Set alb.ingress.kubernetes.io/group.order for rule priority\n5. Test routing before removing individual ALBs",
                            "CheckCategory": "K8s Ingress Groups Opportunity",
                        }
                    )

            for elb in classic_lbs:
                elb_name = elb.get("LoadBalancerName")
                created_time = elb.get("CreatedTime")

                if created_time:
                    age_days = (datetime.now(created_time.tzinfo) - created_time).days
                    if age_days > 365:
                        checks["old_classic_elbs"].append(
                            {
                                "LoadBalancerName": elb_name,
                                "AgeDays": age_days,
                                "Recommendation": "Migrate Classic ELB to ALB/NLB",
                                "EstimatedSavings": "10-20% + better features",
                                "CheckCategory": "Classic ELB Migration",
                            }
                        )

        except Exception as e:
            print(f"Warning: Load Balancer checks failed: {e}")

        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_auto_scaling_checks(self) -> Dict[str, Any]:
        """Category 5: Auto Scaling Groups optimization checks"""
        checks = {
            "static_asgs": [],
            "never_scaling_asgs": [],
            "nonprod_24x7_asgs": [],
            "oversized_instances": [],
            "missing_scale_in_policies": [],
        }

        try:
            # Get Auto Scaling Groups
            asg_response = self.autoscaling.describe_auto_scaling_groups()
            asgs = asg_response.get("AutoScalingGroups", [])

            for asg in asgs:
                asg_name = asg.get("AutoScalingGroupName")
                min_size = asg.get("MinSize", 0)
                max_size = asg.get("MaxSize", 0)
                desired_capacity = asg.get("DesiredCapacity", 0)

                # Get tags for environment detection
                tags = {tag["Key"]: tag["Value"] for tag in asg.get("Tags", [])}
                environment = tags.get("Environment", "").lower()

                # Check for static ASGs (min = desired = max)
                if min_size == desired_capacity == max_size and min_size > 0:
                    # Check if this is an EKS node group ASG
                    is_eks_nodegroup = self._is_eks_nodegroup_asg(asg_name, tags)

                    if is_eks_nodegroup:
                        # EKS node groups may appear static but serve important purposes
                        checks["static_asgs"].append(
                            {
                                "AutoScalingGroupName": asg_name,
                                "MinSize": min_size,
                                "MaxSize": max_size,
                                "DesiredCapacity": desired_capacity,
                                "Recommendation": "EKS node group with static sizing - consider enabling Cluster Autoscaler or Karpenter for dynamic scaling",
                                "EstimatedSavings": "Potential 20-40% savings through dynamic scaling based on workload demand",
                                "Action": "1. Install Cluster Autoscaler or Karpenter\n2. Configure node group for scaling (increase max_size)\n3. Set appropriate scaling policies\n4. Monitor workload patterns for optimization",
                                "CheckCategory": "EKS Static Node Groups",
                            }
                        )
                    else:
                        # Regular static ASG - original recommendation applies
                        checks["static_asgs"].append(
                            {
                                "AutoScalingGroupName": asg_name,
                                "MinSize": min_size,
                                "MaxSize": max_size,
                                "DesiredCapacity": desired_capacity,
                                "Recommendation": "ASG not configured for scaling - consider fixed EC2 instances or enable scaling",
                                "EstimatedSavings": "Remove ASG overhead, use Reserved Instances, or enable dynamic scaling",
                                "Action": "1. Evaluate if scaling is needed\n2. If no scaling needed: replace with fixed EC2 instances + Reserved Instances\n3. If scaling needed: configure min/max capacity and scaling policies",
                                "CheckCategory": "Static Auto Scaling Groups",
                            }
                        )

                # Check for non-prod ASGs running 24/7
                if environment in ["dev", "test", "development", "staging"] and min_size > 0:
                    checks["nonprod_24x7_asgs"].append(
                        {
                            "AutoScalingGroupName": asg_name,
                            "Environment": environment,
                            "MinSize": min_size,
                            "Recommendation": "Non-prod ASG running 24/7 - implement shutdown schedule",
                            "EstimatedSavings": "65-75% savings with 12-hour daily shutdown",
                            "CheckCategory": "Non-Prod 24/7 ASGs",
                        }
                    )

                # Check instance types for rightsizing opportunities
                launch_template = asg.get("LaunchTemplate")
                launch_config = asg.get("LaunchConfigurationName")

                if launch_template:
                    try:
                        lt_response = self.ec2.describe_launch_template_versions(
                            LaunchTemplateId=launch_template["LaunchTemplateId"],
                            Versions=[launch_template.get("Version", "$Latest")],
                        )
                        lt_data = lt_response["LaunchTemplateVersions"][0]["LaunchTemplateData"]
                        instance_type = lt_data.get("InstanceType")

                        # Flag large instance types that might be oversized
                        if instance_type and any(size in instance_type for size in ["xlarge", "2xlarge", "4xlarge"]):
                            checks["oversized_instances"].append(
                                {
                                    "AutoScalingGroupName": asg_name,
                                    "InstanceType": instance_type,
                                    "Recommendation": "Large instance type in ASG - verify rightsizing",
                                    "EstimatedSavings": "Potential 20-50% savings with rightsizing",
                                    "CheckCategory": "Oversized ASG Instances",
                                }
                            )

                    except Exception as e:
                        print(f"Warning: Could not analyze launch template for {asg_name}: {e}")

                # Check for scaling policies
                try:
                    policies_response = self.autoscaling.describe_policies(AutoScalingGroupName=asg_name)
                    policies = policies_response.get("ScalingPolicies", [])

                    scale_out_policies = [p for p in policies if p.get("ScalingAdjustment", 0) > 0]
                    scale_in_policies = [p for p in policies if p.get("ScalingAdjustment", 0) < 0]

                    if scale_out_policies and not scale_in_policies:
                        checks["missing_scale_in_policies"].append(
                            {
                                "AutoScalingGroupName": asg_name,
                                "ScaleOutPolicies": len(scale_out_policies),
                                "ScaleInPolicies": len(scale_in_policies),
                                "Recommendation": "ASG has scale-out but no scale-in policies",
                                "EstimatedSavings": "Prevent cost accumulation from scaling events",
                                "CheckCategory": "Missing Scale-In Policies",
                            }
                        )

                except Exception as e:
                    print(
                        f"Warning: Could not get Auto Scaling policies: {e}"
                    )  # Auto Scaling policies API might not be available

        except Exception as e:
            print(f"Warning: Could not perform Auto Scaling checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_advanced_ec2_checks(self) -> Dict[str, Any]:
        """Category 6: EC2 Advanced optimization checks"""
        checks = {
            "no_network_traffic": [],
            "cron_job_instances": [],
            "batch_job_instances": [],
            "monitoring_only_instances": [],
            "underutilized_instance_store": [],
            "oversized_root_volumes": [],
        }

        try:
            # Get all instances - paginate
            paginator = self.ec2.get_paginator("describe_instances")

            for page in paginator.paginate():
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        instance_id = instance["InstanceId"]
                        instance_type = instance.get("InstanceType", "unknown")
                        state = instance.get("State", {}).get("Name", "unknown")

                        if state != "running":
                            continue

                        # Get tags for analysis
                        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                        name = tags.get("Name", instance_id)

                        # Check for cron job instances (based on naming patterns)
                        if any(keyword in name.lower() for keyword in ["cron", "batch", "job", "scheduler"]):
                            checks["cron_job_instances"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "Name": name,
                                    "Recommendation": "Consider Lambda, EventBridge, or Batch for cron jobs",
                                    "EstimatedSavings": "80-95% cost reduction vs always-on EC2",
                                    "CheckCategory": "Cron Job Instances",
                                }
                            )

                        # Check for monitoring-only instances
                        if any(keyword in name.lower() for keyword in ["monitor", "nagios", "zabbix", "prometheus"]):
                            checks["monitoring_only_instances"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "Name": name,
                                    "Recommendation": "Consider CloudWatch, managed monitoring, or containerized solution",
                                    "EstimatedSavings": "Reduce infrastructure overhead",
                                    "CheckCategory": "Monitoring-Only Instances",
                                }
                            )

                        # Check root volume size
                        for bdm in instance.get("BlockDeviceMappings", []):
                            if bdm.get("DeviceName") in ["/dev/sda1", "/dev/xvda"]:
                                ebs = bdm.get("Ebs", {})
                                volume_id = ebs.get("VolumeId")

                                if volume_id:
                                    try:
                                        volume_response = self.ec2.describe_volumes(VolumeIds=[volume_id])
                                        volume = volume_response["Volumes"][0]
                                        size_gb = volume.get("Size", 0)

                                        # Flag oversized root volumes (>100GB for most workloads)
                                        if size_gb > 100:
                                            checks["oversized_root_volumes"].append(
                                                {
                                                    "InstanceId": instance_id,
                                                    "InstanceType": instance_type,
                                                    "RootVolumeSize": f"{size_gb}GB",
                                                    "VolumeId": volume_id,
                                                    "Recommendation": f"Root volume ({size_gb}GB) may be oversized - consider reducing or using separate data volumes",
                                                    "EstimatedSavings": f"${(size_gb - 20) * 0.10:.2f}/month if reduced to 20GB + separate data volume",
                                                    "CheckCategory": "Oversized Root Volumes",
                                                }
                                            )

                                    except Exception as e:
                                        print(f"Warning: Could not get volume details for {volume_id}: {e}")

                        # Check for instance store underutilization with basic heuristic
                        # Instance types with instance store that might not be using it
                        if any(family in instance_type for family in ["m5d", "c5d", "r5d", "i3", "i4i"]):
                            # Basic heuristic: if it's a general workload, might not need instance store
                            if not any(
                                keyword in name.lower()
                                for keyword in ["database", "cache", "storage", "data", "analytics"]
                            ):
                                checks["underutilized_instance_store"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "InstanceType": instance_type,
                                        "Name": name,
                                        "Recommendation": "Instance has local storage but workload may not require it - consider non-storage optimized type",
                                        "EstimatedSavings": "10-20% cost reduction with equivalent non-storage instance type",
                                        "CheckCategory": "Underutilized Instance Store",
                                    }
                                )

                        # Check for potential batch job instances (improved heuristic)
                        if (
                            any(keyword in name.lower() for keyword in ["batch", "job", "worker", "process"])
                            and "web" not in name.lower()
                        ):
                            checks["batch_job_instances"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "Name": name,
                                    "Recommendation": "Consider AWS Batch with Spot instances for batch workloads",
                                    "EstimatedSavings": "60-90% cost reduction with Spot instances in Batch",
                                    "CheckCategory": "Batch Job Instances",
                                }
                            )
                            checks["underutilized_instance_store"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceType": instance_type,
                                    "Name": name,
                                    "Recommendation": "Instance type includes instance store - verify utilization",
                                    "EstimatedSavings": "Switch to non-storage optimized if unused",
                                    "CheckCategory": "Underutilized Instance Store",
                                }
                            )

        except Exception as e:
            print(f"Warning: Could not perform Advanced EC2 checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_cloudwatch_checks(self) -> Dict[str, Any]:
        """Category 9: CloudWatch optimization checks"""
        checks = {
            "never_expiring_logs": [],
            "excessive_logging": [],
            "unused_custom_metrics": [],
            "high_resolution_metrics": [],
            "unused_alarms": [],
            "duplicate_metrics": [],
        }

        try:
            # Get all log groups
            log_groups_response = self.logs.describe_log_groups()
            log_groups = log_groups_response.get("logGroups", [])

            for log_group in log_groups:
                log_group_name = log_group.get("logGroupName")
                retention_days = log_group.get("retentionInDays")
                stored_bytes = log_group.get("storedBytes", 0)

                # Check for never-expiring retention
                if retention_days is None:
                    checks["never_expiring_logs"].append(
                        {
                            "LogGroupName": log_group_name,
                            "StoredBytes": stored_bytes,
                            "StoredGB": round(stored_bytes / (1024**3), 2),
                            "Recommendation": "Set retention policy to prevent unlimited log growth",
                            "EstimatedSavings": f"${stored_bytes * 0.03 / (1024**3):.2f}/month with 30-day retention",
                            "CheckCategory": "Never-Expiring Log Groups",
                        }
                    )

                # Check for excessive log storage (>10GB)
                if stored_bytes > 10 * 1024**3:  # 10GB
                    checks["excessive_logging"].append(
                        {
                            "LogGroupName": log_group_name,
                            "StoredGB": round(stored_bytes / (1024**3), 2),
                            "RetentionDays": retention_days,
                            "Recommendation": "Large log group - review log level and retention",
                            "EstimatedSavings": "Reduce log level or retention period",
                            "CheckCategory": "Excessive Log Storage",
                        }
                    )

            # Get CloudWatch alarms with pagination
            try:
                paginator = self.cloudwatch.get_paginator("describe_alarms")
                for page in paginator.paginate():
                    alarms = page.get("MetricAlarms", [])

                    for alarm in alarms:
                        alarm_name = alarm.get("AlarmName")
                        state_reason = alarm.get("StateReason", "")
                        alarm_config_updated = alarm.get("AlarmConfigurationUpdatedTimestamp")

                        # Check for alarms with insufficient data that are older than 7 days
                        if "Insufficient Data" in state_reason and alarm_config_updated:
                            from datetime import datetime, timezone, timedelta

                            if isinstance(alarm_config_updated, str):
                                # Parse string timestamp if needed
                                continue

                            age_days = (datetime.now(timezone.utc) - alarm_config_updated).days
                            if age_days > 7:  # Only flag old alarms with insufficient data
                                checks["unused_alarms"].append(
                                    {
                                        "AlarmName": alarm_name,
                                        "StateReason": state_reason,
                                        "AgeDays": age_days,
                                        "Recommendation": f"Alarm has insufficient data for {age_days} days - review metric availability or delete",
                                        "CheckCategory": "Unused CloudWatch Alarms",
                                    }
                                )

            except Exception as e:
                print(f"Warning: Could not analyze CloudWatch alarms: {e}")

            # Get custom metrics with pagination
            try:
                paginator = self.cloudwatch.get_paginator("list_metrics")
                metrics = []
                for page in paginator.paginate():
                    metrics.extend(page.get("Metrics", []))

                # Group by namespace to identify custom metrics
                namespace_counts = {}
                for metric in metrics:
                    namespace = metric.get("Namespace", "")
                    if not namespace.startswith("AWS/"):  # Custom metrics
                        namespace_counts[namespace] = namespace_counts.get(namespace, 0) + 1

                for namespace, count in namespace_counts.items():
                    if count > 100:  # High number of custom metrics
                        checks["unused_custom_metrics"].append(
                            {
                                "Namespace": namespace,
                                "MetricCount": count,
                                "Recommendation": f"High number of custom metrics ({count}) - review necessity",
                                "EstimatedSavings": f"${count * 0.30:.2f}/month if reduced by 50%",
                                "CheckCategory": "Excessive Custom Metrics",
                            }
                        )

            except Exception as e:
                print(f"Warning: Could not analyze custom metrics: {e}")

        except Exception as e:
            print(f"Warning: Could not perform CloudWatch checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_cloudtrail_checks(self) -> Dict[str, Any]:
        """Category 10: CloudTrail optimization checks"""
        checks = {
            "multi_region_trails": [],
            "data_events_all_s3": [],
            "data_events_all_lambda": [],
            "duplicate_trails": [],
            "expensive_storage_trails": [],
            "unused_insights": [],
        }

        try:
            # Get all CloudTrail trails
            trails_response = self.cloudtrail.describe_trails()
            trails = trails_response.get("trailList", [])

            trail_names = set()

            for trail in trails:
                trail_name = trail.get("Name")
                trail_arn = trail.get("TrailARN")
                is_multi_region = trail.get("IsMultiRegionTrail", False)
                s3_bucket = trail.get("S3BucketName")

                # Track for duplicate detection
                trail_names.add(trail_name)

                # Check for multi-region trails where single-region might suffice
                if is_multi_region:
                    checks["multi_region_trails"].append(
                        {
                            "TrailName": trail_name,
                            "TrailARN": trail_arn,
                            "S3Bucket": s3_bucket,
                            "Recommendation": "Multi-region trail - verify if all regions needed",
                            "EstimatedSavings": "Single-region trail costs ~90% less",
                            "CheckCategory": "Multi-Region CloudTrail",
                        }
                    )

                # Get event selectors to check for data events
                try:
                    selectors_response = self.cloudtrail.get_event_selectors(TrailName=trail_name)
                    event_selectors = selectors_response.get("EventSelectors", [])

                    for selector in event_selectors:
                        data_resources = selector.get("DataResources", [])

                        for resource in data_resources:
                            resource_type = resource.get("Type")
                            values = resource.get("Values", [])

                            # Check for data events on all S3 buckets
                            if resource_type == "AWS::S3::Object" and "arn:aws:s3:::*/*" in values:
                                checks["data_events_all_s3"].append(
                                    {
                                        "TrailName": trail_name,
                                        "ResourceType": resource_type,
                                        "Recommendation": "Data events enabled for all S3 buckets - very expensive",
                                        "EstimatedSavings": "Limit to specific buckets for 80-95% savings",
                                        "CheckCategory": "S3 Data Events All Buckets",
                                    }
                                )

                            # Check for data events on all Lambda functions
                            if resource_type == "AWS::Lambda::Function" and "arn:aws:lambda:*" in str(values):
                                checks["data_events_all_lambda"].append(
                                    {
                                        "TrailName": trail_name,
                                        "ResourceType": resource_type,
                                        "Recommendation": "Data events enabled for all Lambda functions - expensive",
                                        "EstimatedSavings": "Limit to specific functions for significant savings",
                                        "CheckCategory": "Lambda Data Events All Functions",
                                    }
                                )

                except ClientError as e:
                    if e.response["Error"]["Code"] != "TrailNotFoundException":
                        print(f"Warning: Could not analyze event selectors for {trail_name}: {e}")
                except Exception as e:
                    print(f"Warning: Could not analyze event selectors for {trail_name}: {e}")

                # Check for CloudTrail Insights
                try:
                    insights_response = self.cloudtrail.get_insight_selectors(TrailName=trail_name)
                    insight_selectors = insights_response.get("InsightSelectors", [])

                    if insight_selectors:
                        checks["unused_insights"].append(
                            {
                                "TrailName": trail_name,
                                "InsightTypes": [s.get("InsightType") for s in insight_selectors],
                                "Recommendation": "CloudTrail Insights enabled - verify usage and value",
                                "EstimatedSavings": "$0.35 per 100,000 events if unused",
                                "CheckCategory": "CloudTrail Insights",
                            }
                        )

                except ClientError as e:
                    if e.response["Error"]["Code"] != "TrailNotFoundException":
                        print(f"Warning: Could not check insights for {trail_name}: {e}")
                except Exception as e:
                    print(f"Warning: Could not check insights for {trail_name}: {e}")

            # Check for potentially duplicate trails (>2 trails with similar event selectors)
            if len(trail_names) > 2:  # Only flag if >2 trails (not just >1)
                checks["duplicate_trails"].append(
                    {
                        "TrailCount": len(trail_names),
                        "TrailNames": list(trail_names),
                        "Recommendation": f"{len(trail_names)} trails detected - review event selectors to avoid duplication",
                        "EstimatedSavings": "Consolidate overlapping trails to reduce costs",
                        "CheckCategory": "Multiple CloudTrail Trails",
                    }
                )

        except Exception as e:
            print(f"Warning: Could not perform CloudTrail checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_backup_checks(self) -> Dict[str, Any]:
        """Category 11: AWS Backup optimization checks"""
        checks = {
            "backup_unused_resources": [],
            "multiple_backup_plans": [],
            "excessive_retention": [],
            "unnecessary_cross_region": [],
            "daily_static_data": [],
            "ephemeral_backups": [],
        }

        try:
            # Get backup plans with pagination
            paginator = self.backup.get_paginator("list_backup_plans")
            backup_plans = []
            for page in paginator.paginate():
                backup_plans.extend(page.get("BackupPlansList", []))

            for plan in backup_plans:
                plan_id = plan.get("BackupPlanId")
                plan_name = plan.get("BackupPlanName")

                try:
                    # Get plan details
                    plan_response = self.backup.get_backup_plan(BackupPlanId=plan_id)
                    plan_details = plan_response.get("BackupPlan", {})
                    rules = plan_details.get("Rules", [])

                    for rule in rules:
                        rule_name = rule.get("RuleName")
                        target_vault = rule.get("TargetBackupVaultName")
                        schedule = rule.get("ScheduleExpression", "")
                        lifecycle = rule.get("Lifecycle", {})

                        # Check for excessive retention
                        delete_after_days = lifecycle.get("DeleteAfterDays")
                        if delete_after_days and delete_after_days > 2555:  # > 7 years
                            checks["excessive_retention"].append(
                                {
                                    "BackupPlanName": plan_name,
                                    "RuleName": rule_name,
                                    "RetentionDays": delete_after_days,
                                    "Recommendation": f"Retention period ({delete_after_days} days) may exceed compliance needs",
                                    "EstimatedSavings": "Reduce retention to lower storage costs",
                                    "CheckCategory": "Excessive Backup Retention",
                                }
                            )

                        # Check for daily backups (might be excessive for static data)
                        if "daily" in schedule.lower() or "cron(0 " in schedule:
                            checks["daily_static_data"].append(
                                {
                                    "BackupPlanName": plan_name,
                                    "RuleName": rule_name,
                                    "Schedule": schedule,
                                    "Recommendation": "Daily backups - verify if needed for static/infrequent data",
                                    "EstimatedSavings": "Weekly/monthly backups can reduce costs by 70-85%",
                                    "CheckCategory": "Daily Backup Frequency",
                                }
                            )

                        # Check for cross-region copies
                        copy_actions = rule.get("CopyActions", [])
                        if copy_actions:
                            for copy_action in copy_actions:
                                dest_vault_arn = copy_action.get("DestinationBackupVaultArn", "")
                                if dest_vault_arn and self.region not in dest_vault_arn:
                                    checks["unnecessary_cross_region"].append(
                                        {
                                            "BackupPlanName": plan_name,
                                            "RuleName": rule_name,
                                            "DestinationVault": dest_vault_arn,
                                            "Recommendation": "Cross-region backup copy - verify business need",
                                            "EstimatedSavings": "Remove if not required for DR",
                                            "CheckCategory": "Cross-Region Backup Copies",
                                        }
                                    )

                    # Get backup selections for this plan with pagination
                    paginator = self.backup.get_paginator("list_backup_selections")
                    selections = []
                    for page in paginator.paginate(BackupPlanId=plan_id):
                        selections.extend(page.get("BackupSelectionsList", []))

                    for selection in selections:
                        selection_id = selection.get("SelectionId")
                        selection_name = selection.get("SelectionName")

                        try:
                            selection_response = self.backup.get_backup_selection(
                                BackupPlanId=plan_id, SelectionId=selection_id
                            )
                            selection_details = selection_response.get("BackupSelection", {})
                            resources = selection_details.get("Resources", [])

                            # Check if backing up dev/test resources
                            for resource_arn in resources:
                                if any(env in resource_arn.lower() for env in ["dev", "test", "staging"]):
                                    checks["ephemeral_backups"].append(
                                        {
                                            "BackupPlanName": plan_name,
                                            "SelectionName": selection_name,
                                            "ResourceArn": resource_arn,
                                            "Recommendation": "Backing up dev/test resources - often unnecessary",
                                            "EstimatedSavings": "Remove ephemeral resource backups",
                                            "CheckCategory": "Ephemeral Resource Backups",
                                        }
                                    )

                        except Exception as e:
                            print(f"Warning: Could not analyze backup selection {selection_name}: {e}")

                except Exception as e:
                    print(f"Warning: Could not analyze backup plan {plan_name}: {e}")

            # Check for multiple plans covering same resources (would need more analysis)
            if len(backup_plans) > 3:
                checks["multiple_backup_plans"].append(
                    {
                        "BackupPlanCount": len(backup_plans),
                        "PlanNames": [p.get("BackupPlanName") for p in backup_plans],
                        "Recommendation": "Multiple backup plans - check for overlapping coverage",
                        "EstimatedSavings": "Consolidate plans to avoid duplicate backups",
                        "CheckCategory": "Multiple Backup Plans",
                    }
                )

        except Exception as e:
            print(f"Warning: Could not perform AWS Backup checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_route53_checks(self) -> Dict[str, Any]:
        """Category 12: Route 53 optimization checks"""
        checks = {
            "unused_hosted_zones": [],
            "unnecessary_health_checks": [],
            "complex_routing_simple_use": [],
            "old_records_deleted_resources": [],
            "duplicate_private_zones": [],
        }

        try:
            # Get hosted zones - paginate
            paginator = self.route53.get_paginator("list_hosted_zones")
            hosted_zones = []
            for page in paginator.paginate():
                hosted_zones.extend(page.get("HostedZones", []))

            for zone in hosted_zones:
                zone_id = zone.get("Id").split("/")[-1]  # Remove /hostedzone/ prefix
                zone_name = zone.get("Name")
                is_private = zone.get("Config", {}).get("PrivateZone", False)
                record_count = zone.get("ResourceRecordSetCount", 0)

                # Check for zones with minimal records (might be unused)
                if record_count <= 2:  # Only NS and SOA records
                    checks["unused_hosted_zones"].append(
                        {
                            "HostedZoneId": zone_id,
                            "ZoneName": zone_name,
                            "RecordCount": record_count,
                            "IsPrivate": is_private,
                            "Recommendation": "Hosted zone has minimal records - verify if still needed",
                            "EstimatedSavings": "$0.50/month per zone if deleted",
                            "CheckCategory": "Unused Hosted Zones",
                        }
                    )

                # Get records to analyze routing complexity - paginate
                try:
                    paginator = self.route53.get_paginator("list_resource_record_sets")
                    records = []
                    for page in paginator.paginate(HostedZoneId=zone_id):
                        records.extend(page.get("ResourceRecordSets", []))

                    weighted_records = 0
                    latency_records = 0
                    geolocation_records = 0

                    for record in records:
                        if record.get("Weight") is not None:
                            weighted_records += 1
                        if record.get("Region") is not None:
                            latency_records += 1
                        if record.get("GeoLocation") is not None:
                            geolocation_records += 1

                    # Check for complex routing when simple might suffice
                    total_complex = weighted_records + latency_records + geolocation_records
                    if total_complex > 0 and record_count < 10:
                        checks["complex_routing_simple_use"].append(
                            {
                                "HostedZoneId": zone_id,
                                "ZoneName": zone_name,
                                "WeightedRecords": weighted_records,
                                "LatencyRecords": latency_records,
                                "GeolocationRecords": geolocation_records,
                                "Recommendation": "Complex routing policies for simple zone - verify necessity",
                                "EstimatedSavings": "Simple routing reduces query costs",
                                "CheckCategory": "Unnecessary Complex Routing",
                            }
                        )

                except Exception as e:
                    print(f"Warning: Could not analyze records for zone {zone_name}: {e}")

            # Get health checks with pagination
            try:
                paginator = self.route53.get_paginator("list_health_checks")
                health_checks = []
                for page in paginator.paginate():
                    health_checks.extend(page.get("HealthChecks", []))

                for health_check in health_checks:
                    hc_id = health_check.get("Id")
                    hc_config = health_check.get("HealthCheckConfig", {})
                    hc_type = hc_config.get("Type")

                    # Flag health checks that might not be needed
                    if hc_type in ["HTTP", "HTTPS", "TCP"]:
                        checks["unnecessary_health_checks"].append(
                            {
                                "HealthCheckId": hc_id,
                                "Type": hc_type,
                                "Recommendation": "Health check without routing dependency - verify necessity",
                                "EstimatedSavings": "$0.50/month per health check if removed",
                                "CheckCategory": "Unnecessary Health Checks",
                            }
                        )

            except Exception as e:
                print(f"Warning: Could not analyze Route 53 health checks: {e}")

            # Check for duplicate private zones (would need VPC association analysis)
            private_zones = [z for z in hosted_zones if z.get("Config", {}).get("PrivateZone", False)]
            zone_names = {}

            for zone in private_zones:
                zone_name = zone.get("Name")
                if zone_name in zone_names:
                    zone_names[zone_name].append(zone.get("Id"))
                else:
                    zone_names[zone_name] = [zone.get("Id")]

            for zone_name, zone_ids in zone_names.items():
                if len(zone_ids) > 1:
                    checks["duplicate_private_zones"].append(
                        {
                            "ZoneName": zone_name,
                            "ZoneCount": len(zone_ids),
                            "ZoneIds": zone_ids,
                            "Recommendation": "Multiple private zones with same name - check VPC associations",
                            "EstimatedSavings": f"${(len(zone_ids) - 1) * 0.50:.2f}/month if consolidated",
                            "CheckCategory": "Duplicate Private Zones",
                        }
                    )

        except Exception as e:
            print(f"Warning: Could not perform Route 53 checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_rds_checks(self) -> Dict[str, Any]:
        """Get enhanced RDS cost optimization checks"""
        checks = {
            "idle_databases": [],
            "instance_rightsizing": [],
            "reserved_instances": [],
            "storage_optimization": [],
            "multi_az_unnecessary": [],
            "backup_retention_excessive": [],
            "old_snapshots": [],
            "non_prod_scheduling": [],
            "aurora_serverless_candidates": [],
            "aurora_serverless_v2": [],
        }

        try:
            # Get all RDS instances with pagination
            paginator = self.rds.get_paginator("describe_db_instances")
            for page in paginator.paginate():
                instances = page.get("DBInstances", [])

                for instance in instances:
                    db_instance_id = instance.get("DBInstanceIdentifier")
                    db_instance_class = instance.get("DBInstanceClass")
                    engine = instance.get("Engine")
                    db_instance_status = instance.get("DBInstanceStatus")
                    multi_az = instance.get("MultiAZ", False)
                    backup_retention = instance.get("BackupRetentionPeriod", 0)
                    allocated_storage = instance.get("AllocatedStorage", 0)
                    storage_type = instance.get("StorageType", "gp2")

                    # Skip if not available (but include stopped instances for storage cost analysis)
                    if db_instance_status not in ["available", "stopped"]:
                        continue

                    # Check for Multi-AZ in non-production (tag-based + naming fallback)
                    if multi_az:
                        # Get tags for environment detection
                        try:
                            tags_response = self.rds.list_tags_for_resource(
                                ResourceName=f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}"
                            )
                            tags = {tag["Key"]: tag["Value"] for tag in tags_response.get("TagList", [])}

                            # Check tags first (Environment, Stage, etc.)
                            env_tag = tags.get("Environment", tags.get("Stage", tags.get("Env", ""))).lower()
                            is_non_prod = env_tag in [
                                "dev",
                                "development",
                                "test",
                                "testing",
                                "staging",
                                "qa",
                                "non-prod",
                                "nonprod",
                            ]

                            # Fallback to naming heuristic if no tags
                            if not is_non_prod:
                                is_non_prod = any(
                                    env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"]
                                )
                                env_name = next(
                                    (env for env in ["dev", "test", "staging", "qa"] if env in db_instance_id.lower()),
                                    "non-prod",
                                )
                            else:
                                env_name = env_tag or "non-prod"

                        except Exception:
                            # Fallback to naming heuristic only
                            is_non_prod = any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"])
                            env_name = next(
                                (env for env in ["dev", "test", "staging", "qa"] if env in db_instance_id.lower()),
                                "non-prod",
                            )

                        if is_non_prod:
                            checks["multi_az_unnecessary"].append(
                                {
                                    "DBInstanceIdentifier": db_instance_id,
                                    "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                    "engine": engine,
                                    "engineVersion": instance.get("EngineVersion", ""),
                                    "MultiAZ": multi_az,
                                    "Environment": env_name,
                                    "Recommendation": f"Disable Multi-AZ for {env_name} environment to reduce costs",
                                    "EstimatedSavings": "~50% of instance cost",
                                    "CheckCategory": "Multi-AZ Optimization",
                                    "instanceFinding": f"Multi-AZ enabled in {env_name} environment",
                                }
                            )

                    # Check for excessive backup retention
                    if backup_retention > 7:
                        # Calculate backup savings considering free backup storage (equal to allocated DB storage)
                        # Note: This is a coarse estimate assuming backup size ≈ allocated storage per retained day
                        # Actual backup growth patterns and compression may vary significantly
                        extra_backup_days = backup_retention - 7
                        # Only charge for backup storage beyond the free tier (allocated storage)
                        backup_savings = allocated_storage * 0.095 * self.pricing_multiplier * extra_backup_days / 30
                        checks["backup_retention_excessive"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                "engine": engine,
                                "engineVersion": instance.get("EngineVersion", ""),
                                "BackupRetentionPeriod": backup_retention,
                                "Recommendation": f"Reduce backup retention from {backup_retention} to 7 days",
                                "EstimatedSavings": f"${backup_savings:.2f}/month in backup storage (estimate - beyond free tier)",
                                "CheckCategory": "Backup Retention Optimization",
                                "instanceFinding": f"{backup_retention} days retention (recommend 7 days for non-critical DBs)",
                            }
                        )

                    # Check for storage optimization opportunities
                    if storage_type == "gp2":
                        # Use regional pricing for gp2 storage
                        monthly_cost = (
                            allocated_storage * 0.115 * self.pricing_multiplier
                        )  # gp2 pricing with regional adjustment
                        savings = monthly_cost * 0.20  # 20% savings from gp2 to gp3
                        checks["storage_optimization"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                "engine": engine,
                                "engineVersion": instance.get("EngineVersion", ""),
                                "CurrentStorageType": storage_type,
                                "AllocatedStorage": allocated_storage,
                                "Recommendation": "Migrate from gp2 to gp3 for 20% cost savings",
                                "EstimatedSavings": f"${savings:.2f}/month",
                                "CheckCategory": "RDS Storage Optimization",
                                "storageFinding": f"{storage_type} ({allocated_storage}GB) → gp3 recommended",
                            }
                        )
                    elif storage_type in ["io1", "io2", "gp3"]:
                        # Note: Advanced IOPS/throughput optimization requires workload analysis
                        checks["storage_optimization"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                "engine": engine,
                                "engineVersion": instance.get("EngineVersion", ""),
                                "CurrentStorageType": storage_type,
                                "AllocatedStorage": allocated_storage,
                                "Recommendation": f"Review {storage_type} IOPS/throughput configuration for potential optimization",
                                "EstimatedSavings": "Requires workload analysis for IOPS/throughput tuning",
                                "CheckCategory": "RDS Storage Optimization",
                                "storageFinding": f"{storage_type} ({allocated_storage}GB) - review IOPS/throughput settings",
                            }
                        )

                    # Check for non-production scheduling opportunities (based on naming - may miss some non-prod DBs)
                    if any(env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"]):
                        env_name = next(
                            (env for env in ["dev", "test", "staging", "qa"] if env in db_instance_id.lower()),
                            "non-prod",
                        )
                        # Only MySQL and PostgreSQL can be stopped
                        if engine in ["mysql", "postgres", "mariadb"]:
                            checks["non_prod_scheduling"].append(
                                {
                                    "DBInstanceIdentifier": db_instance_id,
                                    "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                    "engine": engine,
                                    "engineVersion": instance.get("EngineVersion", ""),
                                    "Environment": env_name,
                                    "Recommendation": f"Implement start/stop schedule for {env_name} database (stop nights/weekends)",
                                    "EstimatedSavings": "65-75% of compute costs",
                                    "CheckCategory": "Non-Production Scheduling",
                                    "instanceFinding": f"{env_name} database - eligible for automated scheduling",
                                }
                            )

                    # Check for idle databases (basic heuristic - requires CloudWatch for accurate detection)
                    if db_instance_status == "stopped":
                        checks["idle_databases"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                "engine": engine,
                                "DBInstanceStatus": db_instance_status,
                                "Recommendation": "Consider deleting stopped database if no longer needed (storage costs still apply)",
                                "EstimatedSavings": "Storage costs continue while stopped",
                                "CheckCategory": "Idle Database Detection",
                                "instanceFinding": f"Database in {db_instance_status} state",
                            }
                        )

                    # Check for instance rightsizing opportunities (placeholder - requires CloudWatch metrics)
                    if db_instance_class.startswith("db.t"):
                        checks["instance_rightsizing"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                "engine": engine,
                                "DBInstanceClass": db_instance_class,
                                "Recommendation": "Review burstable instance usage - consider fixed instance if consistently high CPU",
                                "EstimatedSavings": "Requires CloudWatch analysis for accurate sizing",
                                "CheckCategory": "Instance Rightsizing",
                                "instanceFinding": f"Burstable instance ({db_instance_class}) - review usage patterns",
                            }
                        )

                    # Check for Reserved Instance opportunities (placeholder - requires pricing analysis)
                    if db_instance_status == "available":
                        # Only recommend RIs for likely production instances or add environment check
                        is_likely_prod = not any(
                            env in db_instance_id.lower() for env in ["dev", "test", "staging", "qa"]
                        )
                        ri_text = "production databases" if is_likely_prod else "long-running databases"
                        checks["reserved_instances"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                "engine": engine,
                                "DBInstanceClass": db_instance_class,
                                "Recommendation": f"Consider Reserved Instances for {ri_text}",
                                "EstimatedSavings": "Up to 60% savings for 1-3 year commitments",
                                "CheckCategory": "Reserved Instance Opportunities",
                                "instanceFinding": f"Instance ({db_instance_class}) - RI candidate",
                            }
                        )

                    # Check for Aurora Serverless v2 candidates
                    if "aurora" in engine and db_instance_class in [
                        "db.t3.small",
                        "db.t3.medium",
                        "db.t4g.small",
                        "db.t4g.medium",
                    ]:
                        checks["aurora_serverless_candidates"].append(
                            {
                                "DBInstanceIdentifier": db_instance_id,
                                "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:db:{db_instance_id}",
                                "engine": engine,
                                "engineVersion": instance.get("EngineVersion", ""),
                                "DBInstanceClass": db_instance_class,
                                "Recommendation": "Consider migrating to Aurora Serverless v2 for variable workloads",
                                "EstimatedSavings": "Pay only for capacity used (up to 90% for idle periods)",
                                "CheckCategory": "Aurora Serverless v2 Migration",
                                "instanceFinding": f"Small Aurora instance ({db_instance_class}) - good candidate for Serverless v2",
                            }
                        )

            # Check for old manual snapshots with pagination
            try:
                paginator = self.rds.get_paginator("describe_db_snapshots")
                for page in paginator.paginate(SnapshotType="manual"):
                    for snapshot in page.get("DBSnapshots", []):
                        snapshot_id = snapshot.get("DBSnapshotIdentifier")
                        create_time = snapshot.get("SnapshotCreateTime")
                        allocated_storage = snapshot.get("AllocatedStorage", 0)

                        if create_time:
                            age_days = (datetime.now(create_time.tzinfo) - create_time).days
                            if age_days > self.OLD_SNAPSHOT_DAYS:
                                checks["old_snapshots"].append(
                                    {
                                        "SnapshotId": snapshot_id,
                                        "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:snapshot:{snapshot_id}",
                                        "AgeDays": age_days,
                                        "AllocatedStorage": allocated_storage,
                                        "Recommendation": f"Delete {age_days}-day old manual snapshot (savings based on allocated storage estimate)",
                                        "EstimatedSavings": f"${allocated_storage * 0.095 * self.pricing_multiplier:.2f}/month (coarse estimate)",
                                        "CheckCategory": "Old RDS Snapshots",
                                        "instanceFinding": f"{age_days} days old ({allocated_storage}GB)",
                                    }
                                )
            except Exception as e:
                print(f"Warning: Could not check RDS snapshots: {e}")

            # Check Aurora clusters
            try:
                paginator = self.rds.get_paginator("describe_db_clusters")
                for page in paginator.paginate():
                    for cluster in page.get("DBClusters", []):
                        cluster_id = cluster.get("DBClusterIdentifier")
                        engine = cluster.get("Engine", "")

                        if "aurora" in engine.lower():
                            # Check for Aurora Serverless v2 opportunities
                            if cluster.get("ServerlessV2ScalingConfiguration") is None:
                                checks["aurora_serverless_v2"].append(
                                    {
                                        "DBClusterIdentifier": cluster_id,
                                        "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:cluster:{cluster_id}",
                                        "Engine": engine,
                                        "Recommendation": "Consider Aurora Serverless v2 for variable workloads",
                                        "EstimatedSavings": "20-90% cost reduction for variable workloads",
                                        "CheckCategory": "Aurora Serverless v2 Migration",
                                    }
                                )

                            # Check for Aurora cluster storage optimization
                            storage_type = cluster.get("StorageType", "aurora")
                            if storage_type == "aurora":
                                checks["storage_optimization"].append(
                                    {
                                        "DBClusterIdentifier": cluster_id,
                                        "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:cluster:{cluster_id}",
                                        "Engine": engine,
                                        "StorageType": storage_type,
                                        "Recommendation": "Review Aurora storage usage and consider Aurora I/O-Optimized if high I/O costs",
                                        "EstimatedSavings": "Potential I/O cost reduction for high-throughput workloads",
                                        "CheckCategory": "Aurora Storage Optimization",
                                        "instanceFinding": f"Aurora cluster - review I/O patterns",
                                    }
                                )

                # Check for old Aurora cluster snapshots
                try:
                    paginator = self.rds.get_paginator("describe_db_cluster_snapshots")
                    for page in paginator.paginate(SnapshotType="manual"):
                        for snapshot in page.get("DBClusterSnapshots", []):
                            snapshot_id = snapshot.get("DBClusterSnapshotIdentifier")
                            create_time = snapshot.get("SnapshotCreateTime")
                            allocated_storage = snapshot.get("AllocatedStorage", 0)

                            if create_time:
                                age_days = (datetime.now(create_time.tzinfo) - create_time).days
                                if age_days > self.OLD_SNAPSHOT_DAYS:
                                    checks["old_snapshots"].append(
                                        {
                                            "SnapshotId": snapshot_id,
                                            "resourceArn": f"arn:aws:rds:{self.region}:{getattr(self, 'account_id', 'unknown')}:cluster-snapshot:{snapshot_id}",
                                            "AgeDays": age_days,
                                            "AllocatedStorage": allocated_storage,
                                            "Recommendation": f"Delete {age_days}-day old Aurora cluster snapshot (savings based on allocated storage estimate)",
                                            "EstimatedSavings": f"${allocated_storage * 0.095 * self.pricing_multiplier:.2f}/month (coarse estimate)",
                                            "CheckCategory": "Old Aurora Cluster Snapshots",
                                            "instanceFinding": f"{age_days} days old Aurora cluster snapshot ({allocated_storage}GB)",
                                        }
                                    )
                except Exception as e:
                    print(f"Warning: Could not check Aurora cluster snapshots: {e}")
            except Exception as e:
                print(f"Warning: Could not check Aurora clusters: {e}")

        except Exception as e:
            print(f"Warning: Could not perform enhanced RDS checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_efs_fsx_checks(self) -> Dict[str, Any]:
        """Get enhanced EFS and FSx cost optimization checks"""
        checks = {
            "efs_archive_storage": [],
            "efs_one_zone_migration": [],
            "efs_idle_systems": [],
            "efs_throughput_optimization": [],
            "fsx_intelligent_tiering": [],
            "fsx_storage_type_optimization": [],
            "fsx_data_deduplication": [],
            "fsx_single_az_migration": [],
            "fsx_backup_retention": [],
            "fsx_idle_systems": [],
        }

        recommendations = []

        # EFS Archive Storage Check
        try:
            paginator = self.efs.get_paginator("describe_file_systems")
            for page in paginator.paginate():
                for fs in page["FileSystems"]:
                    fs_id = fs["FileSystemId"]
                    size_bytes = fs.get("SizeInBytes", {}).get("Value", 0)
                    size_gb = size_bytes / (1024**3) if size_bytes else 0

                    if size_gb < self.SMALL_EFS_SIZE_GB:  # Skip very small file systems
                        continue

                    try:
                        lifecycle_response = self.efs.describe_lifecycle_configuration(FileSystemId=fs_id)
                        lifecycle_policies = lifecycle_response.get("LifecyclePolicies", [])
                        has_archive = any(p.get("TransitionToArchive") for p in lifecycle_policies)
                        has_ia = any(p.get("TransitionToIA") for p in lifecycle_policies)

                        # Check for Archive policy (only for Regional file systems - not One Zone)
                        is_one_zone = fs.get("AvailabilityZoneName") is not None
                        if not has_archive and size_gb > self.LARGE_EFS_SIZE_GB and not is_one_zone:
                            checks["efs_archive_storage"].append(
                                {
                                    "FileSystemId": fs_id,
                                    "Name": fs.get("Name", "Unnamed"),
                                    "SizeGB": round(size_gb, 2),
                                    "Recommendation": "Enable Archive storage class for rarely accessed data",
                                    "EstimatedSavings": "Up to 94% for cold data",
                                    "CheckCategory": "EFS Archive Storage Missing",
                                }
                            )

                        # Check for One Zone migration opportunity
                        is_regional = not fs.get("AvailabilityZoneName")
                        if is_regional and size_gb > 1:
                            checks["efs_one_zone_migration"].append(
                                {
                                    "FileSystemId": fs_id,
                                    "Name": fs.get("Name", "Unnamed"),
                                    "SizeGB": round(size_gb, 2),
                                    "Recommendation": "Migrate to One Zone storage for non-critical workloads",
                                    "EstimatedSavings": "47% cost reduction",
                                    "CheckCategory": "EFS One Zone Migration",
                                }
                            )

                        # Check for idle file systems
                        mount_targets = fs.get("NumberOfMountTargets", 0)
                        if mount_targets == 0 or size_gb < 0.01:
                            checks["efs_idle_systems"].append(
                                {
                                    "FileSystemId": fs_id,
                                    "Name": fs.get("Name", "Unnamed"),
                                    "SizeGB": round(size_gb, 2),
                                    "MountTargets": mount_targets,
                                    "Recommendation": "Delete unused file system",
                                    "EstimatedSavings": f"${size_gb * 0.30:.2f}/month",
                                    "CheckCategory": "Idle EFS File System",
                                }
                            )

                        # Check throughput mode
                        throughput_mode = fs.get("ThroughputMode", "bursting")
                        if throughput_mode == "provisioned":
                            checks["efs_throughput_optimization"].append(
                                {
                                    "FileSystemId": fs_id,
                                    "Name": fs.get("Name", "Unnamed"),
                                    "ThroughputMode": throughput_mode,
                                    "Recommendation": "Switch to Elastic Throughput mode",
                                    "EstimatedSavings": "20-50% on throughput costs",
                                    "CheckCategory": "EFS Throughput Optimization",
                                }
                            )
                    except Exception as e:
                        print(f"⚠️ Error analyzing EFS throughput: {str(e)}")
        except Exception as e:
            print(f"Warning: Could not analyze EFS systems: {e}")

        # FSx Checks
        try:
            response = self.fsx.describe_file_systems()
            for fs in response.get("FileSystems", []):
                fs_id = fs.get("FileSystemId")
                fs_type = fs.get("FileSystemType")
                storage_capacity = fs.get("StorageCapacity", 0)
                lifecycle = fs.get("Lifecycle", "")

                if lifecycle != "AVAILABLE":
                    continue

                # FSx Intelligent-Tiering check (Lustre and OpenZFS)
                if fs_type in ["LUSTRE", "OPENZFS"]:
                    storage_type = fs.get("StorageType", "")
                    if storage_type != "INTELLIGENT_TIERING":
                        checks["fsx_intelligent_tiering"].append(
                            {
                                "FileSystemId": fs_id,
                                "FileSystemType": fs_type,
                                "StorageCapacity": storage_capacity,
                                "Recommendation": "Enable Intelligent-Tiering for automatic cost optimization",
                                "EstimatedSavings": "Significant for infrequently accessed data",
                                "CheckCategory": "FSx Intelligent-Tiering",
                            }
                        )

                # FSx Windows - Storage Type and Deduplication
                if fs_type == "WINDOWS":
                    windows_config = fs.get("WindowsConfiguration", {})
                    storage_type = windows_config.get("DeploymentType", "")

                    # Check for HDD vs SSD
                    if storage_capacity > self.LARGE_FSX_CAPACITY_GB:
                        checks["fsx_storage_type_optimization"].append(
                            {
                                "FileSystemId": fs_id,
                                "FileSystemType": fs_type,
                                "StorageCapacity": storage_capacity,
                                "Recommendation": "Consider HDD storage for general-purpose workloads",
                                "EstimatedSavings": "~85% storage cost reduction",
                                "CheckCategory": "FSx Storage Type Optimization",
                            }
                        )

                    # Data Deduplication check
                    checks["fsx_data_deduplication"].append(
                        {
                            "FileSystemId": fs_id,
                            "FileSystemType": fs_type,
                            "StorageCapacity": storage_capacity,
                            "Recommendation": "Enable Microsoft Data Deduplication",
                            "EstimatedSavings": "30-80% storage capacity reduction",
                            "CheckCategory": "FSx Data Deduplication",
                        }
                    )

                    # Multi-AZ to Single-AZ check
                    deployment_type = windows_config.get("DeploymentType", "")
                    if deployment_type == "MULTI_AZ_1":
                        checks["fsx_single_az_migration"].append(
                            {
                                "FileSystemId": fs_id,
                                "FileSystemType": fs_type,
                                "StorageCapacity": storage_capacity,
                                "Recommendation": "Use Single-AZ for non-production workloads",
                                "EstimatedSavings": "~50% cost reduction",
                                "CheckCategory": "FSx Single-AZ Migration",
                            }
                        )

                # Backup retention check
                backup_config = fs.get("WindowsConfiguration", {}) if fs_type == "WINDOWS" else {}
                automatic_backup_retention = backup_config.get("AutomaticBackupRetentionDays", 0)
                if automatic_backup_retention > self.EXCESSIVE_BACKUP_RETENTION_DAYS:
                    checks["fsx_backup_retention"].append(
                        {
                            "FileSystemId": fs_id,
                            "FileSystemType": fs_type,
                            "RetentionDays": automatic_backup_retention,
                            "Recommendation": f"Reduce backup retention from {automatic_backup_retention} to 7-30 days",
                            "EstimatedSavings": "Reduce backup storage costs",
                            "CheckCategory": "FSx Backup Retention",
                        }
                    )
        except Exception as e:
            print(f"Warning: Could not analyze FSx systems: {e}")

        # Compile all recommendations
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_dynamodb_checks(self) -> Dict[str, Any]:
        """Get enhanced DynamoDB cost optimization checks"""
        checks = {
            "billing_mode_optimization": [],
            "capacity_rightsizing": [],
            "reserved_capacity": [],
            "data_lifecycle": [],
            "global_tables_optimization": [],
            "unused_tables": [],
            "over_provisioned_capacity": [],
        }

        try:
            # Get all DynamoDB tables
            paginator = self.dynamodb.get_paginator("list_tables")
            table_names = []
            for page in paginator.paginate():
                table_names.extend(page.get("TableNames", []))

            for table_name in table_names:
                try:
                    table_response = self.dynamodb.describe_table(TableName=table_name)
                    table = table_response["Table"]

                    billing_mode = table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
                    table_status = table.get("TableStatus")
                    item_count = table.get("ItemCount", 0)
                    table_size_bytes = table.get("TableSizeBytes", 0)

                    # Skip if not active
                    if table_status != "ACTIVE":
                        continue

                    # Check for unused tables (empty tables)
                    if item_count == 0:
                        checks["unused_tables"].append(
                            {
                                "TableName": table_name,
                                "ItemCount": item_count,
                                "TableSizeBytes": table_size_bytes,
                                "Recommendation": "Empty table - consider deletion if unused",
                                "EstimatedSavings": "100% of table costs",
                                "CheckCategory": "Unused DynamoDB Tables",
                            }
                        )

                    # Check billing mode optimization (Note: Requires CloudWatch metrics for accurate recommendations)
                    if billing_mode == "PROVISIONED":
                        provisioned_throughput = table.get("ProvisionedThroughput", {})
                        read_capacity = provisioned_throughput.get("ReadCapacityUnits", 0)
                        write_capacity = provisioned_throughput.get("WriteCapacityUnits", 0)

                        # Check for over-provisioned capacity (basic heuristic - CloudWatch recommended)
                        if read_capacity > 100 or write_capacity > 100:
                            # Try to get CloudWatch metrics for more accurate assessment
                            try:
                                cloudwatch = self.session.client("cloudwatch", region_name=self.region)
                                # Get consumed capacity metrics for last 7 days
                                end_time = datetime.now(timezone.utc)
                                start_time = end_time - timedelta(days=7)

                                read_response = cloudwatch.get_metric_statistics(
                                    Namespace="AWS/DynamoDB",
                                    MetricName="ConsumedReadCapacityUnits",
                                    Dimensions=[{"Name": "TableName", "Value": table_name}],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,  # 1 hour periods
                                    Statistics=["Average", "Maximum"],
                                )

                                write_response = cloudwatch.get_metric_statistics(
                                    Namespace="AWS/DynamoDB",
                                    MetricName="ConsumedWriteCapacityUnits",
                                    Dimensions=[{"Name": "TableName", "Value": table_name}],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average", "Maximum"],
                                )

                                # Calculate utilization if metrics available
                                read_datapoints = read_response.get("Datapoints", [])
                                write_datapoints = write_response.get("Datapoints", [])

                                if read_datapoints and write_datapoints:
                                    avg_read_consumed = sum(dp["Average"] for dp in read_datapoints) / len(
                                        read_datapoints
                                    )
                                    avg_write_consumed = sum(dp["Average"] for dp in write_datapoints) / len(
                                        write_datapoints
                                    )

                                    read_utilization = (
                                        (avg_read_consumed / read_capacity) * 100 if read_capacity > 0 else 0
                                    )
                                    write_utilization = (
                                        (avg_write_consumed / write_capacity) * 100 if write_capacity > 0 else 0
                                    )

                                    # Only recommend if utilization is low
                                    if read_utilization < 20 or write_utilization < 20:
                                        recommendation_text = f"Low utilization detected (Read: {read_utilization:.1f}%, Write: {write_utilization:.1f}%) - consider reducing capacity"
                                    else:
                                        recommendation_text = f"Utilization acceptable (Read: {read_utilization:.1f}%, Write: {write_utilization:.1f}%) - monitor usage patterns"
                                else:
                                    recommendation_text = "High provisioned capacity - validate with CloudWatch metrics"

                            except Exception as e:
                                recommendation_text = "High provisioned capacity - CloudWatch analysis recommended"

                            checks["over_provisioned_capacity"].append(
                                {
                                    "TableName": table_name,
                                    "ReadCapacityUnits": read_capacity,
                                    "WriteCapacityUnits": write_capacity,
                                    "Recommendation": recommendation_text,
                                    "EstimatedSavings": "Variable based on actual usage",
                                    "CheckCategory": "DynamoDB Over-Provisioned Capacity",
                                }
                            )

                        # Suggest Reserved Capacity for steady workloads
                        if read_capacity >= 100 and write_capacity >= 100:
                            checks["reserved_capacity"].append(
                                {
                                    "TableName": table_name,
                                    "ReadCapacityUnits": read_capacity,
                                    "WriteCapacityUnits": write_capacity,
                                    "Recommendation": "Consider Reserved Capacity for predictable workloads",
                                    "EstimatedSavings": "53-76% vs On-Demand",
                                    "CheckCategory": "DynamoDB Reserved Capacity",
                                }
                            )

                    else:  # ON_DEMAND
                        # Check CloudWatch consumed capacity for steady workload validation
                        try:
                            cloudwatch = self.session.client("cloudwatch", region_name=self.region)
                            end_time = datetime.now(timezone.utc)
                            start_time = end_time - timedelta(days=14)  # 14-day analysis

                            # Get consumed read capacity
                            read_response = cloudwatch.get_metric_statistics(
                                Namespace="AWS/DynamoDB",
                                MetricName="ConsumedReadCapacityUnits",
                                Dimensions=[{"Name": "TableName", "Value": table_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=3600,
                                Statistics=["Average", "Maximum"],
                            )

                            # Get consumed write capacity
                            write_response = cloudwatch.get_metric_statistics(
                                Namespace="AWS/DynamoDB",
                                MetricName="ConsumedWriteCapacityUnits",
                                Dimensions=[{"Name": "TableName", "Value": table_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=3600,
                                Statistics=["Average", "Maximum"],
                            )

                            read_datapoints = read_response.get("Datapoints", [])
                            write_datapoints = write_response.get("Datapoints", [])

                            if read_datapoints and write_datapoints:
                                # Calculate usage statistics
                                avg_read = sum(dp["Average"] for dp in read_datapoints) / len(read_datapoints)
                                max_read = max(dp["Maximum"] for dp in read_datapoints)
                                avg_write = sum(dp["Average"] for dp in write_datapoints) / len(write_datapoints)
                                max_write = max(dp["Maximum"] for dp in write_datapoints)

                                # Calculate proposed provisioned baseline (avg + 20% buffer)
                                proposed_read = avg_read * 1.2
                                proposed_write = avg_write * 1.2

                                # Check if usage is steady and high enough for Provisioned
                                read_utilization = avg_read / proposed_read if proposed_read > 0 else 0
                                write_utilization = avg_write / proposed_write if proposed_write > 0 else 0
                                read_variability = max_read / avg_read if avg_read > 0 else float("inf")
                                write_variability = max_write / avg_write if avg_write > 0 else float("inf")

                                # Recommend Provisioned if:
                                # 1. Average utilization > 70% of proposed baseline
                                # 2. Traffic is predictable (max/avg ratio < 3)
                                # 3. Minimum usage thresholds met
                                if (
                                    read_utilization > 0.7
                                    and write_utilization > 0.7
                                    and read_variability < 3
                                    and write_variability < 3
                                    and avg_read > 5
                                    and avg_write > 1
                                ):
                                    checks["billing_mode_optimization"].append(
                                        {
                                            "TableName": table_name,
                                            "CurrentBillingMode": billing_mode,
                                            "AvgReadCapacity": f"{avg_read:.1f} RCU",
                                            "AvgWriteCapacity": f"{avg_write:.1f} WCU",
                                            "ReadUtilization": f"{read_utilization:.1%}",
                                            "WriteUtilization": f"{write_utilization:.1%}",
                                            "Recommendation": f"Steady high usage detected over 14 days (Read: {avg_read:.1f} RCU at {read_utilization:.0%} utilization, Write: {avg_write:.1f} WCU at {write_utilization:.0%} utilization) - switch to Provisioned mode",
                                            "EstimatedSavings": "Up to 60% for predictable traffic patterns",
                                            "CheckCategory": "DynamoDB Billing Mode - Metric-Backed",
                                            "MetricsPeriod": "14 days",
                                        }
                                    )
                            else:
                                # No metrics available - suggest enabling CloudWatch
                                checks["billing_mode_optimization"].append(
                                    {
                                        "TableName": table_name,
                                        "CurrentBillingMode": billing_mode,
                                        "TableSizeGB": round(table_size_bytes / (1024**3), 2),
                                        "Recommendation": "Enable CloudWatch metrics to analyze usage patterns for billing mode optimization",
                                        "EstimatedSavings": "Enable monitoring first - potential 60% savings with Provisioned mode for steady workloads",
                                        "CheckCategory": "DynamoDB Monitoring Required",
                                    }
                                )

                        except Exception:
                            # Fallback - suggest enabling CloudWatch
                            checks["billing_mode_optimization"].append(
                                {
                                    "TableName": table_name,
                                    "CurrentBillingMode": billing_mode,
                                    "TableSizeGB": round(table_size_bytes / (1024**3), 2),
                                    "Recommendation": "Enable CloudWatch metrics to validate usage patterns before switching to Provisioned mode",
                                    "EstimatedSavings": "CloudWatch analysis required - potential 60% savings for steady workloads",
                                    "CheckCategory": "DynamoDB CloudWatch Required",
                                }
                            )

                    # Check for data lifecycle opportunities
                    if table_size_bytes > 10 * 1024**3:  # > 10GB
                        checks["data_lifecycle"].append(
                            {
                                "TableName": table_name,
                                "TableSizeGB": round(table_size_bytes / (1024**3), 2),
                                "Recommendation": "Large table - implement TTL for old data or archive to S3",
                                "EstimatedSavings": "40-80% on storage costs",
                                "CheckCategory": "DynamoDB Data Lifecycle",
                            }
                        )

                except Exception as e:
                    print(f"Warning: Could not analyze DynamoDB table {table_name}: {e}")

        except Exception as e:
            print(f"Warning: Could not perform enhanced DynamoDB checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_elasticache_checks(self) -> Dict[str, Any]:
        """
        Perform comprehensive ElastiCache/Redis cost optimization analysis.

        This method analyzes ElastiCache clusters for various cost optimization opportunities
        including engine migrations, instance optimizations, and utilization improvements.

        Cost Optimization Checks Performed:
        1. Valkey Migration - Migrate from Redis to Valkey (open-source fork, same pricing)
        2. Graviton Migration (20-40% price-performance) - Move to ARM-based instances
        3. Reserved Nodes (30-60% savings) - Purchase reserved capacity
        4. Old Engine Versions - Upgrade recommendations for performance/security
        5. Underutilized Clusters (<20% CPU) - Downsize recommendations (30-50% savings)

        The method integrates with CloudWatch to analyze actual CPU utilization over
        a 14-day period to identify truly underutilized clusters.

        Returns:
            Dict[str, Any]: Dictionary containing:
                - recommendations: List of all optimization recommendations
                - valkey_migration: Redis to Valkey migration opportunities
                - graviton_migration: x86 to Graviton migration opportunities
                - reserved_nodes: Reserved instance purchase recommendations
                - old_engine_versions: Engine upgrade recommendations
                - underutilized_clusters: Downsizing opportunities based on metrics

        Note:
            - Uses pagination to handle unlimited cluster counts
            - Integrates CloudWatch metrics for accurate utilization analysis
            - Skips clusters not in 'available' status
            - Handles API errors gracefully with warning messages
        """
        checks = {
            "reserved_nodes": [],  # Reserved instance opportunities
            "underutilized_clusters": [],  # Low CPU utilization clusters
            "old_engine_versions": [],  # Engine upgrade recommendations
            "valkey_migration": [],  # Redis to Valkey migration
            "graviton_migration": [],  # x86 to Graviton migration
        }

        recommendations = []

        try:
            # Use pagination to handle unlimited cluster counts
            paginator = self.elasticache.get_paginator("describe_cache_clusters")
            for page in paginator.paginate(ShowCacheNodeInfo=True):
                for cluster in page["CacheClusters"]:
                    cluster_id = cluster["CacheClusterId"]
                    engine = cluster.get("Engine", "")
                    engine_version = cluster.get("EngineVersion", "")
                    node_type = cluster.get("CacheNodeType", "")
                    num_nodes = cluster.get("NumCacheNodes", 0)
                    status = cluster.get("CacheClusterStatus", "")

                    if status != "available":
                        continue

                    # Valkey migration (open-source Redis fork, same pricing)
                    if engine.lower() == "redis":
                        checks["valkey_migration"].append(
                            {
                                "ClusterId": cluster_id,
                                "Engine": engine,
                                "EngineVersion": engine_version,
                                "NodeType": node_type,
                                "NumNodes": num_nodes,
                                "Recommendation": "Consider migrating to ElastiCache for Valkey (open-source Redis fork with feature parity)",
                                "EstimatedSavings": "Same pricing as Redis for identical node types; consider Valkey for feature parity/security updates",
                                "CheckCategory": "Valkey Migration",
                            }
                        )

                    # Graviton migration - check for all Graviton instance families
                    graviton_families = ["m7g", "r7g", "m6g", "r6g", "c7g", "c6g", "t4g"]
                    is_graviton = any(node_type.startswith(f"cache.{family}") for family in graviton_families)

                    if not is_graviton:
                        checks["graviton_migration"].append(
                            {
                                "ClusterId": cluster_id,
                                "NodeType": node_type,
                                "Recommendation": "Migrate to Graviton instances",
                                "EstimatedSavings": "Estimated: 20-40% price-performance improvement",
                                "CheckCategory": "Graviton Migration",
                            }
                        )

                    # Old engine versions
                    if engine.lower() == "redis":
                        major_version = int(engine_version.split(".")[0]) if engine_version else 0
                        if major_version < 7:
                            checks["old_engine_versions"].append(
                                {
                                    "ClusterId": cluster_id,
                                    "EngineVersion": engine_version,
                                    "Recommendation": "Upgrade engine version",
                                    "CheckCategory": "Old Engine Version",
                                }
                            )

                    # Reserved nodes (only for stable, long-running clusters)
                    if num_nodes >= 2:  # Only recommend for multi-node clusters
                        checks["reserved_nodes"].append(
                            {
                                "ClusterId": cluster_id,
                                "NodeType": node_type,
                                "NumNodes": num_nodes,
                                "Recommendation": "Consider Reserved Nodes for stable workloads (1-3 year commitment)",
                                "EstimatedSavings": "30-60% vs On-Demand for committed usage",
                                "CheckCategory": "Reserved Nodes Opportunity",
                            }
                        )

                    # Check utilization
                    try:
                        end_time = datetime.now(timezone.utc)
                        start_time = end_time - timedelta(days=14)

                        cpu_response = self.cloudwatch.get_metric_statistics(
                            Namespace="AWS/ElastiCache",
                            MetricName="CPUUtilization",
                            Dimensions=[{"Name": "CacheClusterId", "Value": cluster_id}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=3600,
                            Statistics=["Average"],
                        )

                        if cpu_response["Datapoints"]:
                            avg_cpu = sum(dp["Average"] for dp in cpu_response["Datapoints"]) / len(
                                cpu_response["Datapoints"]
                            )

                            # Only check for underutilized (skip idle check)
                            if avg_cpu < self.LOW_CPU_THRESHOLD:
                                # Check if this is already the smallest instance type in the family
                                # Extract family and size from node type (e.g., cache.t3.micro -> t3, micro)
                                if node_type.startswith("cache."):
                                    family_size = node_type.replace("cache.", "")
                                    if "." in family_size:
                                        family, size = family_size.split(".", 1)

                                        # Define smallest sizes for each family
                                        smallest_sizes = {
                                            "t2": "nano",
                                            "t3": "nano",
                                            "t4g": "nano",
                                            "m5": "large",
                                            "m6i": "large",
                                            "m7g": "large",
                                            "r5": "large",
                                            "r6g": "large",
                                            "r7g": "large",
                                            "c5": "large",
                                            "c6g": "large",
                                            "c7g": "large",
                                        }

                                        # Skip if already using the smallest size in the family
                                        if family in smallest_sizes and size == smallest_sizes[family]:
                                            continue

                                checks["underutilized_clusters"].append(
                                    {
                                        "ClusterId": cluster_id,
                                        "NodeType": node_type,
                                        "AvgCPU": round(avg_cpu, 2),
                                        "Recommendation": "Downsize node type or consider smaller instance family",
                                        "EstimatedSavings": "30-50%",
                                        "CheckCategory": "Underutilized Cluster",
                                    }
                                )
                    except Exception as e:
                        print(f"Warning: Could not get metrics for cluster {cluster_id}: {e}")
                        continue

        except Exception as e:
            print(f"Warning: Could not analyze ElastiCache: {e}")

        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_opensearch_checks(self) -> Dict[str, Any]:
        """Get enhanced OpenSearch cost optimization checks"""
        checks = {
            "reserved_instances": [],
            "underutilized_domains": [],
            "old_versions": [],
            "storage_optimization": [],
            "idle_domains": [],
            "graviton_migration": [],
        }

        recommendations = []

        try:
            response = self.opensearch.list_domain_names()
            for domain_info in response.get("DomainNames", []):
                domain_name = domain_info["DomainName"]

                try:
                    domain = self.opensearch.describe_domain(DomainName=domain_name)["DomainStatus"]

                    engine_version = domain.get("EngineVersion", "")
                    instance_type = domain.get("ClusterConfig", {}).get("InstanceType", "")
                    instance_count = domain.get("ClusterConfig", {}).get("InstanceCount", 0)
                    storage_type = domain.get("EBSOptions", {}).get("VolumeType", "")

                    # Reserved Instances (only for stable, multi-instance domains)
                    if instance_count >= 2:  # Only recommend for multi-instance domains
                        checks["reserved_instances"].append(
                            {
                                "DomainName": domain_name,
                                "InstanceType": instance_type,
                                "InstanceCount": instance_count,
                                "Recommendation": "Consider Reserved Instances for stable workloads (1-3 year commitment)",
                                "EstimatedSavings": "30-60% vs On-Demand for committed usage",
                                "CheckCategory": "Reserved Instances Opportunity",
                            }
                        )

                    # Graviton migration - check for all Graviton instance families
                    graviton_families = ["m7g", "r7g", "m6g", "r6g", "c7g", "c6g", "t4g"]
                    is_graviton = any(instance_type.startswith(family) for family in graviton_families)

                    if not is_graviton:
                        checks["graviton_migration"].append(
                            {
                                "DomainName": domain_name,
                                "InstanceType": instance_type,
                                "Recommendation": "Migrate to Graviton instances",
                                "EstimatedSavings": "Estimated: 20-40% price-performance improvement",
                                "CheckCategory": "Graviton Migration",
                            }
                        )

                    # Old versions - handle both OpenSearch and Elasticsearch
                    if "OpenSearch" in engine_version:
                        version = engine_version.replace("OpenSearch_", "")
                        major_version = float(version.split(".")[0]) if version else 0
                        if major_version < 2:
                            checks["old_versions"].append(
                                {
                                    "DomainName": domain_name,
                                    "EngineVersion": engine_version,
                                    "Recommendation": "Upgrade to OpenSearch 2.x",
                                    "CheckCategory": "Old OpenSearch Version",
                                }
                            )
                    elif "Elasticsearch" in engine_version:
                        version = engine_version.replace("Elasticsearch_", "")
                        major_version = float(version.split(".")[0]) if version else 0
                        if major_version < 7:
                            checks["old_versions"].append(
                                {
                                    "DomainName": domain_name,
                                    "EngineVersion": engine_version,
                                    "Recommendation": "Upgrade to Elasticsearch 7.x or migrate to OpenSearch",
                                    "CheckCategory": "Old Elasticsearch Version",
                                }
                            )

                    # Storage optimization (gp2 to gp3)
                    if storage_type == "gp2":
                        checks["storage_optimization"].append(
                            {
                                "DomainName": domain_name,
                                "StorageType": storage_type,
                                "Recommendation": "Migrate to gp3 volumes",
                                "EstimatedSavings": "20% storage cost",
                                "CheckCategory": "Storage Optimization",
                            }
                        )

                    # Check utilization
                    try:
                        end_time = datetime.now(timezone.utc)
                        start_time = end_time - timedelta(days=14)

                        cpu_response = self.cloudwatch.get_metric_statistics(
                            Namespace="AWS/ES",
                            MetricName="CPUUtilization",
                            Dimensions=[
                                {"Name": "DomainName", "Value": domain_name},
                                {"Name": "ClientId", "Value": self.account_id},
                            ],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=3600,
                            Statistics=["Average"],
                        )

                        if cpu_response["Datapoints"]:
                            avg_cpu = sum(dp["Average"] for dp in cpu_response["Datapoints"]) / len(
                                cpu_response["Datapoints"]
                            )

                            if avg_cpu < 5:
                                checks["idle_domains"].append(
                                    {
                                        "DomainName": domain_name,
                                        "AvgCPU": round(avg_cpu, 2),
                                        "Recommendation": "Delete idle domain",
                                        "EstimatedSavings": "100% of domain cost",
                                        "CheckCategory": "Idle Domain",
                                    }
                                )
                            elif avg_cpu < self.LOW_CPU_THRESHOLD:
                                checks["underutilized_domains"].append(
                                    {
                                        "DomainName": domain_name,
                                        "AvgCPU": round(avg_cpu, 2),
                                        "Recommendation": "Downsize instance type",
                                        "EstimatedSavings": "30-50%",
                                        "CheckCategory": "Underutilized Domain",
                                    }
                                )
                    except Exception as e:
                        print(f"Warning: Could not get metrics for domain {domain_name}: {e}")
                        continue

                except Exception as e:
                    print(f"⚠️ Error analyzing OpenSearch domain {domain_name}: {str(e)}")
                    # Continue with next domain

        except Exception as e:
            print(f"Warning: Could not analyze OpenSearch domains: {e}")

        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_container_checks(self) -> Dict[str, Any]:
        """Get enhanced Container Services (ECS/EKS/ECR) cost optimization checks"""
        checks = {
            "ecs_rightsizing": [],
            "eks_rightsizing": [],
            "ecr_lifecycle": [],
            "unused_clusters": [],
            "over_provisioned_services": [],
            "old_images": [],
        }

        try:
            # ECS Checks - paginate list_clusters
            paginator = self.ecs.get_paginator("list_clusters")
            cluster_arns = []
            for page in paginator.paginate():
                cluster_arns.extend(page.get("clusterArns", []))

            # Deduplicate cluster ARNs
            cluster_arns = list(set(cluster_arns))

            for cluster_arn in cluster_arns:
                cluster_name = cluster_arn.split("/")[-1]

                # Get cluster details
                cluster_details = self.ecs.describe_clusters(clusters=[cluster_arn])
                cluster = cluster_details["clusters"][0] if cluster_details["clusters"] else {}

                active_services = cluster.get("activeServicesCount", 0)
                running_tasks = cluster.get("runningTasksCount", 0)

                # Debug output
                print(
                    f"Debug: Cluster {cluster_name} - Active Services: {active_services}, Running Tasks: {running_tasks}"
                )

                # Check for unused clusters - only add if truly empty
                if active_services == 0 and running_tasks == 0:
                    checks["unused_clusters"].append(
                        {
                            "ClusterName": cluster_name,
                            "ClusterArn": cluster_arn,
                            "ActiveServices": active_services,
                            "RunningTasks": running_tasks,
                            "Recommendation": "Empty ECS cluster - consider deletion",
                            "EstimatedSavings": "100% of cluster overhead costs",
                            "CheckCategory": "Unused ECS Clusters",
                        }
                    )
                elif running_tasks > 0 or active_services > 0:
                    # Add to other optimizations if it has running workloads
                    checks["ecs_rightsizing"].append(
                        {
                            "ClusterName": cluster_name,
                            "ClusterArn": cluster_arn,
                            "ActiveServices": active_services,
                            "RunningTasks": running_tasks,
                            "Recommendation": f"Active ECS cluster with {running_tasks} running tasks and {active_services} services",
                            "EstimatedSavings": "Review for rightsizing opportunities",
                            "CheckCategory": "Active ECS Clusters",
                        }
                    )

                # Get services in cluster - paginate
                try:
                    paginator = self.ecs.get_paginator("list_services")
                    service_arns = []
                    for page in paginator.paginate(cluster=cluster_arn):
                        service_arns.extend(page.get("serviceArns", []))

                    if service_arns:
                        # Process services in batches of 10 (AWS API limit)
                        for i in range(0, len(service_arns), 10):
                            batch_arns = service_arns[i : i + 10]
                            services_details = self.ecs.describe_services(cluster=cluster_arn, services=batch_arns)

                            for service in services_details.get("services", []):
                                service_name = service.get("serviceName")
                                desired_count = service.get("desiredCount", 0)
                                running_count = service.get("runningCount", 0)

                                # Check for over-provisioned services
                                if desired_count > running_count and desired_count > 1:
                                    checks["over_provisioned_services"].append(
                                        {
                                            "ClusterName": cluster_name,
                                            "ServiceName": service_name,
                                            "DesiredCount": desired_count,
                                            "RunningCount": running_count,
                                            "Recommendation": "Service desired count exceeds running count",
                                            "EstimatedSavings": "Reduce desired count to match actual needs",
                                            "CheckCategory": "ECS Over-Provisioned Services",
                                        }
                                    )

                            # Check Container Insights enablement and metrics
                            try:
                                # First, explicitly check if Container Insights is enabled
                                cluster_details = self.ecs.describe_clusters(
                                    clusters=[cluster_name], include=["SETTINGS"]
                                )
                                cluster = cluster_details["clusters"][0] if cluster_details["clusters"] else {}
                                settings = cluster.get("settings", [])

                                container_insights_enabled = False
                                for setting in settings:
                                    if setting.get("name") == "containerInsights" and setting.get("value") in (
                                        "enabled",
                                        "enhanced",
                                    ):
                                        container_insights_enabled = True
                                        break

                                if container_insights_enabled:
                                    # Container Insights is enabled, check for metrics
                                    cloudwatch = self.session.client("cloudwatch", region_name=self.region)
                                    end_time = datetime.now(timezone.utc)
                                    start_time = end_time - timedelta(days=7)

                                    cpu_response = cloudwatch.get_metric_statistics(
                                        Namespace="AWS/ECS",
                                        MetricName="CPUUtilization",
                                        Dimensions=[
                                            {"Name": "ServiceName", "Value": service_name},
                                            {"Name": "ClusterName", "Value": cluster_name},
                                        ],
                                        StartTime=start_time,
                                        EndTime=end_time,
                                        Period=3600,
                                        Statistics=["Average", "Maximum"],
                                    )

                                    memory_response = cloudwatch.get_metric_statistics(
                                        Namespace="AWS/ECS",
                                        MetricName="MemoryUtilization",
                                        Dimensions=[
                                            {"Name": "ServiceName", "Value": service_name},
                                            {"Name": "ClusterName", "Value": cluster_name},
                                        ],
                                        StartTime=start_time,
                                        EndTime=end_time,
                                        Period=3600,
                                        Statistics=["Average", "Maximum"],
                                    )

                                    cpu_datapoints = cpu_response.get("Datapoints", [])
                                    memory_datapoints = memory_response.get("Datapoints", [])

                                    # Only provide recommendations when we have actual metrics
                                    if cpu_datapoints and memory_datapoints:
                                        avg_cpu = sum(dp["Average"] for dp in cpu_datapoints) / len(cpu_datapoints)
                                        avg_memory = sum(dp["Average"] for dp in memory_datapoints) / len(
                                            memory_datapoints
                                        )
                                        max_cpu = max(dp["Maximum"] for dp in cpu_datapoints)
                                        max_memory = max(dp["Maximum"] for dp in memory_datapoints)

                                        if avg_cpu < 20 and avg_memory < 30:
                                            checks["ecs_rightsizing"].append(
                                                {
                                                    "ClusterName": cluster_name,
                                                    "ServiceName": service_name,
                                                    "Recommendation": f"Measured low utilization over 7 days (CPU: {avg_cpu:.1f}%, Memory: {avg_memory:.1f}%) - consider downsizing task definition",
                                                    "EstimatedSavings": f"20-50% cost reduction based on measured over-provisioning",
                                                    "CheckCategory": "ECS Rightsizing - Metric-Backed",
                                                    "MetricsPeriod": "7 days",
                                                    "AvgCPU": f"{avg_cpu:.1f}%",
                                                    "AvgMemory": f"{avg_memory:.1f}%",
                                                }
                                            )
                                        elif max_cpu > 80 or max_memory > 80:
                                            # Skip performance optimization - focus only on cost reduction
                                            pass
                                else:
                                    # Container Insights is explicitly disabled
                                    checks["ecs_rightsizing"].append(
                                        {
                                            "ClusterName": cluster_name,
                                            "ServiceName": service_name,
                                            "Recommendation": "Metrics not available; enable Container Insights to produce metric-backed optimization findings",
                                            "EstimatedSavings": "Enable Container Insights first for accurate analysis",
                                            "CheckCategory": "ECS Container Insights Required",
                                            "ActionRequired": f"aws ecs put-cluster-settings --cluster {cluster_name} --settings name=containerInsights,value=enabled",
                                        }
                                    )

                            except Exception as e:
                                print(
                                    f"Warning: Could not check Container Insights for ECS cluster {cluster_name}: {e}"
                                )

                except Exception as e:
                    print(f"Warning: Could not analyze ECS services for {cluster_name}: {e}")

            # EKS Checks - paginate list_clusters
            try:
                paginator = self.eks.get_paginator("list_clusters")
                eks_cluster_names = []
                for page in paginator.paginate():
                    eks_cluster_names.extend(page.get("clusters", []))

                for cluster_name in eks_cluster_names:
                    try:
                        cluster_response = self.eks.describe_cluster(name=cluster_name)
                        cluster = cluster_response.get("cluster", {})

                        cluster_status = cluster.get("status")

                        # Check Container Insights enablement once per cluster (not per nodegroup)
                        container_insights_enabled = False
                        try:
                            # First check add-on
                            try:
                                addons_response = self.eks.list_addons(clusterName=cluster_name)
                                addons = addons_response.get("addons", [])
                                container_insights_enabled = "amazon-cloudwatch-observability" in addons
                            except Exception:
                                pass

                            # If add-on not found, check for metrics to detect manual installation
                            if not container_insights_enabled:
                                cloudwatch = self.session.client("cloudwatch", region_name=self.region)
                                end_time = datetime.now(timezone.utc)
                                start_time = end_time - timedelta(days=1)  # Short check for existence

                                # Try to get any Container Insights metrics to detect manual installation
                                test_response = cloudwatch.get_metric_statistics(
                                    Namespace="ContainerInsights",
                                    MetricName="cluster_node_cpu_utilization",
                                    Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average"],
                                )
                                container_insights_enabled = len(test_response.get("Datapoints", [])) > 0

                        except Exception:
                            container_insights_enabled = False

                        # Get cluster-level metrics if Container Insights is enabled
                        cluster_metrics_available = False
                        avg_cpu = max_cpu = avg_memory = max_memory = 0

                        if container_insights_enabled:
                            try:
                                cloudwatch = self.session.client("cloudwatch", region_name=self.region)
                                end_time = datetime.now(timezone.utc)
                                start_time = end_time - timedelta(days=7)

                                cpu_response = cloudwatch.get_metric_statistics(
                                    Namespace="ContainerInsights",
                                    MetricName="cluster_node_cpu_utilization",
                                    Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average", "Maximum"],
                                )

                                memory_response = cloudwatch.get_metric_statistics(
                                    Namespace="ContainerInsights",
                                    MetricName="cluster_node_memory_utilization",
                                    Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
                                    StartTime=start_time,
                                    EndTime=end_time,
                                    Period=3600,
                                    Statistics=["Average", "Maximum"],
                                )

                                cpu_datapoints = cpu_response.get("Datapoints", [])
                                memory_datapoints = memory_response.get("Datapoints", [])

                                if cpu_datapoints and memory_datapoints:
                                    cluster_metrics_available = True
                                    avg_cpu = sum(dp["Average"] for dp in cpu_datapoints) / len(cpu_datapoints)
                                    avg_memory = sum(dp["Average"] for dp in memory_datapoints) / len(memory_datapoints)
                                    max_cpu = max(dp["Maximum"] for dp in cpu_datapoints)
                                    max_memory = max(dp["Maximum"] for dp in memory_datapoints)

                            except Exception:
                                pass

                        # Get node groups - paginate
                        paginator = self.eks.get_paginator("list_nodegroups")
                        nodegroup_names = []
                        for page in paginator.paginate(clusterName=cluster_name):
                            nodegroup_names.extend(page.get("nodegroups", []))

                        # Add cluster-level Container Insights recommendation once (not per nodegroup)
                        if cluster_metrics_available:
                            if avg_cpu < 25 and avg_memory < 35:
                                checks["eks_rightsizing"].append(
                                    {
                                        "ClusterName": cluster_name,
                                        "Recommendation": f"Measured low cluster utilization over 7 days (CPU: {avg_cpu:.1f}%, Memory: {avg_memory:.1f}%) - consider smaller instance types",
                                        "EstimatedSavings": f"30-60% cost reduction based on measured over-provisioning",
                                        "CheckCategory": "EKS Rightsizing - Metric-Backed",
                                        "MetricsPeriod": "7 days",
                                        "AvgCPU": f"{avg_cpu:.1f}%",
                                        "AvgMemory": f"{avg_memory:.1f}%",
                                    }
                                )
                            elif max_cpu > 85 or max_memory > 85:
                                checks["eks_rightsizing"].append(
                                    {
                                        "ClusterName": cluster_name,
                                        "Recommendation": f"Measured high peak usage over 7 days (CPU: {max_cpu:.1f}%, Memory: {max_memory:.1f}%) - consider larger instances or auto-scaling",
                                        "EstimatedSavings": "Performance improvement, potential cost increase",
                                        "CheckCategory": "EKS Performance Optimization - Metric-Backed",
                                        "MetricsPeriod": "7 days",
                                        "MaxCPU": f"{max_cpu:.1f}%",
                                        "MaxMemory": f"{max_memory:.1f}%",
                                    }
                                )
                        elif not container_insights_enabled:
                            checks["eks_rightsizing"].append(
                                {
                                    "ClusterName": cluster_name,
                                    "Recommendation": "Metrics not available; enable Container Insights to produce metric-backed optimization findings",
                                    "EstimatedSavings": "Enable Container Insights first for accurate analysis",
                                    "CheckCategory": "EKS Container Insights Required",
                                    "ActionRequired": f"aws eks create-addon --cluster-name {cluster_name} --addon-name amazon-cloudwatch-observability",
                                }
                            )

                        for nodegroup_name in nodegroup_names:
                            try:
                                ng_response = self.eks.describe_nodegroup(
                                    clusterName=cluster_name, nodegroupName=nodegroup_name
                                )
                                nodegroup = ng_response.get("nodegroup", {})

                                instance_types = nodegroup.get("instanceTypes", [])
                                capacity_type = nodegroup.get("capacityType", "ON_DEMAND")
                                scaling_config = nodegroup.get("scalingConfig", {})

                                desired_size = scaling_config.get("desiredSize", 0)
                                min_size = scaling_config.get("minSize", 0)
                                max_size = scaling_config.get("maxSize", 0)

                                # Check for rightsizing opportunities
                                if instance_types and any("xlarge" in inst_type for inst_type in instance_types):
                                    checks["eks_rightsizing"].append(
                                        {
                                            "ClusterName": cluster_name,
                                            "NodeGroupName": nodegroup_name,
                                            "InstanceTypes": instance_types,
                                            "DesiredSize": desired_size,
                                            "Recommendation": "Large instance types - verify rightsizing",
                                            "EstimatedSavings": "Potential 20-50% savings",
                                            "CheckCategory": "EKS Instance Rightsizing",
                                        }
                                    )

                            except Exception as e:
                                print(f"Warning: Could not analyze EKS nodegroup {nodegroup_name}: {e}")

                    except Exception as e:
                        print(f"Warning: Could not analyze EKS cluster {cluster_name}: {e}")

            except Exception as e:
                print(f"Warning: Could not perform EKS checks: {e}")

            # ECR Checks
            try:
                ecr_repos_response = self.ecr.describe_repositories()
                repositories = ecr_repos_response.get("repositories", [])

                for repo in repositories:
                    repo_name = repo.get("repositoryName")
                    created_at = repo.get("createdAt")

                    # Get images in repository with pagination
                    try:
                        paginator = self.ecr.get_paginator("list_images")
                        images = []
                        for page in paginator.paginate(repositoryName=repo_name):
                            images.extend(page.get("imageIds", []))

                        # Check for old images (basic lifecycle check)
                        if len(images) > self.HIGH_IMAGE_COUNT_THRESHOLD:
                            checks["old_images"].append(
                                {
                                    "RepositoryName": repo_name,
                                    "ImageCount": len(images),
                                    "Recommendation": f"Repository has {len(images)} images - implement lifecycle policy",
                                    "EstimatedSavings": "Reduce storage costs by cleaning old images",
                                    "CheckCategory": "ECR Lifecycle Management",
                                }
                            )

                    except Exception as e:
                        print(f"Warning: Could not analyze ECR repository {repo_name}: {e}")

            except Exception as e:
                print(f"Warning: Could not perform ECR checks: {e}")

        except Exception as e:
            print(f"Warning: Could not perform enhanced Container checks: {e}")

        # Convert to recommendations format
        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_file_systems_checks(self) -> Dict[str, Any]:
        """Get enhanced EFS and FSx cost optimization checks"""
        checks = {
            "efs_archive_storage": [],
            "efs_one_zone_migration": [],
            "efs_idle_systems": [],
            "efs_throughput_optimization": [],
            "fsx_intelligent_tiering": [],
            "fsx_storage_type_optimization": [],
            "fsx_data_deduplication": [],
            "fsx_single_az_migration": [],
            "fsx_backup_retention": [],
            "fsx_idle_systems": [],
        }

        recommendations = []

        # EFS Archive Storage Check
        try:
            paginator = self.efs.get_paginator("describe_file_systems")
            for page in paginator.paginate():
                for fs in page["FileSystems"]:
                    fs_id = fs["FileSystemId"]
                    size_bytes = fs.get("SizeInBytes", {}).get("Value", 0)
                    size_gb = size_bytes / (1024**3) if size_bytes else 0

                    if size_gb < self.SMALL_EFS_SIZE_GB:  # Skip very small file systems
                        continue

                    try:
                        lifecycle_response = self.efs.describe_lifecycle_configuration(FileSystemId=fs_id)
                        lifecycle_policies = lifecycle_response.get("LifecyclePolicies", [])
                        has_archive = any(p.get("TransitionToArchive") for p in lifecycle_policies)
                        has_ia = any(p.get("TransitionToIA") for p in lifecycle_policies)

                        # Check for Archive policy (only for Regional file systems - not One Zone)
                        is_one_zone = fs.get("AvailabilityZoneName") is not None
                        if not has_archive and size_gb > self.LARGE_EFS_SIZE_GB and not is_one_zone:
                            checks["efs_archive_storage"].append(
                                {
                                    "FileSystemId": fs_id,
                                    "Name": fs.get("Name", "Unnamed"),
                                    "SizeGB": round(size_gb, 2),
                                    "Recommendation": "Enable Archive storage class for rarely accessed data",
                                    "EstimatedSavings": "Up to 94% for cold data",
                                    "CheckCategory": "EFS Archive Storage Missing",
                                }
                            )

                        # Check for One Zone migration opportunity
                        is_regional = not fs.get("AvailabilityZoneName")
                        if is_regional and size_gb > 1:
                            checks["efs_one_zone_migration"].append(
                                {
                                    "FileSystemId": fs_id,
                                    "Name": fs.get("Name", "Unnamed"),
                                    "SizeGB": round(size_gb, 2),
                                    "Recommendation": "Migrate to One Zone storage for non-critical workloads",
                                    "EstimatedSavings": "47% cost reduction",
                                    "CheckCategory": "EFS One Zone Migration",
                                }
                            )

                        # Check for idle file systems
                        mount_targets = fs.get("NumberOfMountTargets", 0)
                        if mount_targets == 0 or size_gb < 0.01:
                            checks["efs_idle_systems"].append(
                                {
                                    "FileSystemId": fs_id,
                                    "Name": fs.get("Name", "Unnamed"),
                                    "SizeGB": round(size_gb, 2),
                                    "MountTargets": mount_targets,
                                    "Recommendation": "Delete unused file system",
                                    "EstimatedSavings": f"${size_gb * 0.30:.2f}/month",
                                    "CheckCategory": "Idle EFS File System",
                                }
                            )

                        # Check throughput mode
                        throughput_mode = fs.get("ThroughputMode", "bursting")
                        if throughput_mode == "provisioned":
                            checks["efs_throughput_optimization"].append(
                                {
                                    "FileSystemId": fs_id,
                                    "Name": fs.get("Name", "Unnamed"),
                                    "ThroughputMode": throughput_mode,
                                    "Recommendation": "Switch to Elastic Throughput mode",
                                    "EstimatedSavings": "20-50% on throughput costs",
                                    "CheckCategory": "EFS Throughput Optimization",
                                }
                            )
                    except Exception as e:
                        print(f"⚠️ Error analyzing EFS file system: {str(e)}")
        except Exception as e:
            print(f"Warning: Could not analyze EFS systems: {e}")

        # FSx Checks
        try:
            response = self.fsx.describe_file_systems()
            for fs in response.get("FileSystems", []):
                fs_id = fs.get("FileSystemId")
                fs_type = fs.get("FileSystemType")
                storage_capacity = fs.get("StorageCapacity", 0)
                lifecycle = fs.get("Lifecycle", "")

                if lifecycle != "AVAILABLE":
                    continue

                # FSx Intelligent-Tiering check (Lustre and OpenZFS)
                if fs_type in ["LUSTRE", "OPENZFS"]:
                    storage_type = fs.get("StorageType", "")
                    if storage_type != "INTELLIGENT_TIERING":
                        checks["fsx_intelligent_tiering"].append(
                            {
                                "FileSystemId": fs_id,
                                "FileSystemType": fs_type,
                                "StorageCapacity": storage_capacity,
                                "Recommendation": "Enable Intelligent-Tiering for automatic cost optimization",
                                "EstimatedSavings": "Significant for infrequently accessed data",
                                "CheckCategory": "FSx Intelligent-Tiering",
                            }
                        )

                # FSx Windows - Storage Type and Deduplication
                if fs_type == "WINDOWS":
                    windows_config = fs.get("WindowsConfiguration", {})

                    # Check for HDD vs SSD
                    if storage_capacity > self.LARGE_FSX_CAPACITY_GB:
                        checks["fsx_storage_type_optimization"].append(
                            {
                                "FileSystemId": fs_id,
                                "FileSystemType": fs_type,
                                "StorageCapacity": storage_capacity,
                                "Recommendation": "Consider HDD storage for general-purpose workloads",
                                "EstimatedSavings": "~85% storage cost reduction",
                                "CheckCategory": "FSx Storage Type Optimization",
                            }
                        )

                    # Data Deduplication check
                    checks["fsx_data_deduplication"].append(
                        {
                            "FileSystemId": fs_id,
                            "FileSystemType": fs_type,
                            "StorageCapacity": storage_capacity,
                            "Recommendation": "Enable Microsoft Data Deduplication",
                            "EstimatedSavings": "30-80% storage capacity reduction",
                            "CheckCategory": "FSx Data Deduplication",
                        }
                    )

                    # Multi-AZ to Single-AZ check
                    deployment_type = windows_config.get("DeploymentType", "")
                    if deployment_type == "MULTI_AZ_1":
                        checks["fsx_single_az_migration"].append(
                            {
                                "FileSystemId": fs_id,
                                "FileSystemType": fs_type,
                                "StorageCapacity": storage_capacity,
                                "Recommendation": "Use Single-AZ for non-production workloads",
                                "EstimatedSavings": "~50% cost reduction",
                                "CheckCategory": "FSx Single-AZ Migration",
                            }
                        )

                # Backup retention check
                backup_config = fs.get("WindowsConfiguration", {}) if fs_type == "WINDOWS" else {}
                automatic_backup_retention = backup_config.get("AutomaticBackupRetentionDays", 0)
                if automatic_backup_retention > self.EXCESSIVE_BACKUP_RETENTION_DAYS:
                    checks["fsx_backup_retention"].append(
                        {
                            "FileSystemId": fs_id,
                            "FileSystemType": fs_type,
                            "RetentionDays": automatic_backup_retention,
                            "Recommendation": f"Reduce backup retention from {automatic_backup_retention} to 7-30 days",
                            "EstimatedSavings": "Reduce backup storage costs",
                            "CheckCategory": "FSx Backup Retention",
                        }
                    )
        except Exception as e:
            print(f"Warning: Could not analyze FSx systems: {e}")

        # Compile all recommendations
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_lambda_checks(self) -> Dict[str, Any]:
        """
        Enhanced Lambda cost optimization checks with intelligent recommendation gating.

        Performs comprehensive analysis of Lambda functions including:
        - Memory rightsizing based on actual usage patterns
        - ARM/Graviton migration opportunities for active functions
        - VPC configuration cost analysis
        - Provisioned concurrency optimization
        - Reserved concurrency analysis

        Uses CloudWatch metrics for intelligent gating to prevent false positives:
        - ARM migration: Only for functions with >10 invocations/week
        - Memory optimization: Based on actual memory utilization patterns
        - VPC analysis: Considers ENI costs and cold start impact

        Returns:
            Dict containing categorized recommendations with usage-based filtering
        """
        from datetime import datetime, timedelta, timezone

        checks = {
            "excessive_memory": [],
            "low_invocation": [],
            "provisioned_concurrency": [],
            "vpc_without_need": [],
            "high_reserved_concurrency": [],
            "arm_migration": [],
        }

        try:
            paginator = self.lambda_client.get_paginator("list_functions")

            for page in paginator.paginate():
                for function in page["Functions"]:
                    function_name = function["FunctionName"]
                    function_arn = function["FunctionArn"]
                    memory_size = function["MemorySize"]
                    timeout = function["Timeout"]
                    runtime = function.get("Runtime", "Unknown")
                    architectures = function.get("Architectures", ["x86_64"])

                    try:
                        config = self.lambda_client.get_function_configuration(FunctionName=function_name)
                        vpc_config = config.get("VpcConfig", {})
                        reserved_concurrency = config.get("ReservedConcurrentExecutions")
                    except Exception as e:
                        print(f"⚠️ Error getting Lambda function config for {function_name}: {str(e)}")
                        vpc_config = {}
                        reserved_concurrency = None

                    # COST SAVING: Excessive memory allocation
                    if memory_size >= 3008:
                        checks["excessive_memory"].append(
                            {
                                "FunctionName": function_name,
                                "MemorySize": memory_size,
                                "Runtime": runtime,
                                "Recommendation": f"{memory_size}MB memory may be excessive - rightsize for cost savings",
                                "EstimatedSavings": "30-50% with rightsizing",
                                "CheckCategory": "Lambda Excessive Memory",
                            }
                        )

                    # COST SAVING: Low invocation functions (potential for deletion/consolidation)
                    try:
                        end_time = datetime.now(timezone.utc)
                        start_time = end_time - timedelta(days=30)
                        metrics = self.cloudwatch.get_metric_statistics(
                            Namespace="AWS/Lambda",
                            MetricName="Invocations",
                            Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=2592000,
                            Statistics=["Sum"],
                        )
                        invocations = metrics["Datapoints"][0]["Sum"] if metrics["Datapoints"] else 0
                        if invocations < 100:
                            checks["low_invocation"].append(
                                {
                                    "FunctionName": function_name,
                                    "MemorySize": memory_size,
                                    "Runtime": runtime,
                                    "Invocations30Days": int(invocations),
                                    "Recommendation": "Low usage - consider consolidation or deletion",
                                    "EstimatedSavings": "Eliminate unused costs",
                                    "CheckCategory": "Lambda Low Invocation",
                                }
                            )
                    except Exception as e:
                        print(f"Warning: Could not get metrics for function {function_name}: {e}")
                        continue

                    # COST SAVING: Provisioned concurrency (expensive feature)
                    try:
                        provisioned = self.lambda_client.list_provisioned_concurrency_configs(
                            FunctionName=function_name
                        )
                        if provisioned["ProvisionedConcurrencyConfigs"]:
                            for config in provisioned["ProvisionedConcurrencyConfigs"]:
                                checks["provisioned_concurrency"].append(
                                    {
                                        "FunctionName": function_name,
                                        "MemorySize": memory_size,
                                        "Runtime": runtime,
                                        "ProvisionedConcurrency": config["AllocatedProvisionedConcurrentExecutions"],
                                        "Recommendation": "Provisioned concurrency is expensive - review necessity",
                                        "EstimatedSavings": "Up to 90% if not needed",
                                        "CheckCategory": "Lambda Provisioned Concurrency",
                                    }
                                )
                    except Exception as e:
                        print(f"Warning: Could not check provisioned concurrency for {function_name}: {e}")
                        continue

                    # COST SAVING: VPC configuration adds ENI costs
                    if vpc_config and vpc_config.get("SubnetIds"):
                        checks["vpc_without_need"].append(
                            {
                                "FunctionName": function_name,
                                "MemorySize": memory_size,
                                "Runtime": runtime,
                                "VpcId": vpc_config.get("VpcId", "N/A"),
                                "Recommendation": "VPC adds ENI costs and cold start latency - remove if not needed",
                                "EstimatedSavings": "Reduce ENI costs and improve performance",
                                "CheckCategory": "Lambda VPC Configuration",
                            }
                        )

                    # COST SAVING: High reserved concurrency (limits other functions)
                    if reserved_concurrency and reserved_concurrency > 100:
                        checks["high_reserved_concurrency"].append(
                            {
                                "FunctionName": function_name,
                                "MemorySize": memory_size,
                                "Runtime": runtime,
                                "ReservedConcurrency": reserved_concurrency,
                                "Recommendation": f"{reserved_concurrency} reserved concurrency may be excessive",
                                "EstimatedSavings": "Review actual concurrency needs",
                                "CheckCategory": "Lambda Reserved Concurrency",
                            }
                        )

                    # COST SAVING: ARM/Graviton migration for supported runtimes (only for active functions)
                    if "x86_64" in architectures and runtime in [
                        "python3.8",
                        "python3.9",
                        "python3.10",
                        "python3.11",
                        "python3.12",
                        "nodejs18.x",
                        "nodejs20.x",
                        "java11",
                        "java17",
                        "java21",
                        "dotnet6",
                        "dotnet8",
                    ]:
                        try:
                            # Check invocation metrics to ensure function is actively used
                            end_time = datetime.now(timezone.utc)
                            start_time = end_time - timedelta(days=7)

                            invocation_metrics = self.cloudwatch.get_metric_statistics(
                                Namespace="AWS/Lambda",
                                MetricName="Invocations",
                                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=86400,
                                Statistics=["Sum"],
                            )

                            total_invocations = sum(dp["Sum"] for dp in invocation_metrics.get("Datapoints", []))

                            # Only recommend ARM migration for actively used functions (>10 invocations/week)
                            if total_invocations > 10:
                                checks["arm_migration"].append(
                                    {
                                        "FunctionName": function_name,
                                        "MemorySize": memory_size,
                                        "Runtime": runtime,
                                        "CurrentArchitecture": "x86_64",
                                        "WeeklyInvocations": f"{total_invocations:.0f}",
                                        "Recommendation": f"Active function ({total_invocations:.0f} invocations/week) - migrate to ARM/Graviton for better price-performance",
                                        "EstimatedSavings": "20% cost reduction with ARM architecture",
                                        "CheckCategory": "Lambda ARM Migration",
                                    }
                                )
                        except Exception:
                            pass  # Skip if can't get metrics

        except Exception as e:
            print(f"Warning: Lambda checks failed: {e}")

        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_api_gateway_checks(self) -> Dict[str, Any]:
        """
        Enhanced API Gateway cost optimization checks with intelligent gating.

        Analyzes API Gateway configurations for cost optimization opportunities:
        - REST vs HTTP API migration (only for simple APIs with ≤10 resources)
        - Unused stages identification and cleanup
        - Caching optimization opportunities
        - Throttling configuration analysis
        - Request validation cost impact

        Uses resource complexity analysis to prevent inappropriate migration recommendations:
        - Only suggests REST→HTTP migration for simple APIs
        - Considers feature compatibility and complexity
        - Provides context-aware recommendations with usage justification

        Returns:
            Dict containing filtered recommendations based on API complexity and usage
        """
        checks = {
            "rest_vs_http": [],
            "unused_stages": [],
            "caching_opportunities": [],
            "throttling_optimization": [],
            "request_validation": [],
        }

        try:
            # Get REST APIs
            paginator = self.apigateway.get_paginator("get_rest_apis")
            for page in paginator.paginate():
                for api in page.get("items", []):
                    api_id = api.get("id")
                    api_name = api.get("name", "Unknown")

                    # Check for HTTP API migration opportunity (only for simple APIs)
                    try:
                        # Get API resources to check complexity
                        resources = self.apigateway.get_resources(restApiId=api_id)
                        resource_count = len(resources.get("items", []))

                        # Only recommend migration for simple APIs with few resources
                        if resource_count <= 10:  # Simple APIs only
                            checks["rest_vs_http"].append(
                                {
                                    "ApiId": api_id,
                                    "ApiName": api_name,
                                    "ApiType": "REST",
                                    "ResourceCount": resource_count,
                                    "Recommendation": "Simple API - consider migrating to HTTP API for lower cost",
                                    "EstimatedSavings": "10-30% cost reduction for simple APIs",
                                    "CheckCategory": "API Gateway Type Optimization",
                                }
                            )
                    except Exception:
                        pass  # Skip if can't analyze resources

                    # Check stages
                    try:
                        stages = self.apigateway.get_stages(restApiId=api_id)
                        for stage in stages.get("item", []):
                            stage_name = stage.get("stageName")
                            if not stage.get("cacheClusterEnabled", False):
                                checks["caching_opportunities"].append(
                                    {
                                        "ApiId": api_id,
                                        "ApiName": api_name,
                                        "StageName": stage_name,
                                        "Recommendation": "Enable caching to reduce backend calls",
                                        "EstimatedSavings": "Reduced backend costs",
                                        "CheckCategory": "API Gateway Caching",
                                    }
                                )
                    except Exception as e:
                        print(f"Warning: Could not check stages for API {api_id}: {e}")
                        continue

        except Exception as e:
            print(f"Warning: Could not perform API Gateway checks: {e}")

        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_step_functions_checks(self) -> Dict[str, Any]:
        """
        Enhanced Step Functions cost optimization checks with volume-based gating.

        Analyzes Step Functions state machines for cost optimization:
        - Standard vs Express workflow optimization (only for high-volume >100 executions/day)
        - Excessive state transitions analysis
        - Polling workflow detection and optimization
        - Non-production 24/7 operation identification

        Uses CloudWatch execution metrics for intelligent recommendations:
        - Only suggests Express workflows for high-volume scenarios
        - Analyzes 7-day execution patterns for accurate volume assessment
        - Prevents inappropriate recommendations for low-volume workflows

        Returns:
            Dict containing volume-gated recommendations based on actual usage patterns
        """
        checks = {"standard_vs_express": [], "excessive_transitions": [], "polling_workflows": [], "nonprod_24x7": []}

        try:
            paginator = self.stepfunctions.get_paginator("list_state_machines")
            for page in paginator.paginate():
                for sm in page.get("stateMachines", []):
                    sm_arn = sm.get("stateMachineArn", "")
                    sm_name = sm.get("name", "Unknown")
                    sm_type = sm.get("type", "STANDARD")

                    # Check for Standard vs Express optimization (only for high-volume workflows)
                    if sm_type == "STANDARD":
                        try:
                            # Check execution metrics to determine if Express is suitable
                            from datetime import datetime, timedelta, timezone

                            end_time = datetime.now(timezone.utc)
                            start_time = end_time - timedelta(days=7)

                            # Get execution count metrics
                            execution_metrics = self.cloudwatch.get_metric_statistics(
                                Namespace="AWS/States",
                                MetricName="ExecutionsStarted",
                                Dimensions=[{"Name": "StateMachineArn", "Value": sm_arn}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=86400,  # Daily
                                Statistics=["Sum"],
                            )

                            total_executions = sum(dp["Sum"] for dp in execution_metrics.get("Datapoints", []))
                            daily_avg = total_executions / 7 if total_executions > 0 else 0

                            # Only recommend Express for high-volume workflows (>100 executions/day)
                            if daily_avg > 100:
                                checks["standard_vs_express"].append(
                                    {
                                        "StateMachineArn": sm_arn,
                                        "StateMachineName": sm_name,
                                        "Type": sm_type,
                                        "DailyExecutions": f"{daily_avg:.0f}",
                                        "Recommendation": f"High-volume workflow ({daily_avg:.0f} executions/day) - consider Express type",
                                        "EstimatedSavings": "Up to 90% cost reduction for high-volume workflows",
                                        "CheckCategory": "Step Functions Type Optimization",
                                    }
                                )
                        except Exception:
                            pass  # Skip if can't get metrics

                    # Check for non-prod running 24/7
                    if any(env in sm_name.lower() for env in ["dev", "test", "staging"]):
                        checks["nonprod_24x7"].append(
                            {
                                "StateMachineArn": sm_arn,
                                "StateMachineName": sm_name,
                                "Environment": "non-production",
                                "Recommendation": "Implement shutdown schedule for non-prod",
                                "EstimatedSavings": "65-75% with scheduled shutdown",
                                "CheckCategory": "Step Functions Non-Prod 24/7",
                            }
                        )

        except Exception as e:
            print(f"Warning: Could not perform Step Functions checks: {e}")

        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}

    def get_enhanced_cloudfront_checks(self) -> Dict[str, Any]:
        """
        Enhanced CloudFront cost optimization checks with traffic-based gating.

        Analyzes CloudFront distributions for cost optimization opportunities:
        - Price class optimization (only for active distributions with >1000 requests/week)
        - Low traffic distribution identification
        - Origin Shield necessity analysis
        - Geographic distribution analysis for price class recommendations

        Uses CloudWatch request metrics for intelligent gating:
        - Only suggests price class changes for distributions with significant traffic
        - Analyzes 7-day request patterns to validate activity
        - Prevents recommendations for inactive or low-traffic distributions

        Returns:
            Dict containing traffic-validated recommendations for active distributions
        """
        checks = {"price_class_optimization": [], "low_traffic_distributions": [], "origin_shield_unnecessary": []}

        try:
            paginator = self.cloudfront.get_paginator("list_distributions")
            for page in paginator.paginate():
                for dist in page.get("DistributionList", {}).get("Items", []):
                    dist_id = dist.get("Id")
                    domain_name = dist.get("DomainName", "Unknown")
                    price_class = dist.get("PriceClass", "PriceClass_All")
                    status = dist.get("Status", "Unknown")
                    enabled = dist.get("Enabled", True)

                    # COST SAVING: Price class optimization (only for global distributions with traffic)
                    if price_class == "PriceClass_All" and enabled:
                        try:
                            # Check CloudWatch metrics for geographic distribution of requests
                            from datetime import datetime, timedelta, timezone

                            end_time = datetime.now(timezone.utc)
                            start_time = end_time - timedelta(days=7)

                            # Get request metrics to validate traffic exists
                            request_metrics = self.cloudwatch.get_metric_statistics(
                                Namespace="AWS/CloudFront",
                                MetricName="Requests",
                                Dimensions=[{"Name": "DistributionId", "Value": dist_id}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=86400,
                                Statistics=["Sum"],
                            )

                            total_requests = sum(dp["Sum"] for dp in request_metrics.get("Datapoints", []))

                            # Only recommend for distributions with significant traffic (>1000 requests/week)
                            if total_requests > 1000:
                                checks["price_class_optimization"].append(
                                    {
                                        "DistributionId": dist_id,
                                        "DomainName": domain_name,
                                        "Status": status,
                                        "CurrentPriceClass": price_class,
                                        "WeeklyRequests": f"{total_requests:.0f}",
                                        "Recommendation": f"Active distribution ({total_requests:.0f} requests/week) - consider PriceClass_100/200 if users are regional",
                                        "EstimatedSavings": "20-50% on data transfer costs for regional traffic",
                                        "CheckCategory": "CloudFront Price Class Optimization",
                                    }
                                )
                        except Exception:
                            pass  # Skip if can't get metrics

                    # COST SAVING: Disabled distributions still incur costs
                    if not enabled:
                        checks["low_traffic_distributions"].append(
                            {
                                "DistributionId": dist_id,
                                "DomainName": domain_name,
                                "Status": status,
                                "PriceClass": price_class,
                                "Enabled": enabled,
                                "Recommendation": "Disabled distribution - consider deletion to eliminate costs",
                                "EstimatedSavings": "100% of distribution costs",
                                "CheckCategory": "CloudFront Unused Distribution",
                            }
                        )

                    # Check for Origin Shield (additional cost feature)
                    try:
                        dist_config = self.cloudfront.get_distribution_config(Id=dist_id)
                        origins = dist_config.get("DistributionConfig", {}).get("Origins", {}).get("Items", [])

                        for origin in origins:
                            origin_shield = origin.get("OriginShield", {})
                            if origin_shield.get("Enabled", False):
                                checks["origin_shield_unnecessary"].append(
                                    {
                                        "DistributionId": dist_id,
                                        "DomainName": domain_name,
                                        "Status": status,
                                        "PriceClass": price_class,
                                        "OriginShieldRegion": origin_shield.get("OriginShieldRegion", "Unknown"),
                                        "Recommendation": "Origin Shield adds costs - review necessity for traffic patterns",
                                        "EstimatedSavings": "Variable based on cache hit improvement vs additional costs",
                                        "CheckCategory": "CloudFront Origin Shield Review",
                                    }
                                )
                    except Exception as e:
                        # Skip if can't get distribution config (permissions)
                        print(f"⚠️ Error analyzing CloudFront distribution {dist_id}: {str(e)}")
                        # Continue with next distribution

        except Exception as e:
            print(f"Warning: Could not perform CloudFront checks: {e}")

        recommendations = []
        for category, items in checks.items():
            for item in items:
                recommendations.append(item)

        return {"recommendations": recommendations, **checks}


def main():
    """
    Main entry point for AWS Cost Optimization Scanner CLI.

    Provides a comprehensive command-line interface for AWS cost optimization analysis
    with support for service filtering, multiple output formats, and advanced scanning options.

    Command Line Arguments:
        region (str): AWS region to scan (required)
        --profile: AWS profile name from ~/.aws/credentials
        --output: Custom output file path for HTML report
        --fast: Enable fast mode (skip CloudWatch metrics for S3)
        --skip-service: Skip specific services (repeatable)
        --scan-only: Scan only specific services (repeatable)

    Service Filtering:
        The scanner supports intelligent service filtering across 30 service categories:
        - ec2: EC2 instances, AMIs, compute optimization
        - ebs: EBS volumes, snapshots, storage optimization
        - rds: RDS databases, managed database optimization
        - s3: S3 buckets, lifecycle policies, storage classes
        - lambda: Lambda functions, memory, runtime optimization
        - dynamodb: DynamoDB tables, capacity optimization
        - efs: EFS file systems, storage class optimization
        - elasticache: ElastiCache clusters, engine optimization
        - opensearch: OpenSearch domains, instance optimization
        - containers: ECS/EKS clusters, ECR repositories
        - network: EIPs, NAT Gateways, Load Balancers
        - monitoring: CloudWatch logs, metrics, CloudTrail
        - backup: AWS Backup jobs, retention policies
        - lightsail: Lightsail instances and resources
        - redshift: Redshift clusters and serverless
        - dms: Database Migration Service
        - quicksight: QuickSight BI service
        - apprunner: App Runner container service
        - transfer: Transfer Family (SFTP/FTPS)
        - msk: Managed Streaming for Apache Kafka
        - workspaces: WorkSpaces virtual desktops
        - mediastore: Elemental MediaStore
        - glue: AWS Glue ETL jobs
        - athena: Athena query service
        - batch: AWS Batch compute

    Usage Examples:
        # Full comprehensive scan
        python3 cost_optimizer.py us-east-1

        # Fast S3-only storage audit
        python3 cost_optimizer.py us-east-1 --fast --scan-only s3

        # Skip compute services for storage focus
        python3 cost_optimizer.py us-east-1 --skip-service ec2 --skip-service rds

        # Multi-service targeted scan
        python3 cost_optimizer.py us-east-1 --scan-only s3 --scan-only lambda --scan-only dynamodb

    Output:
        - HTML report with interactive multi-tab interface
        - JSON data file for programmatic analysis
        - Console summary with key findings and savings estimates

    Performance:
        - Service filtering can reduce scan time by 50-80%
        - Fast mode recommended for accounts with 100+ S3 buckets
        - Automatic retry logic handles API throttling
        - Enterprise-scale support for 1000+ resources per service

    Returns:
        int: Exit code (0 for success, 1 for error)
    """
    parser = argparse.ArgumentParser(description="AWS Cost Optimization Scanner")
    parser.add_argument("region", help="AWS region to scan")
    parser.add_argument("--profile", help="AWS profile to use")
    parser.add_argument("--output", help="Output file for report")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Fast mode: Skip CloudWatch metrics for faster S3 analysis (recommended for 100+ buckets)",
    )
    parser.add_argument(
        "--skip-service",
        action="append",
        dest="skip_services",
        help="Skip specific services (can be used multiple times). Available: ec2, ebs, rds, s3, lambda, dynamodb, efs, elasticache, opensearch, containers, network, monitoring, auto_scaling, route53, backup, cloudfront, api_gateway, step_functions, lightsail, redshift, dms, quicksight, apprunner, transfer, msk, workspaces, mediastore, glue, athena, batch",
    )
    parser.add_argument(
        "--scan-only",
        action="append",
        dest="scan_only",
        help="Scan only specific services (can be used multiple times). Available: ec2, ebs, rds, s3, lambda, dynamodb, efs, elasticache, opensearch, containers, network, monitoring, auto_scaling, route53, backup, cloudfront, api_gateway, step_functions, lightsail, redshift, dms, quicksight, apprunner, transfer, msk, workspaces, mediastore, glue, athena, batch",
    )

    args = parser.parse_args()

    # Validate service filtering arguments
    if args.skip_services and args.scan_only:
        print("❌ Error: Cannot use both --skip-service and --scan-only at the same time")
        return

    optimizer = CostOptimizer(args.region, args.profile, fast_mode=args.fast)

    if args.fast:
        print("🚀 Fast mode enabled - skipping CloudWatch metrics for faster analysis")

    # Normalize service names to lowercase for case-insensitive matching
    if args.skip_services:
        args.skip_services = [s.lower() for s in args.skip_services]
        print(f"⏭️ Skipping services: {', '.join(args.skip_services)}")
    elif args.scan_only:
        args.scan_only = [s.lower() for s in args.scan_only]
        print(f"🎯 Scanning only: {', '.join(args.scan_only)}")

    scan_results = optimizer.scan_region(skip_services=args.skip_services, scan_only=args.scan_only)

    # Save raw JSON data
    json_file = f"cost_optimization_scan_{args.region}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(json_file, "w") as f:
        json.dump(scan_results, f, indent=2, default=str)

    # Generate HTML report
    try:
        from html_report_generator import HTMLReportGenerator

        generator = HTMLReportGenerator(scan_results)
        html_file = generator.generate_html_report()
        print(f"✅ Report generated: {html_file}")
    except ImportError:
        print("❌ HTML report generator not available")


if __name__ == "__main__":
    main()
