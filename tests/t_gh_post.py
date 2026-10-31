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

ex = pr.extract_review
R = "Looks good overall; one nit in foo.py."
check("tagged content", ex(f"x <review>{R}</review>", None) == R)
check("reasoning never posted", ex(None, f"thinking... <review>{R}</review>") == "")
check("raw reasoning not posted", ex(None, "Here's a thinking process: 1. ...") == "")
check("untagged content kept", ex(R, None) == R)
check("think block stripped", ex(f"<think>hmm</think>{R}", None) == R)
check("last tag wins", ex(f"<review>draft draft draft draft</review> <review>{R}</review>", None) == R)
check("echoed instruction dropped", ex(None, "wrap it between <review> and </review> tags") == "")
check("unclosed think dropped", ex(f"{R} <think>secret chain of thought", None) == R)
check("lone close think", ex(f"chain of thought here</think>{R}", None) == R)
check("unclosed review -> tail", ex(f"drafts... <review>{R}", None) == R)
check("tagged unusable, no draft fallback", ex("lots of draft notes <review></review>", None) == "")
check("uppercase tags", ex(f"<REVIEW>{R}</REVIEW>", None) == R)
check("mentions neutralised", "@team" not in ex(f"<review>{R} cc @team</review>", None))
check("length capped", len(ex("<review>" + "x" * 9000 + "</review>", None)) < 6100)

check("review inside think ignored", ex(f"<think>x <review>draft draft draft draft</review></think>{R}", None) == R)
check("cut-off think with review dropped", ex("<think>long chain of thought <review>partial partial", None) == "")
check("reasoning unclosed not posted", ex(None, f"thinking <review>{R} and more drafts") == "")

# main(): stubbed _gh / model, no network
import os as _os
calls = []
def run_main(review, comments, head_sha="abc1234def"):
    calls.clear()
    def gh(method, path, token, body=None, accept=None):
        calls.append((method, path, body))
        if method == "GET" and path.endswith("/comments?per_page=100"):
            return comments
        if accept and "diff" in accept:
            return "diff --git a/x b/x\n+1\n"
        if method == "GET":
            return {"title": "t", "head": {"sha": head_sha} if head_sha else {}}
        return {}
    pr._gh = gh
    pr._review_from_model = lambda *a: review
    _os.environ.update(GITHUB_TOKEN="t", GITHUB_REPOSITORY="o/r", PR_NUMBER="1")
    pr.main()
    return [c for c in calls if c[0] in ("POST", "PATCH")]
w = run_main("", [])
check("main: empty, no prior comment -> no post", w == [])
w = run_main("", [{"id": 7, "body": pr.MARKER + " old", "user": {"type": "Bot"}}])
check("main: empty, prior comment -> marked stale", len(w) == 1 and w[0][0] == "PATCH"
      and "No usable review" in w[0][2]["body"])
w = run_main(R, [{"id": 8, "body": "quoting " + pr.MARKER, "user": {"type": "User"}}])
check("main: human quote not patched", len(w) == 1 and w[0][0] == "POST")
w = run_main(R, [])
check("main: review posted with sha", len(w) == 1 and w[0][0] == "POST" and "`abc1234`" in w[0][2]["body"])
w = run_main(R, [], head_sha=None)
check("main: no sha -> no empty code span", "``" not in w[0][2]["body"])

print("ALL PASS" if not FAIL else f"FAILED: {FAIL}")
sys.exit(1 if FAIL else 0)
