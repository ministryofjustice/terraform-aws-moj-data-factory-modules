from dataclasses import replace

import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_versions import (
    ContractVersionConflict,
    ContractVersions,
)


def allocator(setup):
    return ContractVersions(
        registry=setup["registry"],
        registration_table=setup["registration_table"],
    )


def reserve(setup, service, request_id="version-request-one", **changes):
    arguments = {
        "table": setup["table"],
        "request_id": request_id,
        "request_fingerprint": "a" * 64,
    }
    arguments.update(changes)
    return service.reserve(**arguments)


def test_first_reservation_does_not_allocate_an_ingestion(
    registration_integration,
):
    setup = registration_integration
    result = reserve(setup, allocator(setup))

    assert result["contract_version"] == "v1"
    assert "ingestion_id" not in result
    assert "ingestion_version" not in result
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0

    records = setup["registry"].client.scan(
        TableName=setup["registry"].table_name,
        ConsistentRead=True,
    )["Items"]
    kinds = {
        item["record_type"]["S"]
        for item in records
    }
    assert "INGESTION" not in kinds


def test_duplicate_reservation_returns_original_version(
    registration_integration,
):
    setup = registration_integration
    service = allocator(setup)

    first = reserve(setup, service)
    second = reserve(setup, service)

    assert second == first
    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_VERSION_HEAD",
    )["latest_version"] == 1


def test_changed_request_fingerprint_is_rejected(
    registration_integration,
):
    setup = registration_integration
    service = allocator(setup)
    reserve(setup, service)

    with pytest.raises(ContractVersionConflict, match="different content"):
        reserve(
            setup,
            service,
            request_fingerprint="b" * 64,
        )


def test_distinct_requests_get_increasing_versions(
    registration_integration,
):
    setup = registration_integration
    service = allocator(setup)

    first = reserve(setup, service)
    second = reserve(setup, service, request_id="version-request-two")

    assert first["contract_version"] == "v1"
    assert second["contract_version"] == "v2"


def test_existing_registration_versions_are_compared_numerically(
    registration_integration,
):
    setup = registration_integration
    service = allocator(setup)
    contract_id = service._contract_id(setup["table"])

    for version in ("v2", "v9", "v10"):
        setup["registration_table"].put_item(
            Item={
                "contract_id": contract_id,
                "contract_version": version,
            }
        )

    result = reserve(setup, service)

    assert result["contract_version"] == "v11"


def test_tables_have_independent_contract_versions(
    registration_integration,
):
    setup = registration_integration
    service = allocator(setup)

    reserve(setup, service)
    reserve(setup, service, request_id="version-request-two")

    other = reserve(
        setup,
        service,
        table=replace(setup["table"], table_name="another_table"),
    )

    assert other["contract_version"] == "v1"


def test_lost_reservation_response_does_not_allocate_again(
    registration_integration, monkeypatch
):
    setup = registration_integration
    service = allocator(setup)
    client = setup["registry"].client
    original_write = client.transact_write_items

    def write_then_lose_response(**kwargs):
        original_write(**kwargs)
        raise ReadTimeoutError(
            endpoint_url="https://dynamodb.eu-west-2.amazonaws.com"
        )

    monkeypatch.setattr(
        client, "transact_write_items", write_then_lose_response
    )

    with pytest.raises(ReadTimeoutError):
        reserve(setup, service)

    monkeypatch.setattr(
        client, "transact_write_items", original_write
    )

    recovered = reserve(setup, service)

    assert recovered["contract_version"] == "v1"
    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_VERSION_HEAD",
    )["latest_version"] == 1


def test_competing_reservation_is_reconciled(
    registration_integration, monkeypatch
):
    setup = registration_integration
    service = allocator(setup)
    client = setup["registry"].client
    original_write = client.transact_write_items
    competing = {}

    def compete_before_first_write(**kwargs):
        monkeypatch.setattr(
            client, "transact_write_items", original_write
        )
        competing["result"] = reserve(
            setup,
            service,
            request_id="competing-request",
        )
        return original_write(**kwargs)

    monkeypatch.setattr(
        client,
        "transact_write_items",
        compete_before_first_write,
    )

    result = reserve(setup, service)

    assert competing["result"]["contract_version"] == "v1"
    assert result["contract_version"] == "v2"