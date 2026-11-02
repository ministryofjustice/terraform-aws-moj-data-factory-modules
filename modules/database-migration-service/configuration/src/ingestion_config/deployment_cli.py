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
from ingestion_config.deployment_bundle import build_deployment_bundle
from ingestion_config.deployment_inputs import build_deployment_inputs
from ingestion_config.runtime_package import verify_runtime_package_with_aws
from ingestion_config.publication import publish_deployments_with_aws
from ingestion_config.deployment_publication import finalize_deployment_with_aws


def add_deployment_commands(commands: Any) -> None:
    finalize = commands.add_parser(
        "finalize-deployment",
        help="Publish verified deployment documents and register their references",
    )
    finalize.add_argument(
        "--deployments",
        type=Path,
        required=True,
    )
    finalize.add_argument(
        "--account-id",
        required=True,
    )
    finalize.add_argument(
        "--bucket-name",
        required=True,
    )
    finalize.add_argument(
        "--kms-key-arn",
        required=True,
    )
    finalize.add_argument(
        "--registry-table-arn",
        required=True,
    )
    finalize.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    publish = commands.add_parser(
        "publish-deployment-documents",
        help="Publish and verify configuration and deployed resource manifests",
    )
    publish.add_argument(
        "--deployments",
        type=Path,
        required=True,
    )
    publish.add_argument(
        "--account-id",
        required=True,
    )
    publish.add_argument(
        "--bucket-name",
        required=True,
    )
    publish.add_argument(
        "--kms-key-arn",
        required=True,
    )
    publish.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    verify = commands.add_parser(
        "verify-runtime-package",
        help="Verify the exact S3 runtime package selected for deployment",
    )
    verify.add_argument("--prepared-deployment", type=Path, required=True)
    verify.add_argument("--expected-account-id", required=True)
    verify.add_argument("--expected-region", required=True)
    verify.add_argument("--expected-bucket-name", required=True)
    verify.add_argument("--expected-kms-key-arn", required=True)
    verify.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser(
        "prepare-deployment",
        help="Assemble validated configuration and platform deployment inputs",
    )

    for name in (
        "inventory",
        "foundations",
        "runtime-package",
        "capacity-assignments",
        "retained-inventory",
        "retained-assignments",
    ):
        prepare.add_argument(
            f"--{name}",
            type=Path,
            required=True,
        )

    prepare.add_argument(
        "--compiler-commit",
        required=True,
    )

    prepare.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    bundle = commands.add_parser(
        "build-deployment-bundle",
        help="Validate service deployment outputs before publication",
    )
    bundle.add_argument("--deployments", type=Path, required=True)
    bundle.add_argument("--account-id", required=True)
    bundle.add_argument("--output", type=Path, required=True)

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
    if args.command == "finalize-deployment":
        return finalize_deployment_with_aws(
            load_json_object(args.deployments),
            account_id=args.account_id,
            bucket_name=args.bucket_name,
            kms_key_arn=args.kms_key_arn,
            registry_table_arn=args.registry_table_arn,
        )
    if args.command == "publish-deployment-documents":
        return publish_deployments_with_aws(
            load_json_object(args.deployments),
            account_id=args.account_id,
            bucket_name=args.bucket_name,
            kms_key_arn=args.kms_key_arn,
        )
    if args.command == "verify-runtime-package":
        return verify_runtime_package_with_aws(
            load_json_object(args.prepared_deployment),
            expected_account_id=args.expected_account_id,
            expected_region=args.expected_region,
            expected_bucket_name=args.expected_bucket_name,
            expected_kms_key_arn=args.expected_kms_key_arn,
        )
    if args.command == "prepare-deployment":
        return build_deployment_inputs(
            inventory=load_json_object(args.inventory),
            foundations=load_json_object(args.foundations),
            runtime_package=load_json_object(args.runtime_package),
            capacity_assignments=load_json_object(
                args.capacity_assignments
            ),
            retained_inventory=load_json_object(
                args.retained_inventory
            ),
            retained_assignments=load_json_object(
                args.retained_assignments
            ),
            compiler_commit=args.compiler_commit,
        )
    if args.command == "build-deployment-bundle":
        return build_deployment_bundle(
            load_json_object(args.deployments),
            account_id=args.account_id,
        )
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