import copy

import pytest
from botocore.exceptions import ReadTimeoutError

from contract_integration_support import (
    ACCOUNT_ID,
    approve,
    encode,
    prepare_approved_contract_change,
    store_reference,
)
from ingestion_registry.contract_activation import ContractActivation
from ingestion_registry.contract_lookup import CurrentContractLookup
from ingestion_registry.contract_policy import (
    CompatibilityOutcome,
    ContractAssessment,
    ContractPolicy,
    ValidationOutcome,
)
from ingestion_registry.contract_reuse import (
    ContractReuse,
    ContractReuseBlocked,
)
from ingestion_registry.contract_verification import (
    RegisteredContractVerifier,
)
from ingestion_registry.references import S3ObjectReference


@pytest.fixture
def reuse_setup(registration_integration):
    setup = registration_integration

    approve(setup)

    activation = ContractActivation(
        completion=setup["completion"],
        lifecycle=setup["lifecycle"],
    )

    activation.activate(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )

    verifier = RegisteredContractVerifier(
        contract_table=setup["registration_table"],
        s3_client=setup["s3"],
        bucket_name="test-approved-contracts",
        bucket_owner_account_id=ACCOUNT_ID,
    )

    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=setup["promoter"].approval_gate,
        verifier=verifier,
    )

    selection = lookup.resolve(
        table=setup["table"],
    )["selection"]

    candidate_contract = copy.deepcopy(setup["contract"])
    candidate_contract["contract_version"] = "v2"

    candidate = store_reference(
        setup["s3"],
        "test-contract-candidates",
        "candidates/no-change/contract.avsc",
        encode(candidate_contract),
    )

    evidence = store_reference(
        setup["s3"],
        "test-contract-evidence",
        "validation/no-change/result.json",
        encode(
            {
                "validation": "VALID",
                "compatibility": "NO_CHANGE",
            }
        ),
    )

    request = setup["lifecycle"].submit_assessment(
        table=setup["table"],
        request_id="no-change-request",
        assessment=ContractAssessment(
            validation=ValidationOutcome.VALID,
            compatibility=CompatibilityOutcome.NO_CHANGE,
            candidate_reference=candidate,
            baseline_reference=S3ObjectReference(
                **selection["registered_contract"][
                    "object_reference"
                ]
            ),
            evidence_reference=evidence,
        ),
        policy=ContractPolicy(revision="policy-v1"),
    )

    service = ContractReuse(
        lifecycle=setup["lifecycle"],
        current_lookup=lookup,
        contract_verifier=verifier,
    )

    return {
        **setup,
        "activation": activation,
        "lookup": lookup,
        "selection": selection,
        "reuse_request": request,
        "reuse": service,
    }


def complete_reuse(setup, **changes):
    arguments = {
        "table": setup["table"],
        "request_id": "no-change-request",
        "expected_request_fingerprint": setup["reuse_request"][
            "request_fingerprint"
        ],
    }
    arguments.update(changes)

    return setup["reuse"].complete(**arguments)


def activate_next_contract(setup):
    prepare_approved_contract_change(
        setup,
        request_id="next-contract-request",
        contract_version="v2",
        baseline_selection=setup["selection"],
    )

    setup["activation"].activate(
        table=setup["table"],
        request_id="next-contract-request",
        contract_version="v2",
    )


def test_no_change_reuses_baseline_without_registration(
    reuse_setup,
):
    setup = reuse_setup
    calls_before = setup["invocation"].calls

    result = complete_reuse(setup)

    assert result["status"] == "REUSED"
    assert result["registered_contract"] == (
        setup["selection"]["registered_contract"]
    )
    assert result["registered_contract"]["contract_version"] == "v1"

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="no-change-request",
    )

    assert request["status"] == "REUSED"
    assert request["revision"] == 2

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 1
    assert setup["invocation"].calls == calls_before

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == setup["selection"]

    assert setup["registry"]._read(
        request["pk"],
        "REGISTRATION",
    ) is None
    assert setup["registry"]._read(
        request["pk"],
        "ACTIVATION",
    ) is None


def test_duplicate_reuse_returns_original_result(reuse_setup):
    setup = reuse_setup

    original = complete_reuse(setup)

    assert complete_reuse(setup) == original

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="no-change-request",
    )
    assert request["revision"] == 2


def test_changed_baseline_blocks_pending_reuse(reuse_setup):
    setup = reuse_setup
    activate_next_contract(setup)

    with pytest.raises(ContractReuseBlocked, match="baseline changed"):
        complete_reuse(setup)

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="no-change-request",
    )
    assert request["status"] == "REUSE_PENDING"
    assert setup["registry"]._read(
        request["pk"],
        "REUSE",
    ) is None


def test_completed_reuse_keeps_original_selection(reuse_setup):
    setup = reuse_setup
    original = complete_reuse(setup)

    activate_next_contract(setup)

    repeated = complete_reuse(setup)

    assert repeated == original
    assert repeated["registered_contract"]["contract_version"] == "v1"

    current = setup["lookup"].resolve(
        table=setup["table"],
    )["selection"]
    assert current["registered_contract"]["contract_version"] == "v2"


def test_lost_reuse_response_returns_original_result(
    reuse_setup,
    monkeypatch,
):
    setup = reuse_setup
    client = setup["registry"].client
    original_write = client.transact_write_items

    def write_then_lose_response(**kwargs):
        original_write(**kwargs)
        raise ReadTimeoutError(
            endpoint_url="https://dynamodb.eu-west-2.amazonaws.com"
        )

    monkeypatch.setattr(
        client,
        "transact_write_items",
        write_then_lose_response,
    )

    with pytest.raises(ReadTimeoutError):
        complete_reuse(setup)

    stored = setup["registry"]._read(
        setup["reuse_request"]["pk"],
        "REUSE",
    )["result"]

    monkeypatch.setattr(
        client,
        "transact_write_items",
        original_write,
    )

    assert complete_reuse(setup) == stored


def test_wrong_assessment_fingerprint_blocks_reuse(reuse_setup):
    with pytest.raises(ContractReuseBlocked, match="exact assessment"):
        complete_reuse(
            reuse_setup,
            expected_request_fingerprint="f" * 64,
        )


def test_baseline_change_during_transaction_blocks_reuse(
    reuse_setup,
    monkeypatch,
):
    setup = reuse_setup
    client = setup["registry"].client
    original_write = client.transact_write_items

    def change_baseline_before_write(**kwargs):
        monkeypatch.setattr(
            client,
            "transact_write_items",
            original_write,
        )

        activate_next_contract(setup)
        return original_write(**kwargs)

    monkeypatch.setattr(
        client,
        "transact_write_items",
        change_baseline_before_write,
    )

    with pytest.raises(ContractReuseBlocked, match="changed"):
        complete_reuse(setup)

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="no-change-request",
    )

    assert request["status"] == "REUSE_PENDING"
    assert setup["registry"]._read(
        request["pk"],
        "REUSE",
    ) is None