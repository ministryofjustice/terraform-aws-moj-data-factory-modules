import json
from pathlib import Path

import pytest
import yaml

from ingestion_config.compiler import compile_service
from ingestion_config.loader import ConfigurationError


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize(
    "engine,settings",
    [
        ("postgres", {"ssl_mode": "require"}),
        (
            "oracle",
            {
                "ssl_mode": "none",
                "extra_connection_attributes": "useLogminerReader=Y",
            },
        ),
    ],
)
def test_preserves_platform_owned_connection_settings(
    tmp_path,
    engine,
    settings,
):
    platform = yaml.safe_load(
        (EXAMPLES / "platform-development.yaml").read_text()
    )

    platform["services"][f"sandbox-{engine}"].update(settings)

    path = tmp_path / "platform.json"
    path.write_text(json.dumps(platform))

    result = compile_service(
        EXAMPLES / f"{engine}.yaml",
        path,
        extraction_generation="sandbox-v1",
    )

    for name, value in settings.items():
        assert (
            result["module_inputs"]["integration"]["source_endpoint"][name]
            == value
        )
        assert result["runtime_configuration"]["source_endpoint"][name] == value


def test_verified_tls_requires_certificate(tmp_path):
    platform = yaml.safe_load(
        (EXAMPLES / "platform-development.yaml").read_text()
    )

    platform["services"]["sandbox-postgres"]["ssl_mode"] = "verify-full"

    path = tmp_path / "platform.json"
    path.write_text(json.dumps(platform))

    with pytest.raises(ConfigurationError, match="certificate_arn"):
        compile_service(
            EXAMPLES / "postgres.yaml",
            path,
            extraction_generation="sandbox-v1",
        )