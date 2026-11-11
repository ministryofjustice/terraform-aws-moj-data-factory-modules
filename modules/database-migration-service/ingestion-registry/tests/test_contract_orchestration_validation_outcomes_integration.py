import pytest
from sqlalchemy import types as sql_types

from test_contract_orchestration_validation_integration import (
    validated_orchestration as validated_orchestration,
    assess,
)
from test_contract_orchestration_generation_integration import (
    connected_orchestration as connected_orchestration,
    prepare,
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


def registration_count(setup):
    return setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"]


def current_selection(setup):
    return setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    )


def approve_prepared(setup, prepared):
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


def activate_baseline(setup):
    prepared = prepare(setup)

    waiting = assess(setup, prepared)
    assert waiting["status"] == "REVIEW_REQUIRED"

    approve_prepared(setup, prepared)
    ready = assess(setup, prepared)

    assert ready["status"] == "CONTRACT_READY"
    assert ready["action"] == "ACTIVATED"
    assert registration_count(setup) == 1

    return ready["selection"]


def prepare_next(setup, request_id):
    return setup["coordinator"].prepare(
        table=setup["table"],
        request_id=request_id,
    )


def change_source_columns(setup, monkeypatch, columns):
    monkeypatch.setattr(
        setup["runtime"],
        "get_table_metadata",
        lambda *_: (columns, ["id"]),
    )


def id_column(column_type=None):
    return {
        "name": "id",
        "type": (
            sql_types.Integer()
            if column_type is None
            else column_type
        ),
        "nullable": False,
    }


def assert_stored_outcome(
    setup,
    prepared,
    *,
    validation,
    compatibility,
):
    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    )

    assert request["assessment"]["validation"] == validation
    assert request["assessment"]["compatibility"] == compatibility
    assert request["assessment"]["candidate_reference"] == (
        prepared["candidate_reference"]
    )

    return request


def test_unchanged_generation_reuses_exact_approved_baseline(
    validated_orchestration,
):
    setup = validated_orchestration
    baseline = activate_baseline(setup)

    prepared = prepare_next(setup, "unchanged-source-request")

    assert prepared["contract_version"] != (
        baseline["registered_contract"]["contract_version"]
    )

    result = assess(setup, prepared)

    assert result["status"] == "CONTRACT_READY"
    assert result["action"] == "REUSED"
    assert result["selection"] == baseline

    request = assert_stored_outcome(
        setup,
        prepared,
        validation="VALID",
        compatibility="NO_CHANGE",
    )
    assert request["status"] == "REUSED"

    assert registration_count(setup) == 1
    assert current_selection(setup) == baseline

    assert assess(setup, prepared) == result
    assert registration_count(setup) == 1
    assert current_selection(setup) == baseline


def test_nullable_addition_requires_review_before_activation(
    validated_orchestration,
    monkeypatch,
):
    setup = validated_orchestration
    baseline = activate_baseline(setup)

    change_source_columns(
        setup,
        monkeypatch,
        [
            id_column(),
            {
                "name": "description",
                "type": sql_types.String(),
                "nullable": True,
            },
        ],
    )
    prepared = prepare_next(setup, "nullable-addition-request")

    waiting = assess(setup, prepared)

    assert waiting["status"] == "REVIEW_REQUIRED"
    assert waiting["reason"] == (
        "COMPATIBLE_CHANGE_REQUIRES_APPROVAL"
    )

    assert_stored_outcome(
        setup,
        prepared,
        validation="VALID",
        compatibility="COMPATIBLE",
    )
    assert registration_count(setup) == 1
    assert current_selection(setup) == baseline

    approve_prepared(setup, prepared)
    ready = assess(setup, prepared)

    assert ready["status"] == "CONTRACT_READY"
    assert ready["action"] == "ACTIVATED"
    assert ready["selection"]["registered_contract"][
        "contract_version"
    ] == prepared["contract_version"]
    assert registration_count(setup) == 2

    assert assess(setup, prepared) == ready
    assert registration_count(setup) == 2


def test_required_addition_cannot_activate_without_approval(
    validated_orchestration,
    monkeypatch,
):
    setup = validated_orchestration
    baseline = activate_baseline(setup)

    change_source_columns(
        setup,
        monkeypatch,
        [
            id_column(),
            {
                "name": "description",
                "type": sql_types.String(),
                "nullable": False,
            },
        ],
    )
    prepared = prepare_next(setup, "required-addition-request")

    waiting = assess(setup, prepared)

    assert waiting["status"] == "REVIEW_REQUIRED"
    assert waiting["reason"] == "BREAKING_CHANGE_REQUIRES_APPROVAL"

    assert_stored_outcome(
        setup,
        prepared,
        validation="VALID",
        compatibility="BREAKING",
    )

    assert registration_count(setup) == 1
    assert current_selection(setup) == baseline

    assert assess(setup, prepared) == waiting
    assert registration_count(setup) == 1
    assert current_selection(setup) == baseline


def test_numeric_change_without_an_agreed_rule_blocks_progression(
    validated_orchestration,
    monkeypatch,
):
    setup = validated_orchestration
    baseline = activate_baseline(setup)

    change_source_columns(
        setup,
        monkeypatch,
        [id_column(sql_types.BigInteger())],
    )
    prepared = prepare_next(setup, "numeric-change-request")

    result = assess(setup, prepared)

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "COMPATIBILITY_UNSUPPORTED"

    request = assert_stored_outcome(
        setup,
        prepared,
        validation="VALID",
        compatibility="UNSUPPORTED",
    )
    assert request["status"] == "BLOCKED"

    assert registration_count(setup) == 1
    assert current_selection(setup) == baseline

    assert assess(setup, prepared) == result
    assert registration_count(setup) == 1
    assert current_selection(setup) == baseline