import sys
from pathlib import Path

import pytest

from ingestion_registry.contract_orchestration import (
    ContractOrchestrationBlocked,
)
from ingestion_registry.contract_pinning import CurrentContractPinning
from ingestion_registry.contracts import IngestionContracts
from ingestion_registry.progress import IngestionProgress

from dms_runtime.contract_factory import ContractRuntime
from dms_runtime.contract_handler import (
    ContractHandler,
    ContractRequestError,
)


REGISTRY_TESTS = (
    Path(__file__).resolve().parents[2]
    / "ingestion-registry"
    / "tests"
)

if str(REGISTRY_TESTS) not in sys.path:
    sys.path.insert(0, str(REGISTRY_TESTS))


from contract_integration_support import (
    promotion_integration as promotion_integration,
    registration_integration as registration_integration,
)
from test_contract_orchestration_validation_integration import (
    validated_orchestration as validated_orchestration,
)
from test_contract_orchestration_generation_integration import (
    connected_orchestration as connected_orchestration,
)
from test_contract_preparation_integration import (
    preparation_setup as preparation_setup,
)
from test_contract_generation_completion_integration import (
    generation_completion as generation_completion,
)
from test_candidate_generation_integration import (
    candidate_generation as candidate_generation,
    generation_adapter as generation_adapter,
)
from test_contract_orchestration_integration import approve_request


@pytest.fixture
def lifecycle_handler(validated_orchestration):
    setup = validated_orchestration
    coordinator = setup["coordinator"]

    runtime = ContractRuntime(
        table=setup["table"],
        orchestration=coordinator,
        lifecycle=setup["lifecycle"],
        pinning=CurrentContractPinning(
            lookup=coordinator.lookup,
            contracts=IngestionContracts(
                IngestionProgress(setup["registry"])
            ),
        ),
    )

    return {
        **setup,
        "handler": ContractHandler(runtime),
    }


def handle(setup, operation, **fields):
    return setup["handler"].handle(
        {
            "request_version": 1,
            "operation": operation,
            **fields,
        }
    )


def prepare_and_assess(setup):
    prepared = handle(
        setup,
        "PREPARE_CONTRACT",
        request_id="handler-contract-request",
    )

    waiting = handle(
        setup,
        "ASSESS_CONTRACT",
        reservation_key=prepared["reservation_key"],
        preparation_fingerprint=prepared["preparation_fingerprint"],
    )

    return prepared, waiting


def resume(setup, prepared, waiting, **changes):
    fields = {
        "request_id": prepared["request_id"],
        "contract_version": prepared["contract_version"],
        "expected_request_fingerprint": waiting["request_fingerprint"],
    }
    fields.update(changes)

    return handle(setup, "RESUME_CONTRACT", **fields)


def registration_count(setup):
    return setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"]


def test_handler_prepares_assesses_waits_and_resumes_after_approval(
    lifecycle_handler,
):
    setup = lifecycle_handler

    assert handle(setup, "RESOLVE_CONTRACT")["status"] == (
        "CONTRACT_REQUIRED"
    )

    prepared, waiting = prepare_and_assess(setup)

    assert prepared["status"] == "CANDIDATE_READY"
    assert waiting["status"] == "REVIEW_REQUIRED"
    assert registration_count(setup) == 0

    assert resume(setup, prepared, waiting) == waiting
    assert registration_count(setup) == 0

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    )
    approve_request(
        {
            **setup,
            "orchestration_request": request,
        }
    )

    ready = resume(setup, prepared, waiting)

    assert ready["status"] == "CONTRACT_READY"
    assert ready["action"] == "ACTIVATED"
    assert ready["selection"]["registered_contract"][
        "contract_version"
    ] == prepared["contract_version"]
    assert registration_count(setup) == 1

    resolved = handle(setup, "RESOLVE_CONTRACT")
    assert resolved["selection"] == ready["selection"]

    assert resume(setup, prepared, waiting) == ready
    assert registration_count(setup) == 1


def test_handler_resume_rejects_different_request_fingerprint(
    lifecycle_handler,
):
    setup = lifecycle_handler
    prepared, waiting = prepare_and_assess(setup)

    with pytest.raises(
        ContractOrchestrationBlocked,
        match="exact contract request",
    ):
        resume(
            setup,
            prepared,
            waiting,
            expected_request_fingerprint="f" * 64,
        )

    assert registration_count(setup) == 0
    assert handle(setup, "RESOLVE_CONTRACT")["status"] == (
        "CONTRACT_REQUIRED"
    )


def test_handler_cannot_select_another_table_before_generation(
    lifecycle_handler,
):
    setup = lifecycle_handler
    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    with pytest.raises(ContractRequestError, match="exactly"):
        handle(
            setup,
            "PREPARE_CONTRACT",
            request_id="another-table-request",
            table_name="another_table",
        )

    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()
    assert registration_count(setup) == 0