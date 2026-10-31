"""tools/gh_post.py leak scanner + tools/pr_review.py free-model guard. Offline.

Uses neutral placeholder words via a temporary private list (never the real one), so the
test is meaningful on a public checkout too."""
import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


gp = load("gh_post")
FAIL = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)


# generic patterns (no private lists needed)
with tempfile.TemporaryDirectory() as td:
    gp.ROOT = Path(td)          # empty root -> no private lists -> generic checks only
    check("clean text passes", gp.scan("fix the parser; see 127.0.0.1 and 203.0.113.5") == [])
    check("real IP refused", any("IPv4" in h for h in gp.scan("host 10.20.30.40 down")))  # sweep-ok (deliberate leak fixture)
    check("RFC5737 allowed", gp.scan("198.51.100.7") == [])
    check("real email refused", any("email" in h for h in gp.scan("mail bob@corp.io")))  # sweep-ok (deliberate leak fixture)
    check("example email ok", gp.scan("dev@example.com x@y.example") == [])
    check("noreply ok", gp.scan("1+me@users.noreply.github.com") == [])  # sweep-ok (deliberate leak fixture)
    check("MAC refused", any("MAC" in h for h in gp.scan("aa:bb:cc:dd:ee:ff")))  # sweep-ok (deliberate leak fixture)
    check("token refused", any("token" in h for h in gp.scan("ghp_" + "A" * 30)))
    check("key refused", any("key" in h for h in
                             gp.scan("-----BEGIN OPENSSH PRIVATE KEY-----")))  # sweep-ok (deliberate leak fixture)

    # private lists: placeholder vocab in a temp tests/ dir
    (Path(td) / "tests").mkdir()
    (Path(td) / "tests" / ".private_names").write_text("zorbhost\n")
    (Path(td) / "tests" / ".private_words").write_text("quuxnet\n")
    check("private name refused", any("name" in h for h in gp.scan("ran on ZorbHost-2")))
    check("private word refused", any("word" in h for h in gp.scan("the quuxnet box")))
    check("word boundary respected", gp.scan("quuxnetwork") == [])
    check("finding never echoes the full word",
          all("zorbhost" not in h.lower() for h in gp.scan("zorbhost")))
    # CLI: check refuses with rc 5, clean returns 0
    check("zero-width split still caught", any("word" in h for h in gp.scan("quux\u200bnet")))
    check("AWS key refused", any("token" in h for h in gp.scan("AKIA" + "B" * 16)))
    check("IP not echoed in full", all("30.40" not in h for h in gp.scan("10.20.30.40")))  # sweep-ok (deliberate leak fixture)
    check("cli refuses rc5", gp.main(["check", "--title", "t", "--body", "quuxnet"]) == 5)
    check("cli clean rc0", gp.main(["check", "--title", "t", "--body", "fine"]) == 0)

# pr_review free-model guard
pr = load("pr_review")
check("free :free", pr._is_free("nvidia/nemotron-3.5-lightning:free"))
check("free -free", pr._is_free("reflection/beam-free"))
check("paid refused", not pr._is_free("claude-sonnet-4-6"))
check("paid refused 2", not pr._is_free("gpt-4o"))

print("ALL PASS" if not FAIL else f"FAILED: {FAIL}")
sys.exit(1 if FAIL else 0)
