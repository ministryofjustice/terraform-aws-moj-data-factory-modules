import copy

import pytest

from contract_compatibility import assess_contracts
from test_main import main


def contract(version="v1"):
    return {
        "type": "record",
        "name": "Example",
        "namespace": "example.contracts",
        "source": "sandbox-postgres",
        "database_name": "dms_test",
        "schema_name": "public",
        "table_name": "example",
        "contract_version": version,
        "fields": [
            {
                "name": "id",
                "type": "long",
                "key": "primary",
            },
            {
                "name": "description",
                "type": ["null", "string"],
                "default": None,
            },
        ],
    }


def validate_contract(value):
    main.validate_required_metadata(value)
    main.extract_version_number(value["contract_version"])
    main.validate_avro_schema(value)


def assess(candidate, baseline):
    return assess_contracts(
        candidate=candidate,
        baseline=baseline,
        validate_contract=validate_contract,
        canonicalise_schema=main.canonicalise_schema,
    )


def test_first_contract_requires_no_baseline():
    result = assess(contract(), None)

    assert result["validation"] == "VALID"
    assert result["compatibility"] == "FIRST_CONTRACT"


def test_version_change_alone_is_no_change():
    result = assess(contract("v2"), contract("v1"))

    assert result["compatibility"] == "NO_CHANGE"


def test_appended_nullable_field_with_null_default_is_compatible():
    candidate = contract("v2")
    candidate["fields"].append(
        {
            "name": "notes",
            "type": ["null", "string"],
            "default": None,
        }
    )

    assert assess(candidate, contract())["compatibility"] == "COMPATIBLE"


def test_required_addition_without_default_is_breaking():
    candidate = contract("v2")
    candidate["fields"].append(
        {"name": "status", "type": "string"}
    )

    assert assess(candidate, contract())["compatibility"] == "BREAKING"


def test_removed_field_is_breaking():
    candidate = contract("v2")
    candidate["fields"].pop()

    assert assess(candidate, contract())["compatibility"] == "BREAKING"


def test_incompatible_primitive_change_is_breaking():
    candidate = contract("v2")
    candidate["fields"][0]["type"] = "string"

    assert assess(candidate, contract())["compatibility"] == "BREAKING"


def test_primary_key_change_is_breaking():
    candidate = contract("v2")
    candidate["fields"][0].pop("key")

    assert assess(candidate, contract())["compatibility"] == "BREAKING"


def test_sensitive_metadata_change_is_not_no_change():
    candidate = contract("v2")
    candidate["fields"][1]["sensitive"] = True

    result = assess(candidate, contract())

    assert result["compatibility"] == "UNSUPPORTED"
    assert result["reason"] == "FIELD_METADATA_OR_DEFAULT_CHANGED"


def test_numeric_conversion_is_not_assumed_compatible():
    candidate = contract("v2")
    candidate["fields"][0]["type"] = "double"

    assert assess(candidate, contract())["compatibility"] == "UNSUPPORTED"


def test_nullable_addition_without_default_is_unsupported():
    candidate = contract("v2")
    candidate["fields"].append(
        {"name": "notes", "type": ["null", "string"]}
    )

    assert assess(candidate, contract())["compatibility"] == "UNSUPPORTED"


def test_field_reordering_is_unsupported():
    candidate = contract("v2")
    candidate["fields"].reverse()

    assert assess(candidate, contract())["compatibility"] == "UNSUPPORTED"


def test_invalid_candidate_is_not_a_breaking_classification():
    candidate = contract("v2")
    candidate["fields"].append(
        {"name": "invalid-name", "type": "string"}
    )

    result = assess(candidate, contract())

    assert result["validation"] == "INVALID"
    assert result["compatibility"] == "UNSUPPORTED"


@pytest.mark.parametrize(
    "field_type",
    [
        {"type": "array", "items": "string"},
        {"type": "map", "values": "long"},
    ],
)
def test_complex_types_fail_closed(field_type):
    candidate = contract("v2")
    candidate["fields"][1]["type"] = field_type
    candidate["fields"][1].pop("default")

    assert assess(candidate, contract())["compatibility"] == "UNSUPPORTED"


def test_inputs_are_not_modified():
    baseline = contract()
    candidate = contract("v2")
    original_baseline = copy.deepcopy(baseline)
    original_candidate = copy.deepcopy(candidate)

    assess(candidate, baseline)

    assert baseline == original_baseline
    assert candidate == original_candidate