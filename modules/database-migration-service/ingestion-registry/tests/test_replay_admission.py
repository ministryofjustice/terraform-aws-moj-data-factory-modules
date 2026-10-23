from dataclasses import asdict, replace
from uuid import NAMESPACE_DNS, uuid5

import pytest

from ingestion_registry.admission import RequestConflict
from ingestion_registry.contracts import RegisteredContract
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference
from ingestion_registry.replay_admission import (
    ReplayConflict,
    ReplayRegistry,
)
from ingestion_registry.replay_requests import ReplayRequest


@pytest.fixture
def replay_admission_setup(progress_setup):
    registry, _, admitted, _, claim, progress = progress_setup

    history = S3ObjectReference(
        bucket_name="example-history",
        object_key="commits/baseline.json",
        version_id="history-version-one",
        sha256="a" * 64,
    )

    progress.record_stage(
        claim,
        stage="HISTORY_BASELINE",
        status="SUCCEEDED",
        update_id="history-committed",
        expected_revision=0,
        result_reference=asdict(history),
    )

    table = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="dms_integration_test",
    )

    request = ReplayRequest(
        request_id="replay-request-one",
        table=table,
        source_ingestion_id=admitted.ingestion_id,
        history_reference=history,
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

    return registry, ReplayRegistry(registry), request


def test_replay_creates_separate_work_without_changing_ingestion(
    replay_admission_setup,
):
    registry, replays, request = replay_admission_setup

    head_before = registry._read(
        request.table.partition_key,
        "HEAD",
    )
    source_before = registry._read(
        f"INGESTION#{request.source_ingestion_id}",
        "META",
    )
    history_before = registry._read(
        f"INGESTION#{request.source_ingestion_id}",
        "STAGE#HISTORY_BASELINE",
    )

    result = replays.admit(request)

    replay = registry._read(
        f"REPLAY#{result.replay_id}",
        "META",
    )

    assert result.duplicate is False
    assert result.source_ingestion_version == 1
    assert replay["history_reference"] == asdict(
        request.history_reference
    )
    assert replay["processing_revision"] == request.processing_revision
    assert replay["contract"] == asdict(request.contract)
    assert "ingestion_version" not in replay
    assert "extraction_generation" not in replay

    assert registry._read(
        request.table.partition_key,
        "HEAD",
    ) == head_before

    assert registry._read(
        f"INGESTION#{request.source_ingestion_id}",
        "META",
    ) == source_before

    assert registry._read(
        f"INGESTION#{request.source_ingestion_id}",
        "STAGE#HISTORY_BASELINE",
    ) == history_before


def test_duplicate_replay_returns_original_identity(
    replay_admission_setup,
):
    _, replays, request = replay_admission_setup

    first = replays.admit(request)
    repeated = replays.admit(request)

    assert repeated.replay_id == first.replay_id
    assert repeated.source_ingestion_version == 1
    assert repeated.duplicate is True


def test_changed_request_content_is_rejected(
    replay_admission_setup,
):
    _, replays, request = replay_admission_setup
    replays.admit(request)

    with pytest.raises(RequestConflict):
        replays.admit(
            replace(
                request,
                processing_revision="e" * 64,
            )
        )


def test_another_request_creates_another_replay(
    replay_admission_setup,
):
    _, replays, request = replay_admission_setup

    first = replays.admit(request)
    second = replays.admit(
        replace(
            request,
            request_id="replay-request-two",
        )
    )

    assert second.replay_id != first.replay_id
    assert second.source_ingestion_version == (
        first.source_ingestion_version
    )


def test_unknown_source_ingestion_is_rejected(
    replay_admission_setup,
):
    _, replays, request = replay_admission_setup

    with pytest.raises(ReplayConflict, match="does not exist"):
        replays.admit(
            replace(
                request,
                source_ingestion_id="unknown-ingestion",
            )
        )


def test_wrong_table_is_rejected(replay_admission_setup):
    _, replays, request = replay_admission_setup

    with pytest.raises(ReplayConflict, match="different table"):
        replays.admit(
            replace(
                request,
                table=replace(
                    request.table,
                    table_name="another_table",
                ),
            )
        )


def test_different_history_version_is_rejected(
    replay_admission_setup,
):
    _, replays, request = replay_admission_setup

    with pytest.raises(ReplayConflict, match="history reference"):
        replays.admit(
            replace(
                request,
                history_reference=replace(
                    request.history_reference,
                    version_id="another-history-version",
                ),
            )
        )


def test_wrong_contract_identity_is_rejected(
    replay_admission_setup,
):
    _, replays, request = replay_admission_setup

    with pytest.raises(ReplayConflict, match="contract"):
        replays.admit(
            replace(
                request,
                contract=replace(
                    request.contract,
                    contract_id=str(uuid5(NAMESPACE_DNS, "another-table")),
                ),
            )
        )


def test_lost_admission_response_is_reconciled(
    replay_admission_setup,
    monkeypatch,
):
    from botocore.exceptions import ReadTimeoutError

    registry, replays, request = replay_admission_setup
    original_write = registry.client.transact_write_items

    def commit_then_lose_response(**kwargs):
        original_write(**kwargs)
        raise ReadTimeoutError(
            endpoint_url="https://dynamodb.eu-west-2.amazonaws.com",
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            registry.client,
            "transact_write_items",
            commit_then_lose_response,
        )

        with pytest.raises(ReadTimeoutError):
            replays.admit(request)

    stored_request = registry._read(
        request.partition_key,
        "META",
    )
    recovered = replays.admit(request)

    assert recovered.duplicate is True
    assert recovered.replay_id == stored_request["replay_id"]

def test_missing_history_blocks_replay(
    replay_admission_setup,
):
    registry, replays, request = replay_admission_setup

    registry.client.delete_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {
                "pk": f"INGESTION#{request.source_ingestion_id}",
                "sk": "STAGE#HISTORY_BASELINE",
            }
        ),
    )

    with pytest.raises(ReplayConflict, match="committed baseline"):
        replays.admit(request)

    assert registry._read(
        request.partition_key,
        "META",
    ) is None


def test_incomplete_history_blocks_replay(
    replay_admission_setup,
):
    registry, replays, request = replay_admission_setup

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {
                "pk": f"INGESTION#{request.source_ingestion_id}",
                "sk": "STAGE#HISTORY_BASELINE",
            }
        ),
        UpdateExpression="SET #status = :running",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=registry._encode(
            {":running": "RUNNING"}
        ),
    )

    with pytest.raises(ReplayConflict, match="committed baseline"):
        replays.admit(request)

    assert registry._read(
        request.partition_key,
        "META",
    ) is None


def test_ingestion_request_id_cannot_be_reused_for_replay(
    replay_admission_setup,
):
    _, replays, request = replay_admission_setup

    with pytest.raises(RequestConflict):
        replays.admit(
            replace(
                request,
                request_id="request-123",
            )
        )


def test_contract_path_must_match_selected_version(
    replay_admission_setup,
):
    _, replays, request = replay_admission_setup

    changed_contract = replace(
        request.contract,
        object_reference=replace(
            request.contract.object_reference,
            object_key=(
                "sandbox-postgres/dms_test/public/"
                "dms_integration_test/v2/contract.avsc"
            ),
        ),
    )

    with pytest.raises(ReplayConflict, match="contract"):
        replays.admit(
            replace(
                request,
                contract=changed_contract,
            )
        )


def test_concurrent_duplicate_returns_winning_replay(
    replay_admission_setup,
    monkeypatch,
):
    registry, replays, request = replay_admission_setup
    original_write = registry.client.transact_write_items
    winner = {}

    def admit_competing_request_then_write(**kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(
                registry.client,
                "transact_write_items",
                original_write,
            )
            winner["result"] = replays.admit(request)

        return original_write(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            registry.client,
            "transact_write_items",
            admit_competing_request_then_write,
        )
        result = replays.admit(request)

    assert winner["result"].duplicate is False
    assert result.duplicate is True
    assert result.replay_id == winner["result"].replay_id

    stored_request = registry._read(
        request.partition_key,
        "META",
    )
    assert stored_request["replay_id"] == result.replay_id

    response = registry.client.scan(
        TableName=registry.table_name,
        FilterExpression="record_type = :record_type",
        ExpressionAttributeValues=registry._encode(
            {":record_type": "REPLAY"}
        ),
        ConsistentRead=True,
    )
    assert response["Count"] == 1