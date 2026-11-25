import hashlib
import json
from copy import deepcopy
from unittest.mock import Mock

import boto3
import pytest

from dms_runtime.downstream_context import (
    DownstreamContextError,
    build_downstream_context,
)
from dms_runtime.processing_inputs import S3ProcessingInputVerifier
from dms_runtime.raw_history_component import RawHistoryTarget

from test_processing_inputs import pinned_inputs as pinned_inputs


OPERATION = "a" * 64


@pytest.fixture
def context_request(pinned_inputs):
    verifier, request, raw, manifest, put_json = pinned_inputs
    request = deepcopy(request)

    deployment = request["deployment"]
    deployment.update(
        environment="development",
        configuration_revision="a" * 64,
        artifact_revision="b" * 64,
    )

    contract = request["contract"]
    approval = put_json(
        "approvals/example.json",
        {"approved": True},
    )
    contract.update(
        contract_id="example-contract",
        contract_fingerprint=contract["contract_object_sha256"],
        approval_bucket_name=approval.bucket_name,
        approval_object_key=approval.object_key,
        approval_object_version_id=approval.version_id,
        approval_object_sha256=approval.sha256,
    )

    identity = {
        "environment": deployment["environment"],
        "source": deployment["source"],
        "database_name": deployment["database_name"],
        **deployment["table"],
    }

    request["execution_context"] = build_downstream_context(
        ingestion_id=request["ingestion_id"],
        metadata={
            "ingestion_id": request["ingestion_id"],
            "ingestion_version": 2,
            "operation": "REINGESTION",
            "table_identity": identity,
            "configuration_revision": deployment["configuration_revision"],
            "artifact_revision": deployment["artifact_revision"],
            "extraction_generation": deployment["extraction_generation"],
        },
        deployment=deployment,
        contract_stage={
            "ingestion_id": request["ingestion_id"],
            "stage": "CONTRACT",
            "status": "SUCCEEDED",
            "result_reference": contract,
        },
    )

    return verifier, request, raw


def target_for(request, verifier, *, client=None, identity=True):
    s3 = (
        client
        if client is not None
        else boto3.client("s3", region_name="us-east-1")
    )
    owner = boto3.client("sts").get_caller_identity()["Account"]

    return RawHistoryTarget(
        client=s3,
        verifier=verifier,
        raw_bucket_owner=owner,
        history_bucket="versioned-history-test",
        history_bucket_owner=owner,
        history_prefix="history/example-domain/example/",
        max_batch_bytes=1024,
        table_identity=(
            request["execution_context"]["table_identity"]
            if identity
            else None
        ),
    )


def test_shared_verifier_accepts_context_and_exact_raw_version(
    context_request,
):
    verifier, request, raw = context_request

    assert verifier.verify(request).raw_objects == (raw,)


@pytest.mark.parametrize(
    "field",
    [
        "ingestion_id",
        "configuration_revision",
        "extraction_generation",
    ],
)
def test_shared_verifier_rejects_changed_context_before_s3(
    context_request,
    field,
):
    _, request, _ = context_request
    request["execution_context"][field] = "different"

    client = Mock()
    verifier = S3ProcessingInputVerifier(
        client,
        raw_bucket_owner="123456789012",
        contract_bucket_owner="123456789012",
    )

    with pytest.raises(DownstreamContextError):
        verifier.verify(request)

    client.get_object.assert_not_called()


def test_manifest_owner_is_separate_from_raw_owner(context_request):
    verifier, request, _ = context_request
    client = Mock(wraps=verifier._client)

    separate = S3ProcessingInputVerifier(
        client,
        raw_bucket_owner="999988887777",
        manifest_bucket_owner=verifier._raw_bucket_owner,
        contract_bucket_owner=verifier._contract_bucket_owner,
    )

    separate.verify(request)

    assert (
        client.get_object.call_args_list[0].kwargs["ExpectedBucketOwner"]
        == verifier._raw_bucket_owner
    )


def test_history_commits_raw_without_processing_output(context_request):
    verifier, request, raw = context_request
    s3 = boto3.client("s3", region_name="us-east-1")

    s3.create_bucket(Bucket="versioned-history-test")
    s3.put_bucket_versioning(
        Bucket="versioned-history-test",
        VersioningConfiguration={"Status": "Enabled"},
    )

    target = target_for(request, verifier)
    fingerprint = hashlib.sha256(
        json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    completion = target.commit(
        operation_id=OPERATION,
        request_fingerprint=fingerprint,
        request=request,
    )
    evidence = completion.result_reference

    assert evidence["history_location"] == (
        "s3://versioned-history-test/"
        "history/example-domain/example/version=2/"
    )

    with s3.get_object(
        Bucket=evidence["bucket_name"],
        Key=evidence["object_key"],
        VersionId=evidence["version_id"],
    )["Body"] as body:
        output = json.load(body)["output"]

    assert output["ingestion_version"] == 2
    assert output["table_identity"] == (
        request["execution_context"]["table_identity"]
    )
    assert output["objects"][0]["source"]["version_id"] == raw.version_id

    archived = output["objects"][0]["archived"]
    assert archived["object_key"].startswith(
        "history/example-domain/example/version=2/"
    )

    with s3.get_object(
        Bucket=archived["bucket_name"],
        Key=archived["object_key"],
        VersionId=archived["version_id"],
    )["Body"] as body:
        assert body.read() == b"original fixture data"

    before = s3.list_object_versions(
        Bucket="versioned-history-test"
    )["Versions"]

    recovered = target.lookup(
        operation_id=OPERATION,
        request_fingerprint=fingerprint,
        request=request,
    )

    assert recovered == completion
    assert s3.list_object_versions(
        Bucket="versioned-history-test"
    )["Versions"] == before


def test_history_requires_canonical_table_binding(context_request):
    verifier, request, _ = context_request
    client = Mock()
    target = target_for(
        request,
        verifier,
        client=client,
        identity=False,
    )

    with pytest.raises(ValueError, match="canonical table binding"):
        target.commit(
            operation_id=OPERATION,
            request_fingerprint="b" * 64,
            request=request,
        )

    client.put_object.assert_not_called()
    client.get_object.assert_not_called()


def test_history_rejects_target_for_another_table(context_request):
    verifier, request, _ = context_request
    client = Mock()
    target = target_for(request, verifier, client=client)
    target._table_identity["table_name"] = "another"

    with pytest.raises(ValueError, match="different table"):
        target.lookup(
            operation_id=OPERATION,
            request_fingerprint="b" * 64,
            request=request,
        )

    client.get_object.assert_not_called()


@pytest.mark.parametrize("version", [0, True, "2"])
def test_history_rejects_invalid_registry_version(
    context_request,
    version,
):
    verifier, request, _ = context_request
    request["execution_context"]["ingestion_version"] = version

    client = Mock()
    target = target_for(request, verifier, client=client)

    with pytest.raises(DownstreamContextError):
        target.commit(
            operation_id=OPERATION,
            request_fingerprint="b" * 64,
            request=request,
        )

    client.put_object.assert_not_called()


def test_configured_history_cannot_downgrade_to_legacy(context_request):
    verifier, request, _ = context_request
    client = Mock()
    target = target_for(request, verifier, client=client)
    del request["execution_context"]

    with pytest.raises(ValueError, match="requires execution context"):
        target.commit(
            operation_id=OPERATION,
            request_fingerprint="b" * 64,
            request=request,
        )

    client.put_object.assert_not_called()
    client.get_object.assert_not_called()