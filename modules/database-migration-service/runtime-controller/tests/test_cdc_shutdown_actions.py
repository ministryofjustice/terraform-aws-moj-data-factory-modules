from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from dms_runtime.batch_processing import ProcessingCompletion
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