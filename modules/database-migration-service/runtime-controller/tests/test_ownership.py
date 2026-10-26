from dataclasses import replace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import IngestionOwnership
from ingestion_registry.requests import NewIngestionRequest
from ingestion_registry.resource_bindings import IngestionResourceBindings
from ingestion_registry.task_groups import TaskGroupOwnership

from dms_runtime.control import TaskControl
from dms_runtime.ownership import (
    RegistryScopeGuard,
    RuntimeContext,
    RuntimeOwnershipLost,
    RuntimeScopeDenied,
)


@pytest.fixture
def scope_setup():
    with mock_aws():
        client = boto3.client(
            "dynamodb",
            region_name="eu-west-2",
        )

        client.create_table(
            TableName="runtime-registry",
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
            table_name="runtime-registry",
        )

        context = RuntimeContext(
            environment="development",
            account_id="111122223333",
            region="eu-west-2",
            source="sandbox-postgres",
            database_name="dms_test",
        )

        table = TableIdentity(
            environment="development",
            source="sandbox-postgres",
            database_name="dms_test",
            schema_name="public",
            table_name="example",
        )

        admitted = registry.admit(
            NewIngestionRequest(
                request_id="request-one",
                operation="INITIAL_INGESTION",
                table=table,
                configuration_revision="a" * 64,
                artifact_revision="b" * 64,
                extraction_generation="generation-one",
            )
        )

        clock = {"now": 1000}

        claim = IngestionOwnership(
            registry,
            clock=lambda: clock["now"],
        ).claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="table-worker",
        )

        document = {
            "bucket_name": "example-config",
            "object_key": "deployments/manifest.json",
            "version_id": "version-one",
            "sha256": "c" * 64,
        }

        binding = {
            "environment": context.environment,
            "account_id": context.account_id,
            "region": context.region,
            "source": context.source,
            "database_name": context.database_name,
            "configuration_revision": "a" * 64,
            "artifact_revision": "b" * 64,
            "extraction_generation": "generation-one",
            "table": {
                "schema_name": "public",
                "table_name": "example",
            },
            "task_group": {
                "name": "integration",
                "migration_type": "full-load-and-cdc",
                "tables": [
                    {
                        "schema_name": "public",
                        "table_name": "example",
                    }
                ],
            },
            "resources": {
                "deployment_name": "example-deployment",
                "replication_task_id": "example-task",
                "replication_task_arn": (
                    "arn:aws:dms:eu-west-2:"
                    "111122223333:task:TASK"
                ),
                "replication_instance_arn": (
                    "arn:aws:dms:eu-west-2:"
                    "111122223333:rep:INSTANCE"
                ),
                "source_endpoint_arn": (
                    "arn:aws:dms:eu-west-2:"
                    "111122223333:endpoint:SOURCE"
                ),
                "target_endpoint_arn": (
                    "arn:aws:dms:eu-west-2:"
                    "111122223333:endpoint:TARGET"
                ),
            },
            "landing": {
                "bucket_name": "example-raw",
                "prefix": "landing/generation-one/",
            },
            "document_references": {
                "manifest": document,
                "artifact": {
                    **document,
                    "object_key": "deployments/artifact.json",
                },
            },
        }

        IngestionResourceBindings(
            registry,
            clock=lambda: clock["now"],
        ).pin(claim, binding)

        task_claim = TaskGroupOwnership(
            registry,
            clock=lambda: clock["now"],
        ).claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="group-coordinator",
        )

        guard = RegistryScopeGuard(
            registry,
            context=context,
            ingestion_claim=claim,
            task_claim=task_claim,
            clock=lambda: clock["now"],
        )

        yield registry, binding, clock, guard


@pytest.mark.parametrize("action", ["preflight", "start", "resume", "stop"])
def test_valid_claims_pass_without_changing_ownership(
    scope_setup,
    action,
):
    registry, binding, _, guard = scope_setup

    before = registry._read(
        guard.task_claim.group_key,
        "OWNERSHIP",
    )

    assert guard(binding, action) is None

    assert registry._read(
        guard.task_claim.group_key,
        "OWNERSHIP",
    ) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "test"),
        ("account_id", "999900001111"),
        ("region", "eu-west-1"),
        ("source", "another-source"),
        ("database_name", "another_db"),
    ],
)
def test_another_service_is_rejected_before_registry_check(
    scope_setup,
    monkeypatch,
    field,
    value,
):
    registry, binding, _, guard = scope_setup
    guard.context = replace(
        guard.context,
        **{field: value},
    )

    transaction = Mock()
    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        transaction,
    )

    with pytest.raises(RuntimeScopeDenied, match=field):
        guard(binding, "start")

    transaction.assert_not_called()


def test_changed_binding_is_rejected(scope_setup):
    _, binding, _, guard = scope_setup

    binding["document_references"]["manifest"] = {
        **binding["document_references"]["manifest"],
        "version_id": "another-version",
    }

    with pytest.raises(RuntimeOwnershipLost):
        guard(binding, "start")


@pytest.mark.parametrize("owner", ["ingestion", "group", "task"])
def test_expired_lease_is_rejected(scope_setup, owner):
    registry, binding, clock, guard = scope_setup

    pk = {
        "ingestion": (
            f"INGESTION#{guard.ingestion_claim.ingestion_id}"
        ),
        "group": guard.task_claim.group_key,
        "task": guard.task_claim.task_key,
    }[owner]

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {"pk": pk, "sk": "OWNERSHIP"}
        ),
        UpdateExpression="SET lease_expires_at = :now",
        ExpressionAttributeValues=registry._encode(
            {":now": clock["now"]}
        ),
    )

    with pytest.raises(RuntimeOwnershipLost):
        guard(binding, "stop")


def test_stale_ingestion_token_is_rejected(scope_setup):
    _, binding, _, guard = scope_setup

    guard.ingestion_claim = replace(
        guard.ingestion_claim,
        fencing_token=99,
    )

    with pytest.raises(RuntimeOwnershipLost):
        guard(binding, "start")


@pytest.mark.parametrize(
    "field",
    ["group_fencing_token", "task_fencing_token"],
)
def test_stale_group_token_is_rejected(scope_setup, field):
    _, binding, _, guard = scope_setup

    guard.task_claim = replace(
        guard.task_claim,
        **{field: 99},
    )

    with pytest.raises(RuntimeOwnershipLost):
        guard(binding, "start")


def test_retirement_receipt_blocks_operation(scope_setup):
    registry, binding, _, guard = scope_setup

    registry.client.put_item(
        TableName=registry.table_name,
        Item=registry._encode(
            {
                "pk": guard.task_claim.group_key,
                "sk": (
                    f"RETIRED#{guard.task_claim.scope_fingerprint}"
                ),
            }
        ),
    )

    with pytest.raises(RuntimeOwnershipLost):
        guard(binding, "resume")


def test_task_claim_for_another_scope_is_rejected(scope_setup):
    _, binding, _, guard = scope_setup

    guard.task_claim = replace(
        guard.task_claim,
        task_key="DMSTASK#another-task",
    )

    with pytest.raises(
        RuntimeScopeDenied,
        match="resolved scope",
    ):
        guard(binding, "start")


def test_registry_permission_errors_are_not_hidden(
    scope_setup,
    monkeypatch,
):
    registry, binding, _, guard = scope_setup

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        Mock(
            side_effect=ClientError(
                {"Error": {"Code": "AccessDeniedException"}},
                "TransactWriteItems",
            )
        ),
    )

    with pytest.raises(ClientError):
        guard(binding, "start")


def test_real_guard_blocks_start_when_ownership_expires_during_preflight(
    scope_setup,
):
    registry, binding, clock, guard = scope_setup

    dms = Mock()
    inspection = Mock()

    inspection.task_status.return_value = {
        "replication_task_arn": binding[
            "resources"
        ]["replication_task_arn"],
        "status": "ready",
        "has_started": False,
        "migration_type": "full-load-and-cdc",
    }

    preflight = Mock()

    def expire_during_preflight(**kwargs):
        clock["now"] += 300
        return {"connectionStatus": "successful"}

    preflight.run.side_effect = expire_during_preflight

    controller = TaskControl(
        dms,
        inspection,
        preflight,
        check_scope=guard,
    )

    with pytest.raises(RuntimeOwnershipLost):
        controller.start(binding)

    dms.start_replication_task.assert_not_called()


@pytest.mark.parametrize("owner", ["group", "task"])
def test_retired_owner_record_is_rejected(scope_setup, owner):
    registry, binding, _, guard = scope_setup

    pk = (
        guard.task_claim.group_key
        if owner == "group"
        else guard.task_claim.task_key
    )

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {"pk": pk, "sk": "OWNERSHIP"}
        ),
        UpdateExpression="SET retired = :retired",
        ExpressionAttributeValues=registry._encode(
            {":retired": True}
        ),
    )

    with pytest.raises(RuntimeOwnershipLost):
        guard(binding, "start")


def test_lost_table_reservation_is_rejected(scope_setup):
    registry, binding, _, guard = scope_setup

    table = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="example",
    )

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {
                "pk": table.partition_key,
                "sk": "HEAD",
            }
        ),
        UpdateExpression="REMOVE active_ingestion_id",
    )

    with pytest.raises(RuntimeOwnershipLost):
        guard(binding, "stop")