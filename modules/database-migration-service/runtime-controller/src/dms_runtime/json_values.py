import json
from decimal import Decimal
from typing import Any


def _decimal_integer(value: Any) -> int:
    if not isinstance(value, Decimal):
        raise TypeError(
            f"{type(value).__name__} is not JSON serializable"
        )

    if (
        not value.is_finite()
        or value != value.to_integral_value()
    ):
        raise ValueError(
            "Runtime Decimal values must be finite integers"
        )

    return int(value)


def to_json_value(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            default=_decimal_integer,
            ensure_ascii=False,
            allow_nan=False,
        )
    )