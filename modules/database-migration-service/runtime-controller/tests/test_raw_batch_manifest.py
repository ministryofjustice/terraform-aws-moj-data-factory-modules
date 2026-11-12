from dataclasses import asdict
from unittest.mock import patch

import boto3
import pytest

from dms_runtime.raw_batch_manifest import (
    RawBatchManifestConflict,
    RawBatchManifestProducer,
)
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import IngestionOwnership
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.requests import NewIngestionRequest
from test_processing_inputs import pinned_inputs as pinned_inputs


@pytest.fixture
def manifest_setup(pinned_inputs):
    verifier, request, raw, _, _ = pinned_inputs
    deployment = request["deployment"]
    table = TableIdentity(
        environment="development",
        source=deployment["source"],
        database_name=deployment["database_name"],
        **deployment["table"],
    )

    dynamodb = boto3.client("dynamodb", region_name="eu-west-2")
    dynamodb.create_table(
        TableName="raw-manifest-registry",
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
    registry = IngestionRegistry(dynamodb, table_name="raw-manifest-registry")
    admitted = registry.admit(
        NewIngestionRequest(
            request_id="raw-manifest-test",
            operation="INITIAL_INGESTION",
            table=table,
            configuration_revision="a" * 64,
            artifact_revision="b" * 64,
            extraction_generation=deployment["extraction_generation"],
        )
    )

    def now():
        return 1000

    claim = IngestionOwnership(registry, clock=now).claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="extraction-worker",
    )
    progress = IngestionProgress(registry, clock=now)

    s3 = boto3.client("s3", region_name="us-east-1")
    owner = boto3.client("sts").get_caller_identity()["Account"]
    producer = RawBatchManifestProducer(
        client=s3,
        progress=progress,
        bucket_name=raw.bucket_name,
        prefix="completed-batches/",
        expected_bucket_owner=owner,
    )
    request = {**request, "ingestion_id": claim.ingestion_id}

    return producer, progress, claim, verifier, request, raw, s3


@pytest.mark.parametrize("empty", [False, True], ids=["pinned-object", "empty"])
def test_manifest_producer_hands_exact_batch_to_verifier(manifest_setup, empty):
    producer, progress, claim, verifier, request, raw, s3 = manifest_setup
    objects = [] if empty else [raw]
    deployment = request["deployment"]

    reference = producer.commit(claim, deployment=deployment, objects=objects)
    assert producer.commit(
        claim, deployment=deployment, objects=objects
    ) == reference

    stage = progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="EXTRACTION",
    )
    assert stage["status"] == "SUCCEEDED"
    assert stage["result_reference"] == {"kind": "raw-batch", **asdict(reference)}

    recorded = dict(stage["result_reference"])
    recorded.pop("kind")
    verified = verifier.verify({**request, "raw_batch": recorded})
    assert verified.raw_manifest == reference
    assert verified.raw_objects == tuple(objects)

    if not empty:
        latest = s3.head_object(Bucket=raw.bucket_name, Key=raw.object_key)
        assert latest["VersionId"] != verified.raw_objects[0].version_id


def test_manifest_retry_recovers_s3_write_after_registry_interruption(
    manifest_setup,
):
    producer, progress, claim, _, request, raw, s3 = manifest_setup
    deployment = request["deployment"]

    with patch.object(
        progress,
        "record_stage",
        side_effect=TimeoutError("registry write interrupted"),
    ):
        with pytest.raises(TimeoutError, match="registry write interrupted"):
            producer.commit(claim, deployment=deployment, objects=[raw])

    assert progress.read_stage(
        ingestion_id=claim.ingestion_id, stage="EXTRACTION"
    ) is None
    before = s3.list_object_versions(
        Bucket=raw.bucket_name, Prefix="completed-batches/"
    )["Versions"]
    assert len(before) == 1

    recovered = producer.commit(claim, deployment=deployment, objects=[raw])

    assert recovered.object_key == before[0]["Key"]
    assert recovered.version_id == before[0]["VersionId"]
    assert s3.list_object_versions(
        Bucket=raw.bucket_name, Prefix="completed-batches/"
    )["Versions"] == before


def test_completed_extraction_rejects_changed_batch(manifest_setup):
    producer, progress, claim, _, request, raw, _ = manifest_setup
    deployment = request["deployment"]

    original = producer.commit(claim, deployment=deployment, objects=[raw])

    with pytest.raises(RawBatchManifestConflict, match="completed extraction"):
        producer.commit(claim, deployment=deployment, objects=[])

    stage = progress.read_stage(
        ingestion_id=claim.ingestion_id, stage="EXTRACTION"
    )
    assert stage["result_reference"] == {"kind": "raw-batch", **asdict(original)}