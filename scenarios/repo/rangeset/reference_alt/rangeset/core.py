"""Core Range type and set operations on lists of inclusive integer ranges."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List


@dataclass(frozen=True, order=True)
class Range:
    """An inclusive integer range [start, end]. start must be <= end."""
    start: int
    end: int

    def __post_init__(self):
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise TypeError("Range bounds must be integers")
        if self.start > self.end:
            raise ValueError(f"start {self.start} > end {self.end}")

    def __len__(self) -> int:
        return self.end - self.start + 1

    def __contains__(self, value: int) -> bool:
        return self.start <= value <= self.end

    def overlaps_or_touches(self, other: "Range") -> bool:
        """True if the two ranges overlap or are directly adjacent (e.g. 1-3 and 4-6)."""
        return self.start <= other.end + 1 and other.start <= self.end + 1


def merge(ranges: Iterable[Range]) -> List[Range]:
    """Return a sorted list of disjoint, non-adjacent ranges covering the same integers.

    Overlapping and adjacent ranges are coalesced: [1-3, 4-6] -> [1-6],
    [1-10, 3-4] -> [1-10]. The input is not modified.
    """
    items = sorted(ranges)
    if not items:
        return []
    out = [items[0]]
    for r in items[1:]:
        cur = out[-1]
        if r.end <= cur.end:
            continue  # fully contained in the current cover
        if cur.overlaps_or_touches(r):
            out[-1] = Range(cur.start, r.end)
        else:
            out.append(r)
    return out


def total_length(ranges: Iterable[Range]) -> int:
    """Number of distinct integers covered by `ranges`."""
    return sum(len(r) for r in merge(ranges))


def contains(ranges: Iterable[Range], value: int) -> bool:
    """True if `value` lies in any of `ranges`."""
    return any(value in r for r in ranges)
