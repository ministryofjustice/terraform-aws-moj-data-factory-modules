from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from dms_runtime.cdc_drain import CdcDrain
from ingestion_registry.cdc_shutdown import CdcShutdownEvidence
from ingestion_registry.progress import ProgressConflict
from ingestion_registry.references import S3ObjectReference


INGESTION_ID = "ingestion-one"
TASK_ARN = "arn:aws:dms:eu-west-2:111122223333:task:EXAMPLE"
BOUNDARY_ID = "source-stop-one"


@pytest.fixture
def drain_setup():
    stages = {
        "SOURCE_STOPPED": {
            "status": "SUCCEEDED",
            "result_reference": {
                "task_arn": TASK_ARN,
                "boundary_id": BOUNDARY_ID,
            },
        },
        "CDC_CONSUMER": {"status": "SUCCEEDED"},
    }
    progress = Mock()
    progress.read_stage.side_effect = (
        lambda *, ingestion_id, stage: stages.get(stage)
    )
    probe = Mock()
    probe.read_drain_evidence.return_value = None
    claim = SimpleNamespace(ingestion_id=INGESTION_ID)
    evidence = CdcShutdownEvidence(
        ingestion_id=INGESTION_ID,
        task_arn=TASK_ARN,
        boundary_id=BOUNDARY_ID,
        reference=S3ObjectReference(
            bucket_name="example-cdc-evidence",
            object_key="drain/ingestion-one.json",
            version_id="evidence-version-one",
            sha256="a" * 64,
        ),
    )
    return CdcDrain(progress=progress, probe=probe), stages, probe, claim, evidence


def test_drain_waits_for_recorded_source_stop(drain_setup):
    drain, stages, probe, claim, _ = drain_setup
    del stages["SOURCE_STOPPED"]

    with pytest.raises(BatchPrerequisiteMissing, match="SOURCE_STOPPED"):
        drain.advance(claim)

    probe.read_drain_evidence.assert_not_called()


def test_ready_consumer_does_not_count_as_drained(drain_setup):
    drain, _, probe, claim, _ = drain_setup

    with patch.object(drain._shutdown, "record_evidence") as record:
        assert drain.advance(claim) == {
            "ingestion_id": INGESTION_ID,
            "stage": "CDC_DRAINED",
            "status": "RUNNING",
        }
        record.assert_not_called()

    probe.read_drain_evidence.assert_called_once_with(
        ingestion_id=INGESTION_ID,
        task_arn=TASK_ARN,
        boundary_id=BOUNDARY_ID,
    )


def test_wrong_boundary_is_rejected_before_recording_drain(drain_setup):
    drain, _, probe, claim, evidence = drain_setup
    probe.read_drain_evidence.return_value = replace(
        evidence, boundary_id="another-stop"
    )

    with patch.object(drain._shutdown, "record_evidence") as record:
        with pytest.raises(ProgressConflict, match="source-stop boundary"):
            drain.advance(claim)

        record.assert_not_called()


def test_matching_evidence_uses_existing_shutdown_writer(drain_setup):
    drain, _, probe, claim, evidence = drain_setup
    probe.read_drain_evidence.return_value = evidence
    completed = {"stage": "CDC_DRAINED", "status": "SUCCEEDED"}

    with patch.object(
        drain._shutdown, "record_evidence", return_value=completed
    ) as record:
        assert drain.advance(claim) == completed

    assert record.call_args.args == (claim,)
    assert record.call_args.kwargs["stage"] == "CDC_DRAINED"
    assert record.call_args.kwargs["evidence"] == evidence
    assert record.call_args.kwargs["expected_revision"] == 0