# ruff: noqa: E501
"""Load Balancer cost optimization checks.

Extracted from CostOptimizer.get_load_balancer_checks() as a free function.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/load_balancer.py] Load Balancer module active")


def _is_kubernetes_managed_alb(elbv2: Any, lb_name: str, lb_arn: str) -> bool:
    k8s_patterns = [
        "k8s-",
        "eks-",
        "ingress-",
        "kube-",
    ]

    if any(lb_name.lower().startswith(pattern) for pattern in k8s_patterns):
        return True

    try:
        tags_response = elbv2.describe_tags(ResourceArns=[lb_arn])
        for tag_desc in tags_response.get("TagDescriptions", []):
            for tag in tag_desc.get("Tags", []):
                key = tag.get("Key", "").lower()

                k8s_tag_patterns = [
                    "kubernetes.io/",
                    "ingress.k8s.aws/",
                    "elbv2.k8s.aws/",
                    "alb.ingress.kubernetes.io/",
                ]

                if any(pattern in key for pattern in k8s_tag_patterns):
                    return True

                if key in ["kubernetes.io/cluster", "alpha.eksctl.io/cluster-name"]:
                    return True

    except Exception as e:
        print(f"Warning: Could not get tags for ALB {lb_arn}: {e}")

    return False


def get_load_balancer_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 4: Load Balancers optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
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
        elbv2 = ctx.client("elbv2")

        alb_paginator = elbv2.get_paginator("describe_load_balancers")
        load_balancers: list[dict[str, Any]] = []
        for page in alb_paginator.paginate():
            load_balancers.extend(page.get("LoadBalancers", []))

        try:
            elb = ctx.client("elb")
            clb_paginator = elb.get_paginator("describe_load_balancers")
            classic_lbs: list[dict[str, Any]] = []
            for page in clb_paginator.paginate():
                classic_lbs.extend(page.get("LoadBalancerDescriptions", []))
        except Exception as e:
            print(f"⚠️ Error getting Classic Load Balancers: {str(e)}")
            classic_lbs = []

        alb_count = 0
        k8s_managed_albs = 0
        standalone_albs: list[dict[str, Any]] = []

        for lb in load_balancers:
            lb_arn = lb.get("LoadBalancerArn")
            lb_name = lb.get("LoadBalancerName")
            lb_type = lb.get("Type", "application")
            scheme = lb.get("Scheme", "internet-facing")

            is_k8s_managed = _is_kubernetes_managed_alb(elbv2, lb_name, lb_arn) if lb_name and lb_arn else False

            if lb_type == "application":
                alb_count += 1
                if is_k8s_managed:
                    k8s_managed_albs += 1
                else:
                    standalone_albs.append(lb)

            if scheme == "internet-facing" and any(
                keyword in (lb_name or "").lower() for keyword in ["internal", "private", "backend"]
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
                listeners_response = elbv2.describe_listeners(LoadBalancerArn=lb_arn)
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
                        rules_response = elbv2.describe_rules(ListenerArn=listener["ListenerArn"])
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

        if alb_count > 5:
            standalone_count = len(standalone_albs)
            if standalone_count > 2:
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

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}
