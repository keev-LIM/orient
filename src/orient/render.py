"""Formatting. Every function here returns a string and writes nothing.

The eight report sections are always rendered, in SECTIONS order, whether or not
the model contributed anything: with `--no-llm` each section falls back to its
deterministic table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from orient.collect import Facts
from orient.env import OrientError

SECTIONS: tuple[str, ...] = (
    "Snapshot",
    "Entry points",
    "Module map",
    "Hotspots",
    "Risk flags",
    "Who to ask",
    "Reading order",
    "Suggested first tasks",
)

RULE = "─" * 66
NO_GIT = "git history unavailable — this section is size-only."


# --------------------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if value is None:
        return "-"
    return str(value)


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def render_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    """Left-aligned fixed-width text table, numbers right-aligned."""
    if not rows:
        return ""
    cells = [[_cell(row.get(col, "")) for col in columns] for row in rows]
    right = [all(_numeric(row.get(col, "")) for row in rows) for col in columns]
    widths = [
        max(len(col), *(len(cell[i]) for cell in cells)) for i, col in enumerate(columns)
    ]
    out = []
    header = "  ".join(
        columns[i].rjust(widths[i]) if right[i] else columns[i].ljust(widths[i])
        for i in range(len(columns))
    )
    out.append(header.rstrip())
    for cell in cells:
        line = "  ".join(
            cell[i].rjust(widths[i]) if right[i] else cell[i].ljust(widths[i])
            for i in range(len(columns))
        )
        out.append(line.rstrip())
    return "\n".join(out)


def _md_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return ""
    head = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(_cell(row.get(col, "")).replace("|", "\\|") for col in columns) + " |"
        for row in rows
    ]
    return "\n".join([head, rule, *body])


def _prose(text: str) -> str:
    """Model output, with any leading '#' stripped so it cannot forge a section."""
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            line = stripped.lstrip("#").lstrip()
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _split_blocks(text: str, count: int) -> list[str]:
    """Split model output on separator lines of three or more dashes."""
    parts = [part.strip() for part in re.split(r"(?m)^\s*-{3,}\s*$", text)]
    return [part for part in parts if part][:count]


# --------------------------------------------------------------------------------------
# section bodies (shared by markdown and text)
# --------------------------------------------------------------------------------------


def _snapshot_lines(facts: Facts) -> list[str]:
    snap = facts["snapshot"]
    hist = facts["history"]
    cfg = facts["config"]
    langs = ", ".join(
        f"{name} {count}"
        for name, count in sorted(snap["languages"].items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    )
    lines = [
        f"- root: {facts['root']}",
        f"- files: {snap['files_tracked']:,} tracked · "
        f"{snap['files_analyzed']:,} python · {snap['files_other']:,} other · "
        f"{snap['test_files']:,} test {'file' if snap['test_files'] == 1 else 'files'}",
        f"- lines: {snap['lines']:,} across {langs or 'no recognised languages'}",
    ]
    if hist["available"]:
        lines.append(
            f"- history: {cfg['since']} window ({hist['window_days']}d) · "
            f"{hist['commits']:,} commits · {len(hist['authors'])} authors"
        )
    else:
        lines.append(f"- history: unavailable ({hist['reason']})")
    if snap["truncated"]:
        lines.append(f"- note: the walk stopped at {cfg['max_files']:,} files; results are partial")
    if snap["unparsed"]:
        lines.append(f"- note: {snap['unparsed']} python files did not parse; regex fallback used")
    return lines


def _entry_rows(facts: Facts) -> list[dict[str, Any]]:
    return [
        {
            "path": e["path"],
            "kind": e["kind"],
            "detail": e["detail"],
            "confidence": e["confidence"],
        }
        for e in facts["entry_points"][:15]
    ]


def _module_rows(facts: Facts) -> list[dict[str, Any]]:
    return [
        {
            "module": m["path"],
            "files": m["files"],
            "lines": m["lines"],
            "fan_in": m["fan_in"],
            "fan_out": m["fan_out"],
            "commits": m["commits"],
            "tests": "yes" if m["has_tests"] else "no",
            "top authors": ", ".join(m["authors"]) or "-",
        }
        for m in facts["modules"][:15]
    ]


def _hotspot_rows(facts: Facts) -> list[dict[str, Any]]:
    return [
        {
            "score": h["score"],
            "file": h["path"],
            "commits": h["commits"],
            "authors": h["authors"],
            "lines": h["lines"],
            "max_nest": h["max_nesting"],
            "fan_in": h["fan_in"],
            "test_ratio": h["test_ratio"],
            "reasons": ", ".join(h["reasons"]) or "-",
        }
        for h in facts["hotspots"]
    ]


def _flag_rows(facts: Facts) -> list[dict[str, Any]]:
    return [
        {"flag": f["flag"].replace("_", "-"), "path": f["path"], "detail": f["detail"]}
        for f in facts["flags"][:25]
    ]


def _owner_rows(owners: Mapping[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    return [
        {
            "author": a["name"],
            "share": f"{a['share'] * 100:.0f}%",
            "commits": a["commits"],
            "last": (a["last"] or "-")[:10],
            "status": f"INACTIVE {a['inactive_months']}mo" if a["inactive_months"] >= 6 else "",
        }
        for a in owners["authors"][:limit]
    ]


def _reading_fallback(facts: Facts) -> list[str]:
    picks: list[str] = []
    for entry in facts["entry_points"][:2]:
        picks.append(f"{entry['path']} — {entry['kind'].replace('_', ' ')}: {entry['detail']}")
    for hot in facts["hotspots"][:4]:
        line = (
            f"{hot['path']} — {hot['commits']} commits, {hot['lines']} lines, "
            f"{hot['fan_in']} importers"
        )
        if line not in picks:
            picks.append(line)
    return [f"{i}. {text}" for i, text in enumerate(picks[:6], 1)]


def _task_fallback(facts: Facts) -> list[str]:
    tasks: list[str] = []
    for flag in facts["flags"]:
        if flag["flag"] == "no_tests":
            tasks.append(f"- Add a first test for {flag['path']} ({flag['detail']}).")
        elif flag["flag"] == "dead_ish":
            tasks.append(
                f"- Confirm whether {flag['path']} is still live, then delete or document it."
            )
        elif flag["flag"] == "god_file":
            tasks.append(f"- Split one responsibility out of {flag['path']} ({flag['detail']}).")
        elif flag["flag"] == "config_surface":
            tasks.append(f"- Review how configuration is loaded in {flag['path']}.")
        if len(tasks) >= 3:
            break
    for hot in facts["hotspots"][:3]:
        if len(tasks) >= 3:
            break
        if hot["test_ratio"] < 0.25:
            tasks.append(f"- Cover {hot['path']} with a characterization test before changing it.")
    if not tasks:
        tasks.append("- Read the entry points above and trace one request end to end.")
    return tasks[:3]


def _section_bodies(facts: Facts, narrative: Mapping[str, str], *, md: bool) -> dict[str, str]:
    table = _md_table if md else render_table
    git_ok = facts["history"]["available"]
    reading = narrative.get("reading_order", "")
    reading_blocks = _split_blocks(reading, 2) if reading else []

    bodies: dict[str, str] = {}

    parts = [_prose(narrative["snapshot"])] if "snapshot" in narrative else []
    parts.append("\n".join(_snapshot_lines(facts)))
    bodies["Snapshot"] = "\n\n".join(p for p in parts if p)

    entries = _entry_rows(facts)
    bodies["Entry points"] = (
        table(entries, ["path", "kind", "detail", "confidence"])
        if entries
        else "No entry points detected. Look for a Makefile, a Dockerfile, or a "
        "`if __name__` guard added since this scan."
    )

    parts = [_prose(narrative["module_map"])] if "module_map" in narrative else []
    modules = _module_rows(facts)
    parts.append(
        table(
            modules,
            ["module", "files", "lines", "fan_in", "fan_out", "commits", "tests", "top authors"],
        )
        if modules
        else "No source directories found."
    )
    bodies["Module map"] = "\n\n".join(p for p in parts if p)

    parts = []
    if not git_ok:
        parts.append(NO_GIT + " Ranking uses size, nesting, fan-in and test coverage only.")
    if "hotspots" in narrative:
        parts.append(_prose(narrative["hotspots"]))
    hotspots = _hotspot_rows(facts)
    parts.append(
        table(
            hotspots,
            ["score", "file", "commits", "authors", "lines", "max_nest", "fan_in",
             "test_ratio", "reasons"],
        )
        if hotspots
        else "No source files to rank."
    )
    bodies["Hotspots"] = "\n\n".join(p for p in parts if p)

    flags = _flag_rows(facts)
    bodies["Risk flags"] = (
        table(flags, ["flag", "path", "detail"])
        if flags
        else "No bus-factor, dead-code, god-file or config-surface flags were raised."
    )

    parts = []
    owners = facts["owners"]
    if not git_ok:
        parts.append(NO_GIT + " Nobody can be named without commit history.")
    elif not owners["authors"]:
        parts.append("No commits in the window, so no owners can be named.")
    else:
        parts.append(
            f"{owners['commits']:,} commits over {owners['files']:,} files in the window."
        )
        if "ownership" in narrative:
            parts.append(_prose(narrative["ownership"]))
        parts.append(table(_owner_rows(owners), ["author", "share", "commits", "last", "status"]))
    bodies["Who to ask"] = "\n\n".join(p for p in parts if p)

    if reading_blocks:
        bodies["Reading order"] = _prose(reading_blocks[0])
    else:
        bodies["Reading order"] = "\n".join(_reading_fallback(facts)) or "Nothing to read yet."

    if len(reading_blocks) > 1 and reading_blocks[1]:
        bodies["Suggested first tasks"] = _prose(reading_blocks[1])
    else:
        bodies["Suggested first tasks"] = "\n".join(_task_fallback(facts))

    return bodies


# --------------------------------------------------------------------------------------
# whole-document renderers
# --------------------------------------------------------------------------------------


def render_markdown(facts: Facts, narrative: Mapping[str, str]) -> str:
    """The eight `## ` sections in SECTIONS order, always all eight."""
    bodies = _section_bodies(facts, narrative, md=True)
    name = facts["root"].rstrip("/").rsplit("/", 1)[-1] or facts["root"]
    out = [
        f"# orient — {name}",
        "",
        f"_generated {facts['generated_at']} · window {facts['config']['since']} · "
        f"schema v{facts['schema_version']}_",
    ]
    if facts["warnings"]:
        out += ["", *[f"> warning: {w}" for w in facts["warnings"]]]
    for section in SECTIONS:
        out += ["", f"## {section}", "", bodies[section]]
    return "\n".join(out).rstrip() + "\n"


def render_json(facts: Facts, narrative: Mapping[str, str]) -> str:
    import json

    return json.dumps({**facts, "narrative": dict(narrative)}, sort_keys=True, indent=2) + "\n"


def render_text(facts: Facts, narrative: Mapping[str, str]) -> str:
    """Same eight sections as plain text, no Markdown syntax."""
    bodies = _section_bodies(facts, narrative, md=False)
    out = [f"orient — {facts['root']}", f"generated {facts['generated_at']}"]
    for warning in facts["warnings"]:
        out.append(f"warning: {warning}")
    for section in SECTIONS:
        out += ["", section.upper(), RULE, bodies[section]]
    return "\n".join(out).rstrip() + "\n"


def render(facts: Facts, narrative: Mapping[str, str], fmt: str) -> str:
    if fmt == "md":
        return render_markdown(facts, narrative)
    if fmt == "json":
        return render_json(facts, narrative)
    if fmt == "text":
        return render_text(facts, narrative)
    raise OrientError(f"bad value for --format: {fmt}", 2)


def render_summary(
    facts: Facts, narrative: Mapping[str, str], *, stats: Mapping[str, Any]
) -> str:
    """The terminal block: header, counts, llm line, Start here, Top hotspots, Flags."""
    snap = facts["snapshot"]
    hist = facts["history"]
    out = [
        f"orient {stats['version']}  ·  {facts['root']}",
        "",
        f"  files       {snap['files_tracked']:,} tracked   ·  "
        f"{snap['files_analyzed']:,} analyzed (python), {snap['files_other']:,} counted (other)",
        f"  lines       {snap['lines']:,}",
    ]
    if hist["available"]:
        out.append(
            f"  history     {facts['config']['since']} · {hist['commits']:,} commits · "
            f"{len(hist['authors'])} authors"
        )
    else:
        out.append(f"  history     unavailable ({hist['reason']})")
    out.append(
        f"  llm         {stats['llm']} · {stats['calls']} calls · {stats['seconds']:.1f}s"
    )
    out.append("")
    for written in stats.get("written", []):
        out.append(f"Wrote {written}")

    picks = _reading_fallback(facts)[:4]
    if picks:
        out += ["", "  Start here", "  " + RULE]
        out += [f"  {line}" for line in picks]

    hotspots = _hotspot_rows(facts)[:5]
    if hotspots:
        out += ["", f"  Top hotspots (churn x complexity, {facts['config']['since']})", "  " + RULE]
        table = render_table(
            hotspots, ["score", "file", "commits", "lines", "fan_in", "test_ratio"]
        )
        out += [f"  {line}" for line in table.splitlines()]

    flags = _flag_rows(facts)[:6]
    if flags:
        out += ["", "  Flags", "  " + RULE]
        for flag in flags:
            out.append(f"  {flag['flag']:<14} {flag['path']}  · {flag['detail']}")

    if "hotspots" in narrative:
        out += ["", "  " + _prose(narrative["hotspots"]).splitlines()[-1]]
    return "\n".join(out) + "\n"


def render_hotspots(facts: Facts, top: int, fmt: str) -> str:
    """The ranked table alone, in the requested format."""
    rows = _hotspot_rows(facts)[:top]
    columns = [
        "score", "file", "commits", "authors", "lines", "max_nest", "fan_in", "test_ratio",
    ]
    if fmt == "json":
        import json

        return json.dumps(facts["hotspots"][:top], sort_keys=True, indent=2) + "\n"
    if fmt == "md":
        body = _md_table(rows, columns) or "No source files to rank."
        return f"## Hotspots\n\n{body}\n"
    if fmt == "text":
        return (render_table(rows, columns) or "No source files to rank.") + "\n"
    raise OrientError(f"bad value for --format: {fmt}", 2)


def render_owners(owners: Mapping[str, Any], note: str) -> str:
    """The owners table; appends `note` as a paragraph when it is non-empty."""
    if not owners["available"]:
        return f"{owners['path']}\n\n  {NO_GIT}\n"
    head = (
        f"{owners['path']}  ({owners['files']} files, {owners['commits']} commits in window)"
    )
    if not owners["authors"]:
        return f"{head}\n\n  No commits in the window for this path.\n"
    table = render_table(
        _owner_rows(owners, limit=8), ["author", "share", "commits", "last", "status"]
    )
    out = [head, ""] + [f"  {line}" for line in table.splitlines()]
    shown = min(8, len(owners["authors"]))
    if len(owners["authors"]) > shown:
        rest = sum(a["commits"] for a in owners["authors"][shown:])
        out.append(f"  ({len(owners['authors']) - shown} others)  {rest} commits")
    if note.strip():
        out += ["", *[f"  {line}" for line in _prose(note).splitlines()]]
    return "\n".join(out) + "\n"


def render_explain(payload: Mapping[str, Any], narrative: Mapping[str, str]) -> str:
    """The deterministic sections always; the prose ones only when the model spoke."""
    blocks = _split_blocks(narrative["explain"], 3) if "explain" in narrative else []
    head = (
        f"{payload['path']}  ·  {payload['lines']:,} lines  ·  "
        f"{payload['commits']} commits/{payload['window_days']}d  ·  "
        f"{len(payload['callers'])} importers"
    )
    out = [head, ""]

    if blocks:
        out += ["WHAT IT DOES", *[f"  {line}" for line in _prose(blocks[0]).splitlines()], ""]

    shape = (
        f"  {payload['classes']} classes, {payload['functions']} functions, "
        f"max nesting {payload['max_nesting']}, test ratio {payload['test_ratio']:.2f}"
    )
    names = ", ".join(payload["defs"][:12]) or "no public definitions"
    out += ["SHAPE", shape, f"  public: {names}", ""]

    out.append("WHO CALLS IT")
    if payload["callers"]:
        for caller in payload["callers"][:10]:
            names = ", ".join(caller["names"][:6]) or "module import"
            out.append(f"  {caller['path']:<40} {names}")
        if len(payload["callers"]) > 10:
            out.append(f"  ({len(payload['callers']) - 10} more — see .orient/facts.json)")
    else:
        out.append("  nothing in this repository imports it")
    out.append("")

    depends = payload["depends"]
    out += [
        "WHAT IT DEPENDS ON",
        f"  stdlib:   {', '.join(depends['stdlib'][:12]) or '-'}",
        f"  internal: {', '.join(depends['internal'][:12]) or '-'}",
        f"  external: {', '.join(depends['external'][:12]) or '-'}",
    ]

    if len(blocks) > 1 and blocks[1]:
        why = [f"  {ln}" for ln in _prose(blocks[1]).splitlines()]
        out += ["", "WHY IT CHANGES SO OFTEN", *why]
    if len(blocks) > 2 and blocks[2]:
        out += ["", "BEFORE YOU EDIT", *[f"  {ln}" for ln in _prose(blocks[2]).splitlines()]]
    return "\n".join(out) + "\n"
