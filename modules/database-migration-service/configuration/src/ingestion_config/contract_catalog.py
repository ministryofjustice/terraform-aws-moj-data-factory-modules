from ingestion_config.contract_bindings import resolve_contract_execution_bindings
from ingestion_config.loader import ConfigurationError


IDENTITY_FIELDS = ("source", "database_name", "schema_name", "table_name")


def build_contract_catalog(
    artifact: dict, references: dict, *, account_id: str
) -> dict:
    runtime = artifact["runtime_configuration"]
    identities = {
        (
            runtime["source"],
            runtime["source_endpoint"]["database_name"],
            table["schema_name"],
            table["table_name"],
        )
        for group in runtime["task_groups"].values()
        for table in group["tables"]
    }
    registration = references["registration"]
    registry = references["registry"]

    catalog = {
        "catalog_version": 1,
        "environment": runtime["environment"],
        "account_id": account_id,
        "region": runtime["region"],
        "configuration_revision": runtime["configuration_revision"],
        "artifact_revision": artifact["artifact_revision"],
        "contracts": {
            "tables": [],
            "registration": registration,
            "registry": registry,
        },
    }

    for source, database, schema, table in sorted(identities):
        identity = dict(zip(IDENTITY_FIELDS, (source, database, schema, table)))
        generation = [
            deployment
            for deployment in references["generation"].values()
            if all(
                deployment[field] == identity[field]
                for field in IDENTITY_FIELDS[:-1]
            )
            and table in deployment["table_names"]
        ]
        validation = [
            deployment
            for deployment in references["validation"].values()
            if any(
                all(mapping[field] == identity[field] for field in IDENTITY_FIELDS)
                for mapping in deployment["source_mappings"]
            )
        ]
        if len(generation) != 1 or len(validation) != 1:
            raise ConfigurationError(
                f"{source}/{database}/{schema}/{table}: expected one generation "
                f"and one validation deployment, found {len(generation)} "
                f"generation and {len(validation)} validation deployments"
            )

        catalog["contracts"]["tables"].append(
            {
                **identity,
                "generation": {
                    "kind": generation[0]["kind"],
                    "arn": generation[0]["arn"],
                },
                "validation": {
                    "kind": validation[0]["kind"],
                    "arn": validation[0]["arn"],
                },
            }
        )

    context = {
        field: catalog[field]
        for field in (
            "environment",
            "account_id",
            "region",
            "configuration_revision",
            "artifact_revision",
        )
    }
    for entry in catalog["contracts"]["tables"]:
        resolve_contract_execution_bindings(
            {
                **context,
                "source": entry["source"],
                "database_name": entry["database_name"],
                "table": {
                    "schema_name": entry["schema_name"],
                    "table_name": entry["table_name"],
                },
            },
            catalog,
            generation_function_arn=entry["generation"]["arn"],
            validation_function_arn=entry["validation"]["arn"],
            registration_function_arn=registration["arn"],
            registry_table_name=registry["table_name"],
            registry_table_arn=registry["arn"],
        )

    return catalog