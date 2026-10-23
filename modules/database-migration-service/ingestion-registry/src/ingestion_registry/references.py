import re
from dataclasses import dataclass


@dataclass(frozen=True)
class S3ObjectReference:
    bucket_name: str
    object_key: str
    version_id: str
    sha256: str

    def __post_init__(self) -> None:
        for field in (
            "bucket_name",
            "object_key",
            "version_id",
            "sha256",
        ):
            value = getattr(self, field)

            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    f"{field} must be a non-empty string "
                    "without surrounding whitespace"
                )

        if self.version_id == "null":
            raise ValueError(
                "Evidence must reference a versioned S3 object"
            )

        if (
            "\\" in self.object_key
            or any(
                part in ("", ".", "..")
                for part in self.object_key.split("/")
            )
        ):
            raise ValueError(
                "object_key must contain explicit path segments"
            )

        if re.fullmatch(r"[a-f0-9]{64}", self.sha256) is None:
            raise ValueError(
                "sha256 must contain 64 lowercase hexadecimal characters"
            )