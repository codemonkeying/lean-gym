"""LOW: a failed cancel-wrap warns on stderr exactly once.
Run: python3 tests/t_cancel_warn.py"""
import contextlib
import io
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_fixture as AF


class Frozen:
    __slots__ = ()          # no __dict__: every setattr fails

    def run_turn(self, p):
        return None

    def _dispatch(self, n, a):
        return "ok"


AF._WRAP_WARNED.clear()
err = io.StringIO()
with contextlib.redirect_stderr(err):
    AF._arm_cancel(Frozen(), threading.Event())
    AF._arm_cancel(Frozen(), threading.Event())
n = err.getvalue().count("probe cancel wrap")
print("stderr:", err.getvalue().strip())
ok = n == 1
print("PASS" if ok else f"FAIL warnings={n}")
sys.exit(0 if ok else 1)
