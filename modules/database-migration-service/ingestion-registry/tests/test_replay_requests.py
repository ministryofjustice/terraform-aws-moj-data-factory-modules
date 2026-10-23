from dataclasses import FrozenInstanceError, replace
from uuid import NAMESPACE_DNS, uuid5

import pytest

from ingestion_registry.contracts import RegisteredContract
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference
from ingestion_registry.replay_requests import ReplayRequest


@pytest.fixture
def replay_request():
    return ReplayRequest(
        request_id="replay-request-one",
        table=TableIdentity(
            environment="development",
            source="sandbox-postgres",
            database_name="dms_test",
            schema_name="public",
            table_name="dms_integration_test",
        ),
        source_ingestion_id="original-ingestion",
        history_reference=S3ObjectReference(
            bucket_name="example-history",
            object_key="commits/original-ingestion/baseline.json",
            version_id="history-commit-version-one",
            sha256="a" * 64,
        ),
        processing_revision="b" * 64,
        contract=RegisteredContract(
            contract_id=str(
                uuid5(
                    NAMESPACE_DNS,
                    "sandbox-postgres:dms_test:"
                    "public:dms_integration_test",
                )
            ),
            contract_version="v1",
            contract_fingerprint="c" * 64,
            object_reference=S3ObjectReference(
                bucket_name="example-contracts",
                object_key=(
                    "sandbox-postgres/dms_test/public/"
                    "dms_integration_test/v1/contract.avsc"
                ),
                version_id="contract-version-one",
                sha256="d" * 64,
            ),
        ),
    )


def test_identical_replay_requests_are_repeatable(replay_request):
    repeated = replace(replay_request)

    assert repeated.partition_key == replay_request.partition_key
    assert repeated.fingerprint == replay_request.fingerprint


def test_new_request_id_has_separate_identity(replay_request):
    changed = replace(
        replay_request,
        request_id="replay-request-two",
    )

    assert changed.partition_key != replay_request.partition_key
    assert changed.fingerprint == replay_request.fingerprint


def test_changed_processing_revision_changes_fingerprint(
    replay_request,
):
    changed = replace(
        replay_request,
        processing_revision="e" * 64,
    )

    assert changed.partition_key == replay_request.partition_key
    assert changed.fingerprint != replay_request.fingerprint


def test_changed_history_version_changes_fingerprint(
    replay_request,
):
    changed = replace(
        replay_request,
        history_reference=replace(
            replay_request.history_reference,
            version_id="history-commit-version-two",
        ),
    )

    assert changed.fingerprint != replay_request.fingerprint


def test_changed_source_ingestion_changes_fingerprint(
    replay_request,
):
    changed = replace(
        replay_request,
        source_ingestion_id="another-ingestion",
    )

    assert changed.fingerprint != replay_request.fingerprint


def test_changed_contract_changes_fingerprint(replay_request):
    changed = replace(
        replay_request,
        contract=replace(
            replay_request.contract,
            contract_fingerprint="f" * 64,
        ),
    )

    assert changed.fingerprint != replay_request.fingerprint


def test_replay_payload_does_not_allocate_ingestion_version(
    replay_request,
):
    payload = replay_request.fingerprint_payload()

    assert payload["operation"] == "REPLAY"
    assert payload["source_ingestion_id"] == "original-ingestion"
    assert "ingestion_version" not in payload
    assert "extraction_generation" not in payload


def test_replay_payload_is_detached(replay_request):
    original_fingerprint = replay_request.fingerprint
    payload = replay_request.fingerprint_payload()

    payload["history_reference"]["version_id"] = "changed"
    payload["contract"]["contract_version"] = "v99"

    assert replay_request.fingerprint == original_fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_ingestion_id", ""),
        ("source_ingestion_id", " original-ingestion"),
        ("processing_revision", "invalid"),
        ("processing_revision", "B" * 64),
        ("request_id", " replay-request-one"),
    ],
)
def test_invalid_replay_fields_are_rejected(
    replay_request,
    field,
    value,
):
    with pytest.raises(ValueError):
        replace(replay_request, **{field: value})


def test_replay_request_is_immutable(replay_request):
    with pytest.raises(FrozenInstanceError):
        setattr(
            replay_request,
            "source_ingestion_id",
            "changed",
        )