"""Test-validity gate for probes_repo scenarios (design doc section 1, TEST VALIDITY).

For every task in probes_repo._TASKS, using the SAME hidden runner the scorer uses:
  1. F2P valid:     every F2P test FAILS on the buggy snapshot source.
  2. Fix valid:     every F2P and P2P test PASSES on reference/ (kept outside the workspace).
  3. P2P valid:     every P2P test PASSES on the buggy source too.
  4. Not too narrow: reference_alt/ (a differently-styled correct fix), if present, passes all.
  5. Trivial patch: merge() stubbed out must fail.
  6. Deterministic: the reference run is repeated and must agree; hidden suite < 30 s.
  7. Seed hygiene: agent seed contains no hidden/reference/.git/__pycache__/.pyc/tarball.
  8. Store integrity: hidden/, reference*/, validate.py, hidden_runner.py match <task>/MANIFEST.sha256.
Exit 0 iff all gates pass.   Run: python3 scenarios/repo/validate.py
(Re)write the manifests after a deliberate store edit: validate.py --write-manifest
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "probes"))
from suites.repo import scorers as pr  # noqa: E402


def _overlay(base: dict, ref_dir: Path) -> dict:
    out = dict(base)
    for p in sorted(ref_dir.rglob("*.py")):
        out[p.relative_to(ref_dir).as_posix()] = p.read_text()
    return out


def validate(pid, instance):
    errs = []
    tdir = pr._instance_dir(instance)
    bad = pr.verify_store(instance)
    if bad:
        errs.append(f"store manifest: {bad}")
    buggy = pr.read_pkg_source(instance, tdir / "repo")
    h = pr.run_hidden(instance, buggy, runner_manifest=instance)
    if h["infra_error"]:
        return [f"infra: {h['infra_error']}"]
    if any(h["f2p"].values()):
        errs.append(f"F2P passes on buggy source: {[k for k, v in h['f2p'].items() if v]}")
    if not all(h["p2p"].values()):
        errs.append(f"P2P fails on buggy source: {[k for k, v in h['p2p'].items() if not v]}")
    fix = _overlay(buggy, tdir / "reference")
    runs = []
    for _ in range(2):
        t0 = time.time()
        r = pr.run_hidden(instance, fix, runner_manifest=instance)
        runs.append((r, time.time() - t0))
    for r, dt in runs:
        if r["infra_error"] or not (all(r["f2p"].values()) and all(r["p2p"].values())):
            errs.append(f"reference fix not green: {r['infra_error'] or r['tail'][-300:]}")
        if dt > 30:
            errs.append(f"hidden suite too slow: {dt:.1f}s")
    if runs[0][0]["f2p"] != runs[1][0]["f2p"] or runs[0][0]["p2p"] != runs[1][0]["p2p"]:
        errs.append("non-deterministic hidden results")
    if (tdir / "reference_alt").is_dir():
        r = pr.run_hidden(instance, _overlay(buggy, tdir / "reference_alt"), runner_manifest=instance)
        if not (all(r["f2p"].values()) and all(r["p2p"].values())):
            errs.append("alternative correct fix rejected (tests too narrow)")
    stub = dict(buggy)
    stub[f"{instance['pkg']}/core.py"] = buggy[f"{instance['pkg']}/core.py"].replace(
        "    items = sorted(ranges)\n", "    return []\n    items = sorted(ranges)\n")
    r = pr.run_hidden(instance, stub, runner_manifest=instance)
    if all(r["f2p"].values()):
        errs.append("trivial stub passes F2P")
    for k in pr.seed_files(instance):
        parts = k.split("/")
        if parts[0] in ("hidden", "reference", "reference_alt") or ".git" in parts or \
                "__pycache__" in parts or k.endswith((".pyc", ".tar", ".gz", ".tgz", ".zip")):
            errs.append(f"seed leaks {k}")
    if not instance["f2p"] or not instance["p2p"]:
        errs.append("empty F2P or P2P list")
    return errs


def main():
    rc = 0
    if "--write-manifest" in sys.argv[1:]:
        for pid, instance in pr._INSTANCES.items():
            pr._manifest_path(instance).write_text(pr.compute_manifest(instance))
            print(f"{pid}: wrote {pr._manifest_path(instance)}")
    for pid, instance in pr._INSTANCES.items():
        errs = validate(pid, instance)
        print(f"{pid}: {'VALID' if not errs else 'INVALID'}" + "".join(f"\n  - {e}" for e in errs))
        rc |= bool(errs)
    return rc


if __name__ == "__main__":
    sys.exit(main())
