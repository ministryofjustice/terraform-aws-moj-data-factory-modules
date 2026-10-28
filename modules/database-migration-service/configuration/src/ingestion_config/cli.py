import argparse
import os
import sys
import tempfile
from pathlib import Path

from ingestion_config.compiler import (
    canonical_json,
    compile_service,
)
from ingestion_config.deployment_cli import (
    add_deployment_commands,
    run_deployment_command,
)
from ingestion_config.loader import ConfigurationError
from ingestion_config.platform import resolve_service
from ingestion_config.inventory import compile_inventory
from ingestion_config.json_documents import load_json_object


def write_artifact(output: Path, content: str) -> None:
    """Publish a complete file without replacing an existing destination."""
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=".ingestion-config-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)

            stream.write(content + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.link(temporary, output)

    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingestion-config"
    )

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    for name in ("validate", "compile"):
        command = commands.add_parser(name)

        command.add_argument(
            "--service",
            type=Path,
            required=True,
        )

        command.add_argument(
            "--platform",
            type=Path,
            required=True,
        )

        if name == "compile":
            command.add_argument(
                "--extraction-generation",
                required=True,
            )

            command.add_argument(
                "--output",
                type=Path,
                required=True,
            )

    inventory = commands.add_parser("compile-directory")
    inventory.add_argument("--services", type=Path, required=True)
    inventory.add_argument("--platform", type=Path, required=True)
    inventory.add_argument("--generations", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    inventory.add_argument("--retained", type=Path)
    add_deployment_commands(commands)

    args = parser.parse_args(argv)

    try:
        if args.command == "compile-directory":
            result = compile_inventory(
                args.services,
                args.platform,
                load_json_object(args.generations),
                retained=(
                    load_json_object(args.retained)
                    if args.retained
                    else None
                ),
            )

            write_artifact(
                args.output,
                canonical_json(result),
            )

            print(
                f"COMPILED: {len(result['compiled_artifacts'])} deployments"
            )

        elif args.command == "validate":
            result = resolve_service(
                args.service,
                args.platform,
            )

            print(
                "VALID: "
                + result["service_configuration"]["service"]
            )

        elif args.command == "compile":
            result = compile_service(
                args.service,
                args.platform,
                extraction_generation=(
                    args.extraction_generation
                ),
            )

            write_artifact(
                args.output,
                canonical_json(result),
            )

            print(f"COMPILED: {args.output}")

            print(
                "Configuration revision: "
                + result["configuration_revision"]
            )

            print(
                "Artifact revision: "
                + result["artifact_revision"]
            )

        else:
            result = run_deployment_command(args)

            write_artifact(
                args.output,
                canonical_json(result),
            )

            label = (
                "MANIFEST"
                if args.command == "build-manifest"
                else "RESOLVED"
            )

            print(f"{label}: {args.output}")

        return 0

    except FileExistsError:
        print(
            "ERROR: output already exists; "
            "choose a different output path",
            file=sys.stderr,
        )
        return 2

    except (ConfigurationError, OSError) as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        return 2