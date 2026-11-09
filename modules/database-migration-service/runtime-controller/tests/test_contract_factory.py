import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ingestion_config.loader import ConfigurationError
from ingestion_config.published_resolver import TableLookup
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.contract_policy import ContractPolicy

from dms_runtime import contract_factory
from dms_runtime.contract_factory import (
    ContractObjectLocation,
    ContractRuntimeSettings,
    build_contract_runtime,
)


REGION = "eu-west-2"
OWNER = "123456789012"
KMS_KEY = (
    "arn:aws:kms:eu-west-2:123456789012:"
    "key/12345678-1234-1234-1234-123456789012"
)


@pytest.fixture
def factory_setup(monkeypatch):
    prefix = f"arn:aws:lambda:{REGION}:{OWNER}:function:"

    binding = {
        "environment": "development",
        "account_id": OWNER,
        "region": REGION,
        "source": "sandbox-postgres",
        "database_name": "dms_test",
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "table": {
            "schema_name": "public",
            "table_name": "example",
        },
        "contract_bindings": {
            "generation": {
                "kind": "lambda",
                "arn": prefix + "selected-generation:7",
            },
            "validation": {
                "kind": "lambda",
                "arn": prefix + "selected-validation:9",
            },
            "registration": {
                "kind": "lambda",
                "arn": prefix + "selected-registration:5",
            },
            "registry": {
                "table_name": "selected-contract-registry",
                "arn": (
                    f"arn:aws:dynamodb:{REGION}:{OWNER}:"
                    "table/selected-contract-registry"
                ),
            },
        },
    }

    resolver = Mock(
        side_effect=lambda *args, **kwargs: copy.deepcopy(binding)
    )
    monkeypatch.setattr(
        contract_factory,
        "resolve_published_table",
        resolver,
    )

    registry_client = Mock(
        meta=SimpleNamespace(region_name=REGION)
    )
    registry = IngestionRegistry(
        registry_client,
        table_name="test-ingestion-registry",
    )
    lambda_client = Mock(
        meta=SimpleNamespace(region_name=REGION)
    )
    dynamodb = Mock()
    dynamodb.meta.client.meta.region_name = REGION
    registration_table = Mock()
    dynamodb.Table.return_value = registration_table

    settings = ContractRuntimeSettings(
        candidate=ContractObjectLocation(
            bucket_name="test-contract-candidates",
            prefix="candidates/",
            bucket_owner_account_id=OWNER,
        ),
        approval=ContractObjectLocation(
            bucket_name="test-contract-evidence",
            prefix="approvals/",
            bucket_owner_account_id=OWNER,
        ),
        evidence=ContractObjectLocation(
            bucket_name="test-contract-evidence",
            prefix="validation/",
            bucket_owner_account_id=OWNER,
        ),
        approved_bucket_name="test-approved-contracts",
        approved_bucket_owner_account_id=OWNER,
        candidate_kms_key_arn=KMS_KEY,
        approved_kms_key_arn=KMS_KEY,
        assessor_revision="test-assessor-v1",
        policy=ContractPolicy(revision="policy-v1"),
    )

    arguments = {
        "registry": registry,
        "document_client": Mock(),
        "dynamodb_resource": dynamodb,
        "lambda_client": lambda_client,
        "s3_client": Mock(),
        "manifest_reference": Mock(),
        "artifact_reference": Mock(),
        "catalog_reference": Mock(),
        "allowed_bucket": "test-deployment-documents",
        "allowed_prefix": "services/sandbox-postgres/",
        "expected_bucket_owner": OWNER,
        "lookup": TableLookup(
            environment=binding["environment"],
            account_id=OWNER,
            region=REGION,
            source=binding["source"],
            database_name=binding["database_name"],
            schema_name=binding["table"]["schema_name"],
            table_name=binding["table"]["table_name"],
            configuration_revision=binding["configuration_revision"],
            artifact_revision=binding["artifact_revision"],
        ),
        "settings": settings,
        "authorise": Mock(),
    }

    return {
        "arguments": arguments,
        "binding": binding,
        "resolver": resolver,
        "registry": registry,
        "registration_table": registration_table,
    }


def test_factory_constructs_real_services_from_resolved_targets(
    factory_setup,
):
    setup = factory_setup

    runtime = build_contract_runtime(**setup["arguments"])
    coordinator = runtime.orchestration
    capabilities = setup["binding"]["contract_bindings"]

    assert coordinator.preparation.completion.generator.function_arn == (
        capabilities["generation"]["arn"]
    )
    assert coordinator.validator.validation_function_arn == (
        capabilities["validation"]["arn"]
    )
    assert (
        coordinator.activation.completion.registrar.registration_function_arn
        == capabilities["registration"]["arn"]
    )

    setup["arguments"]["dynamodb_resource"].Table.assert_called_once_with(
        capabilities["registry"]["table_name"]
    )

    assert coordinator.preparation.versions.registration_table is (
        setup["registration_table"]
    )
    assert coordinator.lookup.verifier.contract_table is (
        setup["registration_table"]
    )
    assert coordinator.preparation.table == runtime.table
    assert runtime.pinning.lookup is coordinator.lookup
    assert runtime.lifecycle is coordinator.lifecycle

    setup["arguments"]["lambda_client"].invoke.assert_not_called()
    setup["registry"].client.transact_write_items.assert_not_called()
    setup["arguments"]["s3_client"].put_object.assert_not_called()


def test_factory_requests_all_contract_capabilities(factory_setup):
    setup = factory_setup

    build_contract_runtime(**setup["arguments"])

    arguments = setup["resolver"].call_args.kwargs

    assert arguments["required_contract_capabilities"] == (
        "generation",
        "validation",
        "registration",
        "registry",
    )
    assert arguments["catalog_reference"] is (
        setup["arguments"]["catalog_reference"]
    )


def test_document_resolution_failure_prevents_service_use(factory_setup):
    setup = factory_setup
    setup["resolver"].side_effect = ConfigurationError(
        "Published document checksum does not match"
    )

    with pytest.raises(ConfigurationError, match="checksum"):
        build_contract_runtime(**setup["arguments"])

    setup["arguments"]["dynamodb_resource"].Table.assert_not_called()
    setup["arguments"]["lambda_client"].invoke.assert_not_called()
    setup["registry"].client.transact_write_items.assert_not_called()


def test_wrong_client_region_is_rejected_before_document_loading(
    factory_setup,
):
    setup = factory_setup
    setup["arguments"]["lambda_client"].meta.region_name = "eu-west-1"

    with pytest.raises(ValueError, match="deployment region"):
        build_contract_runtime(**setup["arguments"])

    setup["resolver"].assert_not_called()


def test_policy_service_uses_same_lookup_and_admission(factory_setup):
    setup = factory_setup

    runtime = build_contract_runtime(**setup["arguments"])
    coordinator = runtime.orchestration
    policy = coordinator.lookup.policy_authorisation

    assert policy.admission is coordinator.assessment_admission
    assert policy.lifecycle is coordinator.lifecycle
    assert policy.trusted_policy == (
        setup["arguments"]["settings"].policy
    )
    assert policy.trusted_policy.automatically_accept_compatible is False


def test_factory_preserves_explicit_compatible_acceptance_policy(
    factory_setup,
):
    setup = factory_setup
    policy = ContractPolicy(
        revision="approved-compatible-policy-v2",
        automatically_accept_compatible=True,
    )
    arguments = {
        **setup["arguments"],
        "settings": replace(
            setup["arguments"]["settings"],
            policy=policy,
        ),
    }

    runtime = build_contract_runtime(**arguments)

    assert runtime.orchestration.trusted_policy == policy
    assert runtime.orchestration.lookup.policy_authorisation.trusted_policy == (
        policy
    )