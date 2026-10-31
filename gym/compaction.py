"""Compaction cert probes: the 'what state survived a compaction' corpus + field match.

A compaction stage asks the model to compact and keep load-bearing state alive. We score
by building ONE corpus of every durable sink core's auto_compact instruction sanctions
(the compact block, the pinned plan, the queued next-step self-prompt, the session
notebook, disk writes) and checking that the required fields are present in it.

Gym-native: reads the live lean-coder agent's attributes directly (pinned_plan, notes,
_autostart_pending) - no probes_lean dependency. Every part is optional and defensive."""
from __future__ import annotations

import os
import re


def notebook_text(agent) -> str:
    """Concatenated text of the agent's session notebook (the `note` tool sink; core keeps
    it on agent.notes as [{dtg, text}]). A durable store a model may park state in during a
    compact. Empty string if none."""
    try:
        return "\n".join(n.get("text", "") for n in (getattr(agent, "notes", None) or []))
    except Exception:                                              # noqa
        return ""


def disk_text(root, max_files=40, max_bytes=200_000) -> str:
    """Concatenated text of every file under a workspace, so state the model wrote to DISK
    during the compact counts as preserved. Bounded and fully defensive: unreadable/binary
    files are skipped, never raises."""
    try:
        chunks, seen, total = [], 0, 0
        for base, _dirs, files in os.walk(root):
            if ".git" in base.split(os.sep):
                continue
            for fn in files:
                if seen >= max_files or total >= max_bytes:
                    break
                try:
                    with open(os.path.join(base, fn), "r", errors="ignore") as fh:
                        data = fh.read(max_bytes - total)
                except (OSError, ValueError):
                    continue
                chunks.append(data); seen += 1; total += len(data)
        return "\n".join(chunks)
    except Exception:                                              # noqa
        return ""


def durable_corpus(agent, ws=None, *, block="", extra=()) -> str:
    """THE shared 'what survived a compaction' corpus. One definition so the compaction
    probes can't drift on what counts as preserved. Unions every sink core's auto_compact
    sanctions:
      - the compact summary block (passed in; the compaction return value)
      - the pinned plan (rides across the compaction)
      - the queued next-step self-prompt (_autostart_pending)
      - the session notebook (the `note` tool -> agent.notes)
      - anything written to the workspace on disk (if `ws` given)
      - any caller-specific extra strings
    Every part is optional and defensive; returns one newline-joined string to field-check
    against."""
    parts = [
        block or "",
        getattr(agent, "pinned_plan", "") or "",
        getattr(agent, "_autostart_pending", None) or "",
        notebook_text(agent),
    ]
    if ws is not None:
        parts.append(disk_text(ws))
    parts.extend(str(e) for e in extra if e)
    return "\n".join(parts)


def present(corpus, groups):
    """corpus -> {field: any token present}. groups: field -> [tokens].

    Tokens match at a WORD/FILENAME boundary (no `helper` inside `helpers.py`, no `done`
    inside `abandoned`), and groups are NON-OVERLAPPING - a span consumed by an earlier
    group can't also satisfy a later one (so "helpers.py" doesn't satisfy both names_file
    and states_task via the substring 'helper')."""
    c = corpus.lower()
    used, out = [], {}
    for k, toks in groups.items():
        hit = False
        for t in toks:
            # boundary: no word/dotted-name char before; after, no word char and no
            # `.word` continuation (a sentence-final '.' is fine).
            pat = re.compile(r"(?<![\w.])" + re.escape(t.lower()) + r"(?!\w|\.\w)")
            for m in pat.finditer(c):
                if not any(m.start() < e and s < m.end() for s, e in used):
                    used.append((m.start(), m.end()))
                    hit = True
                    break
            if hit:
                break
        out[k] = hit
    return out
