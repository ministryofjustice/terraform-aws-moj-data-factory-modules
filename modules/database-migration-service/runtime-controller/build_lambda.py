import argparse
import subprocess
from pathlib import Path


PLATFORMS = {
    "x86_64": "linux/amd64",
    "arm64": "linux/arm64",
}


CONTAINER_BUILD = r'''
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from importlib.metadata import distributions
from pathlib import Path

architecture = sys.argv[1]

expected_machine = {
    "x86_64": "x86_64",
    "arm64": "aarch64",
}[architecture]

if sys.version_info[:2] != (3, 12):
    raise RuntimeError("Build requires Python 3.12")

if platform.machine() != expected_machine:
    raise RuntimeError("Container architecture does not match the target")


def pip(*arguments):
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--disable-pip-version-check",
            "--no-input",
            *arguments,
        ],
        check=True,
    )


with tempfile.TemporaryDirectory(
    prefix="dms-runtime-build-"
) as temporary:
    workspace = Path(temporary)
    sources = workspace / "sources"
    wheels = workspace / "wheels"
    package = workspace / "package"

    sources.mkdir()
    wheels.mkdir()
    package.mkdir()

    projects = []

    for name in (
        "configuration",
        "ingestion-registry",
        "runtime-controller",
    ):
        destination = sources / name

        # Build from writable copies, leaving the mounted repository
        # unchanged and excluding previous build output.
        shutil.copytree(
            Path("/source") / name,
            destination,
            ignore=shutil.ignore_patterns(
                "build",
                "dist",
                "__pycache__",
                ".pytest_cache",
                "*.egg-info",
                ".venv",
                "venv",
            ),
        )

        projects.append(destination)

    pip(
        "wheel",
        "--no-deps",
        "--wheel-dir",
        str(wheels),
        *(str(project) for project in projects),
    )

    local_wheels = sorted(wheels.glob("*.whl"))

    if len(local_wheels) != 3:
        raise RuntimeError(
            "Expected exactly three local project wheels"
        )

    for wheel in local_wheels:
        if not wheel.name.endswith("-py3-none-any.whl"):
            raise RuntimeError(
                f"Local project is not platform-independent: {wheel.name}"
            )

    # Resolve dependencies under the actual target Python and OS.
    pip(
        "install",
        "--target",
        str(package),
        "--only-binary=:all:",
        "--no-compile",
        *(str(wheel) for wheel in local_wheels),
    )

    required_files = {
        "dms_runtime/lambda_function.py",
        "ingestion_registry/admission.py",
        "ingestion_config/published_documents.py",
        "ingestion_config/schemas/deployment-v1.schema.json",
    }

    for relative_path in sorted(required_files):
        if not (package / relative_path).is_file():
            raise RuntimeError(
                f"Lambda package is missing {relative_path}"
            )

    # Import from the installed package in a separate Python process.
    smoke_test = """
import jsonschema
import rpds
import yaml
import yaml._yaml

from dms_runtime.lambda_function import lambda_handler
from ingestion_config.schema import load_schema
from ingestion_registry.admission import IngestionRegistry

schema = load_schema("deployment-v1.schema.json")
jsonschema.Draft202012Validator.check_schema(schema)

assert callable(lambda_handler)

print("PASS: shared packages and native dependencies import")
print("PASS: deployment schema loads and validates")
"""

    subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            smoke_test,
        ],
        check=True,
        cwd=workspace,
        env={
            **os.environ,
            "PYTHONPATH": str(package),
            "AWS_EC2_METADATA_DISABLED": "true",
        },
    )

    versions = {}

    for distribution in distributions(path=[str(package)]):
        name = distribution.metadata["Name"]

        if not name:
            raise RuntimeError(
                "Installed distribution has no package name"
            )

        versions[name] = distribution.version

    metadata = {
        "python_version": "3.12",
        "architecture": architecture,
        "platform": platform.system(),
        "handler": "dms_runtime.lambda_function.lambda_handler",
        "packages": dict(sorted(versions.items())),
    }

    (package / "runtime-build.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    archive = workspace / (
        f"dms-runtime-python312-{architecture}.zip"
    )

    with zipfile.ZipFile(
        archive,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as bundle:
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue

            relative = path.relative_to(package)

            if (
                "__pycache__" in relative.parts
                or path.suffix in {".pyc", ".pyo"}
            ):
                continue

            entry = zipfile.ZipInfo(
                relative.as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16

            bundle.writestr(entry, path.read_bytes())

    with zipfile.ZipFile(archive) as bundle:
        bad_file = bundle.testzip()

        if bad_file is not None:
            raise RuntimeError(
                f"Archive integrity check failed: {bad_file}"
            )

    output = Path("/output") / archive.name
    staged = output.with_suffix(".zip.tmp")

    try:
        shutil.copyfile(archive, staged)
        staged.replace(output)
    finally:
        staged.unlink(missing_ok=True)

    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()

    print(f"\nCreated: {output.name}")
    print(f"Architecture: {architecture}")
    print(f"Size: {output.stat().st_size:,} bytes")
    print(f"SHA-256: {digest}")
'''


def build(architecture: str) -> Path:
    module_directory = Path(__file__).resolve().parent
    dms_directory = module_directory.parent

    for name in (
        "configuration",
        "ingestion-registry",
        "runtime-controller",
    ):
        project = dms_directory / name

        if not (project / "pyproject.toml").is_file():
            raise RuntimeError(
                f"Missing Python project: {project}"
            )

    output_directory = module_directory / "dist"
    output_directory.mkdir(exist_ok=True)

    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            PLATFORMS[architecture],
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,size=1g",
            "--env",
            "PIP_NO_CACHE_DIR=1",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--mount",
            (
                f"type=bind,source={dms_directory},"
                "target=/source,readonly"
            ),
            "--mount",
            (
                f"type=bind,source={output_directory},"
                "target=/output"
            ),
            "--entrypoint",
            "/var/lang/bin/python3.12",
            "public.ecr.aws/lambda/python:3.12",
            "-B",
            "-c",
            CONTAINER_BUILD,
            architecture,
        ],
        check=True,
    )

    output = output_directory / (
        f"dms-runtime-python312-{architecture}.zip"
    )

    print(f"Local archive: {output}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build and smoke-test the DMS runtime "
            "inside a Python 3.12 Lambda container"
        )
    )
    parser.add_argument(
        "--architecture",
        choices=sorted(PLATFORMS),
        default="x86_64",
    )

    arguments = parser.parse_args()
    build(arguments.architecture)


if __name__ == "__main__":
    main()