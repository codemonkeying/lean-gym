# merge() / compress() lose coverage when one range contains a later one

**Observed** (rangeset 0.3.1):

```python
>>> from rangeset import compress, merge, Range, total_length
>>> compress("1-10,3-4")
'1-4'
>>> total_length([Range(1, 10), Range(2, 3)])
3
```

**Expected:** `compress("1-10,3-4") == "1-10"` and `total_length(...) == 10`. A range that is
fully contained in an earlier (sorted) range must not shrink the merged result.

Requirements:
- `merge()` returns the sorted, disjoint, non-adjacent cover of exactly the integers in its input,
  including when a range is nested inside another.
- Existing behaviour (adjacent ranges coalesce, parse/format unchanged) must keep working.

Interface: `rangeset.core.merge(ranges) -> list[Range]` (also used by `compress` and `total_length`).
No public signatures change.
