import pytest

from ingestion_config.contract_catalog import build_contract_catalog
from ingestion_config.loader import ConfigurationError


ACCOUNT_ID = "111122223333"
LAMBDA_PREFIX = f"arn:aws:lambda:eu-west-2:{ACCOUNT_ID}:function:"
TABLES = [
    ("sandbox-postgres", "dms_test", "public", "dms_integration_test"),
    ("sandbox-oracle", "DMSTEST", "DMS_USER", "DMS_INTEGRATION_TEST"),
]


def deployment(source, database, schema, table):
    identity = {
        "source": source,
        "database_name": database,
        "schema_name": schema,
        "table_name": table,
    }
    artifact = {
        "artifact_revision": "a" * 64,
        "runtime_configuration": {
            "configuration_revision": "b" * 64,
            "environment": "development",
            "region": "eu-west-2",
            "source": source,
            "source_endpoint": {"database_name": database},
            "task_groups": {
                "integration": {
                    "tables": [{"schema_name": schema, "table_name": table}],
                },
            },
        },
    }
    return identity, artifact


@pytest.fixture
def references():
    return {
        "generation": {
            source: {
                "kind": "lambda",
                "arn": f"{LAMBDA_PREFIX}gen-{source}:1",
                "source": source,
                "database_name": database,
                "schema_name": schema,
                "table_names": [table],
            }
            for source, database, schema, table in TABLES
        },
        "validation": {
            source: {
                "kind": "lambda",
                "arn": f"{LAMBDA_PREFIX}val-{source}:2",
                "source_mappings": [deployment(source, database, schema, table)[0]],
            }
            for source, database, schema, table in TABLES
        },
        "registration": {
            "kind": "lambda",
            "arn": f"{LAMBDA_PREFIX}register:3",
        },
        "registry": {
            "table_name": "contracts",
            "arn": f"arn:aws:dynamodb:eu-west-2:{ACCOUNT_ID}:table/contracts",
        },
    }


@pytest.mark.parametrize(("source", "database", "schema", "table"), TABLES)
def test_catalog_selects_capabilities_for_table(
    references, source, database, schema, table
):
    identity, artifact = deployment(source, database, schema, table)

    catalog = build_contract_catalog(artifact, references, account_id=ACCOUNT_ID)

    assert catalog["configuration_revision"] == "b" * 64
    assert catalog["artifact_revision"] == "a" * 64
    assert catalog["contracts"] == {
        "tables": [
            {
                **identity,
                "generation": {
                    "kind": "lambda",
                    "arn": f"{LAMBDA_PREFIX}gen-{source}:1",
                },
                "validation": {
                    "kind": "lambda",
                    "arn": f"{LAMBDA_PREFIX}val-{source}:2",
                },
            },
        ],
        "registration": references["registration"],
        "registry": references["registry"],
    }


@pytest.mark.parametrize("match_count", [0, 2])
def test_catalog_rejects_missing_or_ambiguous_validation(references, match_count):
    source = TABLES[0][0]
    _, artifact = deployment(*TABLES[0])
    validation = references["validation"].pop(source)
    for index in range(match_count):
        references["validation"][f"match-{index}"] = validation

    with pytest.raises(ConfigurationError, match="sandbox-postgres/dms_test/public"):
        build_contract_catalog(artifact, references, account_id=ACCOUNT_ID)


def test_catalog_rejects_unqualified_registration_arn(references):
    _, artifact = deployment(*TABLES[0])
    references["registration"]["arn"] = f"{LAMBDA_PREFIX}register"

    with pytest.raises(ConfigurationError, match="published numeric Lambda version"):
        build_contract_catalog(artifact, references, account_id=ACCOUNT_ID)