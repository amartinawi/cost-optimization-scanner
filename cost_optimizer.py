from __future__ import annotations

import boto3
from botocore.config import Config
from dataclasses import asdict
from typing import Any, Dict

from core.client_registry import ClientRegistry
from core.scan_context import ScanContext
from core.session import AwsSessionFactory


class CostOptimizer:
    REGIONAL_PRICING = {
        "us-east-1": 1.00,
        "us-east-2": 1.00,
        "us-west-1": 1.02,
        "us-west-2": 1.00,
        "us-gov-east-1": 1.05,
        "us-gov-west-1": 1.05,
        "ca-central-1": 1.02,
        "ca-west-1": 1.02,
        "eu-west-1": 1.10,
        "eu-west-2": 1.10,
        "eu-west-3": 1.10,
        "eu-central-1": 1.12,
        "eu-central-2": 1.12,
        "eu-north-1": 1.05,
        "eu-south-1": 1.10,
        "eu-south-2": 1.10,
        "eusc-de-east-1": 1.15,
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
        "me-south-1": 1.15,
        "me-central-1": 1.15,
        "il-central-1": 1.15,
        "af-south-1": 1.20,
        "sa-east-1": 1.25,
        "mx-central-1": 1.15,
    }

    @classmethod
    def get_regional_pricing_multiplier(cls, region: str) -> float:
        if region not in cls.REGIONAL_PRICING:
            print(f"⚠️ WARNING: Regional pricing not defined for {region}")
            print(f"   Using conservative 15% premium over us-east-1 pricing")
            print(f"   Actual costs may vary - verify with AWS Pricing Calculator")
            return 1.15
        return cls.REGIONAL_PRICING[region]

    def add_warning(self, message: str, service: str | None = None) -> None:
        self._ctx.warn(message, service or "")

    def add_permission_issue(self, message: str, service: str, action: str | None = None) -> None:
        self._ctx.permission_issue(message, service, action)

    def __init__(self, region: str, profile: str | None = None, fast_mode: bool = False) -> None:
        print(f"🚀 Initializing AWS Cost Optimization Scanner...")
        print(f"📍 Target region: {region}")
        print(f"👤 AWS profile: {profile or 'default'}")

        self.region = region
        self.profile = profile
        self.fast_mode = fast_mode
        self.pricing_multiplier = self.get_regional_pricing_multiplier(region)

        self.scan_warnings: list[Any] = []
        self.permission_issues: list[Any] = []

        if self.fast_mode:
            print("🚀 Fast mode enabled - skipping CloudWatch metrics for faster analysis")

        factory = AwsSessionFactory(self.region, self.profile)
        self.account_id = factory.account_id()
        print(f"✅ Connected to AWS account: {self.account_id}")

        registry = ClientRegistry(factory)
        self._ctx = ScanContext(
            region=self.region,
            account_id=self.account_id,
            profile=self.profile,
            fast_mode=self.fast_mode,
            clients=registry,
            pricing_multiplier=self.pricing_multiplier,
        )
        print("✅ All AWS service clients initialized successfully!")
        print(f"🎯 Ready to scan {region} with comprehensive cost optimization analysis")

    def scan_region(
        self,
        skip_services: list[str] | None = None,
        scan_only: list[str] | None = None,
    ) -> Dict[str, Any]:
        from core.scan_orchestrator import ScanOrchestrator
        from core.result_builder import ScanResultBuilder
        from services import ALL_MODULES

        print(f"Starting comprehensive cost optimization scan for region: {self.region}")
        print(f"Using AWS profile: {self.profile}")

        skip_set: set[str] | None = set(skip_services) if skip_services else None
        scan_only_set: set[str] | None = set(scan_only) if scan_only else None

        if scan_only_set:
            print(f"Analyzing {len(scan_only_set)} AWS services with 220+ cost optimization checks...")
            print(f"🎯 Scanning only: {', '.join(sorted(scan_only_set))}")
        elif skip_set:
            all_keys = {m.key for m in ALL_MODULES}
            remaining = len(all_keys - skip_set)
            print(f"Analyzing {remaining} AWS services with 220+ cost optimization checks...")
            print(f"⏭️ Skipping: {', '.join(sorted(skip_set))}")
        else:
            print(f"Analyzing {len(ALL_MODULES)} AWS services with 220+ cost optimization checks...")

        orchestrator = ScanOrchestrator(self._ctx, ALL_MODULES)
        findings = orchestrator.run(scan_only=scan_only_set, skip=skip_set)
        builder = ScanResultBuilder(self._ctx)
        result = builder.build(findings)

        print("✅ Cost optimization scan completed successfully!")
        total_recs = result["summary"]["total_recommendations"]
        svc_count = len(result["services"])
        print(f"📊 Found {total_recs} optimization opportunities across {svc_count} services")

        return result
