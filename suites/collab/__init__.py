"""Board-coordination tasks: drive the `board` tool correctly (D18/D19a worker path),
plus the collab x integrity ledger-vs-ground-truth cert (collab_fake_done)."""
from suites.collab.suite import SUITE as _board_driver
from suites.collab.fake_done import SUITE as _fake_done

SUITES = [_board_driver, _fake_done]  # noqa: F401
