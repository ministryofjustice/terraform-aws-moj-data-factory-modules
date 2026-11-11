import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from dms_runtime import contract_lambda_function
from dms_runtime.contract_handler import ContractRequestError

from test_contract_handler import (
    contract_handler as contract_handler,
)


@pytest.fixture
def bootstrap_setup(contract_handler, monkeypatch):
    owner = "123456789012"
    key = (
        "arn:aws:kms:eu-west-2:123456789012:"
        "key/12345678-1234-1234-1234-123456789012"
    )

    def location(bucket, prefix):
        return {
            "bucket_name": bucket,
            "prefix": prefix,
            "bucket_owner_account_id": owner,
        }

    document = {
        "configuration_version": 1,
        "registry_table_name": "test-ingestion-registry",
        "lookup": {
            "environment": "development",
            "account_id": owner,
            "region": "eu-west-2",
            "source": "sandbox-postgres",
            "database_name": "dms_test",
            "schema_name": "public",
            "table_name": "example",
            "configuration_revision": "a" * 64,
            "artifact_revision": "b" * 64,
        },
        "documents": {
            "bucket_name": "test-deployment-documents",
            "prefix": "services/sandbox-postgres/",
            "bucket_owner_account_id": owner,
            **{
                name: {
                    "bucket_name": "test-deployment-documents",
                    "object_key": (
                        f"services/sandbox-postgres/{name}/document.json"
                    ),
                    "version_id": f"{name}-version-one",
                    "sha256": digest * 64,
                }
                for name, digest in (
                    ("manifest", "a"),
                    ("artifact", "b"),
                    ("catalog", "c"),
                )
            },
        },
        "settings": {
            "candidate": location(
                "test-contract-candidates",
                "candidates/",
            ),
            "approval": location(
                "test-contract-evidence",
                "approvals/",
            ),
            "evidence": location(
                "test-contract-evidence",
                "validation/",
            ),
            "approved_bucket_name": "test-approved-contracts",
            "approved_bucket_owner_account_id": owner,
            "candidate_kms_key_arn": key,
            "approved_kms_key_arn": key,
            "assessor_revision": "test-assessor-v1",
            "policy": {
                "revision": "policy-v1",
                "automatically_accept_compatible": False,
            },
        },
        "authorised_reviewers": ["trusted-reviewer"],
    }

    factory = Mock(
        return_value=contract_handler["handler"].runtime
    )
    monkeypatch.setattr(
        contract_lambda_function,
        "build_contract_runtime",
        factory,
    )

    return {
        "document": document,
        "factory": factory,
        "session": Mock(),
        "runtime": contract_handler["handler"].runtime,
    }


def environment(setup):
    return {
        "AWS_REGION": "eu-west-2",
        "CONTRACT_RUNTIME_CONFIG": json.dumps(setup["document"]),
    }


def test_bootstrap_passes_pinned_references_and_trusted_settings(
    bootstrap_setup,
):
    setup = bootstrap_setup

    handler = contract_lambda_function.build_handler(
        environment(setup),
        session=setup["session"],
    )
    arguments = setup["factory"].call_args.kwargs

    assert handler.runtime is setup["runtime"]
    assert arguments["registry"].table_name == "test-ingestion-registry"
    assert arguments["manifest_reference"].version_id == (
        "manifest-version-one"
    )
    assert arguments["catalog_reference"].version_id == (
        "catalog-version-one"
    )
    assert arguments["lookup"].table_name == "example"
    assert arguments["settings"].policy.automatically_accept_compatible is False
    assert arguments["document_client"] is arguments["s3_client"]


def test_bootstrap_reviewer_authorisation_is_scoped_to_selected_table(
    bootstrap_setup,
):
    setup = bootstrap_setup

    contract_lambda_function.build_handler(
        environment(setup),
        session=setup["session"],
    )
    authorise = setup["factory"].call_args.kwargs["authorise"]
    table = setup["runtime"].table

    assert authorise("trusted-reviewer", table) is None

    with pytest.raises(PermissionError):
        authorise("another-reviewer", table)

    with pytest.raises(PermissionError):
        authorise(
            "trusted-reviewer",
            replace(table, table_name="another_table"),
        )


@pytest.mark.parametrize("version", [True, "1", 1.0, 2])
def test_invalid_configuration_version_prevents_client_creation(
    bootstrap_setup,
    version,
):
    setup = bootstrap_setup
    setup["document"]["configuration_version"] = version

    with pytest.raises(ValueError, match="structure or version"):
        contract_lambda_function.build_handler(
            environment(setup),
            session=setup["session"],
        )

    setup["session"].client.assert_not_called()
    setup["factory"].assert_not_called()


def test_region_mismatch_prevents_client_creation(bootstrap_setup):
    setup = bootstrap_setup
    supplied = environment(setup)
    supplied["AWS_REGION"] = "eu-west-1"

    with pytest.raises(ValueError, match="AWS_REGION"):
        contract_lambda_function.build_handler(
            supplied,
            session=setup["session"],
        )

    setup["session"].client.assert_not_called()


def test_duplicate_reviewers_are_rejected(bootstrap_setup):
    setup = bootstrap_setup
    setup["document"]["authorised_reviewers"] *= 2

    with pytest.raises(ValueError, match="duplicates"):
        contract_lambda_function.build_handler(
            environment(setup),
            session=setup["session"],
        )

    setup["session"].client.assert_not_called()


def test_missing_configuration_is_rejected(bootstrap_setup):
    setup = bootstrap_setup

    with pytest.raises(ValueError, match="CONTRACT_RUNTIME_CONFIG"):
        contract_lambda_function.build_handler(
            {"AWS_REGION": "eu-west-2"},
            session=setup["session"],
        )

    setup["session"].client.assert_not_called()


def test_unknown_configuration_field_is_rejected(bootstrap_setup):
    setup = bootstrap_setup
    setup["document"]["unrecognised_setting"] = True

    with pytest.raises(ValueError, match="structure or version"):
        contract_lambda_function.build_handler(
            environment(setup),
            session=setup["session"],
        )

    setup["factory"].assert_not_called()


def test_capability_invocation_uses_one_sdk_attempt(bootstrap_setup):
    setup = bootstrap_setup

    contract_lambda_function.build_handler(
        environment(setup),
        session=setup["session"],
    )

    invocation = next(
        call
        for call in setup["session"].client.call_args_list
        if call.args == ("lambda",)
    )
    config = invocation.kwargs["config"]

    assert config.region_name == "eu-west-2"
    assert config.retries["total_max_attempts"] == 1


def test_lambda_result_is_json_compatible(monkeypatch):
    handler = Mock()
    handler.handle.return_value = {
        "status": "CONTRACT_READY",
        "revision": Decimal(3),
    }
    monkeypatch.setattr(
        contract_lambda_function,
        "_get_handler",
        Mock(return_value=handler),
    )

    event = {
        "request_version": 1,
        "operation": "RESOLVE_CONTRACT",
    }

    result = contract_lambda_function.lambda_handler(event, None)

    assert result == {
        "status": "CONTRACT_READY",
        "revision": 3,
    }
    json.dumps(result)
    handler.handle.assert_called_once_with(event)


def test_invalid_operation_is_rejected_before_bootstrap(monkeypatch):
    get_handler = Mock()
    monkeypatch.setattr(
        contract_lambda_function,
        "_get_handler",
        get_handler,
    )

    with pytest.raises(ContractRequestError):
        contract_lambda_function.lambda_handler(
            {
                "request_version": 1,
                "operation": "APPROVE_CONTRACT",
            },
            None,
        )

    get_handler.assert_not_called()