"""AWS Cost Optimization Scanner — unified CLI entry point.

This module provides the command-line interface for running cost optimization
scans across AWS services. It delegates to the modular service architecture
in ``services/`` via the ``CostOptimizer`` orchestration layer.

Usage:
    python3 cli.py us-east-1
    python3 cli.py us-east-1 --profile production --fast
    python3 cli.py us-east-1 --scan-only s3 --scan-only lambda
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from cost_optimizer import CostOptimizer


def main() -> int:
    """Main entry point for AWS Cost Optimization Scanner CLI."""
    parser = argparse.ArgumentParser(
        description="AWS Cost Optimization Scanner — 30 services, 220+ checks",
    )
    parser.add_argument("region", help="AWS region to scan")
    parser.add_argument("--profile", help="AWS profile to use")
    parser.add_argument("--output", help="Output file for HTML report")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Fast mode: skip CloudWatch metrics for faster S3 analysis",
    )
    parser.add_argument(
        "--skip-service",
        action="append",
        dest="skip_services",
        help="Skip specific services (repeatable)",
    )
    parser.add_argument(
        "--scan-only",
        action="append",
        dest="scan_only",
        help="Scan only specific services (repeatable)",
    )

    args = parser.parse_args()

    if args.skip_services and args.scan_only:
        print("❌ Error: Cannot use both --skip-service and --scan-only at the same time")
        return 1

    optimizer = CostOptimizer(args.region, args.profile, fast_mode=args.fast)

    if args.fast:
        print("🚀 Fast mode enabled — skipping CloudWatch metrics for faster analysis")

    if args.skip_services:
        args.skip_services = [s.lower() for s in args.skip_services]
        print(f"⏭️ Skipping services: {', '.join(args.skip_services)}")
    elif args.scan_only:
        args.scan_only = [s.lower() for s in args.scan_only]
        print(f"🎯 Scanning only: {', '.join(args.scan_only)}")

    # Run scan — delegates to extracted service modules in services/
    scan_results = optimizer.scan_region(
        skip_services=args.skip_services,
        scan_only=args.scan_only,
    )

    # Save JSON
    json_file = f"cost_optimization_scan_{args.region}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(json_file, "w") as f:
        json.dump(scan_results, f, indent=2, default=str)

    # Generate HTML report
    try:
        from html_report_generator import HTMLReportGenerator

        generator = HTMLReportGenerator(scan_results)
        html_file = generator.generate_html_report(args.output)
        print(f"✅ Report generated: {html_file}")
    except ImportError:
        print("❌ HTML report generator not available")

    return 0


if __name__ == "__main__":
    sys.exit(main())
