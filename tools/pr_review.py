#!/usr/bin/env python3
"""Post an automated code review on a pull request, written by a FREE hosted model.

Fetches the PR's diff from the GitHub API, asks a free Kilo gateway model (keyless,
OpenAI-compatible wire format) to review it, and posts the review back as a PR comment.

Deliberately stdlib-only (leangym has no third-party deps) and FREE-MODEL-ONLY: the
model id must end in ':free'/'-free' (or be the free router), so this can never spend
against an account. It is advisory - a cheap extra reviewer, not a merge gate.

Env:
  GITHUB_TOKEN        repo token with pull-requests: write (required to post)
  GITHUB_REPOSITORY   owner/repo (required)
  PR_NUMBER           pull request number (required)
  REVIEW_MODEL        free model id (default nvidia/nemotron-3.5-lightning:free)
  KILO_API_KEY        optional; free models need none

Exit codes: 0 posted (or nothing to do), 2 bad usage/env, 1 model/API error.
"""
from __future__ import annotations

import json
import re
import os
import sys
import urllib.error
import urllib.request

KILO_BASE = "https://api.kilo.ai/api/gateway"   # sweep-ok (OpenAI-compatible gateway)
GH_API = "https://api.github.com"
DIFF_LIMIT = 24000          # chars of diff sent to the model (keeps the prompt bounded)
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning:free"
MARKER = "<!-- leangym-pr-review -->"   # lets us find/replace our own prior comment


def _die(msg, code=2):
    print(f"pr_review: {msg}", file=sys.stderr)
    sys.exit(code)


def _is_free(model):
    return model.endswith(":free") or model.endswith("-free") or model.endswith("/free")


def _gh(method, path, token, body=None, accept="application/vnd.github+json"):
    url = path if path.startswith("http") else f"{GH_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "User-Agent": "leangym-pr-review",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Content-Type": "application/json"} if data else {}),
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode()
    return raw if accept.endswith("diff") else (json.loads(raw) if raw else {})


def _review_from_model(model, title, diff):
    key = os.environ.get("KILO_API_KEY", "").strip()
    prompt = (
        "You are a concise senior code reviewer. Review the pull request diff below and "
        "reply in GitHub-flavoured markdown with: a one-line summary verdict, then at most "
        "five bullet points of concrete, actionable findings (bugs, missing tests, risky "
        "changes, style). If it looks good, say so briefly. Do not restate the diff. Do "
        "not invent issues. Wrap the final review, and nothing else, in an XML element "
        "named review.\n\n"
        f"PR title: {title}\n\n```diff\n{diff[:DIFF_LIMIT]}\n```")
    # Reasoning models spend part of the budget thinking before emitting content, so
    # give enough room that the visible review survives.
    payload = {"model": model, "max_tokens": 3072, "temperature": 0.2,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        f"{KILO_BASE}/chat/completions", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "leangym-pr-review",
                 **({"Authorization": f"Bearer {key}"} if key else {})})   # sweep-ok
    with urllib.request.urlopen(req, timeout=300) as resp:
        out = json.loads(resp.read().decode())
    choices = out.get("choices") or []
    if not choices:
        raise RuntimeError(f"model returned no choices: {str(out)[:200]}")
    msg = choices[0].get("message") or {}
    return extract_review(msg.get("content"), msg.get("reasoning"))


_OPEN = re.compile(r"<review>", re.I)
_CLOSE = re.compile(r"</review>", re.I)
_THINK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_THINK_OPEN = re.compile(r"<think(?:ing)?>.*", re.S | re.I)
_THINK_CLOSE = re.compile(r"^.*</think(?:ing)?>", re.S | re.I)
MIN_REVIEW = 20       # shorter than this is an echo / fragment, not a review
MAX_REVIEW = 6000


def _strip_think(text):
    """Remove closed, unclosed (to end) and lone-closing think blocks."""
    return _THINK_CLOSE.sub("", _THINK_OPEN.sub("", _THINK.sub("", text)))


def _last_block(src):
    """Text of the last <review> block. An unclosed final block counts up to the end
    (truncated output); returns "" when no opening tag exists."""
    opens = list(_OPEN.finditer(src))
    if not opens:
        return ""
    tail = src[opens[-1].end():]
    close = _CLOSE.search(tail)
    if close:
        return tail[:close.start()].strip()
    # unclosed last block: if an earlier block closed, prefer that complete one
    for o in reversed(opens[:-1]):
        t = src[o.end():]
        c = _CLOSE.search(t)
        if c:
            return t[:c.start()].strip()
    return tail.strip()


def _sanitize(text):
    text = text.replace("@", "@\u200b")       # no pings to users/teams
    if len(text) > MAX_REVIEW:
        text = text[:MAX_REVIEW].rstrip() + "\n\n<sub>(truncated)</sub>"
    return text


def extract_review(content, reasoning):
    """Return only the final review text, or "" if there is none to post.

    Free reasoning models sometimes leave `content` null and put everything in
    `reasoning`. That field is the model thinking aloud, so it is used only through an
    explicit review block. Content is used tagged if possible, else with any think
    block (closed, unclosed, or a lone closing tag) removed. Anything shorter than
    MIN_REVIEW is treated as an echo/fragment and dropped."""
    # Think blocks go first, so review tags drafted inside them never count.
    c = _strip_think(content or "")
    block = _last_block(c)
    if len(block) >= MIN_REVIEW:
        return _sanitize(block)
    if _OPEN.search(c):
        return ""            # tagged but unusable: never fall back to the drafts around it
    c = c.strip()
    if len(c) >= MIN_REVIEW:
        return _sanitize(c)
    # Reasoning is thinking aloud and may draft the review element while planning, so it
    # is never posted - not even a closed block. No content -> no review.
    return ""


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    pr = os.environ.get("PR_NUMBER", "").strip() or (sys.argv[1] if len(sys.argv) > 1 else "")
    model = os.environ.get("REVIEW_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if not token or not repo or not pr:
        _die("need GITHUB_TOKEN, GITHUB_REPOSITORY and PR_NUMBER")
    if not _is_free(model):
        _die(f"refusing non-free model '{model}' (must end in :free/-free) - this bot "
             f"never spends against an account", 2)

    pr_obj = _gh("GET", f"/repos/{repo}/pulls/{pr}", token)
    title = pr_obj.get("title", f"PR #{pr}")
    diff = _gh("GET", f"/repos/{repo}/pulls/{pr}", token,
               accept="application/vnd.github.v3.diff")
    if not diff.strip():
        print("pr_review: empty diff, nothing to review")
        return
    truncated = len(diff) > DIFF_LIMIT

    try:
        review = _review_from_model(model, title, diff)
    except (urllib.error.URLError, RuntimeError, ValueError) as e:
        _die(f"model review failed: {e}", 1)
    existing = _gh("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100", token)
    # Only our own comment (a human quoting the marker must not be PATCHed -> 403).
    mine = next((c for c in existing if MARKER in (c.get("body") or "")
                 and (c.get("user") or {}).get("type") == "Bot"), None)
    if not review:
        # No usable final answer (e.g. only reasoning came back). Never post raw
        # chain-of-thought; advisory bot, so not a CI failure. Only mark an existing
        # comment stale - don't create a placeholder on the first run.
        if not mine:
            print("pr_review: model gave no final review - not posting")
            return
        review = ("_No usable review was produced for this commit "
                  "(the model returned no final answer)._")
        print("pr_review: model gave no final review - marking comment as stale")

    note = ("\n\n<sub>Diff truncated for review.</sub>" if truncated else "")
    sha = ((pr_obj.get("head") or {}).get("sha") or "")[:7]
    head = f"### Automated review ({model})" + (f" - `{sha}`" if sha else "")
    comment = (f"{MARKER}\n{head}\n\n{review}{note}\n\n"
               f"<sub>Advisory only - a free model's second opinion, not a merge gate.</sub>")

    if mine:
        _gh("PATCH", f"/repos/{repo}/issues/comments/{mine['id']}", token, {"body": comment})
        print(f"pr_review: updated comment {mine['id']}")
    else:
        _gh("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": comment})
        print("pr_review: posted new review comment")


if __name__ == "__main__":
    main()
