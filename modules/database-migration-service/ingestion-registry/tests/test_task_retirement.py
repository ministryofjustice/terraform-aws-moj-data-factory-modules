import copy

import pytest

from ingestion_registry.completion import IngestionCompletion
from ingestion_registry.identity import TableIdentity
from ingestion_registry.lifecycle import (
    BASELINE_STAGES,
    IngestionLifecycle,
)
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.references import S3ObjectReference
from ingestion_registry.requests import NewIngestionRequest
from ingestion_registry.resource_bindings import IngestionResourceBindings
from ingestion_registry.task_groups import (
    TaskGroupConflict,
    TaskGroupOwnership,
)
from ingestion_registry.task_retirement import TaskScopeRetirement


@pytest.fixture
def retirement_setup(binding_setup, request):
    (
        registry,
        owner,
        admitted,
        clock,
        claim,
        bindings,
        binding,
    ) = binding_setup

    binding["task_group"]["migration_type"] = "full-load"

    for index in range(1, getattr(request, "param", 1)):
        binding["task_group"]["tables"].append(
            {
                "schema_name": "public",
                "table_name": f"additional_table_{index}",
            }
        )

    bindings.pin(claim, binding)

    groups = TaskGroupOwnership(
        registry,
        clock=lambda: clock["now"],
    )
    group_claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator",
    )

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )
    evidence = S3ObjectReference(
        bucket_name="example-evidence",
        object_key="source/stopped.json",
        version_id="version-one",
        sha256="e" * 64,
    )

    def finish():
        for stage in BASELINE_STAGES:
            progress.record_stage(
                claim,
                stage=stage,
                status="SUCCEEDED",
                update_id=f"finish-{stage}",
                expected_revision=0,
                result_reference={
                    "uri": f"s3://example-evidence/{stage}.json",
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
            source_stopped_reference=evidence,
        )

    return (
        registry,
        owner,
        admitted,
        clock,
        binding,
        groups,
        group_claim,
        evidence,
        finish,
    )


def next_generation(setup, *, reuse_task=False):
    registry, owner, _, clock, original, _, _, _, _ = setup

    binding = copy.deepcopy(original)
    binding["artifact_revision"] = "f" * 64
    binding["extraction_generation"] = "generation-two"
    binding["landing"]["prefix"] = original["landing"]["prefix"].replace(
        "generation-one",
        "generation-two",
    )

    if not reuse_task:
        binding["resources"]["replication_task_arn"] += "TWO"

    admitted = registry.admit(
        NewIngestionRequest(
            request_id="next-generation",
            operation="REINGESTION",
            table=TableIdentity(
                environment=binding["environment"],
                source=binding["source"],
                database_name=binding["database_name"],
                **binding["table"],
            ),
            configuration_revision=binding["configuration_revision"],
            artifact_revision=binding["artifact_revision"],
            extraction_generation=binding["extraction_generation"],
        )
    )

    claim = owner.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="next-worker",
    )

    IngestionResourceBindings(
        registry,
        clock=lambda: clock["now"],
    ).pin(claim, binding)

    return admitted


def test_active_table_prevents_retirement(retirement_setup):
    registry, _, _, _, _, groups, claim, evidence, _ = retirement_setup

    with pytest.raises(TaskGroupConflict, match="no active ingestion"):
        TaskScopeRetirement(groups).retire(
            claim,
            source_stopped_reference=evidence,
        )

    assert registry._read(
        claim.group_key,
        f"RETIRED#{claim.scope_fingerprint}",
    ) is None

    assert not registry._read(
        claim.group_key,
        "OWNERSHIP",
    ).get("retired", False)


def test_retirement_preserves_old_scope_and_physical_task(
    retirement_setup,
):
    registry, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    receipt = TaskScopeRetirement(groups).retire(
        claim,
        source_stopped_reference=evidence,
    )

    assert receipt["scope"]["extraction_generation"] == "generation-one"

    for key in (claim.group_key, claim.task_key):
        record = registry._read(key, "OWNERSHIP")
        assert record["retired"] is True
        assert int(record["lease_expires_at"]) == 0


def test_new_generation_can_claim_after_retirement(retirement_setup):
    (
        registry,
        _,
        _,
        _,
        _,
        groups,
        old_claim,
        evidence,
        finish,
    ) = retirement_setup
    finish()

    TaskScopeRetirement(groups).retire(
        old_claim,
        source_stopped_reference=evidence,
    )

    admitted = next_generation(retirement_setup)
    claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="next-coordinator",
    )

    assert claim.group_fencing_token > old_claim.group_fencing_token
    assert claim.task_arn != old_claim.task_arn

    assert registry._read(
        old_claim.task_key,
        "OWNERSHIP",
    )["retired"] is True

    assert registry._read(
        old_claim.group_key,
        f"RETIRED#{old_claim.scope_fingerprint}",
    ) is not None


def test_new_generation_is_blocked_without_retirement(retirement_setup):
    _, _, _, _, _, groups, _, _, finish = retirement_setup
    finish()

    admitted = next_generation(retirement_setup)

    with pytest.raises(TaskGroupConflict, match="scope differs"):
        groups.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="next-coordinator",
        )


def test_retired_physical_task_cannot_be_reused(retirement_setup):
    _, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    TaskScopeRetirement(groups).retire(
        claim,
        source_stopped_reference=evidence,
    )

    admitted = next_generation(
        retirement_setup,
        reuse_task=True,
    )

    with pytest.raises(TaskGroupConflict, match="retired physical task"):
        groups.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="next-coordinator",
        )


def test_retirement_retry_after_handover_is_read_only(
    retirement_setup,
    monkeypatch,
):
    registry, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    retirement = TaskScopeRetirement(groups)
    first = retirement.retire(
        claim,
        source_stopped_reference=evidence,
    )

    admitted = next_generation(retirement_setup)
    groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="next-coordinator",
    )

    before = registry._read(claim.group_key, "OWNERSHIP")

    def unexpected(**kwargs):
        pytest.fail("A retirement receipt retry must not write")

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        unexpected,
    )

    assert retirement.retire(
        claim,
        source_stopped_reference=evidence,
    ) == first

    assert registry._read(
        claim.group_key,
        "OWNERSHIP",
    ) == before


def test_expired_coordination_can_be_retired_after_tables_finish(
    retirement_setup,
):
    _, _, _, clock, _, groups, claim, evidence, finish = retirement_setup
    finish()
    clock["now"] = claim.lease_expires_at

    result = TaskScopeRetirement(groups).retire(
        claim,
        source_stopped_reference=evidence,
    )

    assert result["scope"]["extraction_generation"] == "generation-one"


def test_old_coordinator_cannot_renew_after_handover(retirement_setup):
    _, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    TaskScopeRetirement(groups).retire(
        claim,
        source_stopped_reference=evidence,
    )

    admitted = next_generation(retirement_setup)
    groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="next-coordinator",
    )

    with pytest.raises(TaskGroupConflict):
        groups.renew(claim)


def test_lost_retirement_response_returns_original_receipt(
    retirement_setup,
    monkeypatch,
):
    from botocore.exceptions import ReadTimeoutError

    registry, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    retirement = TaskScopeRetirement(groups)
    original = registry.client.transact_write_items

    def commit_then_timeout(**kwargs):
        original(**kwargs)
        raise ReadTimeoutError(
            endpoint_url="https://dynamodb.eu-west-2.amazonaws.com",
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            registry.client,
            "transact_write_items",
            commit_then_timeout,
        )

        with pytest.raises(ReadTimeoutError):
            retirement.retire(
                claim,
                source_stopped_reference=evidence,
            )

    result = retirement.retire(
        claim,
        source_stopped_reference=evidence,
    )

    assert result == registry._read(
        claim.group_key,
        f"RETIRED#{claim.scope_fingerprint}",
    )


def test_concurrent_admission_prevents_retirement(
    retirement_setup,
    monkeypatch,
):
    registry, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    original = registry.client.transact_write_items

    def admit_then_retire(**kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(
                registry.client,
                "transact_write_items",
                original,
            )
            next_generation(retirement_setup)

        return original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            registry.client,
            "transact_write_items",
            admit_then_retire,
        )

        with pytest.raises(TaskGroupConflict, match="no active ingestion"):
            TaskScopeRetirement(groups).retire(
                claim,
                source_stopped_reference=evidence,
            )

    assert registry._read(
        claim.group_key,
        f"RETIRED#{claim.scope_fingerprint}",
    ) is None

    assert not registry._read(
        claim.group_key,
        "OWNERSHIP",
    ).get("retired", False)


@pytest.mark.parametrize(
    "retirement_setup",
    [97],
    indirect=True,
)
def test_atomic_retirement_accepts_97_tables(retirement_setup):
    _, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    result = TaskScopeRetirement(groups).retire(
        claim,
        source_stopped_reference=evidence,
    )

    assert len(result["scope"]["tables"]) == 97


@pytest.mark.parametrize(
    "retirement_setup",
    [98],
    indirect=True,
)
def test_larger_scope_is_rejected_without_partial_retirement(
    retirement_setup,
):
    registry, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    with pytest.raises(ValueError, match="97 selected tables"):
        TaskScopeRetirement(groups).retire(
            claim,
            source_stopped_reference=evidence,
        )

    assert registry._read(
        claim.group_key,
        f"RETIRED#{claim.scope_fingerprint}",
    ) is None

    assert not registry._read(
        claim.group_key,
        "OWNERSHIP",
    ).get("retired", False)


def test_released_coordination_can_be_retired(retirement_setup):
    _, _, _, _, _, groups, claim, evidence, finish = retirement_setup
    finish()

    groups.release(
        claim,
        release_id="finished-operation",
        result_reference={
            "uri": "s3://example-evidence/finished.json",
        },
    )

    result = TaskScopeRetirement(groups).retire(
        claim,
        source_stopped_reference=evidence,
    )

    assert result["scope"]["extraction_generation"] == "generation-one"


def test_previous_tokens_cannot_retire_after_takeover(retirement_setup):
    (
        _,
        _,
        admitted,
        _,
        _,
        groups,
        old_claim,
        evidence,
        finish,
    ) = retirement_setup

    groups.release(
        old_claim,
        release_id="yield-operation",
        result_reference={
            "uri": "s3://example-evidence/yield.json",
        },
    )

    new_claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="replacement-coordinator",
    )
    finish()

    with pytest.raises(TaskGroupConflict, match="tokens"):
        TaskScopeRetirement(groups).retire(
            old_claim,
            source_stopped_reference=evidence,
        )

    assert TaskScopeRetirement(groups).retire(
        new_claim,
        source_stopped_reference=evidence,
    )