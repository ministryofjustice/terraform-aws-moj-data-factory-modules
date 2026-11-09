import os
import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from ingestion_config.json_documents import parse_json_object
from ingestion_config.published_documents import DocumentReference
from ingestion_config.published_resolver import TableLookup
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.identity import TableIdentity

from dms_runtime.contract_factory import (
    ContractObjectLocation,
    ContractRuntimeSettings,
    build_contract_runtime,
)
from dms_runtime.contract_handler import (
    ContractHandler,
    validate_contract_request,
)
from dms_runtime.json_values import to_json_value
from dms_runtime.ownership import RuntimeContext


MAX_CONFIGURATION_BYTES = 16384


def _parse_configuration(environment):
    raw = environment.get("CONTRACT_RUNTIME_CONFIG")

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            "CONTRACT_RUNTIME_CONFIG must contain deployment configuration"
        )

    try:
        encoded = raw.encode("utf-8")
    except UnicodeError as error:
        raise ValueError(
            "CONTRACT_RUNTIME_CONFIG must contain valid UTF-8"
        ) from error

    if len(encoded) > MAX_CONFIGURATION_BYTES:
        raise ValueError(
            "CONTRACT_RUNTIME_CONFIG exceeds 16 KiB"
        )

    document = parse_json_object(
        encoded,
        source="CONTRACT_RUNTIME_CONFIG",
    )

    expected = {
        "configuration_version",
        "registry_table_name",
        "lookup",
        "documents",
        "settings",
        "authorised_reviewers",
    }

    if (
        set(document) != expected
        or type(document["configuration_version"]) is not int
        or document["configuration_version"] != 1
    ):
        raise ValueError(
            "Unsupported contract runtime configuration structure or version"
        )

    try:
        lookup = TableLookup(**document["lookup"])

        RuntimeContext(
            environment=lookup.environment,
            account_id=lookup.account_id,
            region=lookup.region,
            source=lookup.source,
            database_name=lookup.database_name,
        )

        table = TableIdentity(
            environment=lookup.environment,
            source=lookup.source,
            database_name=lookup.database_name,
            schema_name=lookup.schema_name,
            table_name=lookup.table_name,
        )

        for revision in (
            lookup.configuration_revision,
            lookup.artifact_revision,
        ):
            if (
                not isinstance(revision, str)
                or re.fullmatch(r"[a-f0-9]{64}", revision) is None
            ):
                raise ValueError(
                    "Deployment revisions must contain SHA-256 digests"
                )

        documents = document["documents"]

        if (
            not isinstance(documents, dict)
            or set(documents) != {
                "bucket_name",
                "prefix",
                "bucket_owner_account_id",
                "manifest",
                "artifact",
                "catalog",
            }
        ):
            raise ValueError(
                "Published document configuration is incomplete"
            )

        references = {
            name: DocumentReference(**documents[name])
            for name in ("manifest", "artifact", "catalog")
        }

        supplied_settings = dict(document["settings"])

        for name in ("candidate", "approval", "evidence"):
            supplied_settings[name] = ContractObjectLocation(
                **supplied_settings[name]
            )

        supplied_settings["policy"] = ContractPolicy(
            **supplied_settings["policy"]
        )
        settings = ContractRuntimeSettings(**supplied_settings)

    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "Contract runtime deployment configuration is invalid"
        ) from error

    table_name = document["registry_table_name"]

    if (
        not isinstance(table_name, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table_name) is None
    ):
        raise ValueError(
            "registry_table_name must be a DynamoDB table name"
        )

    reviewers = document["authorised_reviewers"]

    if (
        not isinstance(reviewers, list)
        or any(
            not isinstance(reviewer, str)
            or not reviewer
            or reviewer != reviewer.strip()
            or len(reviewer.encode("utf-8")) > 256
            for reviewer in reviewers
        )
    ):
        raise ValueError(
            "authorised_reviewers must contain trimmed reviewer identifiers"
        )

    if len(reviewers) != len(set(reviewers)):
        raise ValueError(
            "authorised_reviewers must not contain duplicates"
        )

    if environment.get("AWS_REGION") != lookup.region:
        raise ValueError(
            "AWS_REGION must match the selected deployment region"
        )

    return {
        "lookup": lookup,
        "table": table,
        "table_name": table_name,
        "documents": documents,
        "references": references,
        "settings": settings,
        "reviewers": frozenset(reviewers),
    }


def build_handler(
    environment: Mapping[str, str],
    *,
    session: Any,
) -> ContractHandler:
    configuration = _parse_configuration(environment)
    lookup = configuration["lookup"]
    documents = configuration["documents"]
    references = configuration["references"]
    selected_table = configuration["table"]
    reviewers = configuration["reviewers"]

    def authorise(reviewer, table):
        if (
            not isinstance(reviewer, str)
            or reviewer not in reviewers
            or table != selected_table
        ):
            raise PermissionError(
                "Reviewer is not authorised for the selected contract table"
            )

    storage_config = Config(
        region_name=lookup.region,
        retries={
            "mode": "standard",
            "total_max_attempts": 3,
        },
    )

    invocation_config = Config(
        region_name=lookup.region,
        retries={
            "mode": "standard",
            "total_max_attempts": 1,
        },
        connect_timeout=5,
        read_timeout=910,
    )

    registry = IngestionRegistry(
        session.client(
            "dynamodb",
            config=storage_config,
        ),
        table_name=configuration["table_name"],
    )

    s3_client = session.client(
        "s3",
        config=storage_config,
    )

    runtime = build_contract_runtime(
        registry=registry,
        document_client=s3_client,
        dynamodb_resource=session.resource(
            "dynamodb",
            config=storage_config,
        ),
        lambda_client=session.client(
            "lambda",
            config=invocation_config,
        ),
        s3_client=s3_client,
        manifest_reference=references["manifest"],
        artifact_reference=references["artifact"],
        catalog_reference=references["catalog"],
        allowed_bucket=documents["bucket_name"],
        allowed_prefix=documents["prefix"],
        expected_bucket_owner=documents["bucket_owner_account_id"],
        lookup=lookup,
        settings=configuration["settings"],
        authorise=authorise,
    )

    return ContractHandler(runtime)


@lru_cache(maxsize=1)
def _get_handler() -> ContractHandler:
    return build_handler(
        os.environ,
        session=boto3.session.Session(),
    )


def lambda_handler(
    event: Any,
    context: Any,
) -> dict[str, Any]:
    validate_contract_request(event)

    return to_json_value(
        _get_handler().handle(event)
    )