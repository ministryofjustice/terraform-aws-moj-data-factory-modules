import hashlib
import json

import boto3
import pytest

from dms_runtime.downstream_commit_adapter import DownstreamCommitAdapter
from dms_runtime.raw_history_component import RawHistoryTarget
from test_processing_inputs import pinned_inputs as pinned_inputs
from dataclasses import replace
from unittest.mock import Mock


OPERATION_ID = "a" * 64


def test_history_recovers_original_commit_after_lost_response(pinned_inputs):
    verifier, request, raw, _, _ = pinned_inputs
    s3 = boto3.client("s3", region_name="us-east-1")
    owner = boto3.client("sts").get_caller_identity()["Account"]
    bucket = "raw-history-component-test"
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    table = boto3.resource("dynamodb", region_name="eu-west-2").create_table(
        TableName="raw-history-intents",
        KeySchema=[{"AttributeName": "operation_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "operation_id", "AttributeType": "S"}
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    def build_target():
        return RawHistoryTarget(
            client=s3,
            verifier=verifier,
            raw_bucket_owner=owner,
            history_bucket=bucket,
            history_bucket_owner=owner,
            history_prefix="history/",
            max_batch_bytes=1024,
        )

    target = build_target()

    class LoseCommitResponse:
        def commit(self, **arguments):
            target.commit(**arguments)
            raise TimeoutError("History commit response lost")

        def lookup(self, **arguments):
            return target.lookup(**arguments)

    request_bytes = json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    adapter = DownstreamCommitAdapter(
        table=table, target=LoseCommitResponse(), component="raw_history"
    )

    with pytest.raises(TimeoutError, match="History commit response lost"):
        adapter.submit(
            operation_id=OPERATION_ID,
            request_fingerprint=hashlib.sha256(request_bytes).hexdigest(),
            request=request,
        )

    versions_before = s3.list_object_versions(Bucket=bucket)["Versions"]
    assert len(versions_before) == 2  # Archived object and commit document

    recovered = DownstreamCommitAdapter(
        table=table, target=build_target(), component="raw_history"
    ).lookup(operation_id=OPERATION_ID)

    assert recovered is not None
    evidence = recovered.result_reference
    with s3.get_object(
        Bucket=bucket,
        Key=evidence["object_key"],
        VersionId=evidence["version_id"],
    )["Body"] as body:
        commit = json.loads(body.read())

    archived = commit["output"]["objects"]
    assert len(archived) == 1
    assert archived[0]["source"]["version_id"] == raw.version_id
    destination = archived[0]["archived"]

    with s3.get_object(
        Bucket=bucket,
        Key=destination["object_key"],
        VersionId=destination["version_id"],
    )["Body"] as body:
        assert body.read() == b"original fixture data"

    assert s3.list_object_versions(Bucket=bucket)["Versions"] == versions_before

def test_oversized_history_commit_is_rejected_before_archive_writes(
    pinned_inputs,
):
    verifier, request, raw, _, _ = pinned_inputs
    verified = verifier.verify(request)
    landing_prefix = request["deployment"]["landing"]["prefix"]
    raw_objects = tuple(
        replace(raw, object_key=f"{landing_prefix}part-{index:03d}.parquet")
        for index in range(50)
    )

    pinned_verifier = Mock(spec=type(verifier))
    pinned_verifier.verify.return_value = replace(
        verified, raw_objects=raw_objects
    )
    s3 = Mock()
    target = RawHistoryTarget(
        client=s3,
        verifier=pinned_verifier,
        raw_bucket_owner="123456789012",
        history_bucket="raw-history-component-test",
        history_bucket_owner="123456789012",
        history_prefix="history/",
        max_batch_bytes=1024,
    )

    with pytest.raises(ValueError, match="Processing evidence exceeds 256 KiB"):
        target.commit(
            operation_id=OPERATION_ID,
            request_fingerprint="b" * 64,
            request=request,
        )

    s3.put_object.assert_not_called()
    s3.get_object.assert_not_called()