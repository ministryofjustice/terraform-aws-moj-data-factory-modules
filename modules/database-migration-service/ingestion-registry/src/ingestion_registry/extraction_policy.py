from dataclasses import dataclass
from typing import Any


SUPPORTED_MIGRATION_TYPES = frozenset(
    {
        "full-load",
        "full-load-and-cdc",
        "cdc",
    }
)


@dataclass(frozen=True)
class ExtractionPolicy:
    migration_type: str

    @property
    def includes_full_load(self) -> bool:
        return self.migration_type in {
            "full-load",
            "full-load-and-cdc",
        }

    @property
    def includes_cdc(self) -> bool:
        return self.migration_type in {
            "full-load-and-cdc",
            "cdc",
        }

    def __post_init__(self) -> None:
        if (
            not isinstance(self.migration_type, str)
            or self.migration_type not in SUPPORTED_MIGRATION_TYPES
        ):
            raise ValueError(
                "migration_type must be full-load, "
                "full-load-and-cdc or cdc"
            )


def resolve_extraction_policy(
    binding: dict[str, Any],
) -> ExtractionPolicy:
    if not isinstance(binding, dict):
        raise ValueError("Resolved binding must be an object")

    group = binding.get("task_group")

    if not isinstance(group, dict):
        raise ValueError(
            "Resolved binding must contain a task_group object"
        )

    return ExtractionPolicy(
        migration_type=group.get("migration_type"),
    )