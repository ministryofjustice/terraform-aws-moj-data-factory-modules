import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import boto3
import pytest

from dms_runtime.full_load_raw_batch import (
    DmsParquetFullLoadSelector,
    FullLoadRawBatchHandoff,
    RawBatchUnavailable,
)
from dms_runtime.raw_batch_manifest import RawBatchManifestConflict
from test_processing_inputs import pinned_inputs as pinned_inputs
from test_raw_batch_manifest import manifest_setup as manifest_setup


@pytest.fixture
def handoff_setup(manifest_setup):
    producer, _, claim, _, request, raw, s3 = manifest_setup
    binding = request["deployment"]
    prefix = binding["landing"]["prefix"] + "public/example/"
    key = prefix + "LOAD0000A.parquet"

    written = s3.put_object(
        Bucket=raw.bucket_name,
        Key=key,
        Body=b"full-load",
    )
    s3.put_object(
        Bucket=raw.bucket_name,
        Key=prefix + "20261124-100000000.parquet",
        Body=b"cdc",
    )

    boundary = datetime.now(timezone.utc) - timedelta(minutes=1)
    history = Mock()
    history.full_load_start.return_value = binding, boundary

    readiness = Mock()
    readiness.assess.return_value = {
        "full_load_status": "SUCCEEDED",
        "full_load_ready": True,
        "task_requires_attention": False,
    }

    owner = boto3.client("sts").get_caller_identity()["Account"]
    selector = DmsParquetFullLoadSelector(
        s3, bucket_owner=owner
    )
    handoff = FullLoadRawBatchHandoff(
        history=history,
        readiness=readiness,
        selector=selector,
        producer=producer,
    )

    return (
        handoff, claim, request, s3, key,
        written["VersionId"], selector, boundary,
    )


def read_manifest(s3, reference):
    response = s3.get_object(
        Bucket=reference.bucket_name,
        Key=reference.object_key,
        VersionId=reference.version_id,
    )
    body = response["Body"]
    try:
        return json.loads(body.read())
    finally:
        body.close()


def test_handoff_pins_full_load_and_ignores_cdc(handoff_setup):
    handoff, claim, _, s3, key, version, _, _ = handoff_setup

    reference = handoff.advance(
        claim, start_operation_id="original-start"
    )
    document = read_manifest(s3, reference)

    assert len(document["objects"]) == 1
    assert document["objects"][0]["object_key"] == key
    assert document["objects"][0]["version_id"] == version
    handoff.history.full_load_start.assert_called_once_with(
        ingestion_id=claim.ingestion_id,
        operation_id="original-start",
    )
    assert handoff.readiness.assess.call_count == 2


def test_committed_retry_does_not_relist_or_reselect(handoff_setup):
    handoff, claim, _, s3, key, _, _, _ = handoff_setup
    original = handoff.advance(
        claim, start_operation_id="original-start"
    )

    s3.put_object(
        Bucket=original.bucket_name,
        Key=key,
        Body=b"later overwrite",
    )
    handoff.selector = Mock()

    assert handoff.advance(
        claim, start_operation_id="original-start"
    ) == original
    handoff.selector.select.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"full_load_ready": False},
        {"full_load_status": "WAITING"},
        {"task_requires_attention": True},
    ],
)
def test_handoff_blocks_unready_input(handoff_setup, change):
    handoff, claim, *_ = handoff_setup
    handoff.readiness.assess.return_value.update(change)
    handoff.selector = Mock()

    with pytest.raises(RawBatchUnavailable):
        handoff.advance(
            claim, start_operation_id="original-start"
        )

    handoff.selector.select.assert_not_called()


def test_readiness_lost_after_selection_does_not_commit(handoff_setup):
    handoff, claim, *_ = handoff_setup
    good = dict(handoff.readiness.assess.return_value)
    handoff.readiness.assess.side_effect = [
        good,
        {**good, "full_load_ready": False},
    ]
    handoff.producer = Mock()
    handoff.producer.completed.return_value = None

    with pytest.raises(RawBatchUnavailable):
        handoff.advance(
            claim, start_operation_id="original-start"
        )

    handoff.producer.commit.assert_not_called()


def test_selector_rejects_objects_from_before_start(handoff_setup):
    _, _, request, _, _, _, selector, _ = handoff_setup

    with pytest.raises(RawBatchUnavailable, match="predates"):
        selector.select(
            request["deployment"],
            not_before=datetime.now(timezone.utc)
            + timedelta(minutes=1),
        )


def test_selector_blocks_empty_batch(handoff_setup):
    _, _, request, s3, key, _, selector, boundary = handoff_setup
    s3.delete_object(
        Bucket=request["deployment"]["landing"]["bucket_name"],
        Key=key,
    )

    with pytest.raises(
        RawBatchUnavailable, match="empty-table evidence"
    ):
        selector.select(
            request["deployment"], not_before=boundary
        )


def test_selector_enforces_byte_budget(handoff_setup):
    _, _, request, _, _, _, selector, boundary = handoff_setup
    selector.max_bytes = 1

    with pytest.raises(RawBatchUnavailable, match="byte budget"):
        selector.select(
            request["deployment"], not_before=boundary
        )


def test_selector_blocks_nested_layout(handoff_setup):
    _, _, request, s3, key, _, selector, boundary = handoff_setup
    s3.put_object(
        Bucket=request["deployment"]["landing"]["bucket_name"],
        Key=key.replace("LOAD0000A", "part/LOAD00001"),
        Body=b"nested",
    )

    with pytest.raises(
        RawBatchUnavailable,
        match="Unsupported DMS object layout",
    ):
        selector.select(
            request["deployment"], not_before=boundary
        )


def test_committed_manifest_rejects_changed_generation(handoff_setup):
    handoff, claim, request, *_ = handoff_setup
    handoff.advance(claim, start_operation_id="original-start")
    request["deployment"]["extraction_generation"] = "another-generation"

    with pytest.raises(RawBatchManifestConflict):
        handoff.advance(
            claim, start_operation_id="original-start"
        )


def test_retry_after_selection_does_not_relist_raw(
    handoff_setup, monkeypatch
):
    handoff, claim, request, s3, key, original_version, _, _ = handoff_setup
    original_commit = handoff.producer.commit

    monkeypatch.setattr(
        handoff.producer,
        "commit",
        Mock(side_effect=TimeoutError("before stage commit")),
    )
    with pytest.raises(TimeoutError):
        handoff.advance(
            claim, start_operation_id="original-start"
        )

    s3.put_object(
        Bucket=request["deployment"]["landing"]["bucket_name"],
        Key=key,
        Body=b"later overwrite",
    )
    monkeypatch.setattr(
        handoff.producer, "commit", original_commit
    )
    handoff.selector = Mock()

    reference = handoff.advance(
        claim, start_operation_id="original-start"
    )
    handoff.selector.select.assert_not_called()
    document = read_manifest(s3, reference)
    assert document["objects"][0]["version_id"] == original_version


def test_selector_requires_versioned_raw_objects(
    handoff_setup, monkeypatch
):
    _, _, request, s3, _, _, selector, boundary = handoff_setup
    monkeypatch.setattr(
        s3,
        "head_object",
        Mock(return_value={"ContentLength": 9}),
    )

    with pytest.raises(
        RawBatchUnavailable, match="must be versioned"
    ):
        selector.select(
            request["deployment"], not_before=boundary
        )


def test_selector_rejects_pagination_cycle(
    handoff_setup, monkeypatch
):
    _, _, request, s3, _, _, selector, boundary = handoff_setup
    monkeypatch.setattr(
        s3,
        "list_objects_v2",
        Mock(return_value={
            "Contents": [],
            "IsTruncated": True,
            "NextContinuationToken": "repeat",
        }),
    )

    with pytest.raises(
        RawBatchUnavailable, match="pagination token"
    ):
        selector.select(
            request["deployment"], not_before=boundary
        )