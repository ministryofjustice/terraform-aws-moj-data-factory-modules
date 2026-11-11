"""Integration tests using the actual registration handler."""

import hashlib

import pytest
from botocore.exceptions import ReadTimeoutError

from contract_integration_support import (
    approve,
    complete_registration_integration,
    expected_contract_id,
    promote,
)
from ingestion_registry.contract_approval import ContractApprovalBlocked


def test_actual_handler_registers_promoted_approved_contract(
    registration_integration,
):
    setup = registration_integration
    approve(setup)

    completion = complete_registration_integration(setup)
    registered = completion["result"]["registered_contract"]

    assert setup["invocation"].calls == 1
    assert registered["contract_id"] == expected_contract_id(
        setup["table"]
    )
    assert registered["contract_version"] == "v1"
    assert registered["contract_fingerprint"] == hashlib.sha256(
        setup["raw"]
    ).hexdigest()

    item = setup["registration_table"].get_item(
        Key={
            "contract_id": registered["contract_id"],
            "contract_version": "v1",
        },
        ConsistentRead=True,
    )["Item"]

    assert item["schema_fingerprint"] == (
        registered["contract_fingerprint"]
    )
    assert item["s3_object_version_id"] == (
        registered["object_reference"]["version_id"]
    )
    assert item["is_active"] is False
    assert item["ingestion_version"] is None

    assert setup["registry"]._read(
        setup["request"]["pk"],
        "REGISTRATION",
    ) == completion


def test_completed_registration_retry_does_not_invoke_again(
    registration_integration,
):
    setup = registration_integration
    approve(setup)

    first = complete_registration_integration(setup)
    second = complete_registration_integration(setup)

    assert second == first
    assert setup["invocation"].calls == 1
    assert len(
        setup["s3"].list_object_versions(
            Bucket="test-approved-contracts"
        )["Versions"]
    ) == 1


def test_lost_registration_response_is_reconciled(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    setup["invocation"].lose_next_response = True

    with pytest.raises(ReadTimeoutError):
        complete_registration_integration(setup)

    assert setup["registry"]._read(
        setup["request"]["pk"],
        "REGISTRATION",
    ) is None

    completion = complete_registration_integration(setup)

    assert completion["record_type"] == "CONTRACT_REGISTRATION"
    assert setup["invocation"].calls == 1


def test_pending_review_never_invokes_registration(
    registration_integration,
):
    setup = registration_integration

    with pytest.raises(
        ContractApprovalBlocked,
        match="not currently approved",
    ):
        complete_registration_integration(setup)

    assert setup["invocation"].calls == 0
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0


def test_conflicting_registration_blocks_completion(
    registration_integration,
):
    from ingestion_registry.contract_verification import (
        ContractVerificationError,
    )

    setup = registration_integration
    approve(setup)

    # Create the approved final object, but do not register it yet.
    promoted = promote(setup)

    # Point the registration at a real object version while recording
    # a conflicting fingerprint. Verification must reject the mismatch.
    setup["registration_table"].put_item(
        Item={
            "contract_id": expected_contract_id(setup["table"]),
            "contract_version": "v1",
            "source": setup["table"].source,
            "database_name": setup["table"].database_name,
            "schema_name": setup["table"].schema_name,
            "table_name": setup["table"].table_name,
            "schema_fingerprint": "f" * 64,
            "s3_contract_uri": promoted["s3_contract_uri"],
            "s3_object_version_id": promoted[
                "object_reference"
            ]["version_id"],
            "is_active": False,
        }
    )

    with pytest.raises(
        ContractVerificationError,
        match="fingerprint",
    ):
        complete_registration_integration(setup)

    assert setup["invocation"].calls == 0
    assert setup["registry"]._read(
        setup["request"]["pk"],
        "REGISTRATION",
    ) is None
