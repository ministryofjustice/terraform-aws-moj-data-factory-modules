import os
import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from ingestion_registry.admission import IngestionRegistry

from dms_runtime.full_load import FullLoadReadiness
from dms_runtime.guard_factory import RuntimeGuardFactory
from dms_runtime.handler import RuntimeHandler
from dms_runtime.inspection import TaskInspection
from dms_runtime.json_values import to_json_value
from dms_runtime.lookup import PinnedRuntimeLookup
from dms_runtime.owned_actions import OwnedRuntimeActions
from dms_runtime.ownership import RuntimeContext
from dms_runtime.preflight import SourcePreflight


def _required(
    environment: Mapping[str, str],
    name: str,
) -> str:
    value = environment.get(name)

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ValueError(
            f"{name} must be a non-empty trimmed deployment setting"
        )

    return value


def build_handler(
    environment: Mapping[str, str],
    *,
    session: Any,
) -> RuntimeHandler:
    context = RuntimeContext(
        environment=_required(
            environment,
            "RUNTIME_ENVIRONMENT",
        ),
        account_id=_required(
            environment,
            "RUNTIME_ACCOUNT_ID",
        ),
        region=_required(
            environment,
            "AWS_REGION",
        ),
        source=_required(
            environment,
            "RUNTIME_SOURCE",
        ),
        database_name=_required(
            environment,
            "RUNTIME_DATABASE_NAME",
        ),
    )

    table_name = _required(
        environment,
        "INGESTION_REGISTRY_TABLE_NAME",
    )

    if re.fullmatch(
        r"[A-Za-z0-9_.-]{3,255}",
        table_name,
    ) is None:
        raise ValueError(
            "INGESTION_REGISTRY_TABLE_NAME must be a DynamoDB table name"
        )

    document_bucket = _required(
        environment,
        "PUBLISHED_DOCUMENT_BUCKET",
    )
    document_prefix = _required(
        environment,
        "PUBLISHED_DOCUMENT_PREFIX",
    )
    bucket_owner = _required(
        environment,
        "PUBLISHED_DOCUMENT_BUCKET_OWNER",
    )

    if (
        document_prefix.startswith("/")
        or not document_prefix.endswith("/")
        or "\\" in document_prefix
        or any(
            segment in ("", ".", "..")
            or segment != segment.strip()
            for segment in document_prefix[:-1].split("/")
        )
    ):
        raise ValueError(
            "PUBLISHED_DOCUMENT_PREFIX must contain explicit "
            "path segments and end with '/'"
        )

    if re.fullmatch(r"[0-9]{12}", bucket_owner) is None:
        raise ValueError(
            "PUBLISHED_DOCUMENT_BUCKET_OWNER must contain 12 digits"
        )

    credential_mode = _required(
        environment,
        "SOURCE_CREDENTIAL_MODE",
    )
    sync_arn = environment.get("CREDENTIAL_SYNC_FUNCTION_ARN")

    if credential_mode == "connection_test":
        if sync_arn is not None:
            raise ValueError(
                "connection_test must omit CREDENTIAL_SYNC_FUNCTION_ARN"
            )

        sync_arn = None

    elif credential_mode == "synchronise_then_test":
        sync_arn = _required(
            environment,
            "CREDENTIAL_SYNC_FUNCTION_ARN",
        )

    else:
        raise ValueError(
            "SOURCE_CREDENTIAL_MODE must be connection_test "
            "or synchronise_then_test"
        )

    command_config = Config(
        region_name=context.region,
        retries={
            "mode": "standard",
            "total_max_attempts": 1,
        },
        connect_timeout=5,
        read_timeout=30,
    )

    sync_config = Config(
        region_name=context.region,
        retries={
            "mode": "standard",
            "total_max_attempts": 1,
        },
        connect_timeout=5,
        read_timeout=330,
    )

    storage_config = Config(
        region_name=context.region,
        retries={
            "mode": "standard",
            "total_max_attempts": 3,
        },
    )

    registry = IngestionRegistry(
        session.client(
            "dynamodb",
            config=storage_config,
        ),
        table_name=table_name,
    )

    document_client = session.client(
        "s3",
        config=storage_config,
    )
    dms_client = session.client(
        "dms",
        config=command_config,
    )
    lambda_client = session.client(
        "lambda",
        config=sync_config,
    )

    lookup = PinnedRuntimeLookup(
        registry,
        document_client,
        context=context,
        allowed_bucket=document_bucket,
        allowed_prefix=document_prefix,
        expected_bucket_owner=bucket_owner,
    )

    inspection = TaskInspection(dms_client)
    preflight = SourcePreflight(
        dms_client,
        lambda_client,
    )

    owned_actions = OwnedRuntimeActions(
        guard_factory=RuntimeGuardFactory(
            registry,
            context=context,
        ),
        dms_client=dms_client,
        inspection=inspection,
        preflight=preflight,
        credential_sync_function_arn=sync_arn,
    )

    return RuntimeHandler(
        lookup=lookup,
        inspection=inspection,
        full_load=FullLoadReadiness(inspection),
        owned_action_handler=owned_actions,
    )


@lru_cache(maxsize=1)
def _get_handler() -> RuntimeHandler:
    return build_handler(
        os.environ,
        session=boto3.session.Session(),
    )


def lambda_handler(
    event: Any,
    context: Any,
) -> dict[str, Any]:
    return to_json_value(
        _get_handler().handle(event)
    )