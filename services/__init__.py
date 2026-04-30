from __future__ import annotations

from typing import Any

from services.adapters.ami import AmiModule
from services.adapters.api_gateway import ApiGatewayModule
from services.adapters.apprunner import AppRunnerModule
from services.adapters.athena import AthenaModule
from services.adapters.batch import BatchModule
from services.adapters.cloudfront import CloudfrontModule
from services.adapters.containers import ContainersModule
from services.adapters.dms import DmsModule
from services.adapters.dynamodb import DynamoDbModule
from services.adapters.ebs import EbsModule
from services.adapters.ec2 import EC2Module
from services.adapters.elasticache import ElasticacheModule
from services.adapters.file_systems import FileSystemsModule
from services.adapters.glue import GlueModule
from services.adapters.lambda_svc import LambdaModule
from services.adapters.lightsail import LightsailModule
from services.adapters.mediastore import MediastoreModule
from services.adapters.monitoring import MonitoringModule
from services.adapters.msk import MskModule
from services.adapters.network import NetworkModule
from services.adapters.opensearch import OpensearchModule
from services.adapters.rds import RdsModule
from services.adapters.redshift import RedshiftModule
from services.adapters.s3 import S3Module
from services.adapters.step_functions import StepFunctionsModule
from services.adapters.transfer import TransferModule
from services.adapters.workspaces import WorkspacesModule
from services.adapters.quicksight import QuicksightModule

ALL_MODULES: list[Any] = [
    EC2Module(),
    AmiModule(),
    EbsModule(),
    RdsModule(),
    FileSystemsModule(),
    S3Module(),
    DynamoDbModule(),
    LambdaModule(),
    ContainersModule(),
    NetworkModule(),
    MonitoringModule(),
    ElasticacheModule(),
    OpensearchModule(),
    CloudfrontModule(),
    ApiGatewayModule(),
    StepFunctionsModule(),
    LightsailModule(),
    RedshiftModule(),
    DmsModule(),
    QuicksightModule(),
    AppRunnerModule(),
    TransferModule(),
    MskModule(),
    WorkspacesModule(),
    MediastoreModule(),
    GlueModule(),
    AthenaModule(),
    BatchModule(),
]
