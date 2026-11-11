from unittest.mock import Mock

import pytest

from ingestion_registry.contract_orchestration import (
    ContractOrchestration,
    ContractOrchestrationBlocked,
)
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.contract_validation import ContractValidationFailed
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


@pytest.fixture
def validation_orchestration():
    registry = Mock()

    lifecycle = Mock(registry=registry)
    lookup = Mock(registry=registry)
    admission = Mock(
        lifecycle=lifecycle,
        assessor_revision="test-assessor-v1",
    )
    reuse = Mock(registry=registry)
    activation = Mock(registry=registry)

    completion = Mock()
    generated_assessment = Mock(
        registry=registry,
        completion=completion,
    )
    assessment_requests = Mock(
        registry=registry,
        completion=completion,
        assessor_revision="test-assessor-v1",
    )

    evidence = S3ObjectReference(
        bucket_name="test-contract-evidence",
        object_key="validation/request-one/result.json",
        version_id="evidence-version-one",
        sha256="a" * 64,
    )

    validator = Mock(assessment_requests=assessment_requests)
    validator.assess.return_value = {
        "status": "ASSESSMENT_EVIDENCE_RETURNED",
        "request_id": "request-one",
        "evidence_reference": evidence,
    }

    constructor = {
        "lifecycle": lifecycle,
        "lookup": lookup,
        "assessment_admission": admission,
        "reuse": reuse,
        "activation": activation,
        "trusted_policy": ContractPolicy(revision="policy-v1"),
        "generated_assessment": generated_assessment,
        "assessment_requests": assessment_requests,
        "validator": validator,
    }

    coordinator = ContractOrchestration(**constructor)

    return {
        "coordinator": coordinator,
        "constructor": constructor,
        "validator": validator,
        "evidence": evidence,
        "arguments": {
            "table": TableIdentity(
                environment="development",
                source="sandbox-postgres",
                database_name="dms_test",
                schema_name="public",
                table_name="example",
            ),
            "reservation_key": "CONTRACTVERSIONREQUEST#" + "b" * 64,
            "preparation_fingerprint": "c" * 64,
        },
    }


def test_validation_evidence_is_passed_to_existing_admission(
    validation_orchestration,
    monkeypatch,
):
    setup = validation_orchestration
    coordinator = setup["coordinator"]

    complete = Mock(
        return_value={
            "status": "REVIEW_REQUIRED",
            "request_id": "request-one",
        }
    )
    monkeypatch.setattr(coordinator, "complete_assessment", complete)

    result = coordinator.assess(**setup["arguments"])

    setup["validator"].assess.assert_called_once_with(
        **setup["arguments"]
    )
    complete.assert_called_once_with(
        **setup["arguments"],
        evidence_reference=setup["evidence"],
    )
    assert result == complete.return_value


def test_invocation_failure_does_not_enter_admission(
    validation_orchestration,
    monkeypatch,
):
    setup = validation_orchestration
    coordinator = setup["coordinator"]
    setup["validator"].assess.side_effect = ContractValidationFailed(
        "Invocation failed"
    )

    complete = Mock()
    monkeypatch.setattr(coordinator, "complete_assessment", complete)

    with pytest.raises(ContractValidationFailed):
        coordinator.assess(**setup["arguments"])

    complete.assert_not_called()


def test_admission_failure_propagates(
    validation_orchestration,
    monkeypatch,
):
    setup = validation_orchestration
    coordinator = setup["coordinator"]

    complete = Mock(side_effect=ValueError("Evidence does not match"))
    monkeypatch.setattr(coordinator, "complete_assessment", complete)

    with pytest.raises(ValueError, match="Evidence does not match"):
        coordinator.assess(**setup["arguments"])


@pytest.mark.parametrize(
    "result",
    [
        {"status": "FAILED"},
        {
            "status": "ASSESSMENT_EVIDENCE_RETURNED",
            "evidence_reference": {},
        },
    ],
)
def test_invalid_adapter_result_does_not_enter_admission(
    validation_orchestration,
    monkeypatch,
    result,
):
    setup = validation_orchestration
    coordinator = setup["coordinator"]
    setup["validator"].assess.return_value = result

    complete = Mock()
    monkeypatch.setattr(coordinator, "complete_assessment", complete)

    with pytest.raises(
        ContractOrchestrationBlocked,
        match="did not return",
    ):
        coordinator.assess(**setup["arguments"])

    complete.assert_not_called()


def test_validation_requires_configuration(validation_orchestration):
    setup = validation_orchestration
    constructor = {**setup["constructor"], "validator": None}
    coordinator = ContractOrchestration(**constructor)

    with pytest.raises(
        ContractOrchestrationBlocked,
        match="not configured",
    ):
        coordinator.assess(**setup["arguments"])


def test_validator_requires_same_assessment_request_service(
    validation_orchestration,
):
    setup = validation_orchestration
    validator = Mock(assessment_requests=Mock())

    with pytest.raises(ValueError, match="same assessment request"):
        ContractOrchestration(
            **{
                **setup["constructor"],
                "validator": validator,
            }
        )


def test_validator_requires_generated_assessment_admission(
    validation_orchestration,
):
    setup = validation_orchestration

    with pytest.raises(ValueError, match="requires assessment requests"):
        ContractOrchestration(
            **{
                **setup["constructor"],
                "generated_assessment": None,
            }
        )