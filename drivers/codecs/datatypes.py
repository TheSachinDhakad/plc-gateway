"""Canonical IEC 61131-3 style data types shared by every driver.

Each driver declares the subset it supports; values are always decoded into native Python
types (bool/int/float/str or lists thereof) — never blindly into float.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class DataType(enum.StrEnum):
    BOOL = "BOOL"
    SINT = "SINT"
    INT = "INT"
    DINT = "DINT"
    LINT = "LINT"
    USINT = "USINT"
    UINT = "UINT"
    UDINT = "UDINT"
    ULINT = "ULINT"
    BYTE = "BYTE"
    WORD = "WORD"
    DWORD = "DWORD"
    LWORD = "LWORD"
    REAL = "REAL"
    LREAL = "LREAL"
    STRING = "STRING"
    WSTRING = "WSTRING"
    CHAR = "CHAR"


class ValueKind(enum.StrEnum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"


@dataclass(frozen=True, slots=True)
class TypeInfo:
    size: int | None            # bytes for fixed-size types; None for strings
    kind: ValueKind
    struct_code: str | None     # big-endian struct code for fixed types
    min_value: int | None = None
    max_value: int | None = None
    default_length: int | None = None  # strings


DATA_TYPES: dict[DataType, TypeInfo] = {
    DataType.BOOL: TypeInfo(1, ValueKind.BOOL, None),
    DataType.SINT: TypeInfo(1, ValueKind.INT, "b", -(2**7), 2**7 - 1),
    DataType.INT: TypeInfo(2, ValueKind.INT, "h", -(2**15), 2**15 - 1),
    DataType.DINT: TypeInfo(4, ValueKind.INT, "i", -(2**31), 2**31 - 1),
    DataType.LINT: TypeInfo(8, ValueKind.INT, "q", -(2**63), 2**63 - 1),
    DataType.USINT: TypeInfo(1, ValueKind.INT, "B", 0, 2**8 - 1),
    DataType.UINT: TypeInfo(2, ValueKind.INT, "H", 0, 2**16 - 1),
    DataType.UDINT: TypeInfo(4, ValueKind.INT, "I", 0, 2**32 - 1),
    DataType.ULINT: TypeInfo(8, ValueKind.INT, "Q", 0, 2**64 - 1),
    DataType.BYTE: TypeInfo(1, ValueKind.INT, "B", 0, 2**8 - 1),
    DataType.WORD: TypeInfo(2, ValueKind.INT, "H", 0, 2**16 - 1),
    DataType.DWORD: TypeInfo(4, ValueKind.INT, "I", 0, 2**32 - 1),
    DataType.LWORD: TypeInfo(8, ValueKind.INT, "Q", 0, 2**64 - 1),
    DataType.REAL: TypeInfo(4, ValueKind.FLOAT, "f"),
    DataType.LREAL: TypeInfo(8, ValueKind.FLOAT, "d"),
    DataType.STRING: TypeInfo(None, ValueKind.STR, None, default_length=254),
    DataType.WSTRING: TypeInfo(None, ValueKind.STR, None, default_length=254),
    DataType.CHAR: TypeInfo(1, ValueKind.STR, None, default_length=1),
}

NUMERIC_KINDS = (ValueKind.INT, ValueKind.FLOAT)


def type_info(data_type: DataType) -> TypeInfo:
    return DATA_TYPES[data_type]


def is_numeric(data_type: DataType) -> bool:
    return DATA_TYPES[data_type].kind in NUMERIC_KINDS


def value_type_of(value: Any) -> str:
    """Storage classification used to choose the MySQL value column."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list | tuple):
        return "array"
    raise TypeError(f"unsupported value type {type(value).__name__}")


def python_value_matches(data_type: DataType, value: Any) -> bool:
    """Check a decoded/received value is compatible with the configured type."""
    kind = DATA_TYPES[data_type].kind
    if kind is ValueKind.BOOL:
        return isinstance(value, bool)
    if kind is ValueKind.INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is ValueKind.FLOAT:
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, str)

