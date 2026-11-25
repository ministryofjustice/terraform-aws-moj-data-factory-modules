import json
import os
import re
from dataclasses import asdict
from functools import lru_cache

import boto3
from botocore.config import Config

from dms_runtime.downstream_context import read_downstream_context
from dms_runtime.lookup import PinnedRuntimeLookup
from dms_runtime.ownership import RuntimeContext, RuntimeScopeDenied
from dms_runtime.processing_inputs import (
    S3ProcessingInputVerifier,
    read_pinned_input,
)
from dms_runtime.raw_history_component import RawHistoryTarget
from dms_runtime.raw_history_handler import RawHistoryHandler
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.identity import validate_database_identifier
from ingestion_registry.references import S3ObjectReference
from dms_runtime.downstream_binding_lookup import DownstreamComponentLookup


MAX_BATCH_BYTES = 64 * 1024 * 1024


def _required(environment, name):
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ValueError(
            f"{name} must be a non-empty trimmed setting"
        )
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"Duplicate configuration key: {key}"
            )
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _decode(raw):
    try:
        return json.loads(
            raw,
            object_pairs_hook=_object,
            parse_constant=_constant,
        )
    except (
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
    ) as error:
        raise ValueError(
            "Invalid Raw History configuration JSON"
        ) from error


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _bucket(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"bucket_name", "bucket_owner"}
        or not isinstance(value["bucket_name"], str)
        or re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
            value["bucket_name"],
        ) is None
        or not isinstance(value["bucket_owner"], str)
        or re.fullmatch(
            r"[0-9]{12}",
            value["bucket_owner"],
        ) is None
    ):
        raise ValueError(
            "Expected an explicit bucket name and 12-digit owner"
        )


def _prefix(value):
    if (
        not isinstance(value, str)
        or not value.endswith("/")
        or any(character in value for character in "*?\\")
        or any(
            part in {"", ".", ".."}
            or part != part.strip()
            for part in value[:-1].split("/")
        )
    ):
        raise ValueError(
            "Expected an explicit relative prefix ending in '/'"
        )


def _location(value):
    if (
        not isinstance(value, dict)
        or set(value) != {
            "bucket_name",
            "bucket_owner",
            "prefix",
        }
    ):
        raise ValueError(
            "Invalid Raw History input location"
        )

    _bucket({
        field: value[field]
        for field in ("bucket_name", "bucket_owner")
    })
    _prefix(value["prefix"])


def _overlap(left, right):
    return left.startswith(right) or right.startswith(left)


def validate_settings(settings, *, context, registry_table):
    fields = {
        "configuration_version",
        "context",
        "operation_table_name",
        "published_documents",
        "history_bucket",
        "max_batch_bytes",
        "tables",
    }

    if not isinstance(settings, dict) or set(settings) != fields:
        raise ValueError(
            "Unexpected Raw History configuration fields"
        )

    if (
        type(settings["configuration_version"]) is not int
        or settings["configuration_version"] != 1
    ):
        raise ValueError(
            "Raw History configuration_version must be 1"
        )

    if settings["context"] != asdict(context):
        raise ValueError(
            "Raw History configuration differs from the runtime context"
        )

    name = settings["operation_table_name"]
    if (
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", name) is None
        or name == registry_table
    ):
        raise ValueError(
            "A separate operation_id-keyed operations table is required"
        )

    size = settings["max_batch_bytes"]
    if type(size) is not int or not 1 <= size <= MAX_BATCH_BYTES:
        raise ValueError(
            "The bounded Raw History backend supports "
            "1 byte to 64 MiB per batch"
        )

    _location(settings["published_documents"])
    _bucket(settings["history_bucket"])

    tables = settings["tables"]
    if not isinstance(tables, list) or not tables:
        raise ValueError(
            "Raw History requires configured table destinations"
        )

    seen_tables = set()
    history_prefixes = []
    inputs = [settings["published_documents"]]

    for table in tables:
        if (
            not isinstance(table, dict)
            or set(table) != {
                "schema_name",
                "table_name",
                "profile",
                "history_prefix",
                "landings",
                "manifest_location",
                "contract_location",
            }
        ):
            raise ValueError(
                "Invalid Raw History table configuration"
            )

        for field in ("schema_name", "table_name"):
            validate_database_identifier(table[field], field)

        key = (table["schema_name"], table["table_name"])
        if key in seen_tables:
            raise ValueError(
                "Duplicate Raw History table configuration"
            )
        seen_tables.add(key)

        profile = table["profile"]
        if (
            not isinstance(profile, str)
            or not profile
            or profile != profile.strip()
        ):
            raise ValueError("A history profile is required")

        _prefix(table["history_prefix"])
        if any(
            _overlap(table["history_prefix"], prefix)
            for prefix in history_prefixes
        ):
            raise ValueError(
                "Canonical table history prefixes must not overlap"
            )
        history_prefixes.append(table["history_prefix"])

        landings = table["landings"]
        if not isinstance(landings, list) or not landings:
            raise ValueError(
                "At least one authorised Raw landing is required"
            )

        seen_landings = set()
        for location in landings:
            _location(location)
            key = (
                location["bucket_name"],
                location["prefix"],
            )
            if key in seen_landings:
                raise ValueError("Duplicate Raw landing")
            seen_landings.add(key)

        for field in ("manifest_location", "contract_location"):
            _location(table[field])

        inputs.extend(
            landings
            + [
                table["manifest_location"],
                table["contract_location"],
            ]
        )

    for location in inputs:
        if (
            location["bucket_name"]
            == settings["history_bucket"]["bucket_name"]
            and any(
                _overlap(location["prefix"], prefix)
                for prefix in history_prefixes
            )
        ):
            raise ValueError(
                "History destinations must not overlap input locations"
            )

    return settings


def build_handler(
    environment,
    *,
    session,
    invoked_function_arn,
):
    context = RuntimeContext(
        environment=_required(
            environment, "RUNTIME_ENVIRONMENT"
        ),
        account_id=_required(
            environment, "RUNTIME_ACCOUNT_ID"
        ),
        region=_required(environment, "AWS_REGION"),
        source=_required(environment, "RUNTIME_SOURCE"),
        database_name=_required(
            environment, "RUNTIME_DATABASE_NAME"
        ),
    )

    prefix = (
        f"arn:aws:lambda:{context.region}:"
        f"{context.account_id}:function:"
    )
    if (
        not isinstance(invoked_function_arn, str)
        or re.fullmatch(
            re.escape(prefix)
            + r"[A-Za-z0-9_-]{1,64}:[1-9][0-9]*",
            invoked_function_arn,
        ) is None
    ):
        raise ValueError(
            "Raw History must be invoked through its numeric Lambda version"
        )

    registry_name = _required(
        environment,
        "INGESTION_REGISTRY_TABLE_NAME",
    )
    if re.fullmatch(
        r"[A-Za-z0-9_.-]{3,255}",
        registry_name,
    ) is None:
        raise ValueError(
            "Invalid ingestion Registry table name"
        )

    reference = S3ObjectReference(**_decode(
        _required(
            environment,
            "RAW_HISTORY_CONFIGURATION",
        )
    ))

    config = Config(
        region_name=context.region,
        connect_timeout=5,
        read_timeout=30,
        retries={
            "mode": "standard",
            "total_max_attempts": 3,
        },
    )

    s3 = session.client("s3", config=config)
    raw = read_pinned_input(
        s3,
        reference,
        owner=context.account_id,
        limit=256 * 1024,
        label="Raw History configuration",
        size_error="Raw History configuration exceeds 256 KiB",
    )
    settings = validate_settings(
        _decode(raw),
        context=context,
        registry_table=registry_name,
    )

    registry = IngestionRegistry(
        session.client("dynamodb", config=config),
        table_name=registry_name,
    )

    documents = settings["published_documents"]
    base_lookup = PinnedRuntimeLookup(
        registry,
        s3,
        context=context,
        allowed_bucket=documents["bucket_name"],
        allowed_prefix=documents["prefix"],
        expected_bucket_owner=documents["bucket_owner"],
    )

    lookup = DownstreamComponentLookup(
        base_lookup,
        component="raw_history",
        registry=registry,
    )

    tables = {
        (table["schema_name"], table["table_name"]): table
        for table in settings["tables"]
    }

    def table_for(request):
        table = request["deployment"]["table"]
        selected = tables.get((
            table["schema_name"],
            table["table_name"],
        ))
        if selected is None:
            raise RuntimeScopeDenied(
                "Raw History has no configured destination for this table"
            )
        return selected

    def landing_for(request, table):
        landing = request["deployment"]["landing"]
        matches = [
            location
            for location in table["landings"]
            if all(
                location[field] == landing[field]
                for field in ("bucket_name", "prefix")
            )
        ]
        if len(matches) != 1:
            raise RuntimeScopeDenied(
                "Raw landing is outside configured locations"
            )
        return matches[0]

    def check_reference(reference, location):
        if (
            reference["bucket_name"] != location["bucket_name"]
            or not reference["object_key"].startswith(
                location["prefix"]
            )
        ):
            raise RuntimeScopeDenied(
                "Pinned input is outside configured locations"
            )

    def check_scope(request):
        ingestion_id = request["ingestion_id"]
        deployment = lookup.resolve(ingestion_id)

        if _canonical(deployment) != _canonical(
            request["deployment"]
        ):
            raise RuntimeScopeDenied(
                "Raw History request differs from the pinned deployment"
            )

        expected = read_downstream_context(
            registry=registry,
            lookup=lookup,
            ingestion_id=ingestion_id,
        )
        if request["execution_context"] != expected:
            raise RuntimeScopeDenied(
                "Raw History execution context differs from the Registry"
            )

        table = table_for(request)
        target = deployment.get(
            "component_bindings", {}
        ).get("raw_history")

        if (
            target != {
                "profile": table["profile"],
                "kind": "lambda",
                "arn": invoked_function_arn,
            }
            or deployment["table"].get("history_profile")
            != table["profile"]
        ):
            raise RuntimeScopeDenied(
                "Raw History must use its pinned profile "
                "and numeric Lambda version"
            )

        landing_for(request, table)
        check_reference(
            request["raw_batch"],
            table["manifest_location"],
        )

        contract = request["contract"]
        check_reference(
            {
                "bucket_name": contract["contract_bucket_name"],
                "object_key": contract["contract_object_key"],
            },
            table["contract_location"],
        )

        stage = registry._read(
            f"INGESTION#{ingestion_id}",
            "STAGE#EXTRACTION",
        )
        if (
            stage is None
            or stage.get("ingestion_id") != ingestion_id
            or stage.get("status") != "SUCCEEDED"
            or stage.get("result_reference") != {
                "kind": "raw-batch",
                **request["raw_batch"],
            }
        ):
            raise RuntimeScopeDenied(
                "Raw History requires the Registry's "
                "committed extraction manifest"
            )

    def resolve_target(request):
        table = table_for(request)
        landing = landing_for(request, table)
        history = settings["history_bucket"]

        verifier = S3ProcessingInputVerifier(
            s3,
            raw_bucket_owner=landing["bucket_owner"],
            manifest_bucket_owner=(
                table["manifest_location"]["bucket_owner"]
            ),
            contract_bucket_owner=(
                table["contract_location"]["bucket_owner"]
            ),
        )

        return RawHistoryTarget(
            client=s3,
            verifier=verifier,
            raw_bucket_owner=landing["bucket_owner"],
            history_bucket=history["bucket_name"],
            history_bucket_owner=history["bucket_owner"],
            history_prefix=table["history_prefix"],
            max_batch_bytes=settings["max_batch_bytes"],
            table_identity=(
                request["execution_context"]["table_identity"]
            ),
        )

    operations = session.resource(
        "dynamodb",
        region_name=context.region,
    ).Table(settings["operation_table_name"])

    return RawHistoryHandler(
        table=operations,
        resolve_target=resolve_target,
        check_scope=check_scope,
    )


@lru_cache(maxsize=1)
def _get_handler(invoked_function_arn):
    return build_handler(
        os.environ,
        session=boto3.session.Session(),
        invoked_function_arn=invoked_function_arn,
    )


def lambda_handler(event, context):
    return _get_handler(
        getattr(context, "invoked_function_arn", None)
    ).handle(event)