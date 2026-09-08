"""Every fact orient can compute without a model.

The entry point is `collect()`, which walks the tree, reads each file once,
builds the import graph, makes a single pass over `git log`, and turns all of
that into the JSON-shaped `facts` dict described by schema/facts.schema.json.
"""

from __future__ import annotations

import ast
import datetime as _dt
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from orient.env import Env, OrientError

Facts: TypeAlias = dict[str, Any]

SCHEMA_VERSION = 1
CACHE_PATH = ".orient/facts.json"

LANGS: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rb": "ruby", ".java": "java", ".rs": "rust", ".php": "php",
    ".cs": "csharp", ".kt": "kotlin", ".swift": "swift", ".scala": "scala",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".sh": "shell", ".bash": "shell", ".sql": "sql", ".ex": "elixir", ".exs": "elixir",
}

_JS_IMPORTS = [
    r"""import\s+[^'"\n]*from\s*['"]([^'"]+)['"]""",
    r"""import\s*['"]([^'"]+)['"]""",
    r"""require\(\s*['"]([^'"]+)['"]\s*\)""",
]

IMPORT_RES: dict[str, list[str]] = {
    "python": [r"(?m)^\s*import\s+([\w.]+)", r"(?m)^\s*from\s+([\w.]+)\s+import"],
    "javascript": _JS_IMPORTS,
    "typescript": _JS_IMPORTS,
    "go": [r"""(?m)^\s*(?:import\s+)?(?:[\w.]+\s+)?"([\w./-]+)"\s*$"""],
    "ruby": [r"""(?m)^\s*require(?:_relative)?\s+['"]([^'"]+)['"]"""],
    "java": [r"(?m)^\s*import\s+(?:static\s+)?([\w.]+)\s*;"],
    "kotlin": [r"(?m)^\s*import\s+([\w.]+)"],
    "rust": [r"(?m)^\s*use\s+([\w:]+)"],
    "php": [r"(?m)^\s*use\s+([\w\\]+)\s*;"],
    "csharp": [r"(?m)^\s*using\s+([\w.]+)\s*;"],
    "scala": [r"(?m)^\s*import\s+([\w.]+)"],
    "elixir": [r"(?m)^\s*(?:import|alias|use)\s+([\w.]+)"],
    "c": [r"""(?m)^\s*#include\s*[<"]([^>"]+)[>"]"""],
    "cpp": [r"""(?m)^\s*#include\s*[<"]([^>"]+)[>"]"""],
    "shell": [r"(?m)^\s*(?:source|\.)\s+([\w./-]+)"],
    "swift": [r"(?m)^\s*import\s+(\w+)"],
}

DEF_RES: dict[str, tuple[str, str]] = {
    # language -> (class-ish regex, function-ish regex)
    "javascript": (
        r"(?m)^\s*(?:export\s+)?class\s+(\w+)",
        r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)",
    ),
    "typescript": (
        r"(?m)^\s*(?:export\s+)?(?:class|interface)\s+(\w+)",
        r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)",
    ),
    "go": (r"(?m)^\s*type\s+(\w+)\s+struct", r"(?m)^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"),
    "ruby": (r"(?m)^\s*class\s+(\w+)", r"(?m)^\s*def\s+(\w+)"),
    "java": (
        r"(?m)^\s*(?:public\s+|final\s+|abstract\s+)*class\s+(\w+)",
        r"(?m)^\s*(?:public|private|protected)\s+[\w<>\[\], ]+\s+(\w+)\s*\(",
    ),
    "rust": (r"(?m)^\s*(?:pub\s+)?struct\s+(\w+)", r"(?m)^\s*(?:pub\s+)?fn\s+(\w+)"),
    "php": (r"(?m)^\s*class\s+(\w+)", r"(?m)^\s*function\s+(\w+)"),
    "csharp": (
        r"(?m)^\s*(?:public|internal)\s+class\s+(\w+)",
        r"(?m)^\s*(?:public|private|protected)\s+[\w<>\[\], ]+\s+(\w+)\s*\(",
    ),
    "kotlin": (r"(?m)^\s*class\s+(\w+)", r"(?m)^\s*fun\s+(\w+)"),
    "swift": (r"(?m)^\s*(?:class|struct)\s+(\w+)", r"(?m)^\s*func\s+(\w+)"),
    "scala": (r"(?m)^\s*(?:class|object|trait)\s+(\w+)", r"(?m)^\s*def\s+(\w+)"),
    "elixir": (r"(?m)^\s*defmodule\s+([\w.]+)", r"(?m)^\s*def\s+(\w+)"),
}

SECRET_RES = [
    r"""(?i)\b(?:api[_-]?key|secret|passwd|password|token|access[_-]?key|private[_-]?key)\b\s*[:=]\s*['"][^'"\n]{8,}['"]""",
    r"""(?i)\b(?:api[_-]?key|secret|password|token)\b\s*=\s*[^\s'"#]{16,}""",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]

ROUTE_RES = [
    r"@\w+\.route\(\s*['\"]([^'\"]+)",
    r"@\w+\.(?:get|post|put|patch|delete)\(\s*['\"]([^'\"]+)",
    r"(?m)^\s*(?:app|router)\.(?:get|post|put|patch|delete)\(\s*['\"]([^'\"]+)",
    r"(?m)^\s*path\(\s*['\"]([^'\"]*)['\"]",
]

TEST_DIR_PARTS = {"test", "tests", "spec", "specs", "__tests__", "testing"}
SOURCE_ROOTS = ("src", "lib", "source", "app")
_BLOCK_NODES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For,
    ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match,
)
_DURATION_RE = re.compile(r"^\s*(\d+)\s*(d|w|mo|m|y)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Config:
    root: Path
    since: str = "12mo"
    top: int = 10
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_files: int = 20_000
    use_cache: bool = True


@dataclass(frozen=True)
class FileRec:
    path: str
    lang: str
    lines: int
    bytes: int
    is_test: bool
    module: str | None
    imports: tuple[str, ...]
    defs: tuple[str, ...]
    classes: int
    functions: int
    max_nesting: int
    parsed: bool
    secret_hits: int = 0


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def parse_duration(text: str) -> int:
    """'18mo' -> 547, '12mo' -> 365, '90d' -> 90, '2y' -> 730."""
    match = _DURATION_RE.match(text or "")
    if not match:
        raise OrientError(f"bad value for --since: {text}", 2)
    count, unit = int(match.group(1)), match.group(2).lower()
    if unit == "d":
        return count
    if unit == "w":
        return count * 7
    if unit == "y":
        return count * 365
    return int(count * 365 / 12)


def _compile_glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches_any(patterns: Sequence[re.Pattern[str]], path: str) -> bool:
    return any(p.match(path) for p in patterns)


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if any(part.lower() in TEST_DIR_PARTS for part in parts[:-1]):
        return True
    stem = name.rsplit(".", 1)[0].lower()
    return (
        stem.startswith("test_")
        or stem.startswith("test")
        and stem[4:5] in ("", "s", "_")
        or stem.endswith("_test")
        or stem.endswith(".test")
        or stem.endswith(".spec")
        or stem.endswith("_spec")
    )


def _module_name(path: str) -> str:
    parts = path[:-3].split("/") if path.endswith(".py") else path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _regex_names(text: str, pattern: str) -> list[str]:
    return re.findall(pattern, text)


# --------------------------------------------------------------------------------------
# per-file analysis
# --------------------------------------------------------------------------------------


def regex_facts(text: str, lang: str) -> dict[str, Any]:
    """Tier-2 analysis: imports and entry markers from a per-language regex list."""
    imports: list[str] = []
    for pattern in IMPORT_RES.get(lang, []):
        for hit in re.findall(pattern, text):
            if hit and hit not in imports:
                imports.append(hit)
    class_re, func_re = DEF_RES.get(lang, ("", ""))
    classes = _regex_names(text, class_re) if class_re else []
    funcs = _regex_names(text, func_re) if func_re else []
    defs = [n for n in classes + funcs if not n.startswith("_")]
    nesting = 0
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if not stripped:
            continue
        indent = line[: len(line) - len(stripped)]
        nesting = max(nesting, indent.count("\t") + (len(indent) - indent.count("\t")) // 4)
    return {
        "imports": tuple(imports),
        "defs": tuple(dict.fromkeys(defs)),
        "classes": len(classes),
        "functions": len(funcs),
        "max_nesting": nesting,
        "parsed": False,
    }


def python_facts(source: str) -> dict[str, Any]:
    """ast-based: imports, defs, classes, functions, max_nesting, parsed."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return regex_facts(source, "python")

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            imports.append(base)
            for alias in node.names:
                joiner = "" if base.endswith(".") else "."
                imports.append(f"{base}{joiner}{alias.name}")

    classes = 0
    functions = 0
    defs: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions += 1
        else:
            continue
        if not node.name.startswith("_"):
            defs.append(node.name)

    max_nesting = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_nesting:
            max_nesting = depth
        for child in ast.iter_child_nodes(node):
            stack.append((child, depth + 1 if isinstance(child, _BLOCK_NODES) else depth))

    return {
        "imports": tuple(dict.fromkeys(i for i in imports if i)),
        "defs": tuple(defs),
        "classes": classes,
        "functions": functions,
        "max_nesting": max_nesting,
        "parsed": True,
    }


def _secret_hits(text: str) -> int:
    return sum(len(re.findall(pattern, text)) for pattern in SECRET_RES)


def scan_files(env: Env, cfg: Config) -> tuple[list[FileRec], bool]:
    """Walk, filter by include/exclude globs, classify language, analyze each file."""
    paths, truncated = env.walk(max_files=cfg.max_files)
    includes = [_compile_glob(p) for p in cfg.include]
    excludes = [_compile_glob(p) for p in cfg.exclude]

    records: list[FileRec] = []
    for path in paths:
        if includes and not _matches_any(includes, path):
            continue
        if excludes and _matches_any(excludes, path):
            continue
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        lang = LANGS.get(suffix, "other")
        text = env.read_text(path)
        if lang == "python":
            analysis = python_facts(text)
        else:
            analysis = regex_facts(text, lang)
        records.append(
            FileRec(
                path=path,
                lang=lang,
                lines=_count_lines(text),
                bytes=env.size(path),
                is_test=_is_test_path(path),
                module=_module_name(path) if suffix in (".py", ".pyi") else None,
                imports=analysis["imports"],
                defs=analysis["defs"],
                classes=analysis["classes"],
                functions=analysis["functions"],
                max_nesting=analysis["max_nesting"],
                parsed=analysis["parsed"],
                secret_hits=_secret_hits(text),
            )
        )
    return records, truncated


# --------------------------------------------------------------------------------------
# coupling
# --------------------------------------------------------------------------------------


def _module_index(files: Sequence[FileRec]) -> dict[str, str]:
    index: dict[str, str] = {}
    pythons = sorted(
        (f for f in files if f.lang == "python" and f.module), key=lambda f: f.path
    )
    for rec in pythons:
        index.setdefault(rec.module or "", rec.path)
    for rec in pythons:
        parts = (rec.module or "").split(".")
        for i in range(1, len(parts)):
            if parts[i - 1] in SOURCE_ROOTS or i == 1:
                index.setdefault(".".join(parts[i:]), rec.path)
    return index


def _resolve(target: str, importer: FileRec, index: dict[str, str]) -> str | None:
    if target.startswith("."):
        level = len(target) - len(target.lstrip("."))
        rest = target.lstrip(".")
        base = (importer.module or "").split(".")[:-level]
        candidate = ".".join([*base, rest] if rest else base)
    else:
        candidate = target
    while candidate:
        hit = index.get(candidate)
        if hit is not None:
            return hit
        if "." not in candidate:
            return None
        candidate = candidate.rsplit(".", 1)[0]
    return None


def couple(files: Sequence[FileRec]) -> dict[str, dict[str, Any]]:
    """path -> fan-in/fan-out plus the resolved internal, external and stdlib targets."""
    index = _module_index(files)
    out: dict[str, dict[str, Any]] = {
        rec.path: {
            "fan_in": 0, "fan_out": 0, "importers": [], "internal": [],
            "external": [], "stdlib": [],
        }
        for rec in files
    }
    importers: dict[str, set[str]] = {rec.path: set() for rec in files}

    for rec in sorted(files, key=lambda f: f.path):
        internal: set[str] = set()
        external: set[str] = set()
        stdlib: set[str] = set()
        for target in rec.imports:
            root = target.lstrip(".").split(".")[0]
            resolved = _resolve(target, rec, index) if rec.lang == "python" else None
            if resolved and resolved != rec.path:
                internal.add(resolved)
                importers[resolved].add(rec.path)
            elif rec.lang == "python" and root in sys.stdlib_module_names:
                stdlib.add(root)
            elif root and not resolved:
                external.add(root)
        entry = out[rec.path]
        entry["internal"] = sorted(internal)
        entry["external"] = sorted(external)
        entry["stdlib"] = sorted(stdlib)
        entry["fan_out"] = len(internal)

    for path, who in importers.items():
        out[path]["importers"] = sorted(who)
        out[path]["fan_in"] = len(who)
    return out


# --------------------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------------------

_LOG_FORMAT = "--format=%x1e%H%x1f%an%x1f%aI"
_RENAME_BRACE = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")


def _rename_target(path: str) -> str:
    match = _RENAME_BRACE.match(path)
    if match:
        prefix, _old, new, suffix = match.groups()
        return re.sub(r"//+", "/", f"{prefix}{new}{suffix}")
    if " => " in path:
        return path.split(" => ", 1)[1]
    return path


def history(env: Env, cfg: Config) -> dict[str, Any]:
    """One `git log --numstat` pass over the since-window, parsed per file."""
    days = parse_duration(cfg.since)
    blank: dict[str, Any] = {
        "available": False, "reason": None, "head": None, "latest": None,
        "commits": 0, "authors": [], "window_days": days, "files": {},
    }

    head = env.git(["rev-parse", "HEAD"])
    if not head.ok:
        if head.code == 127:
            blank["reason"] = "git not found"
        else:
            blank["reason"] = "not a git repository or no commits yet"
        return blank

    log = env.git(["log", f"--since={days} days ago", "--numstat", "-M", _LOG_FORMAT])
    if not log.ok:
        blank["reason"] = f"git log exit {log.code}"
        blank["head"] = head.stdout.strip() or None
        return blank

    per_file: dict[str, dict[str, Any]] = {}
    authors: dict[str, dict[str, Any]] = {}
    commits = 0
    latest: str | None = None

    for chunk in log.stdout.split("\x1e"):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        header = lines[0].split("\x1f")
        if len(header) != 3:
            continue
        _sha, author, when = header
        commits += 1
        who = authors.setdefault(author, {"name": author, "commits": 0, "last": when})
        who["commits"] += 1
        if when > who["last"]:
            who["last"] = when
        if latest is None or when > latest:
            latest = when
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, raw_path = parts
            path = _rename_target(raw_path)
            rec = per_file.setdefault(
                path, {"commits": 0, "authors": {}, "last": when, "churn": 0}
            )
            rec["commits"] += 1
            rec["authors"][author] = rec["authors"].get(author, 0) + 1
            if when > rec["last"]:
                rec["last"] = when
            rec["churn"] += (int(added) if added.isdigit() else 0) + (
                int(deleted) if deleted.isdigit() else 0
            )

    return {
        "available": True,
        "reason": None,
        "head": head.stdout.strip() or None,
        "latest": latest,
        "commits": commits,
        "authors": sorted(authors.values(), key=lambda a: (-a["commits"], a["name"])),
        "window_days": days,
        "files": per_file,
    }


# --------------------------------------------------------------------------------------
# derived facts
# --------------------------------------------------------------------------------------


def test_coverage(files: Sequence[FileRec]) -> dict[str, float]:
    """path -> the share of a file's public defs named by at least one test file."""
    haystack = "\n".join(
        " ".join([*rec.defs, *rec.imports, rec.path])
        for rec in files
        if rec.is_test
    )
    ratios: dict[str, float] = {}
    for rec in files:
        if rec.is_test or not rec.defs:
            ratios[rec.path] = 0.0
            continue
        hits = sum(1 for name in rec.defs if name in haystack)
        ratios[rec.path] = round(hits / len(rec.defs), 4)
    return ratios


def _console_scripts(env: Env, path: str) -> list[dict[str, Any]]:
    """The `[project.scripts]` table of one pyproject.toml, if it has one."""
    try:
        data = tomllib.loads(env.read_text(path))
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    project = data.get("project", {}) if isinstance(data, dict) else {}
    scripts: dict[str, Any] = {}
    for key in ("scripts", "gui-scripts"):
        value = project.get(key)
        if isinstance(value, dict):
            scripts.update(value)
    if not scripts:
        return []
    detail = ", ".join(f"{name} = {target}" for name, target in sorted(scripts.items()))
    return [
        {"path": path, "kind": "console_script", "detail": detail[:200], "confidence": 0.95}
    ]


def find_entry_points(env: Env, files: Sequence[FileRec]) -> list[dict[str, Any]]:
    """Everything that looks like a way into the program, most confident first."""
    found: list[dict[str, Any]] = []

    for rec in files:
        name = rec.path.rsplit("/", 1)[-1]
        lower = name.lower()

        if lower == "pyproject.toml":
            found.extend(_console_scripts(env, rec.path))
            continue

        if lower.startswith("dockerfile"):
            text = env.read_text(rec.path)
            cmds = [
                line.strip()
                for line in text.splitlines()
                if line.strip().upper().startswith(("CMD ", "ENTRYPOINT "))
            ]
            if cmds:
                found.append(
                    {
                        "path": rec.path,
                        "kind": "docker_cmd",
                        "detail": " | ".join(cmds)[:200],
                        "confidence": 0.9,
                    }
                )
            continue

        if lower in ("makefile", "gnumakefile"):
            text = env.read_text(rec.path)
            targets: list[str] = []
            for line in text.splitlines():
                if line.startswith(".PHONY:"):
                    targets.extend(line.split(":", 1)[1].split())
            if targets:
                found.append(
                    {
                        "path": rec.path,
                        "kind": "make_target",
                        "detail": "make " + ", ".join(dict.fromkeys(targets))[:190],
                        "confidence": 0.6,
                    }
                )
            continue

        if rec.path.startswith(".github/workflows/") and lower.endswith((".yml", ".yaml")):
            text = env.read_text(rec.path)
            runs = [
                line.split("run:", 1)[1].strip()
                for line in text.splitlines()
                if line.strip().startswith("run:")
            ]
            found.append(
                {
                    "path": rec.path,
                    "kind": "ci_step",
                    "detail": (runs[0] if runs else "workflow")[:200],
                    "confidence": 0.5,
                }
            )
            continue

        if rec.lang != "python":
            continue

        text = env.read_text(rec.path)
        if name == "manage.py" and "DJANGO_SETTINGS_MODULE" in text:
            found.append(
                {
                    "path": rec.path,
                    "kind": "django_manage",
                    "detail": "django management command entry point",
                    "confidence": 0.95,
                }
            )
        if name in ("wsgi.py", "asgi.py"):
            found.append(
                {
                    "path": rec.path,
                    "kind": "wsgi_asgi",
                    "detail": f"{name.split('.')[0]} application object",
                    "confidence": 0.85,
                }
            )
        routes: list[str] = []
        for pattern in ROUTE_RES:
            routes.extend(re.findall(pattern, text))
        routes = [r for r in dict.fromkeys(routes) if r]
        if routes:
            found.append(
                {
                    "path": rec.path,
                    "kind": "web_route",
                    "detail": f"{len(routes)} route(s), first {routes[0]}"[:200],
                    "confidence": 0.8,
                }
            )
        if re.search(r"(?m)^\s*if\s+__name__\s*==", text):
            found.append(
                {
                    "path": rec.path,
                    "kind": "main_guard",
                    "detail": "runnable as a script",
                    "confidence": 0.7,
                }
            )

    found.sort(key=lambda e: (-e["confidence"], e["path"], e["kind"]))
    return found


def _directory(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


def module_map(
    files: Sequence[FileRec], coupling: dict[str, Any], hist: dict[str, Any]
) -> list[dict[str, Any]]:
    """One row per directory that holds source."""
    hfiles = hist.get("files", {})
    groups: dict[str, list[FileRec]] = {}
    for rec in files:
        groups.setdefault(_directory(rec.path), []).append(rec)

    rows: list[dict[str, Any]] = []
    for directory, members in sorted(groups.items()):
        langs = sorted({rec.lang for rec in members if rec.lang != "other"})
        if not langs:
            continue
        paths = {rec.path for rec in members}
        fan_in: set[str] = set()
        fan_out: set[str] = set()
        commits = 0
        authors: dict[str, int] = {}
        for rec in members:
            entry = coupling[rec.path]
            fan_in.update(p for p in entry["importers"] if p not in paths)
            fan_out.update(p for p in entry["internal"] if p not in paths)
            info = hfiles.get(rec.path)
            if info:
                commits += info["commits"]
                for name, count in info["authors"].items():
                    authors[name] = authors.get(name, 0) + count
        top = sorted(authors.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        rows.append(
            {
                "path": directory,
                "files": len(members),
                "lines": sum(rec.lines for rec in members),
                "languages": langs,
                "fan_in": len(fan_in),
                "fan_out": len(fan_out),
                "commits": commits,
                "authors": [name for name, _ in top],
                "has_tests": any(rec.is_test for rec in members),
            }
        )
    rows.sort(key=lambda r: (-r["lines"], r["path"]))
    return rows


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.values())
    total = len(ordered)
    if total < 2:
        return {key: 0.0 for key in values}
    ranks: dict[str, float] = {}
    for key, value in values.items():
        below = sum(1 for other in ordered if other < value)
        ranks[key] = below / (total - 1)
    return ranks


def score_hotspots(
    files: Sequence[FileRec],
    coupling: dict[str, Any],
    hist: dict[str, Any],
    ratios: dict[str, float],
    top: int,
) -> list[dict[str, Any]]:
    """Percentile-normalized blend of churn, fan-in, size, nesting and test thinness."""
    hfiles = hist.get("files", {})
    candidates = sorted(
        (rec for rec in files if not rec.is_test and rec.lang != "other" and rec.lines > 0),
        key=lambda rec: rec.path,
    )
    if not candidates:
        return []

    churn = {rec.path: float(hfiles.get(rec.path, {}).get("commits", 0)) for rec in candidates}
    fan_in = {rec.path: float(coupling[rec.path]["fan_in"]) for rec in candidates}
    lines = {rec.path: float(rec.lines) for rec in candidates}
    nesting = {rec.path: float(rec.max_nesting) for rec in candidates}
    p_churn, p_fan, p_lines, p_nest = (
        _percentiles(churn), _percentiles(fan_in), _percentiles(lines), _percentiles(nesting)
    )

    rows: list[dict[str, Any]] = []
    for rec in candidates:
        path = rec.path
        ratio = ratios.get(path, 0.0)
        blend = (
            0.45 * p_churn[path]
            + 0.20 * p_fan[path]
            + 0.15 * p_lines[path]
            + 0.10 * p_nest[path]
            + 0.10 * (1.0 - ratio)
        )
        reasons: list[str] = []
        if churn[path] and p_churn[path] >= 0.8:
            reasons.append("high_churn")
        if fan_in[path] and p_fan[path] >= 0.8:
            reasons.append("high_fan_in")
        if p_lines[path] >= 0.8:
            reasons.append("large_file")
        if rec.max_nesting >= 5:
            reasons.append("deep_nesting")
        if ratio == 0.0:
            reasons.append("no_tests")
        elif ratio < 0.25:
            reasons.append("thin_tests")
        info = hfiles.get(path, {})
        rows.append(
            {
                "path": path,
                "score": int(round(blend * 100)),
                "commits": int(churn[path]),
                "authors": len(info.get("authors", {})),
                "lines": rec.lines,
                "max_nesting": rec.max_nesting,
                "fan_in": int(fan_in[path]),
                "fan_out": coupling[path]["fan_out"],
                "test_ratio": ratio,
                "reasons": reasons,
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["path"]))
    return rows[: max(top, 0)]


def _months_between(later: str | None, earlier: str | None) -> int:
    if not later or not earlier:
        return 0
    try:
        end = _dt.datetime.fromisoformat(later)
        start = _dt.datetime.fromisoformat(earlier)
    except ValueError:
        return 0
    return max((end - start).days, 0) // 30


def risk_flags(
    files: Sequence[FileRec],
    coupling: dict[str, Any],
    hist: dict[str, Any],
    ratios: dict[str, float],
) -> list[dict[str, Any]]:
    """The four orientation warnings plus the config surface, path and counts only."""
    hfiles = hist.get("files", {})
    latest = hist.get("latest")
    flags: list[dict[str, Any]] = []

    for rec in sorted(files, key=lambda f: f.path):
        info = hfiles.get(rec.path)
        commits = info["commits"] if info else 0

        if info and commits >= 5:
            name, count = max(info["authors"].items(), key=lambda kv: (kv[1], kv[0]))
            share = count / commits
            author_last = max(
                (when for when in [info["last"]] if when), default=None
            )
            idle = _months_between(latest, author_last)
            if share >= 0.7 and idle >= 6:
                flags.append(
                    {
                        "flag": "bus_factor_1",
                        "path": rec.path,
                        "detail": (
                            f"{share:.0%} by {name} · last commit {author_last[:10]}"
                            f" · {idle} months idle"
                        ),
                    }
                )

        if (
            rec.lines >= 800
            and not rec.is_test
            and (coupling[rec.path]["fan_in"] >= 10 or rec.classes + rec.functions >= 30)
        ):
            flags.append(
                {
                    "flag": "god_file",
                    "path": rec.path,
                    "detail": (
                        f"{rec.lines} lines, {coupling[rec.path]['fan_in']} importers, "
                        f"{rec.classes + rec.functions} top-level definitions"
                    ),
                }
            )

        if (
            hist.get("available")
            and commits == 0
            and coupling[rec.path]["fan_in"] == 0
            and not rec.is_test
            and rec.lang != "other"
            and rec.lines >= 20
        ):
            flags.append(
                {
                    "flag": "dead_ish",
                    "path": rec.path,
                    "detail": (
                        f"0 commits in {hist.get('window_days', 0)}d, 0 importers, "
                        f"{rec.lines} lines"
                    ),
                }
            )

        if rec.secret_hits:
            flags.append(
                {
                    "flag": "config_surface",
                    "path": rec.path,
                    "detail": (
                        f"{rec.secret_hits} credential-shaped "
                        f"string{'s' if rec.secret_hits != 1 else ''} "
                        "(values are never read or printed)"
                    ),
                }
            )

    by_dir: dict[str, list[FileRec]] = {}
    for rec in files:
        if rec.lang != "other":
            by_dir.setdefault(_directory(rec.path), []).append(rec)
    for directory, members in sorted(by_dir.items()):
        sources = [rec for rec in members if not rec.is_test]
        if len(sources) < 2 or len(sources) != len(members):
            continue
        if any(ratios.get(rec.path, 0.0) > 0 for rec in sources):
            continue
        flags.append(
            {
                "flag": "no_tests",
                "path": directory + "/",
                "detail": (
                    f"{len(sources)} files, {sum(r.lines for r in sources)} lines, "
                    "no test file references them"
                ),
            }
        )

    flags.sort(key=lambda f: (f["flag"], f["path"]))
    return flags


def owners(hist: dict[str, Any], prefix: str, *, now: _dt.datetime) -> dict[str, Any]:
    """Authorship for one path prefix."""
    clean = (prefix or "").strip("/")
    hfiles = hist.get("files", {})
    selected = {
        path: info
        for path, info in hfiles.items()
        if not clean or path == clean or path.startswith(clean + "/")
    }
    totals: dict[str, int] = {}
    last_seen: dict[str, str] = {}
    commits = 0
    for info in selected.values():
        commits += info["commits"]
        for name, count in info["authors"].items():
            totals[name] = totals.get(name, 0) + count
            if name not in last_seen or info["last"] > last_seen[name]:
                last_seen[name] = info["last"]

    rows: list[dict[str, Any]] = []
    for name, count in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
        last = last_seen[name]
        rows.append(
            {
                "name": name,
                "commits": count,
                "share": round(count / commits, 4) if commits else 0.0,
                "last": last,
                "inactive_months": _months_between(now.isoformat(), last),
            }
        )
    return {
        "path": clean or ".",
        "available": bool(hist.get("available")),
        "files": len(selected),
        "commits": commits,
        "authors": rows,
    }


def explain_facts(
    files: Sequence[FileRec],
    coupling: dict[str, Any],
    hist: dict[str, Any],
    ratios: dict[str, float],
    target: str,
) -> dict[str, Any]:
    """The SHAPE / WHO CALLS IT / WHAT IT DEPENDS ON payload for one file or directory."""
    clean = target.strip("/")
    by_path = {rec.path: rec for rec in files}
    hfiles = hist.get("files", {})

    if clean in by_path:
        members = [by_path[clean]]
        kind = "file"
    else:
        members = [rec for rec in files if rec.path.startswith(clean + "/")]
        kind = "directory"
    if not members:
        raise OrientError(f"nothing at {target}", 3)

    member_paths = {rec.path for rec in members}
    callers: dict[str, set[str]] = {}
    for rec in files:
        if rec.path in member_paths:
            continue
        hit = set(coupling[rec.path]["internal"]) & member_paths
        if hit:
            names = {
                imp.rsplit(".", 1)[-1]
                for imp in rec.imports
                if imp.rsplit(".", 1)[-1]
                in {name for m in members if m.path in hit for name in m.defs}
            }
            callers[rec.path] = names

    stdlib: set[str] = set()
    external: set[str] = set()
    internal: set[str] = set()
    commits = 0
    authors: dict[str, int] = {}
    last: str | None = None
    for rec in members:
        entry = coupling[rec.path]
        stdlib.update(entry["stdlib"])
        external.update(entry["external"])
        internal.update(p for p in entry["internal"] if p not in member_paths)
        info = hfiles.get(rec.path)
        if info:
            commits += info["commits"]
            for name, count in info["authors"].items():
                authors[name] = authors.get(name, 0) + count
            if last is None or info["last"] > last:
                last = info["last"]

    return {
        "path": clean,
        "kind": kind,
        "lines": sum(rec.lines for rec in members),
        "files": len(members),
        "commits": commits,
        "last": last,
        "window_days": hist.get("window_days", 0),
        "history_available": bool(hist.get("available")),
        "classes": sum(rec.classes for rec in members),
        "functions": sum(rec.functions for rec in members),
        "max_nesting": max(rec.max_nesting for rec in members),
        "defs": sorted({name for rec in members for name in rec.defs}),
        "test_ratio": round(
            sum(ratios.get(rec.path, 0.0) for rec in members) / len(members), 4
        ),
        "callers": [
            {"path": path, "names": sorted(names)} for path, names in sorted(callers.items())
        ],
        "depends": {
            "stdlib": sorted(stdlib),
            "internal": sorted(internal),
            "external": sorted(external),
        },
        "authors": sorted(
            ({"name": n, "commits": c} for n, c in authors.items()),
            key=lambda a: (-a["commits"], a["name"]),
        ),
    }


# --------------------------------------------------------------------------------------
# assembly and cache
# --------------------------------------------------------------------------------------


def collect(env: Env, cfg: Config) -> Facts:
    """Run every deterministic analysis and assemble the schema-shaped facts dict."""
    files, truncated = scan_files(env, cfg)
    coupling = couple(files)
    hist = history(env, cfg)
    ratios = test_coverage(files)
    entry_points = find_entry_points(env, files)
    modules = module_map(files, coupling, hist)
    hotspots = score_hotspots(files, coupling, hist, ratios, cfg.top)
    flags = risk_flags(files, coupling, hist, ratios)
    ownership = owners(hist, "", now=env.now())
    hfiles = hist.get("files", {})

    warnings: list[str] = []
    if not hist["available"]:
        warnings.append(
            f"git history unavailable ({hist['reason']}) — hotspots and ownership are size-only"
        )
    if truncated:
        warnings.append(f"stopped at {cfg.max_files} files — results are partial")

    languages: dict[str, int] = {}
    for rec in files:
        languages[rec.lang] = languages.get(rec.lang, 0) + 1

    file_rows: list[dict[str, Any]] = []
    for rec in sorted(files, key=lambda f: f.path):
        info = hfiles.get(rec.path, {})
        entry = coupling[rec.path]
        file_rows.append(
            {
                "path": rec.path,
                "lang": rec.lang,
                "lines": rec.lines,
                "bytes": rec.bytes,
                "is_test": rec.is_test,
                "module": rec.module,
                "imports": list(rec.imports),
                "defs": list(rec.defs),
                "classes": rec.classes,
                "functions": rec.functions,
                "max_nesting": rec.max_nesting,
                "parsed": rec.parsed,
                "secret_hits": rec.secret_hits,
                "fan_in": entry["fan_in"],
                "fan_out": entry["fan_out"],
                "importers": entry["importers"],
                "internal": entry["internal"],
                "external": entry["external"],
                "stdlib": entry["stdlib"],
                "commits": info.get("commits", 0),
                "authors": info.get("authors", {}),
                "last": info.get("last"),
                "churn": info.get("churn", 0),
                "test_ratio": ratios.get(rec.path, 0.0),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": env.now().isoformat(),
        "root": str(env.root),
        "config": {
            "since": cfg.since,
            "window_days": hist["window_days"],
            "top": cfg.top,
            "include": list(cfg.include),
            "exclude": list(cfg.exclude),
            "max_files": cfg.max_files,
        },
        "snapshot": {
            "files_tracked": len(files),
            "files_analyzed": sum(1 for rec in files if rec.lang == "python"),
            "files_other": sum(1 for rec in files if rec.lang != "python"),
            "test_files": sum(1 for rec in files if rec.is_test),
            "lines": sum(rec.lines for rec in files),
            "bytes": sum(rec.bytes for rec in files),
            "unreadable": sum(1 for rec in files if rec.bytes > 0 and rec.lines == 0),
            "unparsed": sum(1 for rec in files if rec.lang == "python" and not rec.parsed),
            "truncated": truncated,
            "languages": languages,
        },
        "files": file_rows,
        "entry_points": entry_points,
        "modules": modules,
        "hotspots": hotspots,
        "flags": flags,
        "owners": ownership,
        "history": {
            "available": hist["available"],
            "reason": hist["reason"],
            "head": hist["head"],
            "latest": hist["latest"],
            "commits": hist["commits"],
            "authors": hist["authors"],
            "window_days": hist["window_days"],
        },
        "warnings": warnings,
    }


def files_from_facts(facts: Facts) -> list[FileRec]:
    """Rebuild the FileRec list a cached scan was computed from."""
    return [
        FileRec(
            path=row["path"],
            lang=row["lang"],
            lines=row["lines"],
            bytes=row["bytes"],
            is_test=row["is_test"],
            module=row.get("module"),
            imports=tuple(row.get("imports", ())),
            defs=tuple(row.get("defs", ())),
            classes=row.get("classes", 0),
            functions=row.get("functions", 0),
            max_nesting=row.get("max_nesting", 0),
            parsed=row.get("parsed", False),
            secret_hits=row.get("secret_hits", 0),
        )
        for row in facts["files"]
    ]


def history_from_facts(facts: Facts) -> dict[str, Any]:
    """Rebuild the history structure, including its per-file rows, from a cached scan."""
    hist = dict(facts["history"])
    hist["files"] = {
        row["path"]: {
            "commits": row["commits"],
            "authors": row["authors"],
            "last": row["last"],
            "churn": row["churn"],
        }
        for row in facts["files"]
        if row["commits"]
    }
    return hist


def coupling_from_facts(facts: Facts) -> dict[str, dict[str, Any]]:
    """Rebuild the coupling structure from a cached scan."""
    return {
        row["path"]: {
            "fan_in": row["fan_in"],
            "fan_out": row["fan_out"],
            "importers": row["importers"],
            "internal": row["internal"],
            "external": row["external"],
            "stdlib": row["stdlib"],
        }
        for row in facts["files"]
    }


def ratios_from_facts(facts: Facts) -> dict[str, float]:
    return {row["path"]: row["test_ratio"] for row in facts["files"]}


def load_cache(env: Env) -> Facts | None:
    """Parse .orient/facts.json. None if absent, unparseable, or a different schema."""
    text = env.read_text(CACHE_PATH, max_bytes=200_000_000)
    if not text:
        return None
    try:
        facts = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(facts, dict) or facts.get("schema_version") != SCHEMA_VERSION:
        return None
    return facts


def save_cache(env: Env, facts: Facts) -> int:
    return env.write_text(
        env.root / CACHE_PATH, json.dumps(facts, sort_keys=True, indent=2) + "\n"
    )


def cache_is_fresh(facts: Facts, env: Env) -> bool:
    """True if the cached HEAD still matches the working tree's HEAD."""
    head = env.git(["rev-parse", "HEAD"])
    current = head.stdout.strip() if head.ok else None
    return (facts.get("history", {}).get("head") or None) == (current or None)
