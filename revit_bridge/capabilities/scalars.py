"""Invariant C# spelling for the two parameter families allowed in bare code."""
import math
import re
from decimal import Decimal

NUMERIC_TYPES = frozenset({"double", "number", "float", "int", "integer"})
BOOLEAN_TYPES = frozenset({"bool", "boolean"})
BARE_TYPES = NUMERIC_TYPES | BOOLEAN_TYPES
_DECIMAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_MAX_DECIMAL_LENGTH = 256


def bare_literal(value, ptype: str, *, declared_default: bool = False) -> str:
    if ptype in BOOLEAN_TYPES:
        if type(value) is bool:
            return "true" if value else "false"
        if type(value) is str and value.isascii() and value.lower() in ("true", "false"):
            return value.lower()
        raise ValueError(f"expects {ptype} (true or false), got {type(value).__name__}")

    integer = ptype in ("int", "integer")
    if ptype in NUMERIC_TYPES:
        if declared_default and type(value) not in (int, float):
            raise ValueError("the declared default must be a number")
        if type(value) is str and len(value) <= _MAX_DECIMAL_LENGTH:
            trimmed = value.strip(" \t\r\n\f\v")
            if _DECIMAL.fullmatch(trimmed) and not (integer and "." in trimmed):
                if "." not in trimmed:
                    return str(int(trimmed))
                return format(Decimal(trimmed), "f")
        elif type(value) is int:
            try:
                if math.isfinite(float(value)):
                    return str(value)
            except OverflowError:
                pass
        elif type(value) is float and math.isfinite(value):
            if not integer:
                text = format(Decimal(repr(value)), "f")
                return text if "." in text else text + ".0"
            if value.is_integer():
                return str(int(value))
    raise ValueError(f"expects {ptype}, got {type(value).__name__}")
