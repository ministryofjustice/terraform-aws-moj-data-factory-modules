import pytest
from botocore.exceptions import ReadTimeoutError

from contract_integration_support import (
    prepare_approved_contract_change,
)
from ingestion_registry.contract_activation import (
    ContractActivation,
    ContractActivationConflict,
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
def policy_activation(policy_completion):
    setup = policy_completion

    activation = ContractActivation(
        completion=setup["policy_completion"],
        lifecycle=setup["lifecycle"],
    )

    return {**setup, "policy_activation": activation}


def activate(setup):
    return setup["policy_activation"].activate(
        table=setup["table"],
        request_id="policy-request",
        contract_version="v2",
    )


def activate_later_human_approved_contract(setup, baseline):
    prepare_approved_contract_change(
        setup,
        request_id="later-human-request",
        contract_version="v3",
        baseline_selection=baseline,
    )

    return setup["activation"].activate(
        table=setup["table"],
        request_id="later-human-request",
        contract_version="v3",
    )


def test_policy_activation_records_explicit_authorisation(
    policy_activation,
):
    setup = policy_activation

    result = activate(setup)
    selection = result["selection"]

    assert selection["registered_contract"]["contract_version"] == "v2"
    assert selection["authorisation"]["kind"] == "POLICY"
    assert "approval_reference" not in selection

    assert setup["policy_activation"].read_current(
        table=setup["table"],
    ) == selection

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="policy-request",
    )
    assert request["status"] == "POLICY_ACCEPTED"
    assert "review" not in request


def test_duplicate_policy_activation_returns_original_result(
    policy_activation,
):
    setup = policy_activation

    original = activate(setup)
    calls_after_first = setup["invocation"].calls

    assert activate(setup) == original
    assert setup["invocation"].calls == calls_after_first


def test_stale_policy_baseline_cannot_replace_current_contract(
    policy_activation,
):
    setup = policy_activation

    later = activate_later_human_approved_contract(
        setup,
        setup["selection"],
    )

    with pytest.raises(
        ContractActivationConflict,
        match="baseline is no longer current",
    ):
        activate(setup)

    assert setup["policy_activation"].read_current(
        table=setup["table"],
    ) == later["selection"]


def test_policy_retry_does_not_restore_an_older_current_contract(
    policy_activation,
):
    setup = policy_activation

    original = activate(setup)

    later = activate_later_human_approved_contract(
        setup,
        original["selection"],
    )

    assert activate(setup) == original

    assert setup["policy_activation"].read_current(
        table=setup["table"],
    ) == later["selection"]


def test_lost_policy_activation_response_reconciles_original_result(
    policy_activation,
    monkeypatch,
):
    setup = policy_activation
    client = setup["registry"].client
    original_write = client.transact_write_items
    request_key = setup["policy_request"]["pk"]

    def writes_policy_activation(kwargs):
        return any(
            action.get("Put", {})
            .get("Item", {})
            .get("sk", {})
            .get("S") == "ACTIVATION"
            and action.get("Put", {})
            .get("Item", {})
            .get("pk", {})
            .get("S") == request_key
            for action in kwargs["TransactItems"]
        )

    def write_then_lose_response(**kwargs):
        response = original_write(**kwargs)

        if writes_policy_activation(kwargs):
            raise ReadTimeoutError(
                endpoint_url="https://dynamodb.eu-west-2.amazonaws.com"
            )

        return response

    monkeypatch.setattr(
        client,
        "transact_write_items",
        write_then_lose_response,
    )

    with pytest.raises(ReadTimeoutError):
        activate(setup)

    stored = setup["registry"]._read(
        request_key,
        "ACTIVATION",
    )
    assert stored is not None

    monkeypatch.setattr(
        client,
        "transact_write_items",
        original_write,
    )

    assert activate(setup) == stored