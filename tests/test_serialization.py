import json
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pytest

from serialization import MAX_INLINE_BINARY_BYTES, jsonable, jsonable_rows


class Colour(Enum):
    RED = "red"


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (True, True),
        (7, 7),
        ("text", "text"),
        (1.5, 1.5),
        (Decimal("19.99"), 19.99),
        (datetime(2024, 1, 1, 12, 30), "2024-01-01T12:30:00"),
        (date(2024, 1, 1), "2024-01-01"),
        (time(12, 30), "12:30:00"),
        (timedelta(minutes=90), 5400.0),
        (UUID("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
        (Colour.RED, "red"),
    ],
)
def test_values_convert_to_json_primitives(value, expected):
    assert jsonable(value) == expected


def test_decimal_becomes_a_number_not_a_string():
    # The default serializer emits "19.99", which sorts and compares as text.
    assert isinstance(jsonable(Decimal("19.99")), float)


def test_wide_decimal_keeps_exact_digits_instead_of_rounding():
    wide = Decimal("12345678901234567890.12345678901234567890")

    assert jsonable(wide) == str(wide)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_decimals_travel_as_text(value):
    # NaN and Infinity are not valid JSON numbers.
    assert isinstance(jsonable(value), str)


def test_non_finite_floats_travel_as_text():
    assert jsonable(float("inf")) == "inf"
    assert jsonable(float("nan")) == "nan"
    assert math.isfinite(1.5) and jsonable(1.5) == 1.5


@pytest.mark.parametrize("binary", [b"\xde\xad\xbe\xef", bytearray(b"\xde\xad"), memoryview(b"\xff")])
def test_binary_values_are_hex_encoded(binary):
    result = jsonable(binary)

    assert result["encoding"] == "hex"
    assert result["__bytes__"] == bytes(binary).hex()
    assert result["size"] == len(bytes(binary))


def test_large_binary_is_described_rather_than_inlined():
    blob = b"\x00" * (MAX_INLINE_BINARY_BYTES + 1)

    result = jsonable(blob)

    assert result == {
        "__bytes__": None,
        "encoding": "omitted",
        "size": MAX_INLINE_BINARY_BYTES + 1,
    }


def test_nested_containers_are_converted():
    value = {"prices": [Decimal("1.5")], "seen": (date(2024, 1, 1),)}

    assert jsonable(value) == {"prices": [1.5], "seen": ["2024-01-01"]}


def test_unknown_types_degrade_to_text_rather_than_failing():
    class Opaque:
        def __str__(self) -> str:
            return "opaque"

    assert jsonable(Opaque()) == "opaque"


def test_converted_rows_are_json_serializable():
    rows = [{"price": Decimal("19.99"), "pk": memoryview(b"\xde\xad"), "at": datetime(2024, 1, 1)}]

    # The whole point: this raised TypeError before, taking the tool call with it.
    assert json.dumps(jsonable_rows(rows))
