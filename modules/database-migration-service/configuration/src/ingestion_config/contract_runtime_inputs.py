import hashlib

from ingestion_config.compiler import canonical_json
from ingestion_config.loader import ConfigurationError


def build_contract_runtime_configuration(
    *,
    artifact: dict,
    catalog: dict,
    registry: dict,
    published_documents: dict,
    settings: dict,
    authorised_reviewers: list[str],
) -> str:
    """Build the Lambda configuration for one prepared deployment."""
    runtime = artifact["runtime_configuration"]
    tables = catalog["contracts"]["tables"]
    source = runtime["source"]

    if not tables:
        raise ConfigurationError(f"{source}: contract catalog has no tables")

    default_table = tables[0]
    catalog_digest = hashlib.sha256(
        canonical_json(catalog).encode("utf-8")
    ).hexdigest()

    configuration = {
        "configuration_version": 1,
        "registry_table_name": registry["table_name"],
        "lookup": {
            "environment": runtime["environment"],
            "account_id": catalog["account_id"],
            "region": runtime["region"],
            "source": source,
            "database_name": runtime["source_endpoint"]["database_name"],
            "schema_name": default_table["schema_name"],
            "table_name": default_table["table_name"],
            "configuration_revision": runtime["configuration_revision"],
            "artifact_revision": artifact["artifact_revision"],
        },
        "documents": {
            "bucket_name": published_documents["bucket_name"],
            "prefix": published_documents["prefix"],
            "bucket_owner_account_id": published_documents["bucket_owner"],
            "extraction_generation": artifact["extraction_generation"],
            "catalog_sha256": catalog_digest,
        },
        "settings": settings,
        "authorised_reviewers": authorised_reviewers,
    }

    return canonical_json(configuration)