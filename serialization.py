"""Coerce database driver values into JSON-safe primitives.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

MAX_INLINE_BINARY_BYTES = 256


def _binary(value: bytes) -> dict[str, Any]:
    if len(value) <= MAX_INLINE_BINARY_BYTES:
        return {"__bytes__": value.hex(), "encoding": "hex", "size": len(value)}
    return {"__bytes__": None, "encoding": "omitted", "size": len(value)}


def _number(value: Decimal) -> float | str:
    if not value.is_finite():
        # NaN and Infinity are not JSON numbers, so they travel as text.
        return str(value)
    as_float = float(value)
    if Decimal(str(as_float)) == value:
        # A real JSON number, not the quoted string a default serializer emits, so
        # that monetary columns can be compared and sorted without being parsed.
        return as_float
    # A wide NUMERIC does not fit a float. Keep the exact digits rather than
    # silently rounding money.
    return str(value)


def jsonable(value: Any) -> Any:
    """Return `value` converted into something a JSON serializer can carry."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _binary(bytes(value))
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
 
    return str(value)


def jsonable_rows(rows: Any) -> list[dict[str, Any]]:
    """Convert a SQLAlchemy `.mappings()` result into JSON-safe dictionaries."""
    return [{str(key): jsonable(value) for key, value in row.items()} for row in rows]
