from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from dms_runtime.batch_downstream import (
    BatchDownstream,
    BatchPrerequisiteMissing,
)
from dms_runtime.batch_processing import (
    BatchProcessing,
    BatchRawHistory,
    ProcessingCompletion,
    BatchPublication,
)
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import IngestionOwnership
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.requests import NewIngestionRequest


@pytest.fixture
def processing_setup():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="eu-west-2")
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
        registry = IngestionRegistry(client, table_name="runtime-registry")

        table = TableIdentity(
            environment="development",
            source="sandbox-postgres",
            database_name="dms_test",
            schema_name="public",
            table_name="example",
        )
        admitted = registry.admit(
            NewIngestionRequest(
                request_id="processing-integration-one",
                operation="INITIAL_INGESTION",
                table=table,
                configuration_revision="a" * 64,
                artifact_revision="b" * 64,
                extraction_generation="generation-one",
            )
        )
        clock = {"now": 1000}

        def now():
            return clock["now"]

        ownership = IngestionOwnership(registry, clock=now)
        claim = ownership.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="table-worker",
        )
        progress = IngestionProgress(registry, clock=now)

        progress.record_stage(
            claim,
            stage="CONTRACT",
            status="SUCCEEDED",
            update_id="contract-pinned",
            expected_revision=0,
            result_reference={
                "kind": "registered-contract",
                "contract_id": "contract-one",
                "contract_version": "v1",
                "contract_fingerprint": "c" * 64,
                "contract_bucket_name": "example-contracts",
                "contract_object_key": (
                    "sandbox-postgres/dms_test/public/example/v1/contract.avsc"
                ),
                "contract_object_version_id": "contract-version-one",
                "contract_object_sha256": "d" * 64,
            },
        )
        progress.record_stage(
            claim,
            stage="EXTRACTION",
            status="SUCCEEDED",
            update_id="raw-batch-recorded",
            expected_revision=0,
            result_reference={
                "kind": "raw-batch",
                "bucket_name": "example-raw",
                "object_key": "landing/generation-one/batch-manifest.json",
                "version_id": "raw-batch-version-one",
                "sha256": "e" * 64,
            },
        )

        def resolve_deployment(ingestion_id):
            assert ingestion_id == admitted.ingestion_id
            return {
                "configuration_revision": "a" * 64,
                "artifact_revision": "b" * 64,
                "extraction_generation": "generation-one",
                "table": {
                    "schema_name": "public",
                    "table_name": "example",
                },
            }

        downstream = BatchDownstream(
            resolve_deployment=resolve_deployment,
            read_stage=progress.read_stage,
        )
        adapter = Mock(spec=["submit", "lookup"])
        processing = BatchProcessing(
            downstream=downstream,
            progress=progress,
            adapter=adapter,
        )

        history_adapter = Mock(spec=["submit", "lookup"])
        history = BatchRawHistory(
            downstream=downstream,
            progress=progress,
            adapter=history_adapter,
        )

        yield (
            processing,
            adapter,
            progress,
            ownership,
            claim,
            clock,
            downstream,
            history,
            history_adapter,
        )


def test_recovers_processing_after_lost_response_and_owner_takeover(
    processing_setup,
):
    processing, adapter, progress, ownership, claim, clock = processing_setup[:6]
    completions = {}

    def submit(*, operation_id, request_fingerprint, request):
        reservation = progress.read_stage(
            ingestion_id=claim.ingestion_id,
            stage="PROCESSING",
        )
        assert reservation["status"] == "RUNNING"
        assert reservation["result_reference"]["operation_id"] == operation_id
        assert (
            reservation["result_reference"]["request_fingerprint"]
            == request_fingerprint
        )
        assert request["ingestion_id"] == claim.ingestion_id
        assert request["deployment"]["configuration_revision"] == "a" * 64
        assert request["contract"]["contract_object_version_id"] == (
            "contract-version-one"
        )
        assert request["raw_batch"]["version_id"] == "raw-batch-version-one"

        completions[operation_id] = ProcessingCompletion(
            operation_id=operation_id,
            ingestion_id=claim.ingestion_id,
            request_fingerprint=request_fingerprint,
            result_reference={
                "kind": "component-result",
                "execution_id": "processing-execution-one",
            },
        )
        raise TimeoutError("Processing completed but its response was lost")

    def lookup(*, operation_id):
        return completions.get(operation_id)

    adapter.submit.side_effect = submit
    adapter.lookup.side_effect = lookup

    with pytest.raises(TimeoutError, match="response was lost"):
        processing.advance(claim)

    pending = progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="PROCESSING",
    )
    assert pending["status"] == "RUNNING"
    assert pending["revision"] == 1
    operation_id = pending["result_reference"]["operation_id"]
    adapter.lookup.assert_not_called()

    clock["now"] = claim.lease_expires_at
    recovered_claim = ownership.claim(
        ingestion_id=claim.ingestion_id,
        execution_id="recovery-worker",
    )

    result = processing.advance(recovered_claim)

    adapter.lookup.assert_called_once_with(operation_id=operation_id)
    adapter.submit.assert_called_once()
    assert result["status"] == "SUCCEEDED"
    assert result["revision"] == 2
    assert result["fencing_token"] == recovered_claim.fencing_token
    assert result["result_reference"]["operation_id"] == operation_id
    assert result["result_reference"]["execution_id"] == "processing-execution-one"
    assert result == progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="PROCESSING",
    )

def test_raw_history_commits_while_processing_is_running(processing_setup):
    (
        processing,
        processing_adapter,
        progress,
        _,
        claim,
        _,
        downstream,
        history,
        history_adapter,
    ) = processing_setup
    processing_adapter.submit.return_value = None
    history_location = "s3://example-history/generation-one/"

    def commit_history(*, operation_id, request_fingerprint, request):
        return ProcessingCompletion(
            operation_id=operation_id,
            ingestion_id=request["ingestion_id"],
            request_fingerprint=request_fingerprint,
            result_reference={
                "kind": "history-commit",
                "commit_id": "baseline-commit-one",
                "history_location": history_location,
            },
        )

    history_adapter.submit.side_effect = commit_history

    pending = processing.advance(claim)
    assert pending["status"] == "RUNNING"

    committed = history.advance(claim)
    assert committed["status"] == "SUCCEEDED"
    assert committed["result_reference"]["history_location"] == history_location
    assert progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="HISTORY_BASELINE",
    ) == committed
    assert progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="PROCESSING",
    ) == pending

    processing_call = processing_adapter.submit.call_args.kwargs
    history_call = history_adapter.submit.call_args.kwargs
    assert processing_call["request"] == history_call["request"]
    assert processing_call["operation_id"] != history_call["operation_id"]

    with pytest.raises(BatchPrerequisiteMissing, match="PROCESSING"):
        downstream.publication_prerequisites(
            claim.ingestion_id,
            reconciliation_required=False,
        )

    processing_adapter.lookup.return_value = ProcessingCompletion(
        operation_id=processing_call["operation_id"],
        ingestion_id=claim.ingestion_id,
        request_fingerprint=processing_call["request_fingerprint"],
        result_reference={
            "kind": "component-result",
            "execution_id": "processing-execution-one",
        },
    )

    assert processing.advance(claim)["status"] == "SUCCEEDED"

    prerequisites = downstream.publication_prerequisites(
        claim.ingestion_id,
        reconciliation_required=False,
    )
    assert prerequisites["HISTORY_BASELINE"]["commit_id"] == "baseline-commit-one"
    assert prerequisites["HISTORY_BASELINE"]["history_location"] == history_location

    processing_adapter.lookup.assert_called_once_with(
        operation_id=processing_call["operation_id"],
    )
    processing_adapter.submit.assert_called_once()
    history_adapter.submit.assert_called_once()

def test_publication_waits_for_processing_and_recovers_lost_response(
    processing_setup,
):
    (
        _,
        processing_adapter,
        progress,
        _,
        claim,
        _,
        downstream,
        _,
        history_adapter,
    ) = processing_setup

    publication_adapter = Mock(spec=["submit", "lookup"])
    publication = BatchPublication(
        downstream=downstream,
        progress=progress,
        adapter=publication_adapter,
        reconciliation_required=False,
    )
    history_location = "s3://example-history/generation-one/"
    completions = {}

    progress.record_stage(
        claim,
        stage="HISTORY_BASELINE",
        status="SUCCEEDED",
        update_id="history-baseline-complete",
        expected_revision=0,
        result_reference={
            "kind": "history-commit",
            "commit_id": "baseline-commit-one",
            "history_location": history_location,
        },
    )

    with pytest.raises(BatchPrerequisiteMissing, match="PROCESSING"):
        publication.advance(claim)

    publication_adapter.submit.assert_not_called()
    assert progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="PUBLICATION",
    ) is None

    progress.record_stage(
        claim,
        stage="PROCESSING",
        status="SUCCEEDED",
        update_id="processing-complete",
        expected_revision=0,
        result_reference={
            "kind": "component-result",
            "execution_id": "processing-execution-one",
        },
    )
    upstream_before = {
        stage: progress.read_stage(
            ingestion_id=claim.ingestion_id,
            stage=stage,
        )
        for stage in ("EXTRACTION", "PROCESSING", "HISTORY_BASELINE")
    }

    def submit(*, operation_id, request_fingerprint, request):
        assert request["history_location"] == history_location
        assert request["history_commit_id"] == "baseline-commit-one"
        assert request["contract"]["contract_object_version_id"] == (
            "contract-version-one"
        )
        completions[operation_id] = ProcessingCompletion(
            operation_id=operation_id,
            ingestion_id=request["ingestion_id"],
            request_fingerprint=request_fingerprint,
            result_reference={
                "kind": "publication-result",
                "glue_database": "example_history",
                "glue_table": "example_v1",
            },
        )
        raise TimeoutError("Publication completed but its response was lost")

    publication_adapter.submit.side_effect = submit
    publication_adapter.lookup.side_effect = (
        lambda *, operation_id: completions.get(operation_id)
    )

    with pytest.raises(TimeoutError, match="response was lost"):
        publication.advance(claim)

    pending = progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="PUBLICATION",
    )
    assert pending["status"] == "RUNNING"
    operation_id = pending["result_reference"]["operation_id"]

    result = publication.advance(claim)

    assert result["status"] == "SUCCEEDED"
    assert result["result_reference"]["operation_id"] == operation_id
    assert result["result_reference"]["glue_table"] == "example_v1"
    publication_adapter.lookup.assert_called_once_with(operation_id=operation_id)
    publication_adapter.submit.assert_called_once()
    processing_adapter.submit.assert_not_called()
    history_adapter.submit.assert_not_called()

    for stage, before in upstream_before.items():
        assert progress.read_stage(
            ingestion_id=claim.ingestion_id,
            stage=stage,
        ) == before