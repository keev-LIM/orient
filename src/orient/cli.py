"""Argument parsing, command dispatch, exit codes.

0 success · 1 unexpected error · 2 bad usage · 3 missing cache or unreadable
path · 4 degraded run (output was written, but git or claude was unavailable).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from orient import collect, llm, render
from orient.collect import Config
from orient.env import Env, OrientError

VERSION: str = "0.1.0"
SCAN_KINDS = ("snapshot", "module_map", "hotspots", "reading_order")


def _add_common(parser: argparse.ArgumentParser, *, llm_flags: bool = True) -> None:
    parser.add_argument("--since", default="12mo", metavar="DURATION",
                        help="history window for churn and ownership (default: 12mo)")
    parser.add_argument("--top", type=int, default=10, metavar="N",
                        help="rows in ranked tables (default: 10)")
    parser.add_argument("--include", action="append", default=[], metavar="GLOB")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    parser.add_argument("--max-files", type=int, default=20_000, metavar="N")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore .orient/facts.json and recompute")
    parser.add_argument("--quiet", action="store_true")
    if llm_flags:
        parser.add_argument("--no-llm", action="store_true",
                            help="deterministic only, zero claude invocations")
        parser.add_argument("--llm-timeout", type=float, default=90.0, metavar="SECONDS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orient", description="Generate an orientation report for a codebase."
    )
    parser.add_argument("--version", action="version", version=f"orient {VERSION}")
    subs = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    scan = subs.add_parser("scan", help="analyze a codebase and write a report")
    scan.add_argument("path", nargs="?", default=".")
    scan.add_argument("--format", default="md", choices=("md", "json", "text"))
    scan.add_argument("-o", dest="output", default=None, metavar="PATH",
                      help="report destination ('-' for stdout; default .orient/report.md)")
    _add_common(scan)

    report = subs.add_parser("report", help="re-render the last scan in another format")
    report.add_argument("--format", default="md", choices=("md", "json", "text"))
    report.add_argument("-o", dest="output", default=None, metavar="PATH")
    report.add_argument("--quiet", action="store_true")

    hotspots = subs.add_parser("hotspots", help="print the ranked hotspot table only")
    hotspots.add_argument("--format", default="text", choices=("md", "json", "text"))
    _add_common(hotspots, llm_flags=False)

    owners = subs.add_parser("owners", help="print who to ask about a path")
    owners.add_argument("path", nargs="?", default=".")
    _add_common(owners)

    explain = subs.add_parser("explain", help="explain one file or directory in depth")
    explain.add_argument("path")
    _add_common(explain)

    subs.add_parser("doctor", help="check the environment")
    return parser


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _config(args: argparse.Namespace, root: Path) -> Config:
    cfg = Config(
        root=root,
        since=getattr(args, "since", "12mo"),
        top=getattr(args, "top", 10),
        include=tuple(getattr(args, "include", []) or ()),
        exclude=tuple(getattr(args, "exclude", []) or ()),
        max_files=getattr(args, "max_files", 20_000),
        use_cache=not getattr(args, "no_cache", False),
    )
    if cfg.top < 1:
        raise OrientError("bad value for --top: must be at least 1", 2)
    if cfg.max_files < 1:
        raise OrientError("bad value for --max-files: must be at least 1", 2)
    collect.parse_duration(cfg.since)
    return cfg


def _same_config(facts: collect.Facts, cfg: Config) -> bool:
    """A cache computed under different flags answers a different question."""
    cached = facts["config"]
    return (
        cached["since"] == cfg.since
        and cached["top"] == cfg.top
        and cached["include"] == list(cfg.include)
        and cached["exclude"] == list(cfg.exclude)
        and cached["max_files"] == cfg.max_files
    )


def _cached_or_collected(env: Env, cfg: Config) -> collect.Facts:
    if cfg.use_cache:
        cached = collect.load_cache(env)
        if cached is not None and _same_config(cached, cfg) and collect.cache_is_fresh(cached, env):
            return cached
    return collect.collect(env, cfg)


def _relative_target(env: Env, raw: str) -> str:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    try:
        return path.resolve().relative_to(env.root).as_posix()
    except ValueError:
        return raw.strip("/")


def _emit(text: str, quiet: bool = False) -> None:
    if not quiet:
        sys.stdout.write(text)


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace, env: Env) -> int:
    cfg = _config(args, env.root)
    facts = _cached_or_collected(env, cfg)

    narrative: dict[str, str] = {}
    calls = 0
    elapsed = 0.0
    llm_label = "disabled (--no-llm)"
    if not args.no_llm:
        llm_label = "claude"
        excerpts = {
            "hotspots": {
                hot["path"]: llm.excerpt(env, hot["path"]) for hot in facts["hotspots"][:3]
            }
        }
        started = time.monotonic()
        narrative, warnings = llm.narrate(
            env, facts, SCAN_KINDS, timeout=args.llm_timeout, excerpts=excerpts
        )
        elapsed = time.monotonic() - started
        calls = len(narrative) + sum(1 for w in warnings if "not found" not in w)
        facts["warnings"] = [*facts["warnings"], *warnings]
        if not llm.available(env):
            llm_label = "claude (missing)"

    facts["narrative"] = narrative
    body = render.render(facts, narrative, args.format)

    written: list[str] = []
    cache_bytes = collect.save_cache(env, facts)
    written.append(f".orient/facts.json  ({cache_bytes // 1024 or 1} KB)")

    if args.output == "-":
        _emit(body)
    else:
        target = Path(args.output) if args.output else env.root / ".orient" / "report.md"
        size = env.write_text(target, body)
        try:
            shown = target.resolve().relative_to(env.root).as_posix()
        except ValueError:
            shown = str(target)
        written.append(f"{shown}   ({size // 1024 or 1} KB)")

    stats = {
        "version": VERSION,
        "llm": llm_label,
        "calls": calls,
        "seconds": elapsed,
        "written": written,
    }
    if args.output != "-":
        _emit(render.render_summary(facts, narrative, stats=stats), args.quiet)
    for warning in facts["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 4 if facts["warnings"] else 0


def cmd_report(args: argparse.Namespace, env: Env) -> int:
    facts = collect.load_cache(env)
    if facts is None:
        raise OrientError("no .orient/facts.json — run 'orient scan' first", 3)
    narrative = facts.get("narrative", {})
    body = render.render(facts, narrative, args.format)
    if args.output and args.output != "-":
        env.write_text(Path(args.output), body)
        _emit(f"Wrote {args.output}\n", args.quiet)
    else:
        _emit(body)
    return 0


def cmd_hotspots(args: argparse.Namespace, env: Env) -> int:
    cfg = _config(args, env.root)
    facts = _cached_or_collected(env, cfg)
    _emit(render.render_hotspots(facts, cfg.top, args.format))
    return 0


def cmd_owners(args: argparse.Namespace, env: Env) -> int:
    cfg = _config(args, env.root)
    facts = _cached_or_collected(env, cfg)
    hist = collect.history_from_facts(facts)
    prefix = _relative_target(env, args.path)
    ownership = collect.owners(hist, prefix, now=env.now())

    note = ""
    if not args.no_llm and ownership["available"] and ownership["authors"]:
        prose, warnings = llm.narrate(
            env, {**facts, "owners": ownership}, ["ownership"], timeout=args.llm_timeout
        )
        note = prose.get("ownership", "")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
    _emit(render.render_owners(ownership, note))
    return 0


def cmd_explain(args: argparse.Namespace, env: Env) -> int:
    cfg = _config(args, env.root)
    facts = _cached_or_collected(env, cfg)
    target = _relative_target(env, args.path)
    payload = collect.explain_facts(
        collect.files_from_facts(facts),
        collect.coupling_from_facts(facts),
        collect.history_from_facts(facts),
        collect.ratios_from_facts(facts),
        target,
    )

    narrative: dict[str, str] = {}
    if not args.no_llm:
        excerpts = {}
        if payload["kind"] == "file":
            excerpts = {"explain": {payload["path"]: llm.excerpt(env, payload["path"])}}
        narrative, warnings = llm.narrate(
            env, {"explain": payload}, ["explain"], timeout=args.llm_timeout, excerpts=excerpts
        )
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
    _emit(render.render_explain(payload, narrative))
    return 0


def cmd_doctor(args: argparse.Namespace, env: Env) -> int:
    rows: list[tuple[str, str, str]] = [
        ("python", platform.python_version(), "ok")
    ]
    for tool, argv in (("git", ["git", "--version"]), ("claude", ["claude", "--version"])):
        if env.which(tool) is None:
            rows.append((tool, "-", "missing"))
            continue
        done = env.run(argv, timeout=10.0)
        version = done.stdout.strip().split("\n")[0] if done.ok else "-"
        rows.append((tool, version.replace(f"{tool} version ", ""), "ok" if done.ok else "error"))

    width = max(len(name) for name, _, _ in rows) + 2
    value_width = max(len(value) for _, value, _ in rows) + 2
    out = [f"{name:<{width}}{value:<{value_width}}{status}" for name, value, status in rows]

    head = env.git(["rev-parse", "HEAD"]) if env.which("git") else None
    if head is not None and head.ok:
        tracked = env.git(["ls-files"])
        count = len([ln for ln in tracked.stdout.splitlines() if ln]) if tracked.ok else 0
        out.append(f"{'repo':<{width}}{env.root}   git, {count:,} tracked files")
    else:
        out.append(f"{'repo':<{width}}{env.root}   not a git repository")

    cache = env.root / collect.CACHE_PATH
    if cache.exists():
        age = env.now() - _dt.datetime.fromtimestamp(cache.stat().st_mtime, _dt.UTC)
        hours = int(age.total_seconds() // 3600)
        out.append(f"{'cache':<{width}}{collect.CACHE_PATH}  {hours} hours old")
    else:
        out.append(f"{'cache':<{width}}{collect.CACHE_PATH}  absent — run 'orient scan'")
    _emit("\n".join(out) + "\n")
    return 0


COMMANDS = {
    "scan": cmd_scan,
    "report": cmd_report,
    "hotspots": cmd_hotspots,
    "owners": cmd_owners,
    "explain": cmd_explain,
    "doctor": cmd_doctor,
}


def main(argv: Sequence[str] | None = None, env: Env | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exit_request:
        return int(exit_request.code or 0)

    try:
        if env is None:
            raw = args.path if args.command == "scan" else "."
            root = Path(raw).expanduser()
            if not root.is_dir() or not os.access(root, os.R_OK):
                raise OrientError(f"cannot read {root}", 3)
            env = Env(root.resolve())
        return COMMANDS[args.command](args, env)
    except OrientError as err:
        print(f"orient: {err.message}", file=sys.stderr)
        return err.exit_code
    except KeyboardInterrupt:
        print("orient: interrupted", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the top-level guard, by design
        if os.environ.get("ORIENT_DEBUG"):
            raise
        print(f"orient: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
