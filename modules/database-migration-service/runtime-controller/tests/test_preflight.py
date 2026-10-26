import io
import json
from unittest.mock import Mock

import pytest

from dms_runtime.preflight import SourcePreflight


INSTANCE = "arn:aws:dms:eu-west-2:111122223333:rep:INSTANCE"
ENDPOINT = "arn:aws:dms:eu-west-2:111122223333:endpoint:SOURCE"
SYNC = (
    "arn:aws:lambda:eu-west-2:111122223333:"
    "function:credential-sync"
)


@pytest.fixture
def setup():
    dms = Mock()
    lambdas = Mock()
    sleep = Mock()

    dms.describe_connections.return_value = connection("successful")

    return (
        dms,
        lambdas,
        sleep,
        SourcePreflight(dms, lambdas, sleep=sleep),
    )


def connection(status):
    return {
        "Connections": [
            {
                "EndpointArn": ENDPOINT,
                "ReplicationInstanceArn": INSTANCE,
                "Status": status,
            }
        ]
    }


def run(preflight, **kwargs):
    return preflight.run(
        replication_instance_arn=INSTANCE,
        source_endpoint_arn=ENDPOINT,
        **kwargs,
    )


def test_oracle_preflight_skips_optional_sync(setup):
    dms, lambdas, sleep, preflight = setup

    result = run(preflight)

    assert result["credentialsSynchronised"] is False
    assert result["connectionStatus"] == "successful"

    lambdas.invoke.assert_not_called()
    dms.test_connection.assert_called_once_with(
        ReplicationInstanceArn=INSTANCE,
        EndpointArn=ENDPOINT,
    )
    dms.start_replication_task.assert_not_called()
    sleep.assert_not_called()


def test_postgres_sync_happens_before_connection_test(setup):
    dms, lambdas, _, preflight = setup

    payload = io.BytesIO(
        json.dumps({"statusCode": 200}).encode()
    )
    lambdas.invoke.return_value = {"Payload": payload}

    events = Mock()
    events.attach_mock(lambdas.invoke, "sync")
    events.attach_mock(dms.test_connection, "connection")

    result = run(
        preflight,
        credential_sync_function_arn=SYNC,
    )

    assert result["credentialsSynchronised"] is True
    assert [call[0] for call in events.mock_calls] == [
        "sync",
        "connection",
    ]
    assert payload.closed

    assert json.loads(
        lambdas.invoke.call_args.kwargs["Payload"]
    ) == {"trigger": "preflight"}


@pytest.mark.parametrize(
    "body",
    [
        b'{"statusCode":500}',
        b"{}",
        b"[]",
        b"not-json",
    ],
)
def test_unsuccessful_sync_prevents_connection_test(setup, body):
    dms, lambdas, _, preflight = setup

    payload = io.BytesIO(body)
    lambdas.invoke.return_value = {"Payload": payload}

    with pytest.raises(RuntimeError):
        run(
            preflight,
            credential_sync_function_arn=SYNC,
        )

    assert payload.closed
    dms.test_connection.assert_not_called()


def test_lambda_error_prevents_connection_test(setup):
    dms, lambdas, _, preflight = setup

    payload = io.BytesIO(b"{}")
    lambdas.invoke.return_value = {
        "Payload": payload,
        "FunctionError": "Unhandled",
    }

    with pytest.raises(
        RuntimeError,
        match="synchronisation failed",
    ):
        run(
            preflight,
            credential_sync_function_arn=SYNC,
        )

    assert payload.closed
    dms.test_connection.assert_not_called()


def test_testing_then_success_polls_with_bound(setup):
    dms, _, sleep, preflight = setup

    dms.describe_connections.side_effect = [
        connection("testing"),
        connection("successful"),
    ]

    result = run(preflight)

    assert result["pollAttempts"] == 2
    sleep.assert_called_once_with(10)


def test_timeout_does_not_sleep_after_final_attempt(setup):
    dms, _, sleep, preflight = setup
    dms.describe_connections.return_value = connection("testing")

    with pytest.raises(TimeoutError):
        run(
            preflight,
            max_poll_attempts=2,
        )

    assert dms.describe_connections.call_count == 2
    sleep.assert_called_once_with(10)


@pytest.mark.parametrize(
    "status",
    ["failed", "deleting", None],
)
def test_unsuccessful_connection_is_rejected(setup, status):
    dms, _, sleep, preflight = setup
    dms.describe_connections.return_value = connection(status)

    with pytest.raises(RuntimeError):
        run(preflight)

    dms.start_replication_task.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "rows",
    [
        [],
        connection("successful")["Connections"] * 2,
    ],
)
def test_missing_or_ambiguous_connection_is_rejected(setup, rows):
    dms, _, _, preflight = setup
    dms.describe_connections.return_value = {
        "Connections": rows,
    }

    with pytest.raises(RuntimeError, match="exactly one"):
        run(preflight)


def test_different_endpoint_is_rejected(setup):
    dms, _, _, preflight = setup

    response = connection("successful")
    response["Connections"][0]["EndpointArn"] = "another-endpoint"
    dms.describe_connections.return_value = response

    with pytest.raises(
        RuntimeError,
        match="different source connection",
    ):
        run(preflight)


@pytest.mark.parametrize(
    "attempts",
    [0, 31, True],
)
def test_invalid_poll_limit_is_rejected_before_aws_calls(
    setup,
    attempts,
):
    dms, _, _, preflight = setup

    with pytest.raises(ValueError, match="max_poll_attempts"):
        run(
            preflight,
            max_poll_attempts=attempts,
        )

    dms.test_connection.assert_not_called()