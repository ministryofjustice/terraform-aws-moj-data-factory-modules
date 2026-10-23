import boto3
import pytest
from moto import mock_aws

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import IngestionOwnership
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.requests import NewIngestionRequest
from ingestion_registry.resource_bindings import IngestionResourceBindings
from ingestion_registry.task_groups import TaskGroupOwnership


@pytest.fixture
def owned_ingestion():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="eu-west-2")

        client.create_table(
            TableName="test-ingestion-registry",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        registry = IngestionRegistry(
            client,
            table_name="test-ingestion-registry",
        )

        admitted = registry.admit(
            NewIngestionRequest(
                request_id="request-123",
                operation="INITIAL_INGESTION",
                table=TableIdentity(
                    environment="development",
                    source="sandbox-postgres",
                    database_name="dms_test",
                    schema_name="public",
                    table_name="dms_integration_test",
                ),
                configuration_revision="a" * 64,
                artifact_revision="b" * 64,
                extraction_generation="generation-one",
            )
        )

        clock = {"now": 1000}

        ownership = IngestionOwnership(
            registry,
            clock=lambda: clock["now"],
        )

        yield registry, ownership, admitted, clock


@pytest.fixture
def progress_setup(owned_ingestion):
    registry, ownership, admitted, clock = owned_ingestion

    claim = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )

    return registry, ownership, admitted, clock, claim, progress


@pytest.fixture
def binding_setup(progress_setup):
    registry, ownership, admitted, clock, claim, _ = progress_setup

    document = {
        "bucket_name": "example-platform-configuration",
        "object_key": "deployments/development/manifest.json",
        "version_id": "version-one",
        "sha256": "c" * 64,
    }

    binding = {
        "environment": "development",
        "account_id": "111122223333",
        "region": "eu-west-2",
        "source": "sandbox-postgres",
        "database_name": "dms_test",
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "extraction_generation": "generation-one",
        "table": {
            "schema_name": "public",
            "table_name": "dms_integration_test",
        },
        "task_group": {
            "name": "integration",
            "migration_type": "full-load-and-cdc",
            "tables": [
                {
                    "schema_name": "public",
                    "table_name": "dms_integration_test",
                }
            ],
        },
        "resources": {
            "deployment_name": "example-deployment",
            "replication_instance_arn": (
                "arn:aws:dms:eu-west-2:111122223333:rep:INSTANCE"
            ),
            "source_endpoint_arn": (
                "arn:aws:dms:eu-west-2:111122223333:endpoint:SOURCE"
            ),
            "target_endpoint_arn": (
                "arn:aws:dms:eu-west-2:111122223333:endpoint:TARGET"
            ),
            "replication_task_arn": (
                "arn:aws:dms:eu-west-2:111122223333:task:TASK"
            ),
            "replication_task_id": "example-task",
        },
        "landing": {
            "bucket_name": "example-raw",
            "prefix": (
                "landing/development/sandbox-postgres/"
                "dms_test/integration/generation-one/"
            ),
        },
        "document_references": {
            "manifest": document,
            "artifact": {
                **document,
                "object_key": "deployments/development/artifact.json",
                "sha256": "d" * 64,
            },
        },
    }

    bindings = IngestionResourceBindings(
        registry,
        clock=lambda: clock["now"],
    )

    return (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        bindings,
        binding,
    )


@pytest.fixture
def task_group_setup(binding_setup):
    registry, ownership, admitted, clock, claim, bindings, binding = (
        binding_setup
    )
    bindings.pin(claim, binding)

    groups = TaskGroupOwnership(
        registry,
        clock=lambda: clock["now"],
    )

    return registry, admitted, clock, groups