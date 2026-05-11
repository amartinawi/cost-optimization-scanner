"""
Stub responses for services not covered by moto.

These are minimal empty responses that prevent NotImplementedError when
CostOptimizer calls unsupported service APIs. The monolith already wraps
all these calls in broad ``except Exception`` handlers, so the stubs just
need to return valid empty responses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

RECORDED_DIR = Path(__file__).parent.parent / "fixtures" / "recorded_aws_responses"


def load_stub_response(service: str, api_call: str) -> Dict[str, Any]:
    """Load a pre-recorded stub response from JSON file.

    Args:
        service: AWS service name (e.g. 'cost-optimization-hub')
        api_call: API call name (e.g. 'list_recommendations')

    Returns:
        Parsed JSON response dict.
    """
    path = RECORDED_DIR / service / f"{api_call}.json"
    with open(path) as f:
        return json.load(f)


STUB_RESPONSES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "cost-optimization-hub": {
        "list_recommendations": {
            "items": [],
            "nextToken": None,
        },
        "get_recommendation": {
            "recommendationId": "stub-id",
            "currentResourceType": "Ec2Instance",
            "recommendedResourceType": "Ec2Instance",
            "estimatedMonthlySavings": 0.0,
            "currencyCode": "USD",
        },
    },
    "compute-optimizer": {
        "get_ec2_instance_recommendations": {
            "instanceRecommendations": [],
            "nextToken": None,
        },
        "get_ebs_volume_recommendations": {
            "volumeRecommendations": [],
            "nextToken": None,
        },
        "get_rds_database_recommendations": {
            "rdsDBRecommendations": [],
            "nextToken": None,
        },
    },
}


def get_stub_response(service: str, api_call: str) -> Dict[str, Any]:
    """Get a stub response for an unsupported service API call.

    Falls back to the in-memory STUB_RESPONSES dict if no JSON file exists.

    Args:
        service: AWS service name
        api_call: API call name

    Returns:
        Response dict suitable for Stubber activation.
    """
    json_path = RECORDED_DIR / service / f"{api_call}.json"
    if json_path.exists():
        return load_stub_response(service, api_call)

    return STUB_RESPONSES.get(service, {}).get(api_call, {})
