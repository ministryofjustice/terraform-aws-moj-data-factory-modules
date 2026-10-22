import argparse
from pathlib import Path
from typing import Any

from ingestion_config.deployment import load_deployment
from ingestion_config.loader import ConfigurationError
from ingestion_config.json_documents import load_json_object
from ingestion_config.manifest_builder import (
    build_deployment_manifest,
)
from ingestion_config.resolver import resolve_table_binding


def add_deployment_commands(commands: Any) -> None:
    build = commands.add_parser(
        "build-manifest",
        help="Build a manifest from compiler and Terraform outputs",
    )
    build.add_argument(
        "--artifact",
        type=Path,
        required=True,
    )
    build.add_argument(
        "--bindings",
        type=Path,
        required=True,
        help=(
            "JSON value from: "
            "terraform output -json deployment_bindings"
        ),
    )
    build.add_argument(
        "--account-id",
        required=True,
        help="AWS account from the trusted deployment context",
    )
    build.add_argument(
        "--output",
        type=Path,
        required=True,
    )

    resolve = commands.add_parser(
        "resolve-table",
        help="Resolve one selected table using pinned revisions",
    )
    resolve.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    resolve.add_argument(
        "--artifact",
        type=Path,
        required=True,
    )
    resolve.add_argument(
        "--output",
        type=Path,
        required=True,
    )

    for argument in (
        "environment",
        "account-id",
        "region",
        "source",
        "database-name",
        "schema-name",
        "table-name",
        "configuration-revision",
        "artifact-revision",
    ):
        resolve.add_argument(
            f"--{argument}",
            required=True,
        )


def run_deployment_command(
    args: argparse.Namespace,
) -> dict[str, Any]:
    artifact = load_json_object(args.artifact)

    if args.command == "build-manifest":
        return build_deployment_manifest(
            artifact,
            load_json_object(args.bindings),
            account_id=args.account_id,
        )

    if args.command == "resolve-table":
        return resolve_table_binding(
            load_deployment(args.manifest),
            artifact,
            environment=args.environment,
            account_id=args.account_id,
            region=args.region,
            source=args.source,
            database_name=args.database_name,
            schema_name=args.schema_name,
            table_name=args.table_name,
            configuration_revision=args.configuration_revision,
            artifact_revision=args.artifact_revision,
        )

    raise ConfigurationError(
        f"Unsupported deployment command: {args.command}"
    )