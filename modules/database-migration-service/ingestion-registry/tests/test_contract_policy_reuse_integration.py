import copy

import pytest

from contract_integration_support import encode, store_reference
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
from ingestion_registry.references import S3ObjectReference

from test_contract_policy_lookup_integration import (
    policy_lookup as policy_lookup,
)
from test_contract_policy_activation_integration import (
    policy_activation as policy_activation,
    activate_later_human_approved_contract,
)
from test_contract_policy_registration_completion_integration import (
    policy_completion as policy_completion,
)
from test_contract_policy_authorisation_integration import (
    policy_authorisation_setup as policy_authorisation_setup,
)
from test_contract_reuse_integration import (
    reuse_setup as reuse_setup,
)


@pytest.fixture
def policy_reuse(policy_lookup):
    setup = policy_lookup
    selection = setup["activated"]["selection"]

    contract = copy.deepcopy(setup["contract"])
    contract["contract_version"] = "v3"

    candidate = store_reference(
        setup["s3"],
        "test-contract-candidates",
        "candidates/policy-no-change/contract.avsc",
        encode(contract),
    )

    evidence = store_reference(
        setup["s3"],
        "test-contract-evidence",
        "validation/policy-no-change/result.json",
        encode(
            {
                "validation": "VALID",
                "compatibility": "NO_CHANGE",
            }
        ),
    )

    request = setup["lifecycle"].submit_assessment(
        table=setup["table"],
        request_id="policy-no-change-request",
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
        policy=ContractPolicy(revision="no-change-policy-v1"),
    )

    reuse = ContractReuse(
        lifecycle=setup["lifecycle"],
        current_lookup=setup["policy_lookup"],
        contract_verifier=setup["reuse"].verifier,
    )

    return {
        **setup,
        "policy_baseline": selection,
        "policy_reuse_request": request,
        "policy_reuse": reuse,
    }


def complete(setup):
    return setup["policy_reuse"].complete(
        table=setup["table"],
        request_id="policy-no-change-request",
        expected_request_fingerprint=setup["policy_reuse_request"][
            "request_fingerprint"
        ],
    )


def test_no_change_reuses_policy_authorised_baseline(policy_reuse):
    setup = policy_reuse
    calls_before = setup["invocation"].calls

    result = complete(setup)

    assert result["status"] == "REUSED"
    assert result["registered_contract"]["contract_version"] == "v2"
    assert result["selection"]["authorisation"]["kind"] == "POLICY"
    assert "approval_reference" not in result["selection"]

    assert setup["invocation"].calls == calls_before
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 2

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == setup["policy_baseline"]


def test_duplicate_policy_baseline_reuse_returns_original_result(
    policy_reuse,
):
    setup = policy_reuse
    original = complete(setup)

    assert complete(setup) == original


def test_pending_reuse_rejects_a_newer_baseline(policy_reuse):
    setup = policy_reuse

    activate_later_human_approved_contract(
        setup,
        setup["policy_baseline"],
    )

    with pytest.raises(ContractReuseBlocked, match="baseline changed"):
        complete(setup)


def test_completed_reuse_retains_original_policy_baseline(policy_reuse):
    setup = policy_reuse
    original = complete(setup)

    later = activate_later_human_approved_contract(
        setup,
        setup["policy_baseline"],
    )

    assert complete(setup) == original

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == later["selection"]


def test_policy_change_during_reuse_blocks_completion(
    policy_reuse,
    monkeypatch,
):
    setup = policy_reuse
    client = setup["registry"].client
    original_write = client.transact_write_items

    def change_policy_before_reuse(**kwargs):
        writes_reuse = any(
            action.get("Put", {})
            .get("Item", {})
            .get("sk", {})
            .get("S") == "REUSE"
            for action in kwargs["TransactItems"]
        )

        if writes_reuse:
            request = setup["lifecycle"].read(
                table=setup["table"],
                request_id="policy-request",
            )
            request["status"] = "BLOCKED"

            client.put_item(
                TableName=setup["registry"].table_name,
                Item=setup["registry"]._encode(request),
            )

        return original_write(**kwargs)

    monkeypatch.setattr(
        client,
        "transact_write_items",
        change_policy_before_reuse,
    )

    with pytest.raises(ContractReuseBlocked, match="changed"):
        complete(setup)

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="policy-no-change-request",
    )

    assert request["status"] == "REUSE_PENDING"
    assert setup["registry"]._read(
        request["pk"],
        "REUSE",
    ) is None