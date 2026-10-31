"""Parse range specs like "1-5, 8, 10-12" into Range objects."""
from __future__ import annotations

import re
from typing import List

from .core import Range


class ParseError(ValueError):
    """Raised for a malformed range spec."""


_ITEM = re.compile(r"^\s*(-?\d+)\s*(?:-\s*(-?\d+)\s*)?$")


def parse(spec: str) -> List[Range]:
    """Parse a comma-separated spec. Items are "N" or "A-B" (inclusive).

    Empty items are ignored ("1,,2" == "1,2"); an empty spec gives [].
    Order and duplicates are preserved - call merge() to normalise.
    """
    if not isinstance(spec, str):
        raise TypeError("spec must be a string")
    out: List[Range] = []
    for item in spec.split(","):
        if not item.strip():
            continue
        m = _ITEM.match(item)
        if not m:
            raise ParseError(f"bad range item: {item!r}")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) is not None else a
        try:
            out.append(Range(a, b))
        except ValueError as e:
            raise ParseError(str(e)) from None
    return out
