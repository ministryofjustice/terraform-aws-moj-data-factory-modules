import json
from decimal import Decimal

import pytest

from dms_runtime.json_values import to_json_value

from test_operations import operation_setup
from test_ownership import scope_setup
from test_persisted_control import persisted_setup


def test_nested_dynamodb_integers_become_json_integers():
    original = {
        "preflight": {
            "pollAttempts": Decimal("3"),
            "credentialsSynchronised": True,
        },
        "values": [Decimal("0"), Decimal("12345678901234567890")],
    }

    result = to_json_value(original)

    assert type(result["preflight"]["pollAttempts"]) is int
    assert result["preflight"]["credentialsSynchronised"] is True
    assert result["values"] == [0, 12345678901234567890]
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1.25"),
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_decimal_conversion_does_not_round_or_accept_nonfinite_values(
    value,
):
    with pytest.raises(ValueError):
        to_json_value({"value": value})


def test_history_reads_realistic_persisted_preflight_counters(
    persisted_setup,
):
    from unittest.mock import Mock

    from dms_runtime.history import RuntimeOperationHistory

    setup = persisted_setup
    setup["preflight"].run.return_value = {
        "connectionStatus": "successful",
        "credentialsSynchronised": True,
        "pollAttempts": 2,
    }

    setup["controller"].start(
        setup["binding"],
        request=setup["request"],
    )

    lookup = Mock()
    lookup.registry = setup["registry"]
    lookup.resolve.return_value = setup["binding"]

    history = RuntimeOperationHistory(lookup)

    stored = history.get(
        ingestion_id=setup["request"].ingestion_id,
        operation_id=setup["request"].operation_id,
    )

    counter = stored.record["result"]["preflight"]["pollAttempts"]

    assert type(counter) is int
    assert counter == 2
    json.dumps(stored.record)

    binding, boundary = history.full_load_start(
        ingestion_id=setup["request"].ingestion_id,
        operation_id=setup["request"].operation_id,
    )

    assert binding == setup["binding"]
    assert boundary.isoformat() == (
        "1970-01-01T00:16:40+00:00"
    )