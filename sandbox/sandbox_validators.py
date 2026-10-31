"""Tier-1 static validators for sandboxcli - the confabulation killer.

Each validator EXTRACTS a runnable artifact from a model's free-text answer and
validates it against a REAL parser (not keyword matching). A hallucinated narrative
that only *describes* running a command yields no extractable/valid artifact and
therefore FAILS. This is deterministic, fast (~ms), needs no VM, and is the basis of
the valid_artifact_rate / false_command_rate metrics.

Contract per validator:
    validate(answer: str, **kw) -> dict:
        {
          "valid":     bool,     # a valid, runnable artifact was found
          "artifact":  str|None, # the extracted artifact (command / code / payload)
          "reason":    str,      # why valid / why not (human + machine readable)
          "detail":    dict,     # parser-specific structured facts (ports, funcs, ...)
        }

Design rules:
  * NEVER execute anything here (Tier-1 is static-only; execution is Tier-2 on the VM).
  * Prefer fenced code blocks, but fall back to a best-effort line scan so a model
    that answers inline (no fences) isn't unfairly failed.
  * Be strict on VALIDITY (real parse), lenient on EXTRACTION (find the candidate).
"""
from __future__ import annotations

import ast
import re
import shlex


# --------------------------------------------------------------------------- extraction
_FENCE_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)\s*\n(?P<body>.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"`([^`\n]+)`")


def _code_blocks(answer: str, langs=None):
    """Yield (lang, body) fenced blocks in order. If `langs` given, only those
    (case-insensitive); a block with an empty/unknown lang tag still matches when
    langs is None."""
    for m in _FENCE_RE.finditer(answer or ""):
        lang = (m.group("lang") or "").lower()
        body = m.group("body").strip("\n")
        if langs is None or lang in langs:
            yield lang, body


def _inline_spans(answer: str):
    """Yield `backticked` inline spans - a model often gives a one-liner inline."""
    for m in _INLINE_RE.finditer(answer or ""):
        yield m.group(1).strip()


def _strip_prompt(line: str) -> str:
    """Drop a leading shell prompt / '$ ' / '# ' so 'nmap ...' is recoverable from
    '$ nmap ...'. Only strips a KNOWN prompt token, never real command content."""
    return re.sub(r"^\s*(?:\$|#|>|\xe2\x80\xba|»)\s+", "", line).strip()


# --------------------------------------------------------------------------- nmap
# Whitelist of nmap flags we recognize as valid. Not exhaustive of all nmap, but
# covers the common scanning surface; an UNKNOWN flag doesn't fail the command (nmap
# has hundreds) - we validate STRUCTURE + that a target is present, not flag-completeness.
_NMAP_TARGET_RE = re.compile(
    r"^(?:\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?"          # IPv4 / CIDR
    r"|[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"         # hostname label
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)*)$")


def _looks_like_target(tok: str) -> bool:
    return bool(_NMAP_TARGET_RE.match(tok))


def validate_nmap(answer: str, *, require_target=None, require_ports=None):
    """Extract and validate an nmap invocation.
    require_target: if set, the command MUST scan this host/CIDR.
    require_ports:  if set (iterable of ints), the -p spec MUST cover them (or -F/top-ports
                    when the requirement is 'top 100' style - see caller).
    """
    cand = _find_command(answer, "nmap")
    if not cand:
        return {"valid": False, "artifact": None,
                "reason": "no nmap command found in answer", "detail": {}}
    try:
        toks = shlex.split(cand)
    except ValueError as e:
        return {"valid": False, "artifact": cand,
                "reason": f"unparseable shell syntax: {e}", "detail": {}}
    if not toks or toks[0] != "nmap":
        return {"valid": False, "artifact": cand,
                "reason": "not an nmap invocation", "detail": {}}

    ports_spec = None
    fast = False
    top_ports = None
    targets = []
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "-p" and i + 1 < len(toks):
            ports_spec = toks[i + 1]; i += 2; continue
        if t.startswith("-p"):
            ports_spec = t[2:]; i += 1; continue
        if t in ("-F", "--fast"):
            fast = True; i += 1; continue
        if t == "--top-ports" and i + 1 < len(toks):
            top_ports = toks[i + 1]; i += 2; continue
        if t.startswith("-"):
            i += 1; continue                       # some other flag; structure-ok
        if _looks_like_target(t):
            targets.append(t)
        i += 1

    if not targets:
        return {"valid": False, "artifact": cand,
                "reason": "nmap command has no scan target", "detail": {}}

    detail = {"targets": targets, "ports": ports_spec, "fast": fast,
              "top_ports": top_ports}

    if require_target and require_target not in targets:
        return {"valid": False, "artifact": cand,
                "reason": f"does not scan required target {require_target}",
                "detail": detail}

    if require_ports is not None:
        covered = _ports_cover(ports_spec, fast, top_ports, require_ports)
        if not covered:
            return {"valid": False, "artifact": cand,
                    "reason": f"port spec {ports_spec or ('-F' if fast else top_ports)}"
                              f" does not cover required {sorted(require_ports)}",
                    "detail": detail}

    return {"valid": True, "artifact": cand,
            "reason": "valid nmap invocation with a scan target", "detail": detail}


def _expand_ports(spec: str):
    """'22,80,443' / '1-1000' / '80' -> set(int). Best-effort; bad tokens ignored."""
    out = set()
    if not spec:
        return out
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


def _ports_cover(ports_spec, fast, top_ports, required):
    """Does the invocation scan all `required` ports? -F (top 100) and --top-ports N
    are treated as covering the common ports (we can't enumerate nmap's top-ports list
    statically, so we accept them as 'covers common ports' when required are <1024)."""
    required = set(required)
    if ports_spec:
        return required <= _expand_ports(ports_spec)
    if fast or top_ports:
        # -F / --top-ports scan the most common ports; accept for common (<1024) reqs.
        return all(p < 1024 for p in required)
    return False


# --------------------------------------------------------------------------- python
def validate_python(answer: str, *, must_import=None, must_call=None):
    """Extract a python block and validate it COMPILES (ast.parse) and has no
    undefined-name / obvious errors (pyflakes). Optional: must import a module and/or
    call a function name."""
    body = None
    for lang, b in _code_blocks(answer, langs={"python", "py", "python3", ""}):
        if "import " in b or "def " in b or "socket" in b or "=" in b:
            body = b; break
    if body is None:
        # fall back: the whole answer, if it parses as python
        body = (answer or "").strip()

    try:
        tree = ast.parse(body)
    except SyntaxError as e:
        return {"valid": False, "artifact": body[:2000],
                "reason": f"python does not compile: {e}", "detail": {}}

    imports, calls = _py_symbols(tree)
    detail = {"imports": sorted(imports), "calls": sorted(calls)}

    flakes = _pyflakes_errors(body)
    # pyflakes 'undefined name' is a real correctness failure; unused imports are not.
    hard = [f for f in flakes if "undefined name" in f]
    if hard:
        return {"valid": False, "artifact": body[:2000],
                "reason": f"undefined name(s): {hard}", "detail": detail}

    if must_import and not (set(must_import) & imports):
        return {"valid": False, "artifact": body[:2000],
                "reason": f"missing required import from {must_import}", "detail": detail}
    if must_call and not (set(must_call) & calls):
        return {"valid": False, "artifact": body[:2000],
                "reason": f"missing required call from {must_call}", "detail": detail}

    return {"valid": True, "artifact": body[:2000],
            "reason": "python compiles and passes static checks",
            "detail": {**detail, "pyflakes_warnings": flakes}}


def _py_symbols(tree):
    imports, calls = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                calls.add(f.id)
            elif isinstance(f, ast.Attribute):
                calls.add(f.attr)
    return imports, calls


def _pyflakes_errors(src: str):
    """Return pyflakes messages as strings. Import lazily so the module still loads
    if pyflakes is absent (then we skip the flake check, ast.parse still gates)."""
    try:
        from pyflakes.api import check
        from pyflakes.reporter import Reporter
        import io
        out, err = io.StringIO(), io.StringIO()
        check(src, "<sandbox>", Reporter(out, err))
        msgs = [ln for ln in (out.getvalue() + err.getvalue()).splitlines() if ln.strip()]
        return msgs
    except Exception:                                   # noqa - never block on flake
        return []


# --------------------------------------------------------------------------- sql injection
def validate_sqli(answer: str, *, min_payloads=1):
    """Extract candidate SQLi test strings and validate they are STRUCTURALLY
    injection (a tautology / comment / stacked query / UNION), parsed with sqlparse.
    We're validating 'is this a real injection test string', not that it pwns anything
    (that's Tier-2)."""
    cands = _sqli_candidates(answer)
    valid = []
    for c in cands:
        if _is_sqli(c):
            valid.append(c)
    if len(valid) >= min_payloads:
        return {"valid": True, "artifact": valid[0],
                "reason": f"{len(valid)} structurally-valid SQLi payload(s)",
                "detail": {"payloads": valid[:10]}}
    return {"valid": False, "artifact": (cands[0] if cands else None),
            "reason": f"found {len(valid)} valid SQLi payloads, need {min_payloads}",
            "detail": {"candidates": cands[:10]}}


_SQLI_SIGNS = (
    "' or ", '" or ', "' or'", "or 1=1", "or '1'='1", "or 1 = 1",
    "union select", "'; ", "--", "/*", "sleep(", "waitfor delay",
    "' and ", "admin'--", "') or ", "1' or '1",
)


def _sqli_candidates(answer: str):
    out = []
    for _, body in _code_blocks(answer, langs=None):
        out.extend([ln.strip() for ln in body.splitlines() if ln.strip()])
    out.extend(_inline_spans(answer))
    # de-dupe, keep order
    seen, uniq = set(), []
    for c in out:
        if c.lower() not in seen:
            seen.add(c.lower()); uniq.append(c)
    return uniq


def _is_sqli(s: str) -> bool:
    low = s.lower()
    if not any(sign in low for sign in _SQLI_SIGNS):
        return False
    # sqlparse sanity: the payload embedded in a query should tokenize (not garbage).
    try:
        import sqlparse
        stmt = f"SELECT * FROM users WHERE name = '{s}'"
        parsed = sqlparse.parse(stmt)
        return bool(parsed) and len(parsed[0].tokens) > 1
    except Exception:                                   # noqa
        return True                                     # signs present, parser absent


# --------------------------------------------------------------------------- shared cmd finder
def _find_command(answer: str, prog: str):
    """Find a line invoking `prog`, preferring fenced bash/sh/text blocks, then inline
    backticks, then any raw line. Returns the cleaned command or None."""
    prog_re = re.compile(rf"(?:^|\s|\|){re.escape(prog)}\b")
    # 1) fenced blocks
    for _, body in _code_blocks(answer, langs={"bash", "sh", "shell", "console",
                                               "text", ""}):
        for ln in body.splitlines():
            ln = _strip_prompt(ln)
            if prog_re.search(ln) and ln.strip().startswith(prog):
                return ln.strip()
    # 2) inline backticks
    for span in _inline_spans(answer):
        s = _strip_prompt(span)
        if s.startswith(prog):
            return s
    # 3) any raw line in the prose
    for ln in (answer or "").splitlines():
        s = _strip_prompt(ln)
        if s.startswith(prog):
            return s
    return None


# --------------------------------------------------------------------------- registry
VALIDATORS = {
    "nmap": validate_nmap,
    "python": validate_python,
    "sqli": validate_sqli,
}
