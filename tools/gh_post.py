#!/usr/bin/env python3
"""Leak-guarded GitHub posting: issues, PRs and comments.

The git hooks only guard what goes through `git push`. Issue titles, PR bodies and
comments posted through the API bypass them entirely - and on GitHub, PRs can never be
deleted and edits keep history. So every piece of text goes through the SAME checks as
tests/_sweep.sh before it is posted:

  - private names / words from the gitignored tests/.private_names / .private_words
    (skipped gracefully when absent, as on a public checkout)
  - non-example IPv4 addresses (only loopback/0.0.0.0/RFC5737 doc ranges allowed)
  - real-looking email addresses (allow *.example / @example. / users.noreply.github.com)
  - private keys, MAC addresses, token-shaped strings

Any hit -> refuse to post, print what matched, exit 5. Stdlib only.

usage:
  gh_post.py issue   --title T --body-file F
  gh_post.py pr      --title T --head BRANCH [--base main] --body-file F
  gh_post.py comment --number N --body-file F
  gh_post.py check   --title T --body-file F          # scan only, post nothing
  (use --body TEXT instead of --body-file for short text; --json for machine output)

env: GITHUB_TOKEN (required to post), GITHUB_REPOSITORY (default codemonkeying/lean-gym)
exit: 0 ok, 2 usage, 5 refused (leak found), 1 API error
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GH_API = "https://api.github.com"

_IP = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")
_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|ssh-(?:rsa|ed25519) AAAA")
_TOKEN = re.compile(r"\b(?:gh[pousr]_|github_pat_|glpat-|sk-|xox[abprs]-)[A-Za-z0-9_-]{12,}"
                    r"|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")


def _ip_ok(m):
    a, b, c, _d = (int(x) for x in m.groups())
    return ((a, b, c) in {(192, 0, 2), (198, 51, 100), (203, 0, 113)}
            or a == 127 or (a, b, c) == (0, 0, 0))


def _email_ok(e):
    e = e.lower()
    return (e.endswith(".example") or "@example." in e
            or e.endswith("users.noreply.github.com"))


def _words(name):
    p = ROOT / "tests" / name
    try:
        return [w.strip() for w in p.read_text(encoding="utf-8").splitlines() if w.strip()]
    except OSError:
        return []


def scan(text):
    """-> list of human-readable findings (empty = clean). Never echoes a private word
    in full - shows only its first two characters, so the refusal message itself can't
    become a leak if it's pasted somewhere."""
    hits = []
    # defeat zero-width / compatibility-form tricks before matching
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))
    # Mirror the hooks' exception: one private name is a substring of a common English
    # word, so that word is blanked before the substring check (built, not spelled, so
    # this source line is not itself a sweep hit).
    low = text.lower().replace("comm" + "un" + "ity", "")
    for w in _words(".private_names"):
        if w.lower() in low:
            hits.append(f"private name '{w[:2]}...'")
    for w in _words(".private_words"):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(w)}(?![A-Za-z0-9_])", text, re.I):
            hits.append(f"private word '{w[:2]}...'")
    hits += [f"IPv4 {m.group(1)}.x.x.x" for m in _IP.finditer(text) if not _ip_ok(m)]
    hits += [f"email {e[:2]}...@..." for e in _EMAIL.findall(text) if not _email_ok(e)]
    hits += [f"MAC {m}" for m in _MAC.findall(text)]
    if _KEY.search(text):
        hits.append("private key / ssh public key material")
    hits += [f"token-shaped string {t[:6]}..." for t in _TOKEN.findall(text)]
    return hits


def _api(method, path, token, body):
    req = urllib.request.Request(
        f"{GH_API}{path}", data=json.dumps(body).encode(), method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "leangym-gh-post", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Post an issue / PR / comment to GitHub after a leak scan.",
        epilog="example:\n  python3 tools/gh_post.py check --title 'fix x' --body 'details'",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["issue", "pr", "comment", "check"])
    ap.add_argument("--title", default="")
    ap.add_argument("--body", default=None)
    ap.add_argument("--body-file", default=None)
    ap.add_argument("--head", default=None)
    ap.add_argument("--base", default="main")
    ap.add_argument("--number", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.body_file:
        try:
            body = Path(a.body_file).read_text(encoding="utf-8")
        except OSError as e:
            ap.error(f"cannot read --body-file: {e}")
    else:
        body = a.body or ""
    if a.kind in ("issue", "pr", "check") and not a.title:
        ap.error("--title is required")
    if a.kind == "pr" and not a.head:
        ap.error("--head is required for pr")
    if a.kind == "comment" and a.number is None:
        ap.error("--number is required for comment")

    findings = scan(f"{a.title}\n{a.head or ''}\n{a.base}\n{body}")
    if findings:
        msg = {"error": "refused: leak scan matched", "hint": "remove the matches and retry",
               "findings": findings}
        print(json.dumps(msg) if a.json else "gh_post: REFUSED - leak scan matched:\n  "
              + "\n  ".join(findings), file=sys.stdout if a.json else sys.stderr)
        return 5
    if a.kind == "check":
        print(json.dumps({"ok": True}) if a.json else "gh_post: clean")
        return 0

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "codemonkeying/lean-gym").strip()
    if not token:
        ap.error("GITHUB_TOKEN not set")
    try:
        if a.kind == "issue":
            r = _api("POST", f"/repos/{repo}/issues", token, {"title": a.title, "body": body})
        elif a.kind == "pr":
            r = _api("POST", f"/repos/{repo}/pulls", token,
                     {"title": a.title, "head": a.head, "base": a.base, "body": body})
        else:
            r = _api("POST", f"/repos/{repo}/issues/{a.number}/comments", token, {"body": body})
    except urllib.error.HTTPError as e:
        err = {"error": f"GitHub API {e.code}", "hint": e.read().decode()[:300]}
        print(json.dumps(err) if a.json else f"gh_post: {err}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        err = {"error": "network error", "hint": str(getattr(e, "reason", e))[:200]}
        print(json.dumps(err) if a.json else f"gh_post: {err}", file=sys.stderr)
        return 1
    out = {"number": r.get("number"), "url": r.get("html_url")}
    print(json.dumps(out) if a.json else f"gh_post: {a.kind} {out['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
