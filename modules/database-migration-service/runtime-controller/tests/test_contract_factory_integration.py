import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from ingestion_config.loader import ConfigurationError
from ingestion_config.published_documents import DocumentReference
from ingestion_config.published_resolver import (
    TableLookup,
    resolve_published_table,
)

from dms_runtime import contract_factory
from dms_runtime.contract_factory import build_contract_runtime

from test_contract_factory import (
    factory_setup as factory_setup,
)


FIXTURES = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "contract_deployments"
)


@pytest.fixture(params=["postgres", "oracle"])
def published_factory(factory_setup, monkeypatch, request):
    setup = factory_setup
    directory = FIXTURES / request.param

    documents = {
        name: json.loads(
            (directory / f"{name}.json").read_text(
                encoding="utf-8"
            )
        )
        for name in ("manifest", "artifact", "catalog")
    }
    manifest = documents["manifest"]
    group = next(iter(manifest["task_groups"].values()))
    table = group["tables"][0]
    owner = manifest["account_id"]
    bucket = "example-platform-configuration"
    prefix = "deployments/development/service/"

    objects = {}
    references = {}

    for name in ("manifest", "artifact", "catalog"):
        raw = (directory / f"{name}.json").read_bytes()
        key = f"{prefix}{name}.json"
        version = f"{name}-version-one"

        objects[(bucket, key, version)] = raw
        references[name] = DocumentReference(
            bucket_name=bucket,
            object_key=key,
            version_id=version,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    reads = []

    def get_object(**kwargs):
        reads.append(kwargs)
        assert kwargs["ExpectedBucketOwner"] == owner

        identity = (
            kwargs["Bucket"],
            kwargs["Key"],
            kwargs["VersionId"],
        )

        return {
            "Body": io.BytesIO(objects[identity]),
            "VersionId": kwargs["VersionId"],
        }

    document_client = Mock()
    document_client.get_object.side_effect = get_object

    settings = setup["arguments"]["settings"]
    settings = replace(
        settings,
        candidate=replace(
            settings.candidate,
            bucket_owner_account_id=owner,
        ),
        approval=replace(
            settings.approval,
            bucket_owner_account_id=owner,
        ),
        evidence=replace(
            settings.evidence,
            bucket_owner_account_id=owner,
        ),
        approved_bucket_owner_account_id=owner,
    )

    arguments = {
        **setup["arguments"],
        "document_client": document_client,
        "manifest_reference": references["manifest"],
        "artifact_reference": references["artifact"],
        "catalog_reference": references["catalog"],
        "allowed_bucket": bucket,
        "allowed_prefix": "deployments/development/",
        "expected_bucket_owner": owner,
        "settings": settings,
        "lookup": TableLookup(
            environment=manifest["environment"],
            account_id=owner,
            region=manifest["region"],
            source=manifest["source"],
            database_name=manifest["database_name"],
            schema_name=table["schema_name"],
            table_name=table["table_name"],
            configuration_revision=manifest["configuration_revision"],
            artifact_revision=manifest["artifact_revision"],
        ),
    }

    monkeypatch.setattr(
        contract_factory,
        "resolve_published_table",
        resolve_published_table,
    )

    return {
        **setup,
        "arguments": arguments,
        "documents": documents,
        "references": references,
        "objects": objects,
        "reads": reads,
    }


def test_factory_resolves_versioned_documents_and_selected_targets(
    published_factory,
):
    setup = published_factory

    runtime = build_contract_runtime(**setup["arguments"])
    coordinator = runtime.orchestration
    catalog = setup["documents"]["catalog"]["contracts"]
    selected = catalog["tables"][0]

    assert coordinator.preparation.completion.generator.function_arn == (
        selected["generation"]["arn"]
    )
    assert coordinator.validator.validation_function_arn == (
        selected["validation"]["arn"]
    )
    assert (
        coordinator.activation.completion.registrar.registration_function_arn
        == catalog["registration"]["arn"]
    )

    setup["arguments"]["dynamodb_resource"].Table.assert_called_once_with(
        catalog["registry"]["table_name"]
    )

    assert runtime.table.source == selected["source"]
    assert runtime.table.database_name == selected["database_name"]
    assert runtime.table.schema_name == selected["schema_name"]
    assert runtime.table.table_name == selected["table_name"]

    assert [item["VersionId"] for item in setup["reads"]] == [
        "manifest-version-one",
        "artifact-version-one",
        "catalog-version-one",
    ]

    setup["arguments"]["lambda_client"].invoke.assert_not_called()
    setup["registry"].client.transact_write_items.assert_not_called()


@pytest.mark.parametrize("name", ["manifest", "artifact", "catalog"])
def test_tampered_document_blocks_factory_before_capability_use(
    published_factory,
    name,
):
    setup = published_factory
    reference = setup["references"][name]

    setup["objects"][
        (
            reference.bucket_name,
            reference.object_key,
            reference.version_id,
        )
    ] = b'{"tampered":true}'

    with pytest.raises(ConfigurationError, match="SHA-256"):
        build_contract_runtime(**setup["arguments"])

    setup["arguments"]["dynamodb_resource"].Table.assert_not_called()
    setup["arguments"]["lambda_client"].invoke.assert_not_called()
    setup["registry"].client.transact_write_items.assert_not_called()


def test_newer_document_versions_do_not_change_selected_targets(
    published_factory,
):
    setup = published_factory

    first = build_contract_runtime(**setup["arguments"])

    for reference in setup["references"].values():
        setup["objects"][
            (
                reference.bucket_name,
                reference.object_key,
                "newer-version",
            )
        ] = b'{"replacement":true}'

    repeated = build_contract_runtime(**setup["arguments"])

    assert (
        repeated.orchestration.preparation.context
        == first.orchestration.preparation.context
    )
    assert (
        repeated.orchestration.validator.validation_function_arn
        == first.orchestration.validator.validation_function_arn
    )
    assert [item["VersionId"] for item in setup["reads"][-3:]] == [
        "manifest-version-one",
        "artifact-version-one",
        "catalog-version-one",
    ]


def test_different_pinned_revision_blocks_factory(published_factory):
    setup = published_factory
    arguments = {
        **setup["arguments"],
        "lookup": replace(
            setup["arguments"]["lookup"],
            artifact_revision="0" * 64,
        ),
    }

    with pytest.raises(ConfigurationError, match="artifact_revision"):
        build_contract_runtime(**arguments)

    setup["arguments"]["dynamodb_resource"].Table.assert_not_called()
    setup["arguments"]["lambda_client"].invoke.assert_not_called()