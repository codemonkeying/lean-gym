"""leangym engine: task library + suites."""

from pathlib import Path

# Release version. leangym tracks leancoder in lockstep: leangym X.Y.Z is tested
# against, and certifies results produced with, leancoder X.Y.Z. The bundled VERSION
# file is the source of truth (the pre-push gate asserts this string matches it); a
# public checkout ships it so `python3 -m gym version` works without git.
__version__ = "0.10.125"


def _version_file() -> str:
    try:
        return (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    except OSError:
        return ""
