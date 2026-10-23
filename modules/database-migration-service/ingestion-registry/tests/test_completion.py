from dataclasses import replace

import pytest

from ingestion_registry.completion import (
    CompletionConflict,
    IngestionCompletion,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.lifecycle import (
    BASELINE_STAGES,
    IngestionLifecycle,
)
from ingestion_registry.ownership import (
    OwnershipLost,
    OwnershipUnavailable,
)
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.references import S3ObjectReference
from ingestion_registry.requests import NewIngestionRequest


@pytest.fixture
def completion_setup(binding_setup):
    (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        bindings,
        binding,
    ) = binding_setup

    binding["task_group"]["migration_type"] = "full-load"
    pinned = bindings.pin(claim, binding)

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )

    for stage in BASELINE_STAGES:
        progress.record_stage(
            claim,
            stage=stage,
            status="SUCCEEDED",
            update_id=f"completed-{stage}",
            expected_revision=0,
            result_reference={
                "uri": f"s3://example-evidence/{stage}.json",
                "version_id": "evidence-version-one",
            },
        )

    lifecycle = IngestionLifecycle(
        progress,
        clock=lambda: clock["now"],
    )
    lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    completion = IngestionCompletion(
        progress,
        clock=lambda: clock["now"],
    )

    evidence = S3ObjectReference(
        bucket_name="example-evidence",
        object_key="source/task-stopped.json",
        version_id="stopped-evidence-version-one",
        sha256="e" * 64,
    )

    return (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        completion,
        evidence,
        pinned,
    )


def test_completion_releases_table_and_preserves_history(
    completion_setup,
):
    (
        registry,
        _,
        admitted,
        _,
        claim,
        completion,
        evidence,
        pinned,
    ) = completion_setup

    result = completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )
    table = TableIdentity(**ingestion["table_identity"])
    head = registry._read(table.partition_key, "HEAD")

    assert result["ingestion_version"] == 1
    assert ingestion["status"] == "COMPLETED"
    assert "active_ingestion_id" not in head
    assert int(head["latest_version"]) == 1

    assert registry._read(
        pinned["boundary_key"],
        "RESERVATION",
    ) is not None

    assert registry._read(
        table.partition_key,
        "VERSION#00000000000000000001",
    )["ingestion_id"] == admitted.ingestion_id

    assert registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "STAGE#HISTORY_BASELINE",
    )["status"] == "SUCCEEDED"


def test_reingestion_allocates_next_version_after_completion(
    completion_setup,
):
    registry, _, admitted, _, claim, completion, evidence, _ = (
        completion_setup
    )

    completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )

    next_ingestion = registry.admit(
        NewIngestionRequest(
            request_id="reingestion-two",
            operation="REINGESTION",
            table=TableIdentity(**ingestion["table_identity"]),
            configuration_revision=ingestion["configuration_revision"],
            artifact_revision=ingestion["artifact_revision"],
            extraction_generation="generation-two",
        )
    )

    assert next_ingestion.ingestion_version == 2
    assert next_ingestion.ingestion_id != admitted.ingestion_id


def test_completion_retry_returns_original_receipt(
    completion_setup,
):
    _, _, _, clock, claim, completion, evidence, _ = completion_setup

    first = completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    clock["now"] += 1000

    repeated = completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    assert repeated == first


def test_different_completion_evidence_is_rejected(
    completion_setup,
):
    _, _, _, _, claim, completion, evidence, _ = completion_setup

    completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    with pytest.raises(CompletionConflict, match="different"):
        completion.complete_full_load(
            claim,
            source_stopped_reference=replace(
                evidence,
                version_id="different-version",
            ),
        )


def test_expired_owner_cannot_complete(
    completion_setup,
):
    registry, _, admitted, clock, claim, completion, evidence, _ = (
        completion_setup
    )
    clock["now"] = claim.lease_expires_at

    with pytest.raises(OwnershipLost):
        completion.complete_full_load(
            claim,
            source_stopped_reference=evidence,
        )

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )
    table = TableIdentity(**ingestion["table_identity"])

    assert ingestion["status"] != "COMPLETED"
    assert registry._read(
        table.partition_key,
        "HEAD",
    )["active_ingestion_id"] == admitted.ingestion_id


def test_stale_execution_cannot_complete(
    completion_setup,
):
    _, ownership, admitted, clock, claim, completion, evidence, _ = (
        completion_setup
    )
    clock["now"] = claim.lease_expires_at

    ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="replacement-execution",
    )

    with pytest.raises(OwnershipLost):
        completion.complete_full_load(
            claim,
            source_stopped_reference=evidence,
        )


def test_completed_ingestion_cannot_be_claimed_again(
    completion_setup,
):
    _, ownership, admitted, _, claim, completion, evidence, _ = (
        completion_setup
    )

    completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    with pytest.raises(OwnershipUnavailable):
        ownership.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="another-execution",
        )


def test_cdc_ingestion_cannot_use_full_load_completion(
    binding_setup,
):
    registry, _, _, clock, claim, bindings, binding = binding_setup
    bindings.pin(claim, binding)

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )
    completion = IngestionCompletion(
        progress,
        clock=lambda: clock["now"],
    )

    evidence = S3ObjectReference(
        bucket_name="example-evidence",
        object_key="source/stopped.json",
        version_id="version-one",
        sha256="f" * 64,
    )

    with pytest.raises(CompletionConflict, match="CDC"):
        completion.complete_full_load(
            claim,
            source_stopped_reference=evidence,
        )


def test_completion_requires_a_ready_baseline(binding_setup):
    registry, _, _, clock, claim, bindings, binding = binding_setup
    binding["task_group"]["migration_type"] = "full-load"
    bindings.pin(claim, binding)

    completion = IngestionCompletion(
        IngestionProgress(
            registry,
            clock=lambda: clock["now"],
        ),
        clock=lambda: clock["now"],
    )

    evidence = S3ObjectReference(
        bucket_name="example-evidence",
        object_key="source/stopped.json",
        version_id="version-one",
        sha256="f" * 64,
    )

    with pytest.raises(CompletionConflict, match="ready baseline"):
        completion.complete_full_load(
            claim,
            source_stopped_reference=evidence,
        )


def test_completion_receipt_is_detached(completion_setup):
    _, _, _, _, claim, completion, evidence, _ = completion_setup

    first = completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )
    first["source_stopped_reference"]["version_id"] = "changed"

    repeated = completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    assert (
        repeated["source_stopped_reference"]["version_id"]
        == evidence.version_id
    )

def admit_next_ingestion(registry, ingestion_id):
    ingestion = registry._read(
        f"INGESTION#{ingestion_id}",
        "META",
    )

    return registry.admit(
        NewIngestionRequest(
            request_id="next-ingestion-boundary-test",
            operation="REINGESTION",
            table=TableIdentity(**ingestion["table_identity"]),
            configuration_revision=ingestion["configuration_revision"],
            artifact_revision=ingestion["artifact_revision"],
            extraction_generation=ingestion["extraction_generation"],
        )
    )


def test_old_completion_retry_preserves_new_ingestion_reservation(
    completion_setup,
):
    registry, _, admitted, _, claim, completion, evidence, _ = (
        completion_setup
    )

    original_receipt = completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    next_ingestion = admit_next_ingestion(
        registry,
        admitted.ingestion_id,
    )

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )
    table = TableIdentity(**ingestion["table_identity"])

    head_before = registry._read(table.partition_key, "HEAD")

    repeated_receipt = completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    head_after = registry._read(table.partition_key, "HEAD")

    assert repeated_receipt == original_receipt
    assert head_after == head_before
    assert head_after["active_ingestion_id"] == (
        next_ingestion.ingestion_id
    )
    assert int(head_after["latest_version"]) == 2


def test_reingestion_cannot_reuse_previous_landing_boundary(
    completion_setup,
):
    from ingestion_registry.resource_bindings import (
        BindingConflict,
        IngestionResourceBindings,
    )

    (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        completion,
        evidence,
        pinned,
    ) = completion_setup

    completion.complete_full_load(
        claim,
        source_stopped_reference=evidence,
    )

    next_ingestion = admit_next_ingestion(
        registry,
        admitted.ingestion_id,
    )
    next_claim = ownership.claim(
        ingestion_id=next_ingestion.ingestion_id,
        execution_id="next-ingestion-execution",
    )

    bindings = IngestionResourceBindings(
        registry,
        clock=lambda: clock["now"],
    )

    with pytest.raises(BindingConflict):
        bindings.pin(
            next_claim,
            pinned["binding"],
        )

    reservation = registry._read(
        pinned["boundary_key"],
        "RESERVATION",
    )

    assert reservation["ingestion_id"] == admitted.ingestion_id

    # The failed transaction must not leave a partially pinned binding.
    assert registry._read(
        f"INGESTION#{next_ingestion.ingestion_id}",
        "BINDING",
    ) is None


def test_table_completion_preserves_shared_task_ownership(
    binding_setup,
):
    from ingestion_registry.task_groups import TaskGroupOwnership

    (
        registry,
        _,
        admitted,
        clock,
        claim,
        bindings,
        binding,
    ) = binding_setup

    binding["task_group"]["migration_type"] = "full-load"
    binding["task_group"]["tables"].append(
        {
            "schema_name": "public",
            "table_name": "another_selected_table",
        }
    )
    bindings.pin(claim, binding)

    groups = TaskGroupOwnership(
        registry,
        clock=lambda: clock["now"],
    )
    group_claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="shared-task-execution",
    )

    group_before = registry._read(
        group_claim.group_key,
        "OWNERSHIP",
    )
    task_before = registry._read(
        group_claim.task_key,
        "OWNERSHIP",
    )

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )

    for stage in BASELINE_STAGES:
        progress.record_stage(
            claim,
            stage=stage,
            status="SUCCEEDED",
            update_id=f"shared-task-completed-{stage}",
            expected_revision=0,
            result_reference={
                "uri": f"s3://example-evidence/{stage}.json",
                "version_id": "evidence-version-one",
            },
        )

    IngestionLifecycle(
        progress,
        clock=lambda: clock["now"],
    ).mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    IngestionCompletion(
        progress,
        clock=lambda: clock["now"],
    ).complete_full_load(
        claim,
        source_stopped_reference=S3ObjectReference(
            bucket_name="example-evidence",
            object_key="source/shared-task-stopped.json",
            version_id="stopped-version-one",
            sha256="e" * 64,
        ),
    )

    assert registry._read(
        group_claim.group_key,
        "OWNERSHIP",
    ) == group_before

    assert registry._read(
        group_claim.task_key,
        "OWNERSHIP",
    ) == task_before


def test_lost_completion_response_is_recovered_by_retry(
    completion_setup,
    monkeypatch,
):
    from botocore.exceptions import ReadTimeoutError

    registry, _, admitted, _, claim, completion, evidence, _ = (
        completion_setup
    )

    original_write = registry.client.transact_write_items

    def commit_then_lose_response(**kwargs):
        original_write(**kwargs)
        raise ReadTimeoutError(
            endpoint_url="https://dynamodb.eu-west-2.amazonaws.com",
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            registry.client,
            "transact_write_items",
            commit_then_lose_response,
        )

        with pytest.raises(ReadTimeoutError):
            completion.complete_full_load(
                claim,
                source_stopped_reference=evidence,
            )

    stored_receipt = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "COMPLETION",
    )
    assert stored_receipt is not None

    # A retry must read the committed receipt without writing again.
    def unexpected_write(**kwargs):
        pytest.fail(
            "A committed completion retry must not write again"
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            registry.client,
            "transact_write_items",
            unexpected_write,
        )

        recovered_receipt = completion.complete_full_load(
            claim,
            source_stopped_reference=evidence,
        )

    assert recovered_receipt == stored_receipt
    assert registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )["status"] == "COMPLETED"