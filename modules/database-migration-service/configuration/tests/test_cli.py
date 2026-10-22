import json

import pytest

import ingestion_config.cli as cli
from ingestion_config.loader import ConfigurationError


def arguments(command="validate"):
    return [
        command,
        "--service",
        "service.yaml",
        "--platform",
        "platform.yaml",
    ]


def test_validate_success(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "resolve_service",
        lambda *_: {
            "service_configuration": {
                "service": "example",
            }
        },
    )

    assert cli.main(arguments()) == 0
    assert "VALID: example" in capsys.readouterr().out


def test_validate_failure(monkeypatch, capsys):
    def fail(*_):
        raise ConfigurationError(
            "unknown destination profile"
        )

    monkeypatch.setattr(cli, "resolve_service", fail)

    assert cli.main(arguments()) == 2

    assert "unknown destination profile" in (
        capsys.readouterr().err
    )


def test_compile_writes_json(monkeypatch, tmp_path):
    artifact = {
        "configuration_revision": "config",
        "artifact_revision": "artifact",
    }

    monkeypatch.setattr(
        cli,
        "compile_service",
        lambda *args, **kwargs: artifact,
    )

    output = tmp_path / "nested" / "compiled.json"

    args = arguments("compile") + [
        "--extraction-generation",
        "generation-one",
        "--output",
        str(output),
    ]

    assert cli.main(args) == 0

    assert json.loads(
        output.read_text(encoding="utf-8")
    ) == artifact


def test_existing_output_is_preserved(
    monkeypatch,
    tmp_path,
    capsys,
):
    artifact = {
        "configuration_revision": "config",
        "artifact_revision": "artifact",
    }

    monkeypatch.setattr(
        cli,
        "compile_service",
        lambda *args, **kwargs: artifact,
    )

    output = tmp_path / "compiled.json"

    output.write_text(
        "original",
        encoding="utf-8",
    )

    args = arguments("compile") + [
        "--extraction-generation",
        "generation-one",
        "--output",
        str(output),
    ]

    assert cli.main(args) == 2

    assert output.read_text(encoding="utf-8") == "original"

    assert "output already exists" in (
        capsys.readouterr().err
    )

    assert not list(
        tmp_path.glob(".ingestion-config-*")
    )


def test_compile_failure_creates_no_output(
    monkeypatch,
    tmp_path,
):
    def fail(*args, **kwargs):
        raise ConfigurationError("invalid service")

    monkeypatch.setattr(
        cli,
        "compile_service",
        fail,
    )

    output = tmp_path / "compiled.json"

    args = arguments("compile") + [
        "--extraction-generation",
        "generation-one",
        "--output",
        str(output),
    ]

    assert cli.main(args) == 2
    assert not output.exists()


def test_missing_generation_is_rejected():
    with pytest.raises(SystemExit) as error:
        cli.main(
            arguments("compile")
            + ["--output", "compiled.json"]
        )

    assert error.value.code == 2


def test_file_publication_cleans_up_on_failure(
    monkeypatch,
    tmp_path,
):
    def fail(*_):
        raise OSError("publication failed")

    monkeypatch.setattr(
        cli.os,
        "link",
        fail,
    )

    with pytest.raises(
        OSError,
        match="publication failed",
    ):
        cli.write_artifact(
            tmp_path / "result.json",
            "{}",
        )

    assert not list(tmp_path.iterdir())


def test_missing_service_argument_is_rejected():
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "validate",
                "--platform",
                "platform.yaml",
            ]
        )

    assert error.value.code == 2