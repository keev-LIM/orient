"""Facts in, prose out.

The model never sees the repository: it sees a subset of facts.json plus, for
one prompt kind, at most 120 lines from each of the three top hotspot files.
Every prompt is hard-truncated at PROMPT_LIMIT characters, and every failure
mode of the `claude` binary — absent, slow, angry — becomes a warning string
rather than an exception.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from orient.collect import Facts
from orient.env import Env, OrientError

PROMPT_LIMIT: int = 40_000
EXCERPT_LINES: int = 120
MAX_CALLS: int = 6

_RULES = (
    "You are helping an engineer who was handed this codebase three days ago.\n"
    "You are given derived facts about the repository, not the repository.\n"
    "Answer only from those facts. Do not invent file names, people, or APIs.\n"
    "Be concrete and short. Plain prose and '-' bullets only; no headings.\n"
)

PROMPTS: Mapping[str, str] = {
    "snapshot": (
        _RULES
        + "\nWrite one paragraph (<=90 words) describing what this repository appears "
        "to be, based on its languages, size, entry points and directory names."
    ),
    "module_map": (
        _RULES
        + "\nFor each of the largest modules listed, write one line of the form "
        "'path - what it appears to be for'. At most 10 lines, no preamble."
    ),
    "hotspots": (
        _RULES
        + "\nFor the top hotspot files, write one line each explaining why an engineer "
        "should care about that file, using its churn, fan-in, size and test coverage. "
        "At most 6 lines. Then one line of the form 'Watch: <the single riskiest file "
        "and why>'."
    ),
    "reading_order": (
        _RULES
        + "\nProduce two blocks separated by a line containing only '---'.\n"
        "Block 1: a numbered reading order of 4-6 files, each with a short reason "
        "('path - reason').\n"
        "Block 2: three suggested starter tasks a new engineer could pick up, each one "
        "line, naming the file they would touch."
    ),
    "ownership": (
        _RULES
        + "\nWrite one short paragraph (<=60 words) saying who to ask first about this "
        "path and why, taking into account how much each author wrote and how long ago "
        "they last committed."
    ),
    "explain": (
        _RULES
        + "\nProduce three blocks separated by lines containing only '---'.\n"
        "Block 1 (WHAT IT DOES): 2-4 sentences on this file's job.\n"
        "Block 2 (WHY IT CHANGES SO OFTEN): 1-3 sentences from its churn and shape; if "
        "history is unavailable, say so in one sentence.\n"
        "Block 3 (BEFORE YOU EDIT): 2-4 '-' bullets of concrete cautions."
    ),
}

_SUBSETS: Mapping[str, tuple[str, ...]] = {
    "snapshot": ("snapshot", "config", "entry_points", "history"),
    "module_map": ("snapshot", "modules", "entry_points"),
    "hotspots": ("hotspots", "flags", "history"),
    "reading_order": ("entry_points", "modules", "hotspots", "flags", "snapshot"),
    "ownership": ("owners", "history"),
    "explain": ("explain",),
}

_LIST_CAPS: Mapping[str, int] = {
    "entry_points": 12,
    "modules": 15,
    "hotspots": 8,
    "flags": 12,
}


def available(env: Env) -> bool:
    """True if `claude` is on PATH."""
    return env.which("claude") is not None


def excerpt(env: Env, path: str, *, max_lines: int = EXCERPT_LINES) -> str:
    """First max_lines lines of one file, prefixed with its path. Never more."""
    lines = env.read_text(path).splitlines()[:max_lines]
    return "\n".join([f"# {path} (first {len(lines)} lines)", *lines])


def _trim(value: Any, key: str) -> Any:
    cap = _LIST_CAPS.get(key)
    if cap is not None and isinstance(value, list):
        return value[:cap]
    if key == "history" and isinstance(value, dict):
        trimmed = dict(value)
        trimmed["authors"] = trimmed.get("authors", [])[:8]
        return trimmed
    return value


def build_prompt(
    kind: str,
    facts: Facts,
    *,
    excerpts: Mapping[str, str] | None = None,
    limit: int = PROMPT_LIMIT,
) -> str:
    """Facts subset + excerpts rendered into PROMPTS[kind], truncated at a line boundary."""
    if kind not in PROMPTS:
        raise OrientError(f"unknown prompt kind: {kind}", 2)
    keys = _SUBSETS[kind]
    payload = {key: _trim(facts[key], key) for key in keys if key in facts}
    parts = [PROMPTS[kind], "", "FACTS", json.dumps(payload, sort_keys=True, indent=1)]
    if excerpts:
        parts += ["", "SOURCE EXCERPTS"]
        parts += [excerpts[path] for path in sorted(excerpts)]
    text = "\n".join(parts)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    newline = cut.rfind("\n")
    return cut[:newline] if newline > 0 else cut


def narrate(
    env: Env,
    facts: Facts,
    kinds: Sequence[str],
    *,
    timeout: float = 90.0,
    excerpts: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """One claude call per kind, at most MAX_CALLS, in the given order."""
    prose: dict[str, str] = {}
    warnings: list[str] = []
    if not available(env):
        return prose, ["claude not found — wrote deterministic report"]

    for kind in list(kinds)[:MAX_CALLS]:
        prompt = build_prompt(kind, facts, excerpts=(excerpts or {}).get(kind, {}))
        done = env.claude(prompt, timeout=timeout)
        if done.timed_out:
            warnings.append(
                f"claude timed out after {timeout:g}s ({kind}) — wrote deterministic report"
            )
            continue
        if done.code != 0:
            warnings.append(f"claude exited {done.code} ({kind}) — section reduced")
            continue
        text = done.stdout.strip()
        if not text:
            warnings.append(f"claude returned no text ({kind}) — section reduced")
            continue
        prose[kind] = text
    return prose, warnings
