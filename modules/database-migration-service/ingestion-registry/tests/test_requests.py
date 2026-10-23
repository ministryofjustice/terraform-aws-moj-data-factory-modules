from dataclasses import FrozenInstanceError, replace

import pytest

from ingestion_registry.identity import TableIdentity
from ingestion_registry.requests import NewIngestionRequest


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


def test_request_is_repeatable(
    ingestion_request: NewIngestionRequest,
) -> None:
    repeated = replace(ingestion_request)

    assert repeated.partition_key == ingestion_request.partition_key
    assert repeated.fingerprint == ingestion_request.fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation", "REINGESTION"),
        ("configuration_revision", "c" * 64),
        ("artifact_revision", "d" * 64),
        ("extraction_generation", "generation-two"),
    ],
)
def test_changed_content_keeps_key_but_changes_fingerprint(
    ingestion_request: NewIngestionRequest,
    field: str,
    value: str,
) -> None:
    changed = replace(ingestion_request, **{field: value})

    assert changed.partition_key == ingestion_request.partition_key
    assert changed.fingerprint != ingestion_request.fingerprint


def test_different_table_with_same_request_id_is_detectable(
    ingestion_request: NewIngestionRequest,
) -> None:
    changed = replace(
        ingestion_request,
        table=replace(
            ingestion_request.table,
            table_name="another_table",
        ),
    )

    assert changed.partition_key == ingestion_request.partition_key
    assert changed.fingerprint != ingestion_request.fingerprint


def test_different_request_id_has_separate_key(
    ingestion_request: NewIngestionRequest,
) -> None:
    changed = replace(
        ingestion_request,
        request_id="request-456",
    )

    assert changed.partition_key != ingestion_request.partition_key
    assert changed.fingerprint == ingestion_request.fingerprint


@pytest.mark.parametrize(
    "operation",
    ["RECOVERY", "REPLAY", "initial_ingestion", ""],
)
def test_other_operations_require_separate_request_models(
    ingestion_request: NewIngestionRequest,
    operation: str,
) -> None:
    with pytest.raises(ValueError, match="operation"):
        replace(ingestion_request, operation=operation)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("configuration_revision", "invalid"),
        ("artifact_revision", "B" * 64),
        ("extraction_generation", "generation/one"),
        ("request_id", " request-123"),
    ],
)
def test_invalid_request_fields_are_rejected(
    ingestion_request: NewIngestionRequest,
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        replace(ingestion_request, **{field: value})


def test_request_is_immutable(
    ingestion_request: NewIngestionRequest,
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(
            ingestion_request,
            "operation",
            "REINGESTION",
        )


def test_fingerprint_payload_is_detached(
    ingestion_request: NewIngestionRequest,
) -> None:
    original_fingerprint = ingestion_request.fingerprint
    payload = ingestion_request.fingerprint_payload()

    payload["table"]["table_name"] = "changed"

    assert ingestion_request.table.table_name == "dms_integration_test"
    assert ingestion_request.fingerprint == original_fingerprint