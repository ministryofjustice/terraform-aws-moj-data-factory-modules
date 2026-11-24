from copy import deepcopy
from decimal import Decimal
from typing import Any

from ingestion_registry.references import S3ObjectReference


class DownstreamContextError(ValueError):
    """Registry metadata and pinned inputs cannot form a valid context."""


def _identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise DownstreamContextError(f"Invalid {field}")
    return value


def _revision(value: Any, field: str) -> str:
    value = _identifier(value, field)
    if len(value) != 64 or any(
        character not in "0123456789abcdef"
        for character in value
    ):
        raise DownstreamContextError(f"Invalid {field}")
    return value


def build_downstream_context(
    *,
    ingestion_id: str,
    metadata: dict[str, Any],
    deployment: dict[str, Any],
    contract_stage: dict[str, Any],
) -> dict[str, Any]:
    ingestion_id = _identifier(ingestion_id, "ingestion_id")

    try:
        if metadata["ingestion_id"] != ingestion_id:
            raise DownstreamContextError(
                "Ingestion ID differs from metadata"
            )

        version = metadata["ingestion_version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, (int, Decimal))
            or version != int(version)
            or version < 1
        ):
            raise DownstreamContextError(
                "Invalid ingestion_version"
            )

        operation = metadata["operation"]
        if operation not in {"INITIAL_INGESTION", "REINGESTION"}:
            raise DownstreamContextError(
                "Unsupported ingestion operation"
            )

        identity = metadata["table_identity"]
        table_identity = {
            field: _identifier(identity[field], field)
            for field in (
                "environment",
                "source",
                "database_name",
                "schema_name",
                "table_name",
            )
        }

        for field in ("environment", "source", "database_name"):
            if deployment[field] != table_identity[field]:
                raise DownstreamContextError(
                    f"Pinned {field} differs from metadata"
                )

        for field in ("schema_name", "table_name"):
            if deployment["table"][field] != table_identity[field]:
                raise DownstreamContextError(
                    f"Pinned {field} differs from metadata"
                )

        revisions = {}
        for field in (
            "configuration_revision",
            "artifact_revision",
        ):
            revisions[field] = _revision(metadata[field], field)
            if deployment[field] != revisions[field]:
                raise DownstreamContextError(
                    f"Pinned {field} differs from metadata"
                )

        generation = _identifier(
            metadata["extraction_generation"],
            "extraction_generation",
        )
        if deployment["extraction_generation"] != generation:
            raise DownstreamContextError(
                "Pinned extraction generation differs"
            )

        if (
            contract_stage["ingestion_id"] != ingestion_id
            or contract_stage["stage"] != "CONTRACT"
            or contract_stage["status"] != "SUCCEEDED"
        ):
            raise DownstreamContextError(
                "A completed CONTRACT stage is required"
            )

        contract = contract_stage["result_reference"]
        if contract["kind"] != "registered-contract":
            raise DownstreamContextError(
                "A registered contract pin is required"
            )

        for prefix in ("contract", "approval"):
            S3ObjectReference(
                bucket_name=contract[f"{prefix}_bucket_name"],
                object_key=contract[f"{prefix}_object_key"],
                version_id=contract[
                    f"{prefix}_object_version_id"
                ],
                sha256=contract[f"{prefix}_object_sha256"],
            )

        _identifier(contract["contract_id"], "contract_id")
        _revision(
            contract["contract_fingerprint"],
            "contract_fingerprint",
        )

        contract_version = _identifier(
            contract["contract_version"],
            "contract_version",
        )
        suffix = contract_version[1:]
        if (
            not contract_version.startswith("v")
            or not suffix.isascii()
            or not suffix.isdecimal()
            or suffix.startswith("0")
        ):
            raise DownstreamContextError(
                "Invalid contract_version"
            )

    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
    ) as error:
        if isinstance(error, DownstreamContextError):
            raise
        raise DownstreamContextError(
            "Incomplete or invalid downstream context"
        ) from error

    return {
        "context_version": 1,
        "ingestion_id": ingestion_id,
        "ingestion_version": int(version),
        "operation": operation,
        "table_identity": table_identity,
        **revisions,
        "extraction_generation": generation,
        "contract": deepcopy(contract),
    }


def read_downstream_context(
    *,
    registry,
    lookup,
    ingestion_id: str,
) -> dict[str, Any]:
    deployment = lookup.resolve(ingestion_id)

    partition_key = f"INGESTION#{ingestion_id}"
    metadata = registry._read(partition_key, "META")
    contract_stage = registry._read(
        partition_key,
        "STAGE#CONTRACT",
    )

    if metadata is None or contract_stage is None:
        raise DownstreamContextError(
            "Ingestion metadata or contract pin is missing"
        )

    return build_downstream_context(
        ingestion_id=ingestion_id,
        metadata=metadata,
        deployment=deployment,
        contract_stage=contract_stage,
    )


def validate_request_context(request: dict[str, Any]) -> None:
    """Validate supplied context against the component request envelope."""
    context = request.get("execution_context")

    if (
        not isinstance(context, dict)
        or type(context.get("context_version")) is not int
        or context["context_version"] != 1
    ):
        raise DownstreamContextError(
            "Expected execution context version 1"
        )

    try:
        expected = build_downstream_context(
            ingestion_id=request["ingestion_id"],
            metadata=context,
            deployment=request["deployment"],
            contract_stage={
                "ingestion_id": request["ingestion_id"],
                "stage": "CONTRACT",
                "status": "SUCCEEDED",
                "result_reference": request["contract"],
            },
        )
    except (KeyError, TypeError) as error:
        raise DownstreamContextError(
            "Incomplete component request"
        ) from error

    if context != expected:
        raise DownstreamContextError(
            "Execution context differs from component request"
        )
