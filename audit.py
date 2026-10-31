#!/usr/bin/env python3
"""Pre-publication audit: scan the harness for anything that must not ship public.

Standalone release gate - run before publishing / in CI. Greps the tree for:
  * private RFC-1918 IPs        (LAN topology leak)
  * MAC addresses               (hardware identity leak)
  * secret literals             (sk-ant-..., api keys, bearer tokens)
  * operator PII                (names / box nicknames from a denylist)
  * Claude-Code impersonation   (billing header, CLIENT_ID, cli User-Agent, beta flags)

Any hit that is NOT tagged `# sweep-ok` (an explicit, reviewed allow) fails the
audit. Exit 0 = clean, 1 = findings, 2 = usage error.

  python3 audit.py                 # scan the harness dir
  python3 audit.py --path ..       # scan a wider tree
  python3 audit.py --json          # machine-readable

Deliberately dependency-free (stdlib only) so it runs anywhere, including CI.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Files/dirs never worth scanning (binaries, caches, this tool's own patterns).
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "runs",
             "results", "archive_90pct", "train", "history"}
SKIP_SUFFIX = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz",
               ".bin", ".gguf", ".safetensors", ".lock", ".hb"}
# This file defines the patterns, so its own regex source would self-trip.
SKIP_NAMES = {"audit.py"}

# An explicit allow marker: a line carrying this has been reviewed + accepted.
ALLOW = "sweep-ok"

# label -> (compiled regex, human explanation)
CHECKS = {
    "private_ip": (
        # RFC-1918 ranges: 10.x, 192.168.x, 172.16-31.x
        re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                   r"|192\.168\.\d{1,3}\.\d{1,3}"
                   r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
        "private LAN IP - leaks network topology; default to localhost",
    ),
    "mac_addr": (
        re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"),
        "MAC address - hardware identity leak",
    ),
    "secret": (
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"
                   r"|sk-[A-Za-z0-9]{20,}"
                   r"|AKIA[0-9A-Z]{16}"
                   r"|Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
        "secret literal (API key / token) - never commit; load from env/keyring",
    ),
    "claude_code_impersonation": (
        re.compile(r"x-anthropic-billing-header"
                   r"|claude-cli/[0-9]"
                   r"|cc_version="
                   r"|claude-code-\d{8}"
                   r"|oauth-\d{4}-\d{2}-\d{2}"
                   r"|9d1c250a-e61b-44d9-88ed-5944d1962f5e"),
        "Claude-Code subscription impersonation - strip/gate before public release",
    ),
    "operator_pii": (
        # names / box nicknames. Word-boundary + case-insensitive. Keep tight to
        # avoid false hits (e.g. do NOT put 'user' here - it's everywhere).
        re.compile(r"\bTom\b", re.IGNORECASE),
        "operator PII (name/nickname) - scrub for public docs/results",
    ),
}


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.name in SKIP_NAMES:
                continue
            if p.suffix.lower() in SKIP_SUFFIX:
                continue
            yield p


def _shipped_files(root: Path):
    """What git would commit/ship: tracked + untracked-not-ignored (= deploy/install.sh remote).
    None if root is not a git work tree (or git is missing) -> caller falls back to a full walk."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    files = []
    for rel in out.stdout.decode(errors="replace").split("\0"):
        p = root / rel
        if (rel and p.is_file() and p.name not in SKIP_NAMES and p.suffix.lower() not in SKIP_SUFFIX
                and not any(part in SKIP_DIRS for part in Path(rel).parts)):
            files.append(p)
    return files


def scan(root: Path, all_files: bool = False) -> list[dict]:
    findings = []
    files = None if all_files else _shipped_files(root)
    for p in (_iter_files(root) if files is None else files):
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW in line:
                continue
            for label, (rx, why) in CHECKS.items():
                m = rx.search(line)
                if m:
                    findings.append({
                        "file": str(p.relative_to(root)) if p.is_relative_to(root)
                                else str(p),
                        "line": lineno,
                        "check": label,
                        "match": m.group(0),
                        "why": why,
                        "text": line.strip()[:160],
                    })
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="pre-publication PII/secret audit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n  python3 audit.py\n  python3 audit.py --json\n  python3 audit.py --all --path .")
    ap.add_argument("--path", default=str(HERE),
                    help="root to scan (default: the harness dir)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--all", action="store_true",
                    help="scan every file incl. gitignored local state (default: only what git would "
                         "commit/ship; falls back to all when not a git tree)")
    args = ap.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.exists():
        if args.json:
            print(json.dumps({"error": f"no such path: {root}", "hint": "pass an existing --path"}))
        else:
            print(f"no such path: {root}", file=sys.stderr)
        return 4   # CLI contract: 4 = not found

    findings = scan(root, all_files=args.all)

    if args.json:
        print(json.dumps({"root": str(root), "findings": findings,
                          "clean": not findings}, indent=2))
        return 1 if findings else 0

    if not findings:
        print(f"AUDIT CLEAN: no findings under {root}")
        print(f"(checks: {', '.join(CHECKS)}; allow-marker: '# {ALLOW}')")
        return 0

    by_check: dict[str, list] = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)
    print(f"AUDIT FOUND {len(findings)} issue(s) under {root}:\n")
    for check, items in sorted(by_check.items()):
        print(f"== {check}: {items[0]['why']} ({len(items)})")
        for it in items:
            print(f"   {it['file']}:{it['line']}  [{it['match']}]  {it['text']}")
        print()
    print("Fix each, or tag a reviewed-safe line with '# " + ALLOW + "'.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
