from dataclasses import asdict, dataclass
from typing import Any

from ingestion_config.component_bindings import (
    resolve_component_bindings,
)
from ingestion_config.contract_bindings import (
    resolve_contract_bindings,
)
from ingestion_config.loader import ConfigurationError
from ingestion_config.published_documents import (
    DocumentReference,
    S3DocumentClient,
    load_published_document,
)
from ingestion_config.resolver import resolve_table_binding


@dataclass(frozen=True)
class TableLookup:
    environment: str
    account_id: str
    region: str
    source: str
    database_name: str
    schema_name: str
    table_name: str
    configuration_revision: str
    artifact_revision: str


def resolve_published_table(
    client: S3DocumentClient,
    *,
    manifest_reference: DocumentReference,
    artifact_reference: DocumentReference,
    allowed_bucket: str,
    allowed_prefix: str,
    expected_bucket_owner: str,
    lookup: TableLookup,
    catalog_reference: DocumentReference | None = None,
    required_components: tuple[str, ...] = (),
    required_contract_capabilities: tuple[str, ...] = (),
) -> dict[str, Any]:
    requires_catalog = bool(
        required_components or required_contract_capabilities
    )

    if requires_catalog and catalog_reference is None:
        raise ConfigurationError(
            "Requested capabilities require a pinned component catalog"
        )

    def read(reference: DocumentReference) -> dict[str, Any]:
        return load_published_document(
            client,
            reference,
            allowed_bucket=allowed_bucket,
            allowed_prefix=allowed_prefix,
            expected_bucket_owner=expected_bucket_owner,
        )

    manifest = read(manifest_reference)
    artifact = read(artifact_reference)

    result = resolve_table_binding(
        manifest,
        artifact,
        **asdict(lookup),
    )

    result["document_references"] = {
        "manifest": asdict(manifest_reference),
        "artifact": asdict(artifact_reference),
    }

    if catalog_reference is not None:
        catalog = read(catalog_reference)

        # This also checks the catalog context and revisions when
        # no downstream capabilities were requested
        components = resolve_component_bindings(
            result,
            catalog,
            required_components=list(required_components),
        )

        contracts = {}

        if required_contract_capabilities:
            contracts = resolve_contract_bindings(
                result,
                catalog,
                required_capabilities=list(
                    required_contract_capabilities
                ),
            )

        result["component_bindings"] = components
        result["contract_bindings"] = contracts
        result["document_references"]["catalog"] = asdict(
            catalog_reference
        )

    return result