import pytest

from dms_runtime.batch_downstream import (
    BatchDownstream,
    BatchPrerequisiteMissing,
)


INGESTION_ID = "ingestion-one"


@pytest.fixture
def batch_setup():
    deployment = {"configuration_revision": "a" * 64}
    stages = {
        "CONTRACT": {
            "status": "SUCCEEDED",
            "result_reference": {
                "kind": "registered-contract",
                "contract_id": "contract-one",
                "contract_version": "v1",
                "contract_fingerprint": "b" * 64,
                "contract_bucket_name": "example-contracts",
                "contract_object_key": "source/db/public/example/v1/contract.avsc",
                "contract_object_version_id": "contract-version-one",
                "contract_object_sha256": "c" * 64,
            },
        },
        "EXTRACTION": {
            "status": "SUCCEEDED",
            "result_reference": {
                "kind": "raw-batch",
                "bucket_name": "example-raw",
                "object_key": "batches/ingestion-one.json",
                "version_id": "batch-version-one",
                "sha256": "d" * 64,
            },
        },
    }

    def resolve_deployment(ingestion_id):
        assert ingestion_id == INGESTION_ID
        return deployment

    def read_stage(*, ingestion_id, stage):
        assert ingestion_id == INGESTION_ID
        return stages.get(stage)

    return BatchDownstream(
        resolve_deployment=resolve_deployment,
        read_stage=read_stage,
    ), stages


def test_loads_the_pinned_contract_and_exact_raw_batch(batch_setup):
    downstream, stages = batch_setup

    inputs = downstream.load_inputs(INGESTION_ID)

    assert inputs.ingestion_id == INGESTION_ID
    assert inputs.deployment["configuration_revision"] == "a" * 64
    assert inputs.contract == stages["CONTRACT"]["result_reference"]
    assert inputs.raw_batch.version_id == "batch-version-one"
    assert inputs.raw_batch.sha256 == "d" * 64


def test_full_load_status_alone_does_not_identify_the_raw_batch(batch_setup):
    downstream, stages = batch_setup
    stages["EXTRACTION"]["result_reference"] = {
        "kind": "full-load-status",
        "status": "SUCCEEDED",
    }

    with pytest.raises(BatchPrerequisiteMissing, match="completed Raw batch"):
        downstream.load_inputs(INGESTION_ID)


def test_publication_waits_while_processing_is_running(batch_setup):
    downstream, stages = batch_setup
    stages["PROCESSING"] = {"status": "RUNNING"}
    stages["HISTORY_BASELINE"] = {
        "status": "SUCCEEDED",
        "result_reference": {
            "history_location": "s3://example-history/ingestion-one/",
            "commit_id": "history-commit-one",
        },
    }

    with pytest.raises(BatchPrerequisiteMissing, match="PROCESSING"):
        downstream.publication_prerequisites(
            INGESTION_ID,
            reconciliation_required=False,
        )

    stages["PROCESSING"] = {
        "status": "SUCCEEDED",
        "result_reference": {"job_run_id": "processing-run-one"},
    }

    results = downstream.publication_prerequisites(
        INGESTION_ID,
        reconciliation_required=False,
    )

    assert results["HISTORY_BASELINE"]["commit_id"] == "history-commit-one"


def test_publication_requires_reconciliation_when_configured(batch_setup):
    downstream, stages = batch_setup
    stages["PROCESSING"] = {
        "status": "SUCCEEDED",
        "result_reference": {"job_run_id": "processing-run-one"},
    }
    stages["HISTORY_BASELINE"] = {
        "status": "SUCCEEDED",
        "result_reference": {"commit_id": "history-commit-one"},
    }

    with pytest.raises(BatchPrerequisiteMissing, match="RECONCILIATION"):
        downstream.publication_prerequisites(
            INGESTION_ID,
            reconciliation_required=True,
        )