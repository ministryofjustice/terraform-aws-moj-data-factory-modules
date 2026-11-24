from decimal import Decimal
from unittest.mock import Mock

import pytest

from dms_runtime.downstream_context import (
    DownstreamContextError,
    build_downstream_context,
    read_downstream_context,
)


@pytest.fixture
def inputs():
    identity = {
        "environment": "development",
        "source": "example-source",
        "database_name": "example_db",
        "schema_name": "public",
        "table_name": "records",
    }
    metadata = {
        "ingestion_id": "ingestion-one",
        "ingestion_version": Decimal(2),
        "operation": "REINGESTION",
        "table_identity": identity,
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "extraction_generation": "generation-two",
    }
    deployment = {
        **identity,
        "table": identity.copy(),
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "extraction_generation": "generation-two",
    }
    contract = {
        "kind": "registered-contract",
        "contract_id": "contract-one",
        "contract_version": "v3",
        "contract_fingerprint": "c" * 64,
    }

    for prefix in ("contract", "approval"):
        contract.update({
            f"{prefix}_bucket_name": "example-evidence",
            f"{prefix}_object_key": f"{prefix}/result.json",
            f"{prefix}_object_version_id": "version-one",
            f"{prefix}_object_sha256": "d" * 64,
        })

    stage = {
        "ingestion_id": "ingestion-one",
        "stage": "CONTRACT",
        "status": "SUCCEEDED",
        "result_reference": contract,
    }

    return {
        "ingestion_id": "ingestion-one",
        "metadata": metadata,
        "deployment": deployment,
        "contract_stage": stage,
    }


def test_context_preserves_version_and_exact_references(inputs):
    result = build_downstream_context(**inputs)

    assert type(result["ingestion_version"]) is int
    assert result["ingestion_version"] == 2
    assert (
        result["contract"]
        == inputs["contract_stage"]["result_reference"]
    )

    result["contract"]["contract_version"] = "v99"

    assert (
        inputs["contract_stage"]["result_reference"][
            "contract_version"
        ]
        == "v3"
    )


@pytest.mark.parametrize(
    "version",
    [True, 0, -1, "2", Decimal("1.5")],
)
def test_invalid_versions_are_rejected(inputs, version):
    inputs["metadata"]["ingestion_version"] = version

    with pytest.raises(DownstreamContextError):
        build_downstream_context(**inputs)


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "database_name",
        "environment",
        "configuration_revision",
        "artifact_revision",
        "extraction_generation",
    ],
)
def test_mismatched_pinned_deployment_is_rejected(inputs, field):
    inputs["deployment"][field] = "different"

    with pytest.raises(DownstreamContextError):
        build_downstream_context(**inputs)


@pytest.mark.parametrize(
    "change",
    ["pending", "other-ingestion", "missing-version"],
)
def test_invalid_contract_stage_is_rejected(inputs, change):
    stage = inputs["contract_stage"]

    if change == "pending":
        stage["status"] = "RUNNING"
    elif change == "other-ingestion":
        stage["ingestion_id"] = "other"
    else:
        del stage["result_reference"][
            "contract_object_version_id"
        ]

    with pytest.raises(DownstreamContextError):
        build_downstream_context(**inputs)


def test_attempt_recovery_does_not_change_stable_context(inputs):
    before = build_downstream_context(**inputs)

    inputs["metadata"].update(
        active_execution_id="recovery",
        attempt_count=2,
    )

    assert build_downstream_context(**inputs) == before


def test_reader_verifies_deployment_before_reading_context(inputs):
    registry = Mock()
    lookup = Mock()
    lookup.resolve.return_value = inputs["deployment"]
    registry._read.side_effect = [
        inputs["metadata"],
        inputs["contract_stage"],
    ]

    result = read_downstream_context(
        registry=registry,
        lookup=lookup,
        ingestion_id="ingestion-one",
    )

    assert result == build_downstream_context(**inputs)
    lookup.resolve.assert_called_once_with("ingestion-one")


def test_lookup_failure_prevents_context_reads():
    registry = Mock()
    lookup = Mock()
    lookup.resolve.side_effect = ValueError("binding mismatch")

    with pytest.raises(ValueError, match="binding mismatch"):
        read_downstream_context(
            registry=registry,
            lookup=lookup,
            ingestion_id="one",
        )

    registry._read.assert_not_called()

@pytest.mark.parametrize(
    "stage_name",
    [
        "BatchProcessing",
        "BatchRawHistory",
        "BatchPublication",
        "BatchReconciliation",
    ],
)
def test_all_stages_include_context_in_fingerprint(inputs, stage_name):
    import hashlib
    import json

    from dms_runtime import batch_processing as stages
    from dms_runtime.batch_downstream import BatchDownstream
    from ingestion_registry.ownership import OwnershipClaim

    context = build_downstream_context(**inputs)
    downstream = BatchDownstream(
        resolve_deployment=Mock(),
        read_stage=Mock(),
        context_reader=lambda _: context,
    )
    progress = Mock()
    progress.read_stage.return_value = None
    progress.record_stage.side_effect = lambda *args, **kwargs: {
        "status": kwargs["status"],
        "revision": 1,
        "result_reference": kwargs["result_reference"],
    }

    adapter = Mock()
    adapter.submit.return_value = None

    options = {
        "downstream": downstream,
        "progress": progress,
        "adapter": adapter,
    }
    if stage_name == "BatchPublication":
        options["reconciliation_required"] = True

    stage = getattr(stages, stage_name)(**options)
    stage._build_request = lambda _: {
        "ingestion_id": inputs["ingestion_id"],
        "deployment": inputs["deployment"],
        "contract": inputs["contract_stage"]["result_reference"],
    }

    claim = OwnershipClaim(
        "ingestion-one", "execution-one", 1, 1, 9999, False
    )
    result = stage.advance(claim)
    submitted = adapter.submit.call_args.kwargs

    assert submitted["request"]["execution_context"] == context
    assert result["result_reference"]["context_version"] == "1"

    expected_fingerprint = hashlib.sha256(
        json.dumps(
            submitted["request"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()

    assert submitted["request_fingerprint"] == expected_fingerprint


def test_legacy_running_operation_keeps_original_request(inputs):
    from dms_runtime.batch_downstream import BatchDownstream
    from dms_runtime.batch_processing import (
        BatchProcessing,
        _fingerprint,
    )
    from ingestion_registry.ownership import OwnershipClaim

    reader = Mock(
        side_effect=AssertionError(
            "Legacy operation must not read context"
        )
    )
    downstream = BatchDownstream(
        resolve_deployment=Mock(),
        read_stage=Mock(),
        context_reader=reader,
    )

    request = {"ingestion_id": "ingestion-one"}
    reservation = {
        "kind": "processing-operation",
        "operation_id": _fingerprint({
            "ingestion_id": "ingestion-one",
            "stage": "PROCESSING",
        }),
        "request_fingerprint": _fingerprint(request),
    }

    progress = Mock()
    progress.read_stage.return_value = {
        "status": "RUNNING",
        "revision": 1,
        "result_reference": reservation,
    }
    adapter = Mock()
    adapter.lookup.return_value = None
    adapter.submit.return_value = None

    stage = BatchProcessing(
        downstream=downstream,
        progress=progress,
        adapter=adapter,
    )
    stage._build_request = lambda _: request.copy()

    stage.advance(
        OwnershipClaim(
            "ingestion-one", "recovery", 2, 2, 9999, True
        )
    )

    assert adapter.submit.call_args.kwargs["request"] == request
    reader.assert_not_called()


def test_context_cannot_disagree_with_component_contract(inputs):
    from dms_runtime.downstream_context import (
        validate_request_context,
    )

    request = {
        "ingestion_id": "ingestion-one",
        "deployment": inputs["deployment"],
        "contract": inputs["contract_stage"][
            "result_reference"
        ].copy(),
        "execution_context": build_downstream_context(**inputs),
    }
    request["contract"]["contract_version"] = "v4"

    with pytest.raises(DownstreamContextError):
        validate_request_context(request)


def test_processing_accepts_and_persists_valid_context(inputs):
    import hashlib

    from dms_runtime.processing_component import (
        ProcessingComponent,
        _encode_request,
    )

    table = Mock()
    request = {
        "ingestion_id": "ingestion-one",
        "deployment": inputs["deployment"],
        "contract": inputs["contract_stage"]["result_reference"],
        "raw_batch": {
            "bucket_name": "example-evidence",
            "object_key": "batch.json",
            "version_id": "version-one",
            "sha256": "e" * 64,
        },
        "execution_context": build_downstream_context(**inputs),
    }

    operation_id = hashlib.sha256(
        _encode_request({
            "ingestion_id": "ingestion-one",
            "stage": "PROCESSING",
        })
    ).hexdigest()
    fingerprint = hashlib.sha256(
        _encode_request(request)
    ).hexdigest()

    ProcessingComponent(table).handle({
        "request_version": 1,
        "action": "submit_processing",
        "operation_id": operation_id,
        "request_fingerprint": fingerprint,
        "request": request,
    })

    stored = table.put_item.call_args.kwargs["Item"]
    assert '"execution_context"' in stored["request_json"]


def _running_context_stage(inputs):
    from copy import deepcopy

    from dms_runtime.batch_downstream import BatchDownstream
    from dms_runtime.batch_processing import BatchProcessing

    records = {}
    progress = Mock()
    progress.read_stage.side_effect = (
        lambda **kwargs: deepcopy(records.get("current"))
    )

    def record(claim, **kwargs):
        current = records.get("current")
        revision = current["revision"] if current else 0
        assert kwargs["expected_revision"] == revision

        records["current"] = {
            "status": kwargs["status"],
            "revision": revision + 1,
            "result_reference": deepcopy(
                kwargs["result_reference"]
            ),
        }
        return deepcopy(records["current"])

    progress.record_stage.side_effect = record

    downstream = BatchDownstream(
        resolve_deployment=Mock(),
        read_stage=Mock(),
        context_reader=lambda _: build_downstream_context(**inputs),
    )
    adapter = Mock()
    stage = BatchProcessing(
        downstream=downstream,
        progress=progress,
        adapter=adapter,
    )
    stage._build_request = lambda _: {
        "ingestion_id": inputs["ingestion_id"],
        "deployment": deepcopy(inputs["deployment"]),
        "contract": deepcopy(
            inputs["contract_stage"]["result_reference"]
        ),
    }
    return stage, adapter, records, downstream


def test_context_operation_recovers_lost_response_without_resubmission(
    inputs,
):
    from types import SimpleNamespace

    from dms_runtime.batch_processing import ProcessingCompletion

    stage, adapter, records, _ = _running_context_stage(inputs)
    completed = {}

    def submit(**kwargs):
        completed["result"] = ProcessingCompletion(
            operation_id=kwargs["operation_id"],
            ingestion_id="ingestion-one",
            request_fingerprint=kwargs["request_fingerprint"],
            result_reference={
                "kind": "processing-result",
                "result_id": "durable-one",
            },
        )
        raise TimeoutError(
            "Response lost after component completion"
        )

    adapter.submit.side_effect = submit
    adapter.lookup.side_effect = (
        lambda **kwargs: completed["result"]
    )

    with pytest.raises(TimeoutError):
        stage.advance(
            SimpleNamespace(ingestion_id="ingestion-one")
        )

    assert (
        records["current"]["result_reference"]["context_version"]
        == "1"
    )

    inputs["metadata"].update(
        active_execution_id="recovery",
        attempt_count=2,
    )
    result = stage.advance(
        SimpleNamespace(ingestion_id="ingestion-one")
    )

    assert result["status"] == "SUCCEEDED"
    assert result["result_reference"]["result_id"] == "durable-one"
    assert adapter.submit.call_count == 1
    assert adapter.lookup.call_count == 1


def test_changed_context_cannot_reuse_a_reserved_operation(inputs):
    from types import SimpleNamespace

    from ingestion_registry.progress import ProgressConflict

    stage, adapter, _, _ = _running_context_stage(inputs)
    adapter.submit.return_value = None
    claim = SimpleNamespace(ingestion_id="ingestion-one")

    stage.advance(claim)
    inputs["metadata"]["ingestion_version"] = 3

    with pytest.raises(
        ProgressConflict,
        match="does not match the pinned request",
    ):
        stage.advance(claim)

    assert adapter.submit.call_count == 1
    adapter.lookup.assert_not_called()


def test_context_operation_cannot_downgrade_to_legacy(inputs):
    from types import SimpleNamespace

    from ingestion_registry.progress import ProgressConflict

    stage, adapter, _, downstream = _running_context_stage(inputs)
    adapter.submit.return_value = None
    claim = SimpleNamespace(ingestion_id="ingestion-one")

    stage.advance(claim)
    downstream._context_reader = None

    with pytest.raises(
        ProgressConflict,
        match="requires its context reader",
    ):
        stage.advance(claim)

    assert adapter.submit.call_count == 1
    adapter.lookup.assert_not_called()
