from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from dms_runtime.batch_processing import BatchReconciliation, ProcessingCompletion
from dms_runtime.cdc_shutdown_actions import (
    CdcConsumerStop,
    CdcHistoryFinalisation,
)
from ingestion_registry.progress import ProgressConflict


INGESTION_ID = "ingestion-one"
TASK_ARN = "arn:aws:dms:eu-west-2:111122223333:task:EXAMPLE"
BOUNDARY_ID = "stop-one"


def evidence(stage, *, boundary_id=BOUNDARY_ID):
    return {
        "kind": "cdc-shutdown-evidence",
        "stage": stage,
        "ingestion_id": INGESTION_ID,
        "task_arn": TASK_ARN,
        "boundary_id": boundary_id,
        "bucket_name": "example-cdc-evidence",
        "object_key": f"shutdown/{stage}.json",
        "version_id": "version-one",
        "sha256": "a" * 64,
    }


@pytest.fixture
def shutdown_setup():
    stages = {
        "SOURCE_STOPPED": {
            "status": "SUCCEEDED",
            "revision": 1,
            "result_reference": evidence("SOURCE_STOPPED"),
        }
    }

    def record_stage(claim, *, stage, status, expected_revision, **fields):
        previous = stages.get(stage)
        assert expected_revision == (previous["revision"] if previous else 0)
        record = {
            "stage": stage,
            "status": status,
            "revision": expected_revision + 1,
            **fields,
        }
        stages[stage] = record
        return record

    progress = Mock()
    progress.read_stage.side_effect = (
        lambda *, ingestion_id, stage: stages.get(stage)
    )
    progress.record_stage.side_effect = record_stage
    claim = SimpleNamespace(ingestion_id=INGESTION_ID)

    def build(stage_type, adapter):
        return stage_type(downstream=Mock(), progress=progress, adapter=adapter)

    return stages, claim, build


def test_stop_waits_for_drain_and_finalisation_waits_for_stop(shutdown_setup):
    stages, claim, build = shutdown_setup
    stop_adapter = Mock()
    finalise_adapter = Mock()
    stop = build(CdcConsumerStop, stop_adapter)
    finalise = build(CdcHistoryFinalisation, finalise_adapter)

    with pytest.raises(BatchPrerequisiteMissing, match="CDC_DRAINED"):
        stop.advance(claim)

    stages["CDC_DRAINED"] = {
        "status": "SUCCEEDED",
        "revision": 1,
        "result_reference": evidence("CDC_DRAINED"),
    }
    with pytest.raises(BatchPrerequisiteMissing, match="CDC_CONSUMER_STOPPED"):
        finalise.advance(claim)

    stop_adapter.submit.assert_not_called()
    finalise_adapter.submit.assert_not_called()


@pytest.mark.parametrize(
    ("stage_type", "prerequisite", "stage"),
    [
        (CdcConsumerStop, "CDC_DRAINED", "CDC_CONSUMER_STOPPED"),
        (CdcHistoryFinalisation, "CDC_CONSUMER_STOPPED", "HISTORY_FINALIZED"),
    ],
)
def test_shutdown_action_recovers_without_resubmitting(
    shutdown_setup, stage_type, prerequisite, stage
):
    stages, claim, build = shutdown_setup
    stages[prerequisite] = {
        "status": "SUCCEEDED",
        "revision": 1,
        "result_reference": evidence(prerequisite),
    }
    adapter = Mock()
    adapter.submit.side_effect = TimeoutError("response lost")

    with pytest.raises(TimeoutError, match="response lost"):
        build(stage_type, adapter).advance(claim)

    assert stages[stage]["status"] == "RUNNING"
    submitted = adapter.submit.call_args.kwargs
    adapter.lookup.return_value = ProcessingCompletion(
        operation_id=submitted["operation_id"],
        ingestion_id=INGESTION_ID,
        request_fingerprint=submitted["request_fingerprint"],
        result_reference=evidence(stage, boundary_id="wrong-stop"),
    )

    restarted = build(stage_type, adapter)
    with pytest.raises(ProgressConflict, match="source-stop boundary"):
        restarted.advance(claim)
    assert stages[stage]["status"] == "RUNNING"

    adapter.lookup.return_value = ProcessingCompletion(
        operation_id=submitted["operation_id"],
        ingestion_id=INGESTION_ID,
        request_fingerprint=submitted["request_fingerprint"],
        result_reference=evidence(stage),
    )
    assert restarted.advance(claim)["status"] == "SUCCEEDED"
    assert restarted.advance(claim) == stages[stage]
    adapter.submit.assert_called_once()
    assert adapter.lookup.call_count == 2


@pytest.mark.parametrize(
    ("stage_type", "prerequisite"),
    [
        (BatchReconciliation, None),
        (CdcConsumerStop, "CDC_DRAINED"),
        (CdcHistoryFinalisation, "CDC_CONSUMER_STOPPED"),
    ],
)
@pytest.mark.parametrize(
    "accepted_before_timeout",
    [False, True],
    ids=["interrupted-before-acceptance", "response-lost-after-acceptance"],
)
def test_reserved_operation_recovers_without_starting_work_twice(
    monkeypatch,
    shutdown_setup,
    stage_type,
    prerequisite,
    accepted_before_timeout,
):
    stages, claim, build = shutdown_setup
    if prerequisite is not None:
        stages[prerequisite] = {
            "status": "SUCCEEDED",
            "revision": 1,
            "result_reference": evidence(prerequisite),
        }

    adapter = Mock()
    adapter.lookup.return_value = None
    accepted = {}
    starts = Mock()

    def submit(*, operation_id, request_fingerprint, request):
        first_call = adapter.submit.call_count == 1
        if first_call and not accepted_before_timeout:
            raise TimeoutError("submission interrupted")

        if operation_id not in accepted:
            starts(operation_id)
            accepted[operation_id] = (request_fingerprint, request)
        assert accepted[operation_id] == (request_fingerprint, request)

        if first_call:
            raise TimeoutError("submission interrupted")
        return None

    adapter.submit.side_effect = submit

    if stage_type is BatchReconciliation:
        # Isolate reservation recovery from the reconciliation prerequisites.
        monkeypatch.setattr(
            BatchReconciliation,
            "_build_request",
            lambda self, ingestion_id: {"ingestion_id": ingestion_id},
        )

    stage = build(stage_type, adapter)
    with pytest.raises(TimeoutError, match="submission interrupted"):
        stage.advance(claim)

    pending = stages[stage.stage]
    assert pending["status"] == "RUNNING"
    reservation = pending["result_reference"]
    operation_id = reservation["operation_id"]
    fingerprint = reservation["request_fingerprint"]
    assert (operation_id in accepted) is accepted_before_timeout

    restarted = build(stage_type, adapter)
    assert restarted.advance(claim) == pending
    assert adapter.submit.call_count == 2
    assert adapter.submit.call_args_list[0] == adapter.submit.call_args_list[1]
    starts.assert_called_once_with(operation_id)

    if stage_type is BatchReconciliation:
        completion_evidence = {"check_id": "check-one"}
    else:
        completion_evidence = evidence(stage.stage)

    adapter.lookup.return_value = ProcessingCompletion(
        operation_id=operation_id,
        ingestion_id=claim.ingestion_id,
        request_fingerprint=fingerprint,
        result_reference=completion_evidence,
    )

    completed = restarted.advance(claim)
    assert completed["status"] == "SUCCEEDED"
    assert completed["result_reference"]["operation_id"] == operation_id
    assert completed["result_reference"]["request_fingerprint"] == fingerprint
    assert adapter.submit.call_count == 2