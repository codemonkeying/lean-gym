"""Board-coordination tasks: drive the `board` tool correctly (D18/D19a worker path),
plus the collab x integrity ledger-vs-ground-truth certs (collab_fake_done,
collab_phantom_reconcile)."""
from suites.collab.suite import SUITE as _board_driver
from suites.collab.fake_done import SUITE as _fake_done
from suites.collab.phantom_reconcile import SUITE as _phantom_reconcile

SUITES = [_board_driver, _fake_done, _phantom_reconcile]  # noqa: F401
