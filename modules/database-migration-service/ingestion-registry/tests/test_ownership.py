import pytest

from ingestion_registry.ownership import (
    OwnershipConflict,
    OwnershipLost,
    OwnershipUnavailable,
)


def test_first_claim_creates_attempt_one(
    owned_ingestion,
) -> None:
    registry, ownership, admitted, _ = owned_ingestion

    claim = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )

    assert claim.fencing_token == 1
    assert claim.attempt == 1
    assert claim.lease_expires_at == 1300
    assert claim.recovery_required is False

    attempt = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "ATTEMPT#00000000000000000001",
    )
    assert attempt["execution_id"] == "execution-one"


def test_repeated_claim_keeps_original_token_and_attempt(
    owned_ingestion,
) -> None:
    _, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )
    clock["now"] = 1010

    repeated = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )

    assert repeated == first


def test_active_owner_blocks_other_execution(
    owned_ingestion,
) -> None:
    _, ownership, admitted, _ = owned_ingestion

    ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )

    with pytest.raises(OwnershipConflict):
        ownership.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="execution-two",
        )


def test_expired_ownership_creates_fenced_recovery_attempt(
    owned_ingestion,
) -> None:
    registry, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )

    clock["now"] = first.lease_expires_at

    recovered = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    assert recovered.ingestion_id == first.ingestion_id
    assert recovered.fencing_token == 2
    assert recovered.attempt == 2
    assert recovered.recovery_required is True

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )

    assert int(ingestion["ingestion_version"]) == 1
    assert int(ingestion["attempt_count"]) == 2
    assert ingestion["active_execution_id"] == "execution-two"


def test_previous_execution_cannot_reclaim_active_recovery(
    owned_ingestion,
) -> None:
    _, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )
    clock["now"] = first.lease_expires_at

    ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    with pytest.raises(OwnershipConflict):
        ownership.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="execution-one",
        )


def test_unknown_ingestion_cannot_be_claimed(
    owned_ingestion,
) -> None:
    _, ownership, _, _ = owned_ingestion

    with pytest.raises(OwnershipUnavailable, match="does not exist"):
        ownership.claim(
            ingestion_id="unknown-ingestion",
            execution_id="execution-one",
        )


@pytest.mark.parametrize(
    "lease_seconds",
    [0, -1, 3601, True],
)
def test_invalid_lease_is_rejected(
    owned_ingestion,
    lease_seconds,
) -> None:
    _, ownership, admitted, _ = owned_ingestion

    with pytest.raises(ValueError, match="lease_seconds"):
        ownership.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="execution-one",
            lease_seconds=lease_seconds,
        )


def test_renewal_keeps_token_and_attempt(
    owned_ingestion,
) -> None:
    registry, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )
    clock["now"] = 1100

    renewed = ownership.renew(first)

    assert renewed.fencing_token == first.fencing_token
    assert renewed.attempt == first.attempt
    assert renewed.lease_expires_at == 1400

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )
    assert int(ingestion["attempt_count"]) == 1


def test_renewal_prevents_takeover_at_original_expiry(
    owned_ingestion,
) -> None:
    _, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )
    clock["now"] = 1100
    ownership.renew(first)

    clock["now"] = first.lease_expires_at

    with pytest.raises(OwnershipConflict):
        ownership.claim(
            ingestion_id=admitted.ingestion_id,
            execution_id="execution-two",
        )


def test_expired_lease_cannot_be_renewed(
    owned_ingestion,
) -> None:
    _, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )
    clock["now"] = first.lease_expires_at

    with pytest.raises(OwnershipLost):
        ownership.renew(first)


def test_previous_token_cannot_renew_after_takeover(
    owned_ingestion,
) -> None:
    _, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )
    clock["now"] = first.lease_expires_at

    recovered = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    with pytest.raises(OwnershipLost):
        ownership.renew(first)

    assert ownership.renew(recovered).fencing_token == 2


def test_shorter_renewal_does_not_reduce_expiry(
    owned_ingestion,
) -> None:
    _, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
        lease_seconds=600,
    )
    clock["now"] = 1010

    renewed = ownership.renew(
        first,
        lease_seconds=30,
    )

    assert renewed.lease_expires_at == first.lease_expires_at


def test_old_receipt_can_renew_current_matching_token(
    owned_ingestion,
) -> None:
    _, ownership, admitted, clock = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )
    clock["now"] = 1100
    ownership.renew(first)

    clock["now"] = 1200
    renewed = ownership.renew(first)

    assert renewed.fencing_token == first.fencing_token
    assert renewed.lease_expires_at == 1500


def test_terminal_ingestion_cannot_renew(
    owned_ingestion,
) -> None:
    registry, ownership, admitted, _ = owned_ingestion

    first = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )

    registry.client.update_item(
        TableName=registry.table_name,
        Key={
            "pk": {"S": f"INGESTION#{admitted.ingestion_id}"},
            "sk": {"S": "META"},
        },
        UpdateExpression="SET #status = :completed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":completed": {"S": "COMPLETED"},
        },
    )

    with pytest.raises(OwnershipLost):
        ownership.renew(first)