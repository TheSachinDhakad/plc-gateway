"""Input validation shared by config loading, the write manager and the API."""

from __future__ import annotations

import math
import re
from typing import Any

from drivers.codecs.datatypes import DATA_TYPES, DataType, ValueKind
from utils.exceptions import WriteRejectedError

IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-:.]{7,63}$")


def is_identifier(value: str) -> bool:
    return bool(IDENTIFIER_RE.match(value))


def _coerce_scalar(data_type: DataType, value: Any, string_length: int | None) -> Any:
    info = DATA_TYPES[data_type]
    if info.kind is ValueKind.BOOL:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise WriteRejectedError(f"{data_type} requires a boolean", code="INVALID_TYPE", http_status=422)
    if info.kind is ValueKind.INT:
        if isinstance(value, bool):
            raise WriteRejectedError(f"{data_type} requires an integer, got boolean",
                                     code="INVALID_TYPE", http_status=422)
        if isinstance(value, float):
            if not value.is_integer():
                raise WriteRejectedError(f"{data_type} requires an integer value",
                                         code="INVALID_TYPE", http_status=422)
            value = int(value)
        if not isinstance(value, int):
            raise WriteRejectedError(f"{data_type} requires an integer", code="INVALID_TYPE", http_status=422)
        assert info.min_value is not None and info.max_value is not None
        if not info.min_value <= value <= info.max_value:
            raise WriteRejectedError(
                f"{value} outside {data_type} range [{info.min_value}, {info.max_value}]",
                code="OUT_OF_TYPE_RANGE", http_status=422)
        return value
    if info.kind is ValueKind.FLOAT:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise WriteRejectedError(f"{data_type} requires a number", code="INVALID_TYPE", http_status=422)
        value = float(value)
        if not math.isfinite(value):
            raise WriteRejectedError("non-finite values cannot be written", code="INVALID_VALUE",
                                     http_status=422)
        if data_type is DataType.REAL and abs(value) > 3.4028234663852886e38:
            raise WriteRejectedError("value exceeds REAL range", code="OUT_OF_TYPE_RANGE", http_status=422)
        return value
    if info.kind is ValueKind.STR:
        if not isinstance(value, str):
            raise WriteRejectedError(f"{data_type} requires a string", code="INVALID_TYPE", http_status=422)
        limit = string_length or info.default_length
        if limit is not None and len(value) > limit:
            raise WriteRejectedError(f"string longer than {limit} characters", code="INVALID_VALUE",
                                     http_status=422)
        if data_type in (DataType.STRING, DataType.CHAR):
            try:
                value.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise WriteRejectedError("STRING/CHAR accept Latin-1 characters only",
                                         code="INVALID_VALUE", http_status=422) from exc
        if data_type is DataType.CHAR and len(value) != 1:
            raise WriteRejectedError("CHAR requires exactly one character", code="INVALID_VALUE",
                                     http_status=422)
        return value
    raise WriteRejectedError(f"unsupported data type {data_type}", code="INVALID_TYPE", http_status=422)


def coerce_write_value(
    data_type: DataType,
    value: Any,
    *,
    array_length: int | None = None,
    string_length: int | None = None,
) -> Any:
    """Strictly validate a value for writing. No implicit string->number conversion."""
    if array_length:
        if not isinstance(value, list) or len(value) != array_length:
            raise WriteRejectedError(f"expected a list of exactly {array_length} elements",
                                     code="INVALID_TYPE", http_status=422)
        return [_coerce_scalar(data_type, v, string_length) for v in value]
    if isinstance(value, list):
        raise WriteRejectedError("scalar tag does not accept a list", code="INVALID_TYPE", http_status=422)
    return _coerce_scalar(data_type, value, string_length)


def check_engineering_limits(value: Any, minimum: float | None, maximum: float | None) -> None:
    values = value if isinstance(value, list) else [value]
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int | float):
            continue
        if minimum is not None and v < minimum:
            raise WriteRejectedError(f"value {v} below engineering minimum {minimum}",
                                     code="BELOW_MIN", http_status=422)
        if maximum is not None and v > maximum:
            raise WriteRejectedError(f"value {v} above engineering maximum {maximum}",
                                     code="ABOVE_MAX", http_status=422)
