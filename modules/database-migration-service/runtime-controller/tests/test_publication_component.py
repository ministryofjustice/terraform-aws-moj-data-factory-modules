import hashlib
import json
from dataclasses import asdict, replace
from unittest.mock import Mock

import boto3
import pytest

from dms_runtime.downstream_commit_adapter import (
    DownstreamCommitAdapter,
    DownstreamCommitConflict,
)
from dms_runtime.publication_component import PublicationTarget
from dms_runtime.raw_history_component import RawHistoryTarget
from test_processing_inputs import pinned_inputs as pinned_inputs


def fingerprint(document):
    return hashlib.sha256(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture
def publication_setup(pinned_inputs):
    verifier, request, _, _, _ = pinned_inputs
    s3 = boto3.client("s3", region_name="us-east-1")
    owner = boto3.client("sts").get_caller_identity()["Account"]
    bucket = "publication-history-test"
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )

    history_id = fingerprint(
        {
            "ingestion_id": request["ingestion_id"],
            "stage": "HISTORY_BASELINE",
        }
    )
    history = RawHistoryTarget(
        client=s3,
        verifier=verifier,
        raw_bucket_owner=owner,
        history_bucket=bucket,
        history_bucket_owner=owner,
        history_prefix="history/",
        max_batch_bytes=1024,
    ).commit(
        operation_id=history_id,
        request_fingerprint=fingerprint(request),
        request=request,
    )
    evidence = history.result_reference

    publication_request = {
        "ingestion_id": request["ingestion_id"],
        "deployment": request["deployment"],
        "contract": request["contract"],
        "history_location": evidence["history_location"],
        "history_commit_id": evidence["commit_id"],
        "history_commit": {
            field: evidence[field]
            for field in ("bucket_name", "object_key", "version_id", "sha256")
        },
    }
    operation_id = fingerprint(
        {
            "ingestion_id": request["ingestion_id"],
            "stage": "PUBLICATION",
        }
    )
    arguments = {
        "operation_id": operation_id,
        "request_fingerprint": fingerprint(publication_request),
        "request": publication_request,
    }
    table = boto3.resource("dynamodb", region_name="eu-west-2").create_table(
        TableName="publication-intents",
        KeySchema=[{"AttributeName": "operation_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "operation_id", "AttributeType": "S"}
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    catalogue = Mock()
    catalogue.read.return_value = None

    def build_adapter():
        return DownstreamCommitAdapter(
            table=table,
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
        )

    return build_adapter, catalogue, arguments


def test_publication_recovers_catalogue_write_after_lost_response(
    publication_setup,
):
    build_adapter, catalogue, arguments = publication_setup
    committed = {}

    def create(registration):
        committed[(registration.database, registration.table)] = registration
        raise TimeoutError("Catalogue response lost")

    def read(*, database, table):
        return committed.get((database, table))

    catalogue.create.side_effect = create
    catalogue.read.side_effect = read

    with pytest.raises(TimeoutError, match="Catalogue response lost"):
        build_adapter().submit(**arguments)

    recovered = build_adapter().lookup(operation_id=arguments["operation_id"])

    assert recovered is not None
    assert recovered.result_reference["glue_table"] == "example_v1"
    registration = catalogue.create.call_args.args[0]
    assert asdict(registration.history_commit) == arguments["request"][
        "history_commit"
    ]
    assert recovered.result_reference["history_commit_id"] == (
        registration.history_commit.sha256
    )
    catalogue.create.assert_called_once()
    assert catalogue.read.call_count == 2
    catalogue.read.assert_called_with(
        database="example_history",
        table="example_v1",
    )


def test_publication_rejects_bad_history_checksum_before_catalogue_write(
    publication_setup,
):
    build_adapter, catalogue, arguments = publication_setup
    request = arguments["request"]
    changed = {
        **request,
        "history_commit_id": "0" * 64,
        "history_commit": {**request["history_commit"], "sha256": "0" * 64},
    }

    with pytest.raises(DownstreamCommitConflict, match="checksum differs"):
        build_adapter().submit(
            operation_id=arguments["operation_id"],
            request_fingerprint=fingerprint(changed),
            request=changed,
        )

    catalogue.create.assert_not_called()
    catalogue.read.assert_not_called()


def test_publication_rejects_conflicting_catalogue_registration(
    publication_setup,
):
    build_adapter, catalogue, arguments = publication_setup

    def create(registration):
        catalogue.read.return_value = replace(
            registration, operation_id="another-operation"
        )

    catalogue.create.side_effect = create

    with pytest.raises(DownstreamCommitConflict, match="registration differs"):
        build_adapter().submit(**arguments)

    catalogue.create.assert_called_once()