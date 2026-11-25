import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock

import pytest

from dms_runtime.downstream_binding_lookup import (
    DownstreamComponentLookup,
)
from dms_runtime.processing_inputs import ProcessingInputError
from ingestion_registry.downstream_bindings import (
    DownstreamBindingConflict,
)
from ingestion_registry.references import S3ObjectReference

from test_downstream_binding_registry import digest, setup
from test_ownership import scope_setup


@pytest.fixture
def component_setup(setup):
    binding = setup["binding"]
    documents = {}

    catalog = {
        "catalog_version": 1,
        **{
            field: binding[field]
            for field in (
                "environment",
                "account_id",
                "region",
                "configuration_revision",
                "artifact_revision",
            )
        },
        "components": {
            "processing": {
                "standard": {
                    "kind": "lambda",
                    "arn": (
                        "arn:aws:lambda:eu-west-2:"
                        "111122223333:function:processing:4"
                    ),
                },
            },
            "raw_history": {
                "standard": {
                    "kind": "lambda",
                    "arn": (
                        "arn:aws:lambda:eu-west-2:"
                        "111122223333:function:history:3"
                    ),
                },
            },
            "publication": {
                "sandbox": {
                    "kind": "lambda",
                    "arn": (
                        "arn:aws:lambda:eu-west-2:"
                        "111122223333:function:publication:5"
                    ),
                },
            },
        },
    }

    def publish(value):
        raw = json.dumps(value).encode("utf-8")
        version = f"catalog-{len(documents) + 1}"
        documents[version] = raw

        return S3ObjectReference(
            bucket_name="example-config",
            object_key="published/components.json",
            version_id=version,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    def get_object(**arguments):
        assert arguments["Bucket"] == "example-config"
        assert arguments["Key"] == "published/components.json"
        assert arguments["ExpectedBucketOwner"] == "111122223333"

        version = arguments["VersionId"]

        return {
            "VersionId": version,
            "Body": BytesIO(documents[version]),
        }

    base = Mock()
    base.registry = setup["registry"]
    base.resolve.return_value = deepcopy(binding)
    base.allowed_bucket = "example-config"
    base.allowed_prefix = "published/"
    base.expected_bucket_owner = "111122223333"
    base.document_client.get_object.side_effect = get_object

    def lookup(component="raw_history"):
        return DownstreamComponentLookup(
            base,
            component=component,
            registry=setup["registry"],
            clock=lambda: setup["clock"]["now"],
        )

    return {
        **setup,
        "base": base,
        "catalog": catalog,
        "reference": publish(catalog),
        "publish": publish,
        "lookup": lookup,
    }


def test_resolves_target_without_changing_extraction(component_setup):
    state = component_setup
    registry = state["registry"]

    before = {
        key: registry._read(state["pk"], key)
        for key in ("BINDING", "STAGE#EXTRACTION")
    }

    lookup = state["lookup"]()
    record = lookup.pin(
        state["claim"],
        catalog_reference=state["reference"],
    )

    resolved = lookup.resolve(state["claim"].ingestion_id)

    assert resolved["component_bindings"]["raw_history"] == (
        state["target"]
    )
    assert resolved["downstream_binding"]["binding_sha256"] == (
        record["binding_sha256"]
    )

    for field in (
        "landing",
        "resources",
        "document_references",
    ):
        assert resolved[field] == state["binding"][field]

    for key, value in before.items():
        assert registry._read(state["pk"], key) == value

    assert state["base"].resolve.return_value == state["binding"]


def test_missing_pin_does_not_invent_target(component_setup):
    state = component_setup

    assert state["lookup"]().resolve(
        state["claim"].ingestion_id
    ) == state["binding"]

    state["base"].document_client.get_object.assert_not_called()


def test_original_component_target_is_preserved(component_setup):
    state = component_setup
    original = {
        "profile": "standard",
        "kind": "lambda",
        "arn": (
            "arn:aws:lambda:eu-west-2:"
            "111122223333:function:original-history:2"
        ),
    }

    state["base"].resolve.return_value[
        "component_bindings"
    ]["raw_history"] = original

    resolved = state["lookup"]().resolve(
        state["claim"].ingestion_id
    )

    assert resolved["component_bindings"]["raw_history"] == original
    state["base"].document_client.get_object.assert_not_called()


def test_catalog_checksum_is_verified_before_pin(component_setup):
    state = component_setup
    invalid = replace(
        state["reference"],
        sha256="f" * 64,
    )

    with pytest.raises(ProcessingInputError, match="checksum"):
        state["lookup"]().pin(
            state["claim"],
            catalog_reference=invalid,
        )

    assert state["registry"]._read(
        state["pk"],
        "DOWNSTREAM_BINDING#raw_history",
    ) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"bucket_name": "another-config-bucket"},
        {"object_key": "outside/components.json"},
    ],
)
def test_catalog_boundary_is_enforced(component_setup, changes):
    state = component_setup
    invalid = replace(
        state["reference"],
        **changes,
    )

    with pytest.raises(
        DownstreamBindingConflict,
        match="published document boundary",
    ):
        state["lookup"]().pin(
            state["claim"],
            catalog_reference=invalid,
        )

    state["base"].document_client.get_object.assert_not_called()


def test_publication_pin_does_not_change_history_request(
    component_setup,
):
    state = component_setup
    history = state["lookup"]()

    history.pin(
        state["claim"],
        catalog_reference=state["reference"],
    )
    before = history.resolve(state["claim"].ingestion_id)

    state["progress"].record_stage(
        state["claim"],
        stage="HISTORY_BASELINE",
        status="RUNNING",
        update_id="history-started",
        expected_revision=0,
        result_reference={
            "kind": "raw-history-operation",
        },
    )

    catalog = deepcopy(state["catalog"])
    catalog["components"]["publication"]["sandbox"]["arn"] = (
        "arn:aws:lambda:eu-west-2:"
        "111122223333:function:publication:6"
    )

    publication = state["lookup"]("publication")
    publication.pin(
        state["claim"],
        catalog_reference=state["publish"](catalog),
    )

    assert history.resolve(
        state["claim"].ingestion_id
    ) == before

    resolved = publication.resolve(
        state["claim"].ingestion_id
    )

    assert resolved["component_bindings"]["publication"][
        "arn"
    ].endswith(":publication:6")

    assert "raw_history" not in resolved["component_bindings"]


def test_record_target_must_match_versioned_catalog(component_setup):
    state = component_setup
    lookup = state["lookup"]()

    record = lookup.pin(
        state["claim"],
        catalog_reference=state["reference"],
    )

    payload = deepcopy(record["payload"])
    payload["target"]["arn"] = (
        "arn:aws:lambda:eu-west-2:"
        "111122223333:function:history:8"
    )

    registry = state["registry"]

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {
                "pk": state["pk"],
                "sk": record["sk"],
            }
        ),
        UpdateExpression=(
            "SET payload = :payload,"
            " binding_sha256 = :sha"
        ),
        ExpressionAttributeValues=registry._encode(
            {
                ":payload": payload,
                ":sha": digest(payload),
            }
        ),
    )

    with pytest.raises(
        DownstreamBindingConflict,
        match="versioned component catalog",
    ):
        lookup.resolve(state["claim"].ingestion_id)