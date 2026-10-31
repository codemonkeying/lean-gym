"""rangeset - inclusive integer range helpers (MIT)."""
from .core import Range, merge, total_length, contains
from .parse import parse, ParseError
from .fmt import format_ranges, compress

__all__ = ["Range", "merge", "total_length", "contains", "parse", "ParseError",
           "format_ranges", "compress"]
__version__ = "0.3.1"
