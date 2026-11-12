from dataclasses import replace
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from dms_runtime.batch_processing import ProcessingCompletion
from dms_runtime.downstream_commit_adapter import (
    DownstreamCommitAdapter,
    DownstreamCommitConflict,
)


OPERATION_ID = "a" * 64
FINGERPRINT = "b" * 64
REQUEST = {"ingestion_id": "ingestion-one"}


@pytest.fixture
def table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
        yield dynamodb.create_table(
            TableName="downstream-intents",
            KeySchema=[{"AttributeName": "operation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "operation_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )


@pytest.mark.parametrize(
    ("component", "evidence"),
    [
        (
            "raw_history",
            {
                "commit_id": "history-commit-one",
                "history_location": "s3://example-history/ingestion-one/",
            },
        ),
        (
            "publication",
            {"glue_database": "example_history", "glue_table": "example_v1"},
        ),
    ],
)
def test_recovers_a_committed_result_after_response_is_lost(
    table, component, evidence
):
    committed = {}
    target = Mock()

    def commit(*, operation_id, request_fingerprint, request):
        assert table.get_item(
            Key={"operation_id": operation_id}, ConsistentRead=True
        )["Item"]["request_fingerprint"] == request_fingerprint
        committed[operation_id] = ProcessingCompletion(
            operation_id=operation_id,
            ingestion_id=request["ingestion_id"],
            request_fingerprint=request_fingerprint,
            result_reference=evidence,
        )
        raise TimeoutError("response lost after commit")

    def lookup(*, operation_id, request_fingerprint, request):
        assert request_fingerprint == FINGERPRINT
        assert request == REQUEST
        return committed.get(operation_id)

    target.commit.side_effect = commit
    target.lookup.side_effect = lookup
    adapter = DownstreamCommitAdapter(
        table=table, target=target, component=component
    )
    arguments = {
        "operation_id": OPERATION_ID,
        "request_fingerprint": FINGERPRINT,
        "request": REQUEST,
    }

    with pytest.raises(TimeoutError, match="response lost"):
        adapter.submit(**arguments)

    recovered = DownstreamCommitAdapter(
        table=table, target=target, component=component
    )
    assert recovered.lookup(operation_id=OPERATION_ID) == committed[OPERATION_ID]
    assert recovered.submit(**arguments) == committed[OPERATION_ID]
    target.commit.assert_called_once()


def test_missing_evidence_remains_pending_and_changed_inputs_are_rejected(table):
    target = Mock()
    target.commit.side_effect = TimeoutError("outcome unknown")
    target.lookup.return_value = None
    adapter = DownstreamCommitAdapter(
        table=table, target=target, component="publication"
    )

    with pytest.raises(TimeoutError, match="outcome unknown"):
        adapter.submit(
            operation_id=OPERATION_ID,
            request_fingerprint=FINGERPRINT,
            request=REQUEST,
        )

    assert adapter.lookup(operation_id=OPERATION_ID) is None

    with pytest.raises(DownstreamCommitConflict, match="different inputs"):
        adapter.submit(
            operation_id=OPERATION_ID,
            request_fingerprint="c" * 64,
            request={**REQUEST, "history_location": "s3://other/history/"},
        )

    target.commit.assert_called_once()


def test_lookup_rejects_evidence_for_a_different_ingestion(table):
    target = Mock()
    target.commit.return_value = None
    adapter = DownstreamCommitAdapter(
        table=table, target=target, component="publication"
    )

    assert adapter.submit(
        operation_id=OPERATION_ID,
        request_fingerprint=FINGERPRINT,
        request=REQUEST,
    ) is None

    valid = ProcessingCompletion(
        operation_id=OPERATION_ID,
        ingestion_id=REQUEST["ingestion_id"],
        request_fingerprint=FINGERPRINT,
        result_reference={
            "glue_database": "example_history",
            "glue_table": "example_v1",
        },
    )
    target.lookup.return_value = replace(valid, ingestion_id="another-ingestion")

    with pytest.raises(DownstreamCommitConflict, match="does not match operation"):
        adapter.lookup(operation_id=OPERATION_ID)