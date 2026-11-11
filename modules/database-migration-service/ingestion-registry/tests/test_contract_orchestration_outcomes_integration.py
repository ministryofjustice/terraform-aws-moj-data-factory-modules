import copy
from dataclasses import asdict

import pytest

from contract_integration_support import encode, store_reference

from test_contract_orchestration_integration import (
    orchestration_setup as orchestration_setup,
    advance,
    approve_request,
)


def submit_case(
    setup,
    *,
    request_id,
    contract_version,
    validation,
    compatibility,
    baseline=None,
):
    contract = copy.deepcopy(setup["contract"])
    contract["contract_version"] = contract_version

    candidate = store_reference(
        setup["s3"],
        "test-contract-candidates",
        f"candidates/{request_id}/contract.avsc",
        encode(contract),
    )

    document = {
        "validation_result_version": 1,
        "request_id": request_id,
        "identity": {
            field: contract[field]
            for field in (
                "source",
                "database_name",
                "schema_name",
                "table_name",
                "contract_version",
            )
        },
        "candidate_reference": asdict(candidate),
        "baseline_reference": (
            None
            if baseline is None
            else baseline["registered_contract"]["object_reference"]
        ),
        "assessor_revision": "test-assessor-v1",
        "validation": validation,
        "compatibility": compatibility,
    }

    evidence = store_reference(
        setup["s3"],
        "test-contract-evidence",
        f"validation/{request_id}/result.json",
        encode(document),
    )

    coordinator = setup["coordinator"]

    return coordinator.assessment_admission.submit(
        table=setup["table"],
        request_id=request_id,
        contract_version=contract_version,
        candidate_reference=candidate,
        evidence_reference=evidence,
        policy=coordinator.trusted_policy,
    )


def advance_case(setup, request, contract_version):
    return setup["coordinator"].advance(
        table=setup["table"],
        request_id=request["request_id"],
        contract_version=contract_version,
        expected_request_fingerprint=request["request_fingerprint"],
    )


def establish_baseline(setup):
    approve_request(setup)
    return advance(setup)["selection"]


@pytest.mark.parametrize(
    "validation,compatibility,reason",
    [
        ("INVALID", "UNSUPPORTED", "INVALID_CONTRACT"),
        ("UNSUPPORTED", "UNSUPPORTED", "VALIDATION_UNSUPPORTED"),
        ("VALID", "UNSUPPORTED", "COMPATIBILITY_UNSUPPORTED"),
    ],
)
def test_blocked_assessment_does_not_register_or_activate(
    orchestration_setup,
    validation,
    compatibility,
    reason,
):
    setup = orchestration_setup

    request = submit_case(
        setup,
        request_id="blocked-assessment",
        contract_version="v1",
        validation=validation,
        compatibility=compatibility,
    )

    result = advance_case(setup, request, "v1")

    assert result["status"] == "BLOCKED"
    assert result["reason"] == reason
    assert setup["invocation"].calls == 0

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) is None


def test_rejected_review_does_not_progress(orchestration_setup):
    setup = orchestration_setup
    request = setup["orchestration_request"]

    evidence = store_reference(
        setup["s3"],
        "test-contract-evidence",
        "approvals/orchestration-request/rejected.json",
        encode(
            {
                "approval_version": 1,
                "table_identity": request["table_identity"],
                "request_id": request["request_id"],
                "request_fingerprint": request["request_fingerprint"],
                "review_id": "rejected-review",
                "reviewer": "test-authorised-reviewer",
                "outcome": "REJECTED",
                "assessment": request["assessment"],
                "policy": request["policy"],
            }
        ),
    )

    setup["lifecycle"].record_review(
        table=setup["table"],
        request_id=request["request_id"],
        review_id="rejected-review",
        reviewer="test-authorised-reviewer",
        outcome="REJECTED",
        expected_revision=1,
        expected_request_fingerprint=request["request_fingerprint"],
        evidence_reference=evidence,
        authorise=setup["authorise"],
    )

    result = advance(setup)

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "REVIEW_REJECTED"
    assert setup["invocation"].calls == 0


def test_no_change_reuses_baseline_without_registering_candidate(
    orchestration_setup,
):
    setup = orchestration_setup
    baseline = establish_baseline(setup)
    calls_before = setup["invocation"].calls

    request = submit_case(
        setup,
        request_id="unchanged-assessment",
        contract_version="v2",
        validation="VALID",
        compatibility="NO_CHANGE",
        baseline=baseline,
    )

    result = advance_case(setup, request, "v2")

    assert result["status"] == "CONTRACT_READY"
    assert result["action"] == "REUSED"
    assert result["selection"] == baseline
    assert result["selection"]["registered_contract"][
        "contract_version"
    ] == "v1"

    assert advance_case(setup, request, "v2") == result
    assert setup["invocation"].calls == calls_before
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 1


@pytest.mark.parametrize(
    "compatibility,reason",
    [
        ("COMPATIBLE", "COMPATIBLE_CHANGE_REQUIRES_APPROVAL"),
        ("BREAKING", "BREAKING_CHANGE_REQUIRES_APPROVAL"),
    ],
)
def test_changes_wait_for_required_review(
    orchestration_setup,
    compatibility,
    reason,
):
    setup = orchestration_setup
    baseline = establish_baseline(setup)
    calls_before = setup["invocation"].calls

    request = submit_case(
        setup,
        request_id="changed-assessment",
        contract_version="v2",
        validation="VALID",
        compatibility=compatibility,
        baseline=baseline,
    )

    result = advance_case(setup, request, "v2")

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reason"] == reason
    assert setup["invocation"].calls == calls_before

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == baseline