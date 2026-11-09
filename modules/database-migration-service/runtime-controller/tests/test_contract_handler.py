from unittest.mock import Mock

import pytest

from ingestion_registry.contract_orchestration import (
    ContractOrchestrationBlocked,
)
from ingestion_registry.identity import TableIdentity

from dms_runtime.contract_factory import ContractRuntime
from dms_runtime.contract_handler import (
    ContractHandler,
    ContractRequestError,
)


@pytest.fixture
def contract_handler():
    coordinator = Mock()
    table = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="example",
    )
    runtime = ContractRuntime(
        table=table,
        orchestration=coordinator,
        lifecycle=Mock(),
        pinning=Mock(),
    )

    return {
        "handler": ContractHandler(runtime),
        "coordinator": coordinator,
        "table": table,
    }


@pytest.mark.parametrize(
    "operation,fields,method,arguments",
    [
        (
            "RESOLVE_CONTRACT",
            {},
            "resolve",
            {},
        ),
        (
            "PREPARE_CONTRACT",
            {"request_id": "request-one"},
            "prepare",
            {"request_id": "request-one"},
        ),
        (
            "ASSESS_CONTRACT",
            {
                "reservation_key": "CONTRACTVERSIONREQUEST#" + "a" * 64,
                "preparation_fingerprint": "b" * 64,
            },
            "assess",
            {
                "reservation_key": "CONTRACTVERSIONREQUEST#" + "a" * 64,
                "preparation_fingerprint": "b" * 64,
            },
        ),
        (
            "RESUME_CONTRACT",
            {
                "request_id": "request-one",
                "contract_version": "v3",
                "expected_request_fingerprint": "c" * 64,
            },
            "advance",
            {
                "request_id": "request-one",
                "contract_version": "v3",
                "expected_request_fingerprint": "c" * 64,
            },
        ),
    ],
)
def test_dispatches_using_runtime_selected_table(
    contract_handler,
    operation,
    fields,
    method,
    arguments,
):
    setup = contract_handler
    target = getattr(setup["coordinator"], method)
    target.return_value = {"status": "TEST_RESULT"}

    result = setup["handler"].handle(
        {
            "request_version": 1,
            "operation": operation,
            **fields,
        }
    )

    assert result == {"status": "TEST_RESULT"}
    target.assert_called_once_with(
        table=setup["table"],
        **arguments,
    )
    assert len(setup["coordinator"].mock_calls) == 1


@pytest.mark.parametrize("version", [True, 1.0, "1", 2, None])
def test_rejects_invalid_request_version(contract_handler, version):
    setup = contract_handler

    with pytest.raises(ContractRequestError, match="request_version"):
        setup["handler"].handle(
            {
                "request_version": version,
                "operation": "RESOLVE_CONTRACT",
            }
        )

    assert setup["coordinator"].mock_calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("table_name", "another_table"),
        ("source", "another-source"),
        ("validation_function_arn", "another-function"),
        ("policy", {"automatically_accept_compatible": True}),
        ("reviewer", "someone"),
        ("approval_reference", {}),
    ],
)
def test_request_cannot_override_trusted_context(
    contract_handler,
    field,
    value,
):
    setup = contract_handler

    with pytest.raises(ContractRequestError, match="exactly"):
        setup["handler"].handle(
            {
                "request_version": 1,
                "operation": "RESOLVE_CONTRACT",
                field: value,
            }
        )

    assert setup["coordinator"].mock_calls == []


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        {},
        {
            "request_version": 1,
            "operation": "APPROVE_CONTRACT",
        },
        {
            "request_version": 1,
            "operation": "PREPARE_CONTRACT",
        },
        {
            "request_version": 1,
            "operation": "ASSESS_CONTRACT",
            "reservation_key": "INGESTION#another-operation",
            "preparation_fingerprint": "a" * 64,
        },
        {
            "request_version": 1,
            "operation": "ASSESS_CONTRACT",
            "reservation_key": "CONTRACTVERSIONREQUEST#" + "a" * 64,
            "preparation_fingerprint": "invalid",
        },
        {
            "request_version": 1,
            "operation": "RESUME_CONTRACT",
            "request_id": "request-one",
            "contract_version": "v0",
            "expected_request_fingerprint": "a" * 64,
        },
    ],
)
def test_invalid_request_has_no_coordinator_side_effects(
    contract_handler,
    event,
):
    setup = contract_handler

    with pytest.raises(ContractRequestError):
        setup["handler"].handle(event)

    assert setup["coordinator"].mock_calls == []


@pytest.mark.parametrize("request_id", ["", " request-one", "é" * 129])
def test_request_id_limits_use_utf8_bytes(contract_handler, request_id):
    setup = contract_handler

    with pytest.raises(ContractRequestError, match="request_id"):
        setup["handler"].handle(
            {
                "request_version": 1,
                "operation": "PREPARE_CONTRACT",
                "request_id": request_id,
            }
        )

    assert setup["coordinator"].mock_calls == []


def test_resume_preserves_current_decision_checks(contract_handler):
    setup = contract_handler
    setup["coordinator"].advance.side_effect = (
        ContractOrchestrationBlocked("Approval changed")
    )

    with pytest.raises(
        ContractOrchestrationBlocked,
        match="Approval changed",
    ):
        setup["handler"].handle(
            {
                "request_version": 1,
                "operation": "RESUME_CONTRACT",
                "request_id": "request-one",
                "contract_version": "v3",
                "expected_request_fingerprint": "a" * 64,
            }
        )


def test_handler_requires_contract_runtime():
    with pytest.raises(ValueError, match="ContractRuntime"):
        ContractHandler(None)