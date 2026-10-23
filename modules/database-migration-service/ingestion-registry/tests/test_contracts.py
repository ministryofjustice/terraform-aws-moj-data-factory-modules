import uuid
from dataclasses import replace

import pytest

from ingestion_registry.contracts import (
    IngestionContracts,
    RegisteredContract,
)
from ingestion_registry.progress import ProgressConflict
from ingestion_registry.references import S3ObjectReference


@pytest.fixture
def contract_setup(progress_setup):
    registry, ownership, admitted, clock, claim, progress = progress_setup

    contract_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_DNS,
            "sandbox-postgres:dms_test:public:dms_integration_test",
        )
    )

    contract = RegisteredContract(
        contract_id=contract_id,
        contract_version="v1",
        contract_fingerprint="e" * 64,
        object_reference=S3ObjectReference(
            bucket_name="example-contracts",
            object_key=(
                "sandbox-postgres/dms_test/public/"
                "dms_integration_test/v1/contract.avsc"
            ),
            version_id="contract-object-version-one",
            sha256="f" * 64,
        ),
    )

    approval = S3ObjectReference(
        bucket_name="example-contract-evidence",
        object_key="approvals/decision-one.json",
        version_id="approval-object-version-one",
        sha256="a" * 64,
    )

    return (
        ownership,
        admitted,
        clock,
        claim,
        progress,
        IngestionContracts(progress),
        contract,
        approval,
    )


def test_contract_is_associated_with_ingestion(
    contract_setup,
) -> None:
    _, admitted, _, claim, progress, contracts, contract, approval = (
        contract_setup
    )

    contracts.pin(
        claim,
        contract=contract,
        approval_reference=approval,
        update_id="contract-selected",
        expected_revision=0,
    )

    recorded = progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="CONTRACT",
    )["result_reference"]

    assert recorded["contract_id"] == contract.contract_id
    assert recorded["contract_version"] == "v1"
    assert recorded["contract_object_version_id"] == (
        "contract-object-version-one"
    )
    assert recorded["approval_object_version_id"] == (
        "approval-object-version-one"
    )


def test_contract_for_another_table_is_rejected(
    contract_setup,
) -> None:
    _, _, _, claim, _, contracts, contract, approval = contract_setup

    with pytest.raises(ValueError, match="contract_id"):
        contracts.pin(
            claim,
            contract=replace(
                contract,
                contract_id=str(uuid.uuid4()),
            ),
            approval_reference=approval,
            update_id="wrong-contract",
            expected_revision=0,
        )


def test_contract_path_must_match_version(
    contract_setup,
) -> None:
    _, _, _, claim, _, contracts, contract, approval = contract_setup

    with pytest.raises(ValueError, match="object key"):
        contracts.pin(
            claim,
            contract=replace(contract, contract_version="v2"),
            approval_reference=approval,
            update_id="wrong-version",
            expected_revision=0,
        )


def test_selected_contract_cannot_be_replaced(
    contract_setup,
) -> None:
    _, _, _, claim, _, contracts, contract, approval = contract_setup

    contracts.pin(
        claim,
        contract=contract,
        approval_reference=approval,
        update_id="contract-selected",
        expected_revision=0,
    )

    with pytest.raises(ProgressConflict, match="completed stage"):
        contracts.pin(
            claim,
            contract=replace(
                contract,
                contract_fingerprint="b" * 64,
            ),
            approval_reference=approval,
            update_id="replace-contract",
            expected_revision=1,
        )


def test_recovery_keeps_original_contract_association(
    contract_setup,
) -> None:
    ownership, admitted, clock, claim, _, contracts, contract, approval = (
        contract_setup
    )

    arguments = {
        "contract": contract,
        "approval_reference": approval,
        "update_id": "contract-selected",
        "expected_revision": 0,
    }

    original = contracts.pin(claim, **arguments)
    clock["now"] = claim.lease_expires_at

    recovered = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    assert contracts.pin(recovered, **arguments) == original


@pytest.mark.parametrize(
    "version",
    ["v0", "v01", "V1", "1"],
)
def test_invalid_contract_version_is_rejected(
    contract_setup,
    version,
) -> None:
    contract = contract_setup[-2]

    with pytest.raises(ValueError, match="contract_version"):
        replace(contract, contract_version=version)


def test_unversioned_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="versioned"):
        S3ObjectReference(
            bucket_name="example-evidence",
            object_key="results/result.json",
            version_id="null",
            sha256="a" * 64,
        )