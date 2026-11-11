import copy
from dataclasses import replace

import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_preparation import (
    ContractPreparation,
    ContractPreparationBlocked,
)
from ingestion_registry.contract_versions import (
    ContractVersionConflict,
    ContractVersions,
)

from test_contract_generation_completion_integration import (
    generation_completion as generation_completion,
)
from test_candidate_generation_integration import (
    candidate_generation as candidate_generation,
    generation_adapter as generation_adapter,
)


@pytest.fixture
def preparation_setup(generation_completion):
    setup = generation_completion
    table = setup["table"]

    binding = {
        "environment": table.environment,
        "account_id": "123456789012",
        "region": "eu-west-2",
        "source": table.source,
        "database_name": table.database_name,
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "table": {
            "schema_name": table.schema_name,
            "table_name": table.table_name,
        },
        "document_references": {
            "manifest": {
                "bucket_name": "test-deployment-manifests",
                "object_key": "development/manifest.json",
                "version_id": "manifest-version-one",
                "sha256": "c" * 64,
            },
        },
    }

    versions = ContractVersions(
        registry=setup["registry"],
        registration_table=setup["registration_table"],
    )

    service = ContractPreparation(
        versions=versions,
        generation_completion=setup["completion"],
        deployment_binding=binding,
    )

    return {
        **setup,
        "binding": binding,
        "versions": versions,
        "preparation": service,
    }


def prepare(setup, service=None):
    return (
        setup["preparation"] if service is None else service
    ).prepare(
        table=setup["table"],
        request_id="coordinated-generation",
    )


def test_preparation_reserves_and_generates_candidate(preparation_setup):
    setup = preparation_setup

    result = prepare(setup)

    assert result["status"] == "CANDIDATE_READY"

    assert result["contract_version"] == "v3"
    assert result["generation_context"]["configuration_revision"] == (
        "a" * 64
    )
    assert result["generation_context"]["generation_function_arn"] == (
        setup["function_arn"]
    )

    stored = setup["registry"]._read(
        result["reservation_key"],
        "GENERATION",
    )
    assert stored["result"]["candidate_reference"] == (
        result["candidate_reference"]
    )

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) is None


def test_duplicate_preparation_does_not_generate_again(preparation_setup):
    setup = preparation_setup

    original = prepare(setup)

    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    assert prepare(setup) == original
    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()


def test_changed_deployment_context_conflicts_with_original_request(
    preparation_setup,
):
    setup = preparation_setup
    prepare(setup)

    changed = copy.deepcopy(setup["binding"])
    changed["configuration_revision"] = "d" * 64

    service = ContractPreparation(
        versions=setup["versions"],
        generation_completion=setup["completion"],
        deployment_binding=changed,
    )

    setup["invocation"].invoke.reset_mock()

    with pytest.raises(ContractVersionConflict, match="different content"):
        prepare(setup, service)

    setup["invocation"].invoke.assert_not_called()


def test_other_table_cannot_use_selected_deployment(preparation_setup):
    setup = preparation_setup

    with pytest.raises(
        ContractPreparationBlocked,
        match="pinned deployment",
    ):
        setup["preparation"].prepare(
            table=replace(
                setup["table"],
                table_name="another_table",
            ),
            request_id="another-table-request",
        )

    setup["invocation"].invoke.assert_not_called()


def test_lost_generation_response_recovers_same_preparation(
    preparation_setup,
):
    setup = preparation_setup

    def invoke_then_lose_response(**kwargs):
        response = setup["invoke_handler"](**kwargs)
        response["Payload"].close()

        raise ReadTimeoutError(
            endpoint_url="https://lambda.eu-west-2.amazonaws.com"
        )

    setup["invocation"].invoke.side_effect = invoke_then_lose_response

    with pytest.raises(ReadTimeoutError):
        prepare(setup)

    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    recovered = prepare(setup)

    assert recovered["status"] == "CANDIDATE_READY"
    assert recovered["contract_version"] == "v3"

    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()

    assert prepare(setup) == recovered