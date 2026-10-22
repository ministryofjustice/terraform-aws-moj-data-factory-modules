import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from ingestion_config.loader import ConfigurationError
from ingestion_config.platform import load_platform, resolve_service
from ingestion_config.schema import load_schema


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture
def documents():
    service = yaml.safe_load(
        (EXAMPLES / "postgres.yaml").read_text(
            encoding="utf-8"
        )
    )

    platform = yaml.safe_load(
        (EXAMPLES / "platform-development.yaml").read_text(
            encoding="utf-8"
        )
    )

    return service, platform


def write_documents(tmp_path, documents):
    service, platform = documents

    service_path = tmp_path / "service.yaml"
    platform_path = tmp_path / "platform.yaml"

    service_path.write_text(
        yaml.safe_dump(service),
        encoding="utf-8",
    )

    platform_path.write_text(
        yaml.safe_dump(platform),
        encoding="utf-8",
    )

    return service_path, platform_path


def selected_binding(documents):
    service, platform = documents
    return platform["services"][service["service"]]


def test_platform_schema_is_valid():
    schema = load_schema("platform-v1.schema.json")

    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("engine", ["postgres", "oracle"])
def test_resolves_example_service(engine):
    result = resolve_service(
        EXAMPLES / f"{engine}.yaml",
        EXAMPLES / "platform-development.yaml",
    )

    service = result["service_configuration"]
    bindings = result["bindings"]

    assert bindings["source_endpoint"]["engine"] == engine
    assert bindings["source_endpoint"]["database_name"] == (
        service["source"]["database_name"]
    )
    assert bindings["environment"] == "development"
    assert bindings["region"] == "eu-west-2"
    assert bindings["destination"]["parent_domain"] == (
        service["parent_domain"]
    )


def test_resolved_network_matches_module_input_shape(
    tmp_path,
    documents,
):
    service, platform = documents
    binding = selected_binding(documents)

    expected_network = platform["network_profiles"][
        binding["network_profile"]
    ]

    result = resolve_service(
        *write_documents(tmp_path, documents)
    )

    bindings = result["bindings"]

    assert bindings["vpc_id"] == expected_network["vpc_id"]
    assert "vpc_id" not in bindings["network"]
    assert bindings["network"]["subnet_ids"] == (
        expected_network["subnet_ids"]
    )


def test_preserves_destination_base_prefix(tmp_path, documents):
    service, platform = documents

    expected = platform["destination_profiles"][
        service["destination_profile"]
    ]["landing_prefix"]

    result = resolve_service(
        *write_documents(tmp_path, documents)
    )

    assert result["bindings"]["destination"]["landing_prefix"] == (
        expected
    )
    assert "bucket_folder" not in result["bindings"]["destination"]


def test_rejects_unknown_service(tmp_path, documents):
    service, _ = documents
    service["service"] = "unconfigured-service"

    with pytest.raises(
        ConfigurationError,
        match="no platform binding",
    ):
        resolve_service(*write_documents(tmp_path, documents))


@pytest.mark.parametrize(
    "field, value",
    [
        ("engine", "oracle"),
        ("database_name", "another_database"),
        ("secret_ref", "another-secret"),
    ],
)
def test_rejects_source_binding_mismatch(
    tmp_path,
    documents,
    field,
    value,
):
    service, _ = documents
    service["source"][field] = value

    with pytest.raises(
        ConfigurationError,
        match=rf"source\.{field}: does not match",
    ):
        resolve_service(*write_documents(tmp_path, documents))


@pytest.mark.parametrize(
    "field, value",
    [
        ("network_profile", "missing-network"),
        ("replication_profile", "missing-replication"),
        ("allowed_destination_profiles", ["missing-destination"]),
        ("allowed_concurrency_profiles", ["missing-concurrency"]),
    ],
)
def test_rejects_missing_profile_references(
    tmp_path,
    documents,
    field,
    value,
):
    selected_binding(documents)[field] = value

    _, platform_path = write_documents(tmp_path, documents)

    with pytest.raises(
        ConfigurationError,
        match="unknown profile",
    ):
        load_platform(platform_path)


@pytest.mark.parametrize(
    "selected_field, allowed_field, collection, alternate_name",
    [
        (
            "destination_profile",
            "allowed_destination_profiles",
            "destination_profiles",
            "other-destination",
        ),
        (
            "concurrency_profile",
            "allowed_concurrency_profiles",
            "concurrency_profiles",
            "other-concurrency",
        ),
    ],
)
def test_rejects_existing_but_unauthorised_profile(
    tmp_path,
    documents,
    selected_field,
    allowed_field,
    collection,
    alternate_name,
):
    service, platform = documents

    platform[collection][alternate_name] = copy.deepcopy(
        platform[collection][service[selected_field]]
    )

    selected_binding(documents)[allowed_field] = [
        alternate_name
    ]

    with pytest.raises(
        ConfigurationError,
        match="not authorised",
    ):
        resolve_service(*write_documents(tmp_path, documents))


def test_rejects_parent_domain_mismatch(tmp_path, documents):
    service, _ = documents
    service["parent_domain"] = "prisons"

    with pytest.raises(
        ConfigurationError,
        match="parent_domain",
    ):
        resolve_service(*write_documents(tmp_path, documents))


@pytest.mark.parametrize(
    "component",
    ["processing", "history", "publication"],
)
def test_rejects_unknown_component_profile(
    tmp_path,
    documents,
    component,
):
    service, _ = documents

    table = service["task_groups"]["integration"]["tables"][0]
    table[f"{component}_profile"] = "missing-profile"

    with pytest.raises(
        ConfigurationError,
        match=rf"{component}_profile: unknown profile",
    ):
        resolve_service(*write_documents(tmp_path, documents))


def test_rejects_secret_in_another_region(tmp_path, documents):
    binding = selected_binding(documents)

    binding["secret_arn"] = binding["secret_arn"].replace(
        ":eu-west-2:",
        ":eu-west-1:",
    )

    _, platform_path = write_documents(tmp_path, documents)

    with pytest.raises(
        ConfigurationError,
        match="expected a secretsmanager ARN in eu-west-2",
    ):
        load_platform(platform_path)


def test_rejects_destination_key_in_another_region(
    tmp_path,
    documents,
):
    service, platform = documents

    destination = platform["destination_profiles"][
        service["destination_profile"]
    ]

    destination["kms_key_arn"] = (
        destination["kms_key_arn"].replace(
            ":eu-west-2:",
            ":eu-west-1:",
        )
    )

    _, platform_path = write_documents(tmp_path, documents)

    with pytest.raises(
        ConfigurationError,
        match="expected a kms ARN in eu-west-2",
    ):
        load_platform(platform_path)


def test_accepts_cross_account_key_reference(
    tmp_path,
    documents,
):
    service, platform = documents

    destination = platform["destination_profiles"][
        service["destination_profile"]
    ]

    cross_account_key = (
        "arn:aws:kms:eu-west-2:444455556666:"
        "key/33333333-3333-3333-3333-333333333333"
    )

    destination["kms_key_arn"] = cross_account_key

    result = resolve_service(
        *write_documents(tmp_path, documents)
    )

    assert result["bindings"]["destination"]["kms_key_arn"] == (
        cross_account_key
    )


def test_rejects_invalid_network_identifier(tmp_path, documents):
    _, platform = documents
    binding = selected_binding(documents)

    network = platform["network_profiles"][
        binding["network_profile"]
    ]

    network["vpc_id"] = "not-a-vpc-id"

    _, platform_path = write_documents(tmp_path, documents)

    with pytest.raises(
        ConfigurationError,
        match="invalid vpc identifier",
    ):
        load_platform(platform_path)


def test_rejects_invalid_concurrency_relationship(
    tmp_path,
    documents,
):
    service, platform = documents

    limits = platform["concurrency_profiles"][
        service["concurrency_profile"]
    ]

    limits["max_parallel_tables"] = 1
    limits["max_parallel_full_loads"] = 2

    _, platform_path = write_documents(tmp_path, documents)

    with pytest.raises(
        ConfigurationError,
        match="full-load limit must not exceed table limit",
    ):
        load_platform(platform_path)


def test_rejects_empty_destination_path_segment(
    tmp_path,
    documents,
):
    service, platform = documents

    destination = platform["destination_profiles"][
        service["destination_profile"]
    ]

    destination["landing_prefix"] = "landing//probation/"

    _, platform_path = write_documents(tmp_path, documents)

    with pytest.raises(
        ConfigurationError,
        match="empty or relative path segments",
    ):
        load_platform(platform_path)


def test_rejects_unknown_platform_setting(tmp_path, documents):
    _, platform = documents
    platform["unexpected_setting"] = True

    _, platform_path = write_documents(tmp_path, documents)

    with pytest.raises(
        ConfigurationError,
        match="Additional properties",
    ):
        load_platform(platform_path)


def test_rejects_duplicate_platform_yaml_keys(tmp_path):
    path = tmp_path / "platform.yaml"

    path.write_text(
        "environment: development\n"
        "environment: production\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError,
        match="Duplicate key",
    ):
        load_platform(path)