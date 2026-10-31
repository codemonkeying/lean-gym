"""Format Range lists back into compact specs."""
from __future__ import annotations

from typing import Iterable

from .core import Range, merge
from .parse import parse


def _fmt_one(r: Range) -> str:
    return str(r.start) if r.start == r.end else f"{r.start}-{r.end}"


def format_ranges(ranges: Iterable[Range], sep: str = ",") -> str:
    """Format ranges as-is (no merging), e.g. [Range(1, 3), Range(5, 5)] -> "1-3,5"."""
    return sep.join(_fmt_one(r) for r in ranges)


def compress(spec: str, sep: str = ",") -> str:
    """Parse `spec`, merge it and format the canonical result.

    compress("10-12,1-3,4") == "1-4,10-12"
    """
    return format_ranges(merge(parse(spec)), sep=sep)
