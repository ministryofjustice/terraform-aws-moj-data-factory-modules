from dataclasses import FrozenInstanceError, replace

import pytest

from ingestion_registry.identity import (
    TableIdentity,
    TaskGroupIdentity,
    identity_digest,
    request_partition_key,
)


@pytest.fixture
def table() -> TableIdentity:
    return TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="dms_integration_test",
    )


def test_table_identity_is_repeatable(
    table: TableIdentity,
) -> None:
    assert table.partition_key == replace(table).partition_key
    assert table.partition_key.startswith("TABLE#")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "test"),
        ("source", "another-service"),
        ("database_name", "another_database"),
        ("schema_name", "another_schema"),
        ("table_name", "another_table"),
    ],
)
def test_each_identity_field_changes_key(
    table: TableIdentity,
    field: str,
    value: str,
) -> None:
    assert replace(table, **{field: value}).partition_key != (
        table.partition_key
    )


def test_database_identifier_case_is_preserved(
    table: TableIdentity,
) -> None:
    assert replace(table, schema_name="PUBLIC").partition_key != (
        table.partition_key
    )


def test_table_identity_is_immutable(
    table: TableIdentity,
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(table, "table_name", "changed")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", ""),
        ("source", "invalid source"),
        ("database_name", " database"),
        ("schema_name", "public/*"),
        ("table_name", "%"),
    ],
)
def test_invalid_identity_is_rejected(
    table: TableIdentity,
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        replace(table, **{field: value})


def test_structured_hash_avoids_delimiter_ambiguity() -> None:
    assert identity_digest(["a:b", "c"]) != (
        identity_digest(["a", "b:c"])
    )


def test_task_group_identity_is_repeatable() -> None:
    first = TaskGroupIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        task_group="integration",
    )

    assert first.partition_key == replace(first).partition_key
    assert first.partition_key.startswith("TASKGROUP#")
    assert replace(first, task_group="another").partition_key != (
        first.partition_key
    )


def test_request_key_is_repeatable() -> None:
    arguments = {
        "environment": "development",
        "source": "sandbox-postgres",
        "request_id": "request-123",
    }

    assert request_partition_key(**arguments) == (
        request_partition_key(**arguments)
    )


def test_request_key_is_scoped_to_service() -> None:
    assert request_partition_key(
        environment="development",
        source="sandbox-postgres",
        request_id="request-123",
    ) != request_partition_key(
        environment="development",
        source="sandbox-oracle",
        request_id="request-123",
    )


@pytest.mark.parametrize(
    "request_id",
    ["", " leading", "trailing ", "x" * 257],
)
def test_invalid_request_id_is_rejected(
    request_id: str,
) -> None:
    with pytest.raises(ValueError, match="request_id"):
        request_partition_key(
            environment="development",
            source="sandbox-postgres",
            request_id=request_id,
        )