import copy

import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_generation_completion import (
    ContractGenerationCompletion,
    ContractGenerationConflict,
)

from test_candidate_generation_integration import (
    candidate_generation as candidate_generation,
    generation_adapter as generation_adapter,
)


@pytest.fixture
def generation_completion(generation_adapter):
    setup = generation_adapter

    completion = ContractGenerationCompletion(
        generator=setup["generator"],
        candidate_kms_key_arn=setup["kms_key"],
    )

    return {**setup, "completion": completion}


def complete(setup):
    return setup["completion"].complete(
        table=setup["table"],
        reservation_key=setup["reservation"]["pk"],
        request_fingerprint=setup["reservation"]["request_fingerprint"],
    )


def test_completion_records_verified_generation(
    generation_completion,
):
    setup = generation_completion

    result = complete(setup)

    record = setup["registry"]._read(
        setup["reservation"]["pk"],
        "GENERATION",
    )

    assert record["record_type"] == "CONTRACT_GENERATION"
    assert record["result"] == result
    assert result["contract_version"] == "v2"
    assert result["status"] == "CANDIDATE_VERIFIED"

    setup["invocation"].invoke.assert_called_once()

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0


def test_duplicate_completion_reuses_original_candidate(
    generation_completion,
):
    setup = generation_completion
    original = complete(setup)

    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    repeated = complete(setup)

    assert repeated == original
    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()


def test_lost_lambda_response_recovers_without_generation_again(
    generation_completion,
):
    setup = generation_completion

    def invoke_then_lose_response(**kwargs):
        response = setup["invoke_handler"](**kwargs)
        response["Payload"].close()

        raise ReadTimeoutError(
            endpoint_url="https://lambda.eu-west-2.amazonaws.com"
        )

    setup["invocation"].invoke.side_effect = (
        invoke_then_lose_response
    )

    with pytest.raises(ReadTimeoutError):
        complete(setup)

    assert setup["registry"]._read(
        setup["reservation"]["pk"],
        "GENERATION",
    ) is None

    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    recovered = complete(setup)

    assert recovered["status"] == "CANDIDATE_VERIFIED"
    assert recovered["contract_version"] == "v2"

    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()

    assert setup["registry"]._read(
        setup["reservation"]["pk"],
        "GENERATION",
    )["result"] == recovered


def test_lost_completion_response_returns_original_result(
    generation_completion,
    monkeypatch,
):
    setup = generation_completion
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
        complete(setup)

    stored = setup["registry"]._read(
        setup["reservation"]["pk"],
        "GENERATION",
    )["result"]

    monkeypatch.setattr(
        client,
        "transact_write_items",
        original_write,
    )
    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    assert complete(setup) == stored
    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()


def test_changed_completion_fingerprint_blocks_reuse(
    generation_completion,
):
    setup = generation_completion
    complete(setup)

    record = setup["registry"]._read(
        setup["reservation"]["pk"],
        "GENERATION",
    )
    changed = copy.deepcopy(record)
    changed["result"]["contract_fingerprint"] = "f" * 64

    setup["registry"].client.put_item(
        TableName=setup["registry"].table_name,
        Item=setup["registry"]._encode(changed),
    )

    setup["invocation"].invoke.reset_mock()

    with pytest.raises(
        ContractGenerationConflict,
        match="does not match",
    ):
        complete(setup)

    setup["invocation"].invoke.assert_not_called()