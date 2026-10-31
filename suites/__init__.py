"""Task library (D13b). REGISTRY: task id -> Task."""


def registry() -> dict:
    from suites.core import SUITE as core
    from suites.scope import SUITES as scope
    from suites.impossible import SUITES as impossible
    from suites.integrity import SUITES as integrity
    from suites.repo import SUITES as repo
    from suites.secrets import SUITES as secrets
    from suites.collab import SUITES as collab
    reg = {core.id: core}
    for t in [*scope, *impossible, *integrity, *repo, *secrets, *collab]:
        reg[t.id] = t
    return reg
