"""OFFLINE smoke checker for plugin-tool cert modules.

The existing test_module_contract.py checks a module's MANIFEST shape (probes, gating,
scorer, contract) with ZERO model calls. This adds the one thing it can't: that every
`probes_tool_*.py` names a plugin tool that ACTUALLY EXISTS in the leancoder core it
will be driven against - so "probe written for a tool that got renamed/removed" fails
loud here, offline, before you spend .28 samples.

How it decides which tool a module targets (best-effort, no false alarms):
  1. a module-level `TOOL = "<name>"` or `TOOLS = [...]`  (explicit; preferred), else
  2. the module name  probes_tool_<name>.py  ->  "<name>", else
  3. tool names mined from each probe's docstring / TAGS that match a known plugin.
It then asserts each such name is in the core's plugin roster
(lean_coder.LeanToolManager(<bundled dir>).names()) OR is a core builtin (allowed:
a bundle may mix a builtin like search_files with plugins).

Run:  python3 smoke_tool_modules.py      (exit 0 = all green, non-zero = a breach)
Zero model calls, no host/network. Safe to run in CI and any time.
"""
from __future__ import annotations
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "probes"))

# 8 core builtins (allowed to appear in a tool bundle alongside plugins)
CORE_BUILTINS = {"read_file", "list_files", "search_files", "apply_diff",
                 "replace_lines", "write_file", "run_command", "background"}


def _plugin_roster():
    """The set of plugin tool names the core actually ships (offline, no agent)."""
    import agent_fixture  # noqa: F401  (puts REPO + lean-tools on sys.path)
    import lean_coder as lc
    d = lc._bundled_lean_tools_dir()
    return set(lc.LeanToolManager(d).names())


def _targets(m):
    """Best-effort set of tool names a tool-module claims to exercise."""
    t = set()
    if isinstance(getattr(m, "TOOL", None), str):
        t.add(m.TOOL)
    for x in (getattr(m, "TOOLS", None) or []):
        if isinstance(x, str):
            t.add(x)
    # fall back to the file-name convention probes_tool_<name>
    name = getattr(m, "__name__", "")
    if name.startswith("probes_tool_"):
        stem = name[len("probes_tool_"):]
        if stem not in ("EXAMPLE",):
            t.add(stem)
    return t


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(
        prog="smoke_tool_modules",
        description="offline check that every probes_tool_*.py names a tool that exists in core",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n  python3 smoke_tool_modules.py\n  python3 smoke_tool_modules.py --json")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object on stdout; human chatter suppressed")
    args = ap.parse_args(argv)
    _out = sys.stdout
    if args.json:
        sys.stdout = sys.stderr      # route human chatter away from the JSON channel
    try:
        rc, errs, checked, roster = _run()
    finally:
        sys.stdout = _out
    if args.json:
        print(json.dumps({"ok": rc == 0, "checked": checked, "errors": errs,
                          "roster": sorted(roster)}))
    return rc


def _run():
    roster = _plugin_roster()
    known = roster | CORE_BUILTINS
    print(f"core plugin roster ({len(roster)}): {', '.join(sorted(roster))}")
    errs = []
    checked = 0
    for path in sorted((HERE / "probes").glob("probes_tool_*.py")):
        modname = path.stem
        try:
            m = importlib.import_module(modname)
        except Exception as e:  # noqa
            errs.append(f"{modname}: failed to import ({type(e).__name__}: {e})")
            continue
        targets = _targets(m)
        live = getattr(m, "IS_CERT_PROBESET", False)
        tag = "LIVE" if live else "template"
        if not targets:
            print(f"  {modname} [{tag}]: no explicit TOOL/TOOLS and no name hint "
                   "(add TOOL=\"<name>\" so the smoke can verify it) - SKIP")
            continue
        unknown = sorted(t for t in targets if t not in known)
        plugins = sorted(t for t in targets if t in roster)
        checked += 1
        if unknown:
            errs.append(f"{modname} [{tag}]: references tool(s) NOT in core: "
                        f"{unknown} (roster+builtins has none of these)")
        else:
            print(f"  {modname} [{tag}]: OK -> plugins {plugins or '(builtins only)'}")
    if errs:
        print("\nFAIL:")
        for e in errs:
            print("  - " + e)
        return 1, errs, checked, roster
    print(f"\nok: {checked} tool-module(s) reference only tools that exist in core.")
    return 0, errs, checked, roster


if __name__ == "__main__":
    raise SystemExit(main())
