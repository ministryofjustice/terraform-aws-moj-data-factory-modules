import hashlib
import json
import re
from typing import Any

from ingestion_config.published_documents import DocumentReference
from ingestion_config.published_resolver import (
    TableLookup,
    resolve_published_table,
)
from ingestion_registry.admission import IngestionRegistry

from dms_runtime.ownership import (
    RuntimeContext,
    RuntimeScopeDenied,
)


class PinnedRuntimeLookup:
    def __init__(
        self,
        registry: IngestionRegistry,
        document_client: Any,
        *,
        context: RuntimeContext,
        allowed_bucket: str,
        allowed_prefix: str,
        expected_bucket_owner: str,
    ) -> None:
        if not isinstance(context, RuntimeContext):
            raise ValueError(
                "context must be a RuntimeContext"
            )

        self.registry = registry
        self.document_client = document_client
        self.context = context
        self.allowed_bucket = allowed_bucket
        self.allowed_prefix = allowed_prefix
        self.expected_bucket_owner = expected_bucket_owner

    def resolve(
        self,
        ingestion_id: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(ingestion_id, str)
            or not ingestion_id
            or ingestion_id != ingestion_id.strip()
            or len(ingestion_id.encode("utf-8")) > 256
        ):
            raise ValueError(
                "ingestion_id must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        pk = f"INGESTION#{ingestion_id}"
        metadata = self.registry._read(pk, "META")

        if metadata is None:
            raise RuntimeScopeDenied(
                "Ingestion does not exist"
            )

        pinned = self.registry._read(pk, "BINDING")

        if pinned is None:
            raise RuntimeScopeDenied(
                "Ingestion has no pinned resource binding"
            )

        try:
            identity = metadata["table_identity"]

            for field in (
                "environment",
                "source",
                "database_name",
            ):
                if identity[field] != getattr(self.context, field):
                    raise RuntimeScopeDenied(
                        "Ingestion is outside "
                        "the authorised service context"
                    )

            binding = pinned["binding"]

            for field in (
                "environment",
                "account_id",
                "region",
                "source",
                "database_name",
            ):
                if binding[field] != getattr(self.context, field):
                    raise RuntimeScopeDenied(
                        "Pinned binding is outside "
                        "the authorised service context"
                    )

            for field in ("schema_name", "table_name"):
                if binding["table"][field] != identity[field]:
                    raise RuntimeScopeDenied(
                        "Pinned binding does not match "
                        "the ingestion table"
                    )

            for field in (
                "configuration_revision",
                "artifact_revision",
                "extraction_generation",
            ):
                if binding[field] != metadata[field]:
                    raise RuntimeScopeDenied(
                        "Pinned binding does not match "
                        "the ingestion revisions"
                    )

            digest = pinned["binding_fingerprint"]

            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            ):
                raise RuntimeScopeDenied(
                    "Pinned binding has no valid fingerprint"
                )

            references = binding["document_references"]

            manifest = DocumentReference(
                **references["manifest"]
            )
            artifact = DocumentReference(
                **references["artifact"]
            )
            catalog = (
                DocumentReference(**references["catalog"])
                if "catalog" in references
                else None
            )

            components = binding.get("component_bindings", {})
            contracts = binding.get("contract_bindings", {})

            if (
                not isinstance(components, dict)
                or not isinstance(contracts, dict)
            ):
                raise RuntimeScopeDenied(
                    "Pinned capability bindings must be objects"
                )

            lookup = TableLookup(
                environment=self.context.environment,
                account_id=self.context.account_id,
                region=self.context.region,
                source=self.context.source,
                database_name=self.context.database_name,
                schema_name=identity["schema_name"],
                table_name=identity["table_name"],
                configuration_revision=metadata[
                    "configuration_revision"
                ],
                artifact_revision=metadata["artifact_revision"],
            )

        except (KeyError, TypeError) as error:
            raise RuntimeScopeDenied(
                "Ingestion resource binding is incomplete"
            ) from error

        result = resolve_published_table(
            self.document_client,
            manifest_reference=manifest,
            artifact_reference=artifact,
            allowed_bucket=self.allowed_bucket,
            allowed_prefix=self.allowed_prefix,
            expected_bucket_owner=self.expected_bucket_owner,
            lookup=lookup,
            catalog_reference=catalog,
            required_components=tuple(sorted(components)),
            required_contract_capabilities=tuple(
                sorted(contracts)
            ),
        )

        try:
            encoded = json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")

        except (TypeError, ValueError) as error:
            raise RuntimeScopeDenied(
                "Resolved binding is not valid JSON"
            ) from error

        if (
            len(encoded) > 65536
            or hashlib.sha256(encoded).hexdigest() != digest
        ):
            raise RuntimeScopeDenied(
                "Published resources do not match "
                "the ingestion's pinned binding"
            )

        return result