import pytest

from ingestion_registry.task_groups import (
    TaskGroupConflict,
    TaskGroupOwnership,
    TaskGroupUnavailable,
)


def test_coordinator_claims_both_task_scope_records(
    task_group_setup,
) -> None:
    registry, admitted, _, groups = task_group_setup

    claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )

    assert claim.group_fencing_token == 1
    assert claim.task_fencing_token == 1
    assert claim.recovery_required is False

    group_record = registry._read(claim.group_key, "OWNERSHIP")
    task_record = registry._read(claim.task_key, "OWNERSHIP")

    assert group_record["paired_key"] == claim.task_key
    assert task_record["paired_key"] == claim.group_key
    assert group_record["scope"]["tables"] == [
        {
            "schema_name": "public",
            "table_name": "dms_integration_test",
        }
    ]


def test_repeated_task_claim_returns_original_tokens(
    task_group_setup,
) -> None:
    _, admitted, clock, groups = task_group_setup

    first = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )
    clock["now"] += 10

    repeated = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )

    assert repeated == first


def test_second_coordinator_cannot_claim_active_task(
    task_group_setup,
) -> None:
    _, admitted, _, groups = task_group_setup

    groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )

    with pytest.raises(TaskGroupConflict, match="Another coordinator"):
        groups.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="coordinator-two",
        )


def test_expired_task_claim_requires_reconciliation(
    task_group_setup,
) -> None:
    _, admitted, clock, groups = task_group_setup

    first = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )
    clock["now"] = first.lease_expires_at

    recovered = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-two",
    )

    assert recovered.group_fencing_token == 2
    assert recovered.task_fencing_token == 2
    assert recovered.recovery_required is True


def test_unbound_ingestion_cannot_claim_task(
    owned_ingestion,
) -> None:
    registry, _, admitted, clock = owned_ingestion

    groups = TaskGroupOwnership(
        registry,
        clock=lambda: clock["now"],
    )

    with pytest.raises(TaskGroupUnavailable, match="pinned resources"):
        groups.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="coordinator-one",
        )


def test_task_renewal_keeps_both_tokens(
    task_group_setup,
) -> None:
    _, admitted, clock, groups = task_group_setup

    first = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )
    clock["now"] = 1100

    renewed = groups.renew(first)

    assert renewed.group_fencing_token == first.group_fencing_token
    assert renewed.task_fencing_token == first.task_fencing_token
    assert renewed.lease_expires_at == 1400


def test_task_renewal_does_not_shorten_existing_leases(
    task_group_setup,
) -> None:
    _, admitted, clock, groups = task_group_setup

    first = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
        lease_seconds=600,
    )
    clock["now"] = 1010

    renewed = groups.renew(first, lease_seconds=30)

    assert renewed.lease_expires_at == first.lease_expires_at


def test_expired_coordinator_cannot_renew(
    task_group_setup,
) -> None:
    _, admitted, clock, groups = task_group_setup

    first = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )
    clock["now"] = first.lease_expires_at

    with pytest.raises(TaskGroupConflict):
        groups.renew(first)


def test_release_preserves_records_and_tokens(
    task_group_setup,
) -> None:
    registry, admitted, _, groups = task_group_setup

    claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )

    receipt = groups.release(
        claim,
        release_id="operation-complete",
        result_reference={
            "kind": "component-result",
            "execution_id": "operation-one",
        },
    )

    group = registry._read(claim.group_key, "OWNERSHIP")
    task = registry._read(claim.task_key, "OWNERSHIP")

    assert int(group["lease_expires_at"]) == 0
    assert int(task["lease_expires_at"]) == 0
    assert int(group["fencing_token"]) == claim.group_fencing_token
    assert int(task["fencing_token"]) == claim.task_fencing_token
    assert receipt["release_id"] == "operation-complete"


def test_repeated_release_returns_original_receipt(
    task_group_setup,
) -> None:
    _, admitted, clock, groups = task_group_setup

    claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )

    arguments = {
        "release_id": "operation-complete",
        "result_reference": {
            "kind": "component-result",
            "execution_id": "operation-one",
        },
    }

    original = groups.release(claim, **arguments)
    clock["now"] += 10

    assert groups.release(claim, **arguments) == original


def test_release_id_cannot_change_content(
    task_group_setup,
) -> None:
    _, admitted, _, groups = task_group_setup

    claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )

    groups.release(
        claim,
        release_id="operation-complete",
        result_reference={"execution_id": "operation-one"},
    )

    with pytest.raises(TaskGroupConflict, match="different content"):
        groups.release(
            claim,
            release_id="operation-complete",
            result_reference={"execution_id": "operation-two"},
        )


def test_released_task_can_be_claimed_with_higher_tokens(
    task_group_setup,
) -> None:
    _, admitted, _, groups = task_group_setup

    first = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )

    groups.release(
        first,
        release_id="operation-complete",
        result_reference={"execution_id": "operation-one"},
    )

    next_claim = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-two",
    )

    assert next_claim.group_fencing_token == 2
    assert next_claim.task_fencing_token == 2
    assert next_claim.recovery_required is True


def test_previous_coordinator_cannot_release_after_takeover(
    task_group_setup,
) -> None:
    _, admitted, clock, groups = task_group_setup

    first = groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-one",
    )
    clock["now"] = first.lease_expires_at

    groups.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="coordinator-two",
    )

    with pytest.raises(TaskGroupConflict):
        groups.release(
            first,
            release_id="stale-release",
            result_reference={"execution_id": "operation-one"},
        )