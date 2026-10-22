import copy
import json
from pathlib import Path

import pytest

from ingestion_config.loader import ConfigurationError, load_service
from ingestion_config.mappings import build_table_mappings


ROOT = Path(__file__).resolve().parents[1]


def make_table(name, schema="public"):
    return {
        "schema_name": schema,
        "table_name": name,
    }


def rules_for(tables):
    return json.loads(
        build_table_mappings(tables)
    )["rules"]


@pytest.mark.parametrize("engine", ["postgres", "oracle"])
def test_builds_rules_from_service_examples(engine):
    service = load_service(
        ROOT / f"examples/{engine}.yaml"
    )

    for group in service["task_groups"].values():
        rules = rules_for(group["tables"])

        expected = {
            (table["schema_name"], table["table_name"])
            for table in group["tables"]
        }

        actual = {
            (
                rule["object-locator"]["schema-name"],
                rule["object-locator"]["table-name"],
            )
            for rule in rules
        }

        assert actual == expected

        assert all(
            rule["rule-action"] == "explicit"
            for rule in rules
        )


def test_selects_only_five_of_ten_tables():
    available = [
        make_table(f"table_{number}")
        for number in range(1, 11)
    ]

    selected = available[::2]
    rules = rules_for(selected)

    actual = {
        rule["object-locator"]["table-name"]
        for rule in rules
    }

    assert len(rules) == 5

    assert actual == {
        "table_1",
        "table_3",
        "table_5",
        "table_7",
        "table_9",
    }

    assert actual.isdisjoint(
        {
            "table_2",
            "table_4",
            "table_6",
            "table_8",
            "table_10",
        }
    )

    assert all(
        rule["rule-action"] == "explicit"
        for rule in rules
    )


def test_preserves_case_and_literal_underscores():
    rules = rules_for(
        [
            make_table(
                "DMS_INTEGRATION_TEST",
                "DMS_USER",
            )
        ]
    )

    assert rules[0]["object-locator"] == {
        "schema-name": "DMS_USER",
        "table-name": "DMS_INTEGRATION_TEST",
        "table-type": "table",
    }

    assert rules[0]["rule-action"] == "explicit"


def test_table_order_does_not_change_output():
    tables = [
        make_table("z_table"),
        make_table("a_table"),
    ]

    assert build_table_mappings(tables) == (
        build_table_mappings(
            list(reversed(tables))
        )
    )


def test_adding_table_preserves_existing_rule_identity():
    original = rules_for(
        [make_table("existing_table")]
    )[0]

    expanded = rules_for(
        [
            make_table("another_table"),
            make_table("existing_table"),
        ]
    )

    existing = next(
        rule
        for rule in expanded
        if rule["object-locator"]["table-name"]
        == "existing_table"
    )

    assert existing == original


def test_does_not_mutate_input():
    tables = [
        make_table("z_table"),
        make_table("a_table"),
    ]

    before = copy.deepcopy(tables)

    build_table_mappings(tables)

    assert tables == before


def test_same_table_name_in_different_schemas_is_distinct():
    rules = rules_for(
        [
            make_table("example", "first"),
            make_table("example", "second"),
        ]
    )

    assert len(rules) == 2

    assert len(
        {rule["rule-id"] for rule in rules}
    ) == 2


@pytest.mark.parametrize(
    "name",
    ["%", "*", "table?", "table[1]", " table"],
)
def test_rejects_unsupported_names(name):
    with pytest.raises(
        ConfigurationError,
        match="unsupported identifier",
    ):
        build_table_mappings(
            [make_table(name)]
        )


def test_rejects_empty_selection():
    with pytest.raises(
        ConfigurationError,
        match="non-empty table list",
    ):
        build_table_mappings([])


def test_rejects_duplicate_selection():
    with pytest.raises(
        ConfigurationError,
        match="Duplicate table",
    ):
        build_table_mappings(
            [
                make_table("example"),
                make_table("example"),
            ]
        )