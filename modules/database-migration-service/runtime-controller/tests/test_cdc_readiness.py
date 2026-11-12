from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dms_runtime import cdc_readiness
from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from dms_runtime.cdc_readiness import CdcReadiness, CdcReadinessEvidence
from ingestion_registry.progress import ProgressConflict
from ingestion_registry.references import S3ObjectReference


INGESTION_ID = "ingestion-one"
TASK_ARN = "arn:aws:dms:eu-west-2:111122223333:task:EXAMPLE"
START_BOUNDARY_ID = "approved-start-one"


@pytest.fixture
def readiness_setup(monkeypatch):
    policy = SimpleNamespace(includes_cdc=True)
    monkeypatch.setattr(
        cdc_readiness,
        "resolve_extraction_policy",
        Mock(return_value=policy),
    )

    progress = Mock()
    progress.read_stage.return_value = None
    progress.record_stage.return_value = {
        "stage": "CDC_CONSUMER",
        "status": "SUCCEEDED",
    }
    probe = Mock()
    probe.read_readiness_evidence.return_value = None
    resolve_start = Mock(return_value=START_BOUNDARY_ID)
    readiness = CdcReadiness(
        progress=progress,
        resolve_deployment=Mock(
            return_value={"resources": {"replication_task_arn": TASK_ARN}}
        ),
        resolve_start_boundary=resolve_start,
        probe=probe,
    )
    claim = SimpleNamespace(ingestion_id=INGESTION_ID)
    evidence = CdcReadinessEvidence(
        ingestion_id=INGESTION_ID,
        task_arn=TASK_ARN,
        start_boundary_id=START_BOUNDARY_ID,
        reference=S3ObjectReference(
            bucket_name="example-cdc-evidence",
            object_key="readiness/ingestion-one.json",
            version_id="evidence-version-one",
            sha256="a" * 64,
        ),
    )
    return readiness, progress, probe, claim, evidence, resolve_start, policy


@pytest.mark.parametrize("missing", ["cdc", "start"])
def test_readiness_requires_cdc_and_an_approved_start(readiness_setup, missing):
    readiness, progress, probe, claim, _, resolve_start, policy = readiness_setup
    if missing == "cdc":
        policy.includes_cdc = False
    else:
        resolve_start.return_value = None

    with pytest.raises(BatchPrerequisiteMissing):
        readiness.advance(claim)

    probe.read_readiness_evidence.assert_not_called()
    progress.record_stage.assert_not_called()


def test_pending_consumer_does_not_record_success(readiness_setup):
    readiness, progress, probe, claim, *_ = readiness_setup

    assert readiness.advance(claim) == {
        "ingestion_id": INGESTION_ID,
        "stage": "CDC_CONSUMER",
        "status": "RUNNING",
    }
    probe.read_readiness_evidence.assert_called_once_with(
        ingestion_id=INGESTION_ID,
        task_arn=TASK_ARN,
        start_boundary_id=START_BOUNDARY_ID,
    )
    progress.record_stage.assert_not_called()


def test_wrong_start_boundary_is_rejected(readiness_setup):
    readiness, progress, probe, claim, evidence, *_ = readiness_setup
    probe.read_readiness_evidence.return_value = replace(
        evidence, start_boundary_id="another-start"
    )

    with pytest.raises(ProgressConflict, match="approved CDC start"):
        readiness.advance(claim)

    progress.record_stage.assert_not_called()


def test_verified_readiness_records_only_the_consumer_stage(readiness_setup):
    readiness, progress, probe, claim, evidence, *_ = readiness_setup
    probe.read_readiness_evidence.return_value = evidence

    assert readiness.advance(claim)["status"] == "SUCCEEDED"

    progress.record_stage.assert_called_once()
    call = progress.record_stage.call_args
    assert call.args == (claim,)
    assert call.kwargs["stage"] == "CDC_CONSUMER"
    assert call.kwargs["expected_revision"] == 0
    reference = call.kwargs["result_reference"]
    assert reference["start_boundary_id"] == START_BOUNDARY_ID
    assert reference["version_id"] == evidence.reference.version_id

    progress.read_stage.return_value = {
        "stage": "CDC_CONSUMER",
        "status": "SUCCEEDED",
        "result_reference": reference,
    }
    assert readiness.advance(claim) == progress.read_stage.return_value
    probe.read_readiness_evidence.assert_called_once()
    progress.record_stage.assert_called_once()