import json

import pytest

from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from ingestion_registry.ownership import OwnershipLost
from test_batch_workflow import (
    INGESTION_ID,
    OWNER,
    WORKFLOW_PATH,
    unique_object,
    walk_states,
)


RENEW = "renew_ingestion_lease"
DRAIN = "advance_cdc_drain"
STOP = "advance_cdc_consumer_stop"
FINALISE = "advance_cdc_history_finalisation"

STAGES = {
    DRAIN: "CDC_DRAINED",
    STOP: "CDC_CONSUMER_STOPPED",
    FINALISE: "HISTORY_FINALIZED",
}


@pytest.fixture
def shutdown_workflow():
    path = WORKFLOW_PATH.with_name("cdc-shutdown.asl.json")
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
    )


def run_shutdown(workflow, script, calls):
    steps = iter(script)

    def invoke(event):
        action = event["action"]
        calls.append(action)
        expected_action, outcome = next(steps)
        assert action == expected_action
        assert event == {
            "request_version": 1,
            "action": action,
            "ingestion_id": INGESTION_ID,
            "ingestion_owner": OWNER,
        }

        if isinstance(outcome, Exception):
            raise outcome
        if action == RENEW:
            return {
                "ingestion_id": INGESTION_ID,
                "lease_expires_at": outcome,
            }
        return {
            "ingestion_id": INGESTION_ID,
            "stage": STAGES[action],
            "status": outcome,
        }

    execution = walk_states(
        workflow,
        {"ingestion_id": INGESTION_ID, "ingestion_owner": OWNER},
        invoke,
    )
    while True:
        try:
            next(execution)
        except StopIteration as finished:
            assert next(steps, None) is None
            return finished.value


def test_shutdown_waits_for_drain_then_stop_before_finalising(shutdown_workflow):
    script = [
        (RENEW, 1300),
        (DRAIN, "RUNNING"),
        (RENEW, 1315),
        (DRAIN, "SUCCEEDED"),
        (STOP, "RUNNING"),
        (RENEW, 1330),
        (DRAIN, "SUCCEEDED"),
        (STOP, "SUCCEEDED"),
        (FINALISE, "SUCCEEDED"),
    ]
    calls = []

    result = run_shutdown(shutdown_workflow, script, calls)

    assert calls == [action for action, _ in script]
    assert result["history_finalisation"]["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    ("script", "error", "message"),
    [
        (
            [
                (RENEW, 1300),
                (DRAIN, BatchPrerequisiteMissing("SOURCE_STOPPED is missing")),
            ],
            BatchPrerequisiteMissing,
            "SOURCE_STOPPED",
        ),
        (
            [(RENEW, 1300), (DRAIN, "FAILED")],
            RuntimeError,
            "CdcDrainFailed",
        ),
        (
            [(RENEW, 1300), (DRAIN, "SUCCEEDED"), (STOP, "FAILED")],
            RuntimeError,
            "CdcConsumerStopFailed",
        ),
        (
            [
                (RENEW, 1300),
                (DRAIN, "RUNNING"),
                (RENEW, OwnershipLost("lease lost")),
            ],
            OwnershipLost,
            "lease lost",
        ),
    ],
)
def test_shutdown_blocks_finalisation_when_a_prerequisite_fails(
    shutdown_workflow, script, error, message
):
    calls = []

    with pytest.raises(error, match=message):
        run_shutdown(shutdown_workflow, script, calls)

    assert calls == [action for action, _ in script]
    assert FINALISE not in calls