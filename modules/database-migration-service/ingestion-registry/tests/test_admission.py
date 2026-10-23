from dataclasses import replace

import boto3
import pytest
from moto import mock_aws

from ingestion_registry.admission import (
    IngestionConflict,
    IngestionRegistry,
    RequestConflict,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.requests import NewIngestionRequest
from botocore.exceptions import ClientError, ReadTimeoutError


@pytest.fixture
def registry():
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

        yield IngestionRegistry(
            client,
            table_name="test-ingestion-registry",
        )


@pytest.fixture
def ingestion_request() -> NewIngestionRequest:
    return NewIngestionRequest(
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


def item_count(registry: IngestionRegistry) -> int:
    return registry.client.scan(
        TableName=registry.table_name,
        Select="COUNT",
    )["Count"]


def test_initial_admission_creates_version_one(
    registry,
    ingestion_request,
) -> None:
    result = registry.admit(ingestion_request)

    assert result.ingestion_version == 1
    assert result.duplicate is False
    assert result.ingestion_id
    assert item_count(registry) == 4


def test_duplicate_returns_original_ingestion(
    registry,
    ingestion_request,
) -> None:
    original = registry.admit(ingestion_request)
    repeated = registry.admit(ingestion_request)

    assert repeated.ingestion_id == original.ingestion_id
    assert repeated.ingestion_version == original.ingestion_version
    assert repeated.duplicate is True
    assert item_count(registry) == 4


def test_same_request_id_with_changed_content_is_rejected(
    registry,
    ingestion_request,
) -> None:
    registry.admit(ingestion_request)

    with pytest.raises(RequestConflict, match="different content"):
        registry.admit(
            replace(
                ingestion_request,
                extraction_generation="generation-two",
            )
        )

    assert item_count(registry) == 4


def test_second_initial_ingestion_is_rejected(
    registry,
    ingestion_request,
) -> None:
    registry.admit(ingestion_request)

    with pytest.raises(IngestionConflict, match="already exists"):
        registry.admit(
            replace(
                ingestion_request,
                request_id="another-request",
            )
        )

    assert item_count(registry) == 4


def test_reingestion_requires_previous_ingestion(
    registry,
    ingestion_request,
) -> None:
    with pytest.raises(IngestionConflict, match="requires an existing"):
        registry.admit(
            replace(
                ingestion_request,
                operation="REINGESTION",
            )
        )

    assert item_count(registry) == 0


def test_active_ingestion_blocks_reingestion(
    registry,
    ingestion_request,
) -> None:
    registry.admit(ingestion_request)

    with pytest.raises(IngestionConflict, match="active ingestion"):
        registry.admit(
            replace(
                ingestion_request,
                request_id="reingestion-request",
                operation="REINGESTION",
                extraction_generation="generation-two",
            )
        )

    assert item_count(registry) == 4


def test_independent_tables_have_independent_versions(
    registry,
    ingestion_request,
) -> None:
    first = registry.admit(ingestion_request)
    second = registry.admit(
        replace(
            ingestion_request,
            request_id="another-table-request",
            table=replace(
                ingestion_request.table,
                table_name="another_table",
            ),
        )
    )

    assert first.ingestion_version == 1
    assert second.ingestion_version == 1
    assert first.ingestion_id != second.ingestion_id
    assert item_count(registry) == 8

def test_same_request_race_returns_winning_ingestion(
    registry,
    ingestion_request,
    monkeypatch,
) -> None:
    original_write = registry.client.transact_write_items
    winner = None
    raced = False

    def competing_write(**kwargs):
        nonlocal winner, raced

        if not raced:
            raced = True
            winner = registry.admit(ingestion_request)

        return original_write(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        competing_write,
    )

    result = registry.admit(ingestion_request)

    assert winner is not None
    assert result.ingestion_id == winner.ingestion_id
    assert result.ingestion_version == 1
    assert result.duplicate is True
    assert item_count(registry) == 4


def test_same_request_id_race_with_changed_content_is_rejected(
    registry,
    ingestion_request,
    monkeypatch,
) -> None:
    original_write = registry.client.transact_write_items
    raced = False
    winner = None

    changed_request = replace(
        ingestion_request,
        extraction_generation="generation-two",
    )

    def competing_write(**kwargs):
        nonlocal winner, raced

        if not raced:
            raced = True
            winner = registry.admit(changed_request)

        return original_write(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        competing_write,
    )

    with pytest.raises(RequestConflict, match="different content"):
        registry.admit(ingestion_request)

    assert winner is not None
    assert item_count(registry) == 4

    repeated = registry.admit(changed_request)

    assert repeated.ingestion_id == winner.ingestion_id
    assert repeated.duplicate is True


def test_different_request_race_admits_only_one_initial_ingestion(
    registry,
    ingestion_request,
    monkeypatch,
) -> None:
    original_write = registry.client.transact_write_items
    raced = False
    winner = None

    competing_request = replace(
        ingestion_request,
        request_id="competing-request",
    )

    def competing_write(**kwargs):
        nonlocal winner, raced

        if not raced:
            raced = True
            winner = registry.admit(competing_request)

        return original_write(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        competing_write,
    )

    with pytest.raises(IngestionConflict, match="already exists"):
        registry.admit(ingestion_request)

    assert winner is not None
    assert winner.ingestion_version == 1
    assert item_count(registry) == 4


def test_lost_commit_response_does_not_create_another_ingestion(
    registry,
    ingestion_request,
    monkeypatch,
) -> None:
    original_write = registry.client.transact_write_items
    committed_items = None

    def commit_then_timeout(**kwargs):
        nonlocal committed_items

        original_write(**kwargs)
        committed_items = kwargs["TransactItems"]

        raise ReadTimeoutError(
            endpoint_url=registry.client.meta.endpoint_url
        )

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        commit_then_timeout,
    )

    with pytest.raises(ReadTimeoutError):
        registry.admit(ingestion_request)

    assert committed_items is not None
    assert item_count(registry) == 4

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        original_write,
    )

    recovered_result = registry.admit(ingestion_request)

    stored_request = registry.client.get_item(
        TableName=registry.table_name,
        Key={
            "pk": {"S": ingestion_request.partition_key},
            "sk": {"S": "META"},
        },
        ConsistentRead=True,
    )["Item"]

    assert recovered_result.ingestion_id == (
        stored_request["ingestion_id"]["S"]
    )
    assert recovered_result.ingestion_version == 1
    assert recovered_result.duplicate is True
    assert item_count(registry) == 4


def test_duplicate_committing_before_head_read_is_reconciled(
    registry,
    ingestion_request,
    monkeypatch,
) -> None:
    original_read = registry.client.get_item
    raced = False
    winner = None

    def competing_read(**kwargs):
        nonlocal raced, winner

        if (
            not raced
            and kwargs["Key"]["sk"]["S"] == "HEAD"
        ):
            raced = True
            winner = registry.admit(ingestion_request)

        return original_read(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "get_item",
        competing_read,
    )

    result = registry.admit(ingestion_request)

    assert winner is not None
    assert result.ingestion_id == winner.ingestion_id
    assert result.duplicate is True
    assert item_count(registry) == 4


def test_non_retryable_transaction_error_is_not_hidden(
    registry,
    ingestion_request,
    monkeypatch,
) -> None:
    error = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "Transaction validation failed",
            },
            "CancellationReasons": [
                {
                    "Code": "ValidationError",
                    "Message": "Invalid transaction",
                }
            ],
        },
        "TransactWriteItems",
    )
    calls = 0

    def fail(**kwargs):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        fail,
    )

    with pytest.raises(ClientError) as caught:
        registry.admit(ingestion_request)

    assert caught.value is error
    assert calls == 1
    assert item_count(registry) == 0