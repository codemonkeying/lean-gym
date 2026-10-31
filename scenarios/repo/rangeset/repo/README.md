# rangeset

Tiny pure-python helpers for inclusive integer ranges such as `"1-5,8,10-12"`. MIT licensed.

```python
from rangeset import parse, merge, compress
merge(parse("1-3,2-6,9"))      # [Range(1, 6), Range(9, 9)]
compress("10-12,1-3,4")        # "1-4,10-12"
```

Run the tests with `python -m unittest` (from this directory; stdlib only).
