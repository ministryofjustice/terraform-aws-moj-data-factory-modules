import json
from pathlib import Path

import pytest

import ingestion_config.cli as cli
from ingestion_config.deployment_cli import load_json_object
from ingestion_config.loader import ConfigurationError


@pytest.mark.parametrize(
    "content",
    [
        '{"source":"one","source":"two"}',
        '{"nested":{"value":1,"value":2}}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":1e999}',
        '["not", "an", "object"]',
        '{"incomplete":',
    ],
)
def test_invalid_json_input_is_rejected(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "input.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_json_object(path)


def test_json_object_is_loaded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "input.json"
    document = {"source": "sandbox-postgres"}
    path.write_text(json.dumps(document), encoding="utf-8")

    assert load_json_object(path) == document


def test_manifest_command_writes_result(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "manifest.json"
    result = {"manifest_version": 1}

    monkeypatch.setattr(
        cli,
        "run_deployment_command",
        lambda _: result,
    )

    assert cli.main(
        [
            "build-manifest",
            "--artifact",
            "compiled.json",
            "--bindings",
            "bindings.json",
            "--account-id",
            "111122223333",
            "--output",
            str(output),
        ]
    ) == 0

    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert "MANIFEST:" in capsys.readouterr().out


def test_manifest_command_preserves_existing_output(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "manifest.json"
    output.write_text("original", encoding="utf-8")

    monkeypatch.setattr(
        cli,
        "run_deployment_command",
        lambda _: {"manifest_version": 1},
    )

    assert cli.main(
        [
            "build-manifest",
            "--artifact",
            "compiled.json",
            "--bindings",
            "bindings.json",
            "--account-id",
            "111122223333",
            "--output",
            str(output),
        ]
    ) == 2

    assert output.read_text(encoding="utf-8") == "original"
    assert "output already exists" in capsys.readouterr().err


def test_manifest_failure_creates_no_output(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "manifest.json"

    def fail(_):
        raise ConfigurationError("target endpoint mismatch")

    monkeypatch.setattr(cli, "run_deployment_command", fail)

    assert cli.main(
        [
            "build-manifest",
            "--artifact",
            "compiled.json",
            "--bindings",
            "bindings.json",
            "--account-id",
            "111122223333",
            "--output",
            str(output),
        ]
    ) == 2

    assert not output.exists()
    assert "target endpoint mismatch" in capsys.readouterr().err


def test_resolve_requires_pinned_revisions() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "resolve-table",
                "--manifest",
                "manifest.json",
                "--artifact",
                "compiled.json",
                "--output",
                "resolved.json",
                "--environment",
                "development",
                "--account-id",
                "111122223333",
                "--region",
                "eu-west-2",
                "--source",
                "sandbox-postgres",
                "--database-name",
                "dms_test",
                "--schema-name",
                "public",
                "--table-name",
                "dms_integration_test",
            ]
        )

    assert error.value.code == 2