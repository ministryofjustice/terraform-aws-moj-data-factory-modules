from unittest.mock import Mock

import boto3
import pytest

from dms_runtime.batch_downstream import (
    BatchDownstream,
    BatchPrerequisiteMissing,
)
from dms_runtime.batch_handler import BatchHandler
from dms_runtime.batch_processing import (
    BatchProcessing,
    BatchPublication,
    BatchRawHistory,
    BatchReconciliation,
    ProcessingCompletion,
)
from dms_runtime.downstream_commit_adapter import DownstreamCommitAdapter
from dms_runtime.publication_component import PublicationTarget
from dms_runtime.raw_history_component import RawHistoryTarget
from ingestion_registry.lifecycle import IngestionLifecycle
from test_processing_inputs import pinned_inputs as pinned_inputs
from test_raw_batch_manifest import manifest_setup as manifest_setup


def test_batch_recovers_publication_after_history_and_reconciliation(
    manifest_setup,
):
    producer, progress, claim, verifier, request, raw, s3 = manifest_setup
    deployment = request["deployment"]
    ingestion_id = claim.ingestion_id

    producer.commit(claim, deployment=deployment, objects=[raw])
    for stage, evidence in (
        ("SOURCE_PREFLIGHT", {"kind": "source-preflight", "status": "verified"}),
        ("CONTRACT", request["contract"]),
    ):
        progress.record_stage(
            claim,
            stage=stage,
            status="SUCCEEDED",
            update_id=f"fixture-{stage}",
            expected_revision=0,
            result_reference=evidence,
        )

    downstream = BatchDownstream(
        resolve_deployment=lambda _: deployment,
        read_stage=progress.read_stage,
    )
    owner = boto3.client("sts").get_caller_identity()["Account"]
    bucket = "downstream-batch-history-test"
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    intents = boto3.resource("dynamodb", region_name="eu-west-2").create_table(
        TableName="downstream-batch-intents",
        KeySchema=[{"AttributeName": "operation_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "operation_id", "AttributeType": "S"}
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    processing_adapter = Mock()
    processing_adapter.submit.return_value = None
    processing = BatchProcessing(
        downstream=downstream,
        progress=progress,
        adapter=processing_adapter,
    )
    history_target = RawHistoryTarget(
        client=s3,
        verifier=verifier,
        raw_bucket_owner=owner,
        history_bucket=bucket,
        history_bucket_owner=owner,
        history_prefix="history/",
        max_batch_bytes=1024,
    )
    history = BatchRawHistory(
        downstream=downstream,
        progress=progress,
        adapter=DownstreamCommitAdapter(
            table=intents,
            target=history_target,
            component="raw_history",
        ),
    )

    reconciliation_adapter = Mock()
    reconciliation_adapter.submit.return_value = None
    reconciliation = BatchReconciliation(
        downstream=downstream,
        progress=progress,
        adapter=reconciliation_adapter,
    )

    registrations = {}
    catalogue = Mock()

    def create(registration):
        key = (registration.database, registration.table)
        assert key not in registrations
        registrations[key] = registration
        raise TimeoutError("Catalogue response lost")

    def read(*, database, table):
        return registrations.get((database, table))

    catalogue.create.side_effect = create
    catalogue.read.side_effect = read

    def build_handler():
        publication = BatchPublication(
            downstream=downstream,
            progress=progress,
            adapter=DownstreamCommitAdapter(
                table=intents,
                component="publication",
                target=PublicationTarget(
                    client=s3,
                    catalogue=catalogue,
                    history_bucket=bucket,
                    history_bucket_owner=owner,
                    history_prefix="history/",
                    database="example_history",
                    table="example_v1",
                ),
            ),
            reconciliation_required=True,
        )
        return BatchHandler(
            processing=processing,
            raw_history=history,
            publication=publication,
            lifecycle=IngestionLifecycle(progress, clock=progress.clock),
            reconciliation_required=True,
            reconciliation=reconciliation,
        )

    def handle(handler, action):
        return handler.handle(
            {
                "request_version": 1,
                "action": action,
                "ingestion_id": ingestion_id,
            },
            claim=claim,
        )

    def read_stage(stage):
        return progress.read_stage(ingestion_id=ingestion_id, stage=stage)

    handler = build_handler()
    assert handle(handler, "advance_processing")["status"] == "RUNNING"
    assert handle(handler, "advance_raw_history")["status"] == "SUCCEEDED"

    with pytest.raises(BatchPrerequisiteMissing, match="PROCESSING"):
        handle(handler, "advance_publication")

    catalogue.create.assert_not_called()
    assert read_stage("PUBLICATION") is None

    submitted = processing_adapter.submit.call_args.kwargs
    processing_adapter.lookup.return_value = ProcessingCompletion(
        operation_id=submitted["operation_id"],
        ingestion_id=ingestion_id,
        request_fingerprint=submitted["request_fingerprint"],
        result_reference={
            "kind": "component-result",
            "execution_id": "processing-run-one",
        },
    )
    assert handle(handler, "advance_processing")["status"] == "SUCCEEDED"

    with pytest.raises(BatchPrerequisiteMissing, match="RECONCILIATION"):
        handle(handler, "advance_publication")

    catalogue.create.assert_not_called()
    assert read_stage("PUBLICATION") is None

    assert handle(handler, "advance_reconciliation")["status"] == "RUNNING"

    reconciliation_call = reconciliation_adapter.submit.call_args.kwargs
    reconciliation_request = reconciliation_call["request"]
    assert reconciliation_request["processing_result"] == (
        read_stage("PROCESSING")["result_reference"]
    )
    assert reconciliation_request["history_result"] == (
        read_stage("HISTORY_BASELINE")["result_reference"]
    )

    with pytest.raises(BatchPrerequisiteMissing, match="RECONCILIATION"):
        handle(handler, "advance_publication")

    catalogue.create.assert_not_called()
    assert read_stage("PUBLICATION") is None

    reconciliation_adapter.lookup.return_value = ProcessingCompletion(
        operation_id=reconciliation_call["operation_id"],
        ingestion_id=ingestion_id,
        request_fingerprint=reconciliation_call["request_fingerprint"],
        result_reference={
            "kind": "reconciliation-result",
            "check_id": "check-one",
        },
    )
    assert handle(handler, "advance_reconciliation")["status"] == "SUCCEEDED"
    reconciliation_adapter.submit.assert_called_once()
    reconciliation_adapter.lookup.assert_called_once_with(
        operation_id=reconciliation_call["operation_id"]
    )

    upstream_before = {
        stage: read_stage(stage)
        for stage in (
            "EXTRACTION",
            "PROCESSING",
            "HISTORY_BASELINE",
            "RECONCILIATION",
        )
    }
    history_versions = s3.list_object_versions(Bucket=bucket)["Versions"]

    with pytest.raises(TimeoutError, match="Catalogue response lost"):
        handle(handler, "advance_publication")

    pending = read_stage("PUBLICATION")
    assert pending["status"] == "RUNNING"
    operation_id = pending["result_reference"]["operation_id"]

    recovered_handler = build_handler()
    assert handle(
        recovered_handler, "advance_publication"
    )["status"] == "SUCCEEDED"
    publication = read_stage("PUBLICATION")
    assert publication["result_reference"]["operation_id"] == operation_id

    history_evidence = upstream_before["HISTORY_BASELINE"]["result_reference"]
    registration = registrations[("example_history", "example_v1")]
    assert registration.operation_id == operation_id
    assert registration.history_commit.version_id == history_evidence["version_id"]
    assert registration.history_commit.sha256 == history_evidence["sha256"]
    assert registration.history_location == history_evidence["history_location"]

    assert handle(recovered_handler, "mark_baseline_ready") == {
        "ingestion_id": ingestion_id,
        "stage": "BASELINE",
        "status": "SUCCEEDED",
    }

    catalogue.create.assert_called_once()
    catalogue.read.assert_called_once_with(
        database="example_history", table="example_v1"
    )
    processing_adapter.submit.assert_called_once()
    processing_adapter.lookup.assert_called_once_with(
        operation_id=submitted["operation_id"]
    )
    assert s3.list_object_versions(Bucket=bucket)["Versions"] == history_versions
    for stage, original in upstream_before.items():
        assert read_stage(stage) == original