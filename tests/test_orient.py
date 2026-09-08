"""The orient test suite.

Nothing here touches the network, a real git repository, a real `claude`, or any
path outside its own temporary directory: `Env` is always built with a fixed
clock and a `FakeRunner` that replays canned subprocess output.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orient import cli, collect, llm, render  # noqa: E402
from orient.collect import Config  # noqa: E402
from orient.env import Completed, Env, OrientError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema" / "facts.schema.json").read_text())

FIXED_NOW = dt.datetime(2025, 9, 1, 12, 0, 0, tzinfo=dt.UTC)
_TEMPS: list[Path] = []


def fixed_clock() -> dt.datetime:
    return FIXED_NOW


def repo(files: dict[str, str]) -> Path:
    """Write a fixture tree into a fresh temporary directory and return its root."""
    root = Path(tempfile.mkdtemp(prefix="orient-test-"))
    _TEMPS.append(root)
    for rel, text in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return root


def tearDownModule() -> None:
    while _TEMPS:
        shutil.rmtree(_TEMPS.pop(), ignore_errors=True)


def gitlog(commits) -> str:
    """Render `git log --numstat --format=<orient format>` output.

    `commits` is a sequence of (hash, author, iso_date, [(added, deleted, path), ...]).
    """
    out: list[str] = []
    for sha, author, when, files in commits:
        out.append("\x1e" + "\x1f".join([sha, author, when]))
        for added, deleted, path in files:
            out.append(f"{added}\t{deleted}\t{path}")
    return "\n".join(out) + "\n"


class FakeRunner:
    """Records every argv/stdin it is handed and replays canned results."""

    def __init__(
        self,
        *,
        log: str = "",
        head: str = "abc1234",
        git_ok: bool = True,
        git_log_code: int = 0,
        claude_out: str = "canned model prose",
        claude_code: int = 0,
        claude_timeout: bool = False,
    ) -> None:
        self.log = log
        self.head = head
        self.git_ok = git_ok
        self.git_log_code = git_log_code
        self.claude_out = claude_out
        self.claude_code = claude_code
        self.claude_timeout = claude_timeout
        self.calls: list[SimpleNamespace] = []

    def __call__(self, argv, stdin, timeout, cwd) -> Completed:
        argv = list(argv)
        self.calls.append(SimpleNamespace(argv=argv, stdin=stdin, timeout=timeout, cwd=cwd))
        if argv[0] == "git":
            if not self.git_ok:
                return Completed(127, "", "git: not found")
            rest = argv[3:] if argv[1] == "-C" else argv[1:]
            sub = rest[0] if rest else ""
            if sub == "rev-parse":
                return Completed(0, self.head + "\n", "")
            if sub == "log":
                if self.git_log_code:
                    return Completed(self.git_log_code, "", "fatal: bad revision")
                return Completed(0, self.log, "")
            if sub == "ls-files":
                return Completed(0, "", "")
            return Completed(0, "", "")
        if argv[0] == "claude":
            if self.claude_timeout:
                return Completed(124, "", "timed out", True)
            return Completed(self.claude_code, self.claude_out, "")
        return Completed(127, "", f"{argv[0]}: not found")

    @property
    def claude_calls(self) -> list[SimpleNamespace]:
        return [c for c in self.calls if c.argv[0] == "claude"]

    @property
    def git_calls(self) -> list[SimpleNamespace]:
        return [c for c in self.calls if c.argv[0] == "git"]


def make_env(root: Path, runner: FakeRunner | None = None) -> Env:
    return Env(root, clock=fixed_clock, runner=runner or FakeRunner())


def no_llm_run():
    """Run a command with `claude` absent and stdout captured."""
    from contextlib import ExitStack, contextmanager

    @contextmanager
    def runner():
        buf = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(no_claude())
            stack.enter_context(redirect_stdout(buf))
            yield buf

    return runner()


def no_claude():
    return mock.patch("orient.env.shutil.which", lambda name: None)


def yes_claude():
    return mock.patch("orient.env.shutil.which", lambda name: f"/usr/bin/{name}")


# --------------------------------------------------------------------------------------
# Files and language analysis
# --------------------------------------------------------------------------------------


class TestFiles(unittest.TestCase):
    def test_walk_prunes_dot_and_vendor_dirs(self):
        root = repo(
            {
                "a.py": "x = 1\n",
                ".git/config": "[core]\n",
                "node_modules/lib/index.js": "module.exports = 1\n",
                "vendor/dep.py": "y = 2\n",
                "__pycache__/a.cpython-311.pyc": "junk\n",
                "src/b.py": "z = 3\n",
            }
        )
        paths, truncated = make_env(root).walk(max_files=100)
        self.assertEqual(paths, ["a.py", "src/b.py"])
        self.assertFalse(truncated)

    def test_walk_honours_max_files_hard_stop(self):
        root = repo({f"pkg/f{i:03d}.py": "x = 1\n" for i in range(50)})
        paths, truncated = make_env(root).walk(max_files=10)
        self.assertEqual(len(paths), 10)
        self.assertTrue(truncated)

    def test_include_exclude_globs_narrow_the_file_set(self):
        root = repo(
            {
                "src/app/main.py": "a = 1\n",
                "src/generated/client.py": "b = 2\n",
                "docs/readme.md": "hi\n",
            }
        )
        cfg = Config(root=root, include=("src/**",), exclude=("src/generated/**",))
        files, _ = collect.scan_files(make_env(root), cfg)
        self.assertEqual([f.path for f in files], ["src/app/main.py"])

    def test_python_facts_counts_classes_functions_and_nesting(self):
        src = (
            "import os\n"
            "from a.b import thing\n"
            "\n"
            "class Poster:\n"
            "    def post(self):\n"
            "        for i in range(3):\n"
            "            if i:\n"
            "                while True:\n"
            "                    break\n"
            "\n"
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def _private():\n"
            "    return 2\n"
        )
        facts = collect.python_facts(src)
        self.assertTrue(facts["parsed"])
        self.assertEqual(facts["classes"], 1)
        self.assertEqual(facts["functions"], 2)
        self.assertIn("os", facts["imports"])
        self.assertIn("a.b", facts["imports"])
        self.assertEqual(sorted(facts["defs"]), ["Poster", "helper"])
        self.assertGreaterEqual(facts["max_nesting"], 5)

    def test_python_facts_syntax_error_falls_back_to_regex_imports(self):
        src = "import os\nfrom pkg.mod import thing\ndef broken(:\n    pass\n"
        facts = collect.python_facts(src)
        self.assertFalse(facts["parsed"])
        self.assertIn("os", facts["imports"])
        self.assertIn("pkg.mod", facts["imports"])

    def test_regex_facts_extracts_imports_for_tier2_language(self):
        js = "import fs from 'fs';\nconst x = require(\"./local/util\");\n"
        facts = collect.regex_facts(js, "javascript")
        self.assertIn("fs", facts["imports"])
        self.assertIn("./local/util", facts["imports"])
        self.assertEqual(collect.regex_facts("whatever", "other")["imports"], ())


# --------------------------------------------------------------------------------------
# Coupling
# --------------------------------------------------------------------------------------


class TestCoupling(unittest.TestCase):
    def files_for(self, tree: dict[str, str]):
        root = repo(tree)
        files, _ = collect.scan_files(make_env(root), Config(root=root))
        return files

    def test_fan_in_counts_distinct_importing_files(self):
        files = self.files_for(
            {
                "pkg/target.py": "VALUE = 1\n",
                "pkg/one.py": "from pkg.target import VALUE\nimport pkg.target\n",
                "pkg/two.py": "import pkg.target\n",
            }
        )
        coupling = collect.couple(files)
        self.assertEqual(coupling["pkg/target.py"]["fan_in"], 2)
        self.assertEqual(
            coupling["pkg/target.py"]["importers"], ["pkg/one.py", "pkg/two.py"]
        )

    def test_fan_out_excludes_stdlib_and_unresolved_imports(self):
        files = self.files_for(
            {
                "pkg/target.py": "VALUE = 1\n",
                "pkg/user.py": "import os\nimport sqlalchemy\nfrom pkg.target import VALUE\n",
            }
        )
        coupling = collect.couple(files)
        user = coupling["pkg/user.py"]
        self.assertEqual(user["fan_out"], 1)
        self.assertEqual(user["internal"], ["pkg/target.py"])
        self.assertIn("os", user["stdlib"])
        self.assertIn("sqlalchemy", user["external"])

    def test_import_cycle_terminates(self):
        files = self.files_for(
            {
                "pkg/a.py": "from pkg import b\n",
                "pkg/b.py": "from pkg import a\n",
            }
        )
        coupling = collect.couple(files)
        self.assertEqual(coupling["pkg/a.py"]["fan_in"], 1)
        self.assertEqual(coupling["pkg/b.py"]["fan_in"], 1)


# --------------------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------------------

LOG = gitlog(
    [
        ("aaa1", "dana.k", "2025-08-30T10:00:00+00:00", [(10, 2, "src/ledger.py")]),
        (
            "bbb2",
            "s.ito",
            "2025-07-14T09:00:00+00:00",
            [(4, 1, "src/ledger.py"), (30, 0, "src/api.py")],
        ),
        ("ccc3", "pmoreau", "2024-11-02T08:00:00+00:00", [(3, 3, "src/api.py")]),
    ]
)


class TestHistory(unittest.TestCase):
    def hist(self, **kw):
        root = repo({"src/ledger.py": "x = 1\n"})
        runner = FakeRunner(**kw)
        env = make_env(root, runner)
        return collect.history(env, Config(root=root)), runner

    def test_history_parses_numstat_into_per_file_commit_counts(self):
        hist, _ = self.hist(log=LOG)
        self.assertTrue(hist["available"])
        self.assertEqual(hist["commits"], 3)
        self.assertEqual(hist["files"]["src/ledger.py"]["commits"], 2)
        self.assertEqual(hist["files"]["src/api.py"]["commits"], 2)
        self.assertEqual(hist["files"]["src/ledger.py"]["churn"], 17)

    def test_history_records_authors_and_last_commit_date(self):
        hist, _ = self.hist(log=LOG)
        names = [a["name"] for a in hist["authors"]]
        self.assertEqual(sorted(names), ["dana.k", "pmoreau", "s.ito"])
        self.assertEqual(hist["files"]["src/ledger.py"]["last"], "2025-08-30T10:00:00+00:00")
        self.assertEqual(hist["files"]["src/api.py"]["authors"], {"pmoreau": 1, "s.ito": 1})
        self.assertEqual(hist["latest"], "2025-08-30T10:00:00+00:00")

    def test_history_follows_numstat_rename_syntax(self):
        log = gitlog(
            [
                (
                    "d1",
                    "dana.k",
                    "2025-08-01T00:00:00+00:00",
                    [(1, 1, "src/{old => new}.py"), (2, 0, "top_old.py => top_new.py")],
                )
            ]
        )
        hist, _ = self.hist(log=log)
        self.assertIn("src/new.py", hist["files"])
        self.assertIn("top_new.py", hist["files"])
        self.assertNotIn("src/{old => new}.py", hist["files"])

    def test_history_absent_git_sets_unavailable_flag(self):
        hist, _ = self.hist(git_ok=False)
        self.assertFalse(hist["available"])
        self.assertEqual(hist["files"], {})
        self.assertIn("git", hist["reason"])

    def test_since_window_is_passed_to_git_log(self):
        root = repo({"a.py": "x = 1\n"})
        runner = FakeRunner(log=LOG)
        collect.history(make_env(root, runner), Config(root=root, since="18mo"))
        argvs = [" ".join(c.argv) for c in runner.git_calls]
        self.assertTrue(any("--since=547 days ago" in a for a in argvs), argvs)


# --------------------------------------------------------------------------------------
# Hotspots
# --------------------------------------------------------------------------------------

HOT_TREE = {
    "src/churny.py": "def one():\n    return 1\n" * 30,
    "src/calm.py": "def two():\n    return 2\n" * 30,
    "src/big_untouched.py": "VALUE = 1\n" * 900,
    "src/quiet.py": "def three():\n    return 3\n" * 10,
    "tests/test_calm.py": (
        "from src.calm import two\n\ndef test_two():\n    assert two() == 2\n"
    ),
}


def hot_log() -> str:
    commits = []
    for i in range(50):
        commits.append(
            (
                f"h{i:03d}",
                "dana.k",
                f"2025-08-{(i % 28) + 1:02d}T00:00:00+00:00",
                [(5, 2, "src/churny.py")],
            )
        )
    commits.append(("c1", "s.ito", "2025-07-01T00:00:00+00:00", [(1, 1, "src/calm.py")]))
    commits.append(("c2", "s.ito", "2025-07-02T00:00:00+00:00", [(1, 1, "src/calm.py")]))
    commits.append(("q1", "s.ito", "2025-06-02T00:00:00+00:00", [(1, 1, "src/quiet.py")]))
    return gitlog(commits)


class TestHotspots(unittest.TestCase):
    def rank(self, tree=None, log=None, top=10, shuffle=False):
        root = repo(tree or HOT_TREE)
        env = make_env(root, FakeRunner(log=log if log is not None else hot_log()))
        cfg = Config(root=root, top=top)
        files, _ = collect.scan_files(env, cfg)
        if shuffle:
            files = list(files)
            random.Random(7).shuffle(files)
        coupling = collect.couple(files)
        hist = collect.history(env, cfg)
        ratios = collect.test_coverage(files)
        return collect.score_hotspots(files, coupling, hist, ratios, top)

    def test_hotspot_ranks_fifty_commit_file_first(self):
        rows = self.rank()
        self.assertEqual(rows[0]["path"], "src/churny.py")
        self.assertEqual(rows[0]["commits"], 50)

    def test_hotspot_keeps_large_untouched_file_out_of_top_three(self):
        rows = self.rank()
        top3 = [r["path"] for r in rows[:3]]
        self.assertNotIn("src/big_untouched.py", top3)

    def test_hotspot_reasons_flag_thin_tests(self):
        rows = {r["path"]: r for r in self.rank()}
        self.assertIn("no_tests", rows["src/churny.py"]["reasons"])
        self.assertIn("high_churn", rows["src/churny.py"]["reasons"])
        self.assertNotIn("no_tests", rows["src/calm.py"]["reasons"])

    def test_hotspot_ranking_independent_of_walk_order(self):
        plain = [r["path"] for r in self.rank()]
        shuffled = [r["path"] for r in self.rank(shuffle=True)]
        self.assertEqual(plain, shuffled)


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------

FLASK = {
    "app/server.py": (
        "from flask import Flask\n\napp = Flask(__name__)\n\n"
        "@app.route('/charges')\ndef charges():\n    return 'ok'\n\n"
        "if __name__ == '__main__':\n    app.run()\n"
    ),
}
DJANGO = {
    "manage.py": (
        "import os, sys\n\n"
        "def main():\n"
        "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proj.settings')\n"
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n"
    ),
    "proj/wsgi.py": (
        "from django.core.wsgi import get_wsgi_application\n\n"
        "application = get_wsgi_application()\n"
    ),
}
CONSOLE = {
    "pyproject.toml": (
        "[project]\nname = 'acme'\nversion = '1.0'\n\n"
        "[project.scripts]\nacme = 'acme.cli:main'\n"
    ),
    "acme/cli.py": "def main():\n    return 0\n",
}
DOCKER = {
    "Dockerfile": (
        "FROM python:3.11-slim\nCOPY . /app\nWORKDIR /app\n"
        "ENTRYPOINT [\"python\"]\nCMD [\"-m\", \"acme.worker\"]\n"
    ),
    "acme/worker.py": "def run():\n    return 1\n",
}
MAKE = {
    "Makefile": (
        ".PHONY: nightly reconcile\n\n"
        "nightly:\n\tpython -m batch.nightly\n\n"
        "reconcile:\n\tpython -m batch.reconcile\n"
    ),
    "batch/nightly.py": "def run():\n    return 1\n",
}


class TestEntryPoints(unittest.TestCase):
    def eps(self, tree):
        root = repo(tree)
        env = make_env(root)
        files, _ = collect.scan_files(env, Config(root=root))
        return collect.find_entry_points(env, files)

    def kinds_for(self, tree, path):
        return {e["kind"] for e in self.eps(tree) if e["path"] == path}

    def test_entrypoints_flask_decorated_app(self):
        kinds = self.kinds_for(FLASK, "app/server.py")
        self.assertIn("web_route", kinds)
        self.assertIn("main_guard", kinds)

    def test_entrypoints_django_manage_py_and_wsgi(self):
        eps = self.eps(DJANGO)
        found = {(e["path"], e["kind"]) for e in eps}
        self.assertIn(("manage.py", "django_manage"), found)
        self.assertIn(("proj/wsgi.py", "wsgi_asgi"), found)

    def test_entrypoints_console_scripts_from_pyproject(self):
        eps = self.eps(CONSOLE)
        scripts = [e for e in eps if e["kind"] == "console_script"]
        self.assertEqual(len(scripts), 1)
        self.assertIn("acme.cli:main", scripts[0]["detail"])

    def test_entrypoints_dockerfile_cmd_and_entrypoint(self):
        eps = [e for e in self.eps(DOCKER) if e["kind"] == "docker_cmd"]
        self.assertEqual(len(eps), 1)
        self.assertIn("acme.worker", eps[0]["detail"])

    def test_entrypoints_makefile_phony_targets(self):
        eps = [e for e in self.eps(MAKE) if e["kind"] == "make_target"]
        self.assertEqual(len(eps), 1)
        self.assertIn("nightly", eps[0]["detail"])
        self.assertIn("reconcile", eps[0]["detail"])

    def test_entrypoints_at_most_two_spurious_on_fixture(self):
        tree: dict[str, str] = {}
        for prefix, part in (
            ("flask", FLASK), ("dj", DJANGO), ("cli", CONSOLE),
            ("dock", DOCKER), ("mk", MAKE),
        ):
            for path, text in part.items():
                tree[f"{prefix}/{path}"] = text
        expected = {
            ("flask/app/server.py", "web_route"),
            ("flask/app/server.py", "main_guard"),
            ("dj/manage.py", "django_manage"),
            ("dj/proj/wsgi.py", "wsgi_asgi"),
            ("cli/pyproject.toml", "console_script"),
            ("dock/Dockerfile", "docker_cmd"),
            ("mk/Makefile", "make_target"),
        }
        found = {(e["path"], e["kind"]) for e in self.eps(tree)}
        self.assertTrue(expected <= found, expected - found)
        self.assertLessEqual(len(found - expected), 2, found - expected)


# --------------------------------------------------------------------------------------
# Risk flags
# --------------------------------------------------------------------------------------


class TestFlags(unittest.TestCase):
    def flags(self, tree, log):
        root = repo(tree)
        env = make_env(root, FakeRunner(log=log))
        cfg = Config(root=root)
        files, _ = collect.scan_files(env, cfg)
        coupling = collect.couple(files)
        hist = collect.history(env, cfg)
        ratios = collect.test_coverage(files)
        return collect.risk_flags(files, coupling, hist, ratios)

    def test_flag_bus_factor_one_needs_dominant_and_inactive_author(self):
        tree = {
            "src/lonely.py": "def a():\n    return 1\n" * 20,
            "src/shared.py": "def b():\n    return 2\n" * 20,
        }
        commits = [
            (f"l{i}", "pmoreau", "2024-11-02T00:00:00+00:00", [(2, 1, "src/lonely.py")])
            for i in range(9)
        ]
        commits += [
            (f"s{i}", "dana.k" if i % 2 else "s.ito", "2025-08-20T00:00:00+00:00",
             [(2, 1, "src/shared.py")])
            for i in range(8)
        ]
        commits.append(("recent", "dana.k", "2025-08-31T00:00:00+00:00", [(1, 0, "src/shared.py")]))
        flags = self.flags(tree, gitlog(commits))
        bus = {f["path"] for f in flags if f["flag"] == "bus_factor_1"}
        self.assertIn("src/lonely.py", bus)
        self.assertNotIn("src/shared.py", bus)

    def test_flag_dead_ish_needs_zero_commits_and_zero_importers(self):
        tree = {
            "src/legacy/v1_sync.py": "def sync():\n    return 1\n" * 20,
            "src/live.py": "def live():\n    return 1\n" * 20,
            "src/caller.py": "from src.live import live\n" * 1,
        }
        log = gitlog([("z1", "dana.k", "2025-08-01T00:00:00+00:00", [(1, 1, "src/live.py")])])
        flags = self.flags(tree, log)
        dead = {f["path"] for f in flags if f["flag"] == "dead_ish"}
        self.assertIn("src/legacy/v1_sync.py", dead)
        self.assertNotIn("src/live.py", dead)

    def test_flag_config_surface_reports_path_and_never_the_string(self):
        secret = "sk-live-abcdef0123456789"
        tree = {
            "conf/settings.py": f"API_KEY = '{secret}'\nPASSWORD = 'hunter2hunter2'\n",
            "src/plain.py": "VALUE = 1\n",
        }
        flags = self.flags(tree, gitlog([]))
        conf = [f for f in flags if f["flag"] == "config_surface"]
        self.assertEqual([f["path"] for f in conf], ["conf/settings.py"])
        self.assertNotIn(secret, json.dumps(flags))
        self.assertIn("2", conf[0]["detail"])


# --------------------------------------------------------------------------------------
# LLM budget and failure
# --------------------------------------------------------------------------------------


def big_repo(n_files: int = 300) -> Path:
    tree = {}
    body = "\n".join(
        f"def function_{i}(argument_one, argument_two):\n"
        f"    # a line of explanatory commentary about function number {i}\n"
        f"    return argument_one + argument_two + {i}\n"
        for i in range(40)
    )
    for i in range(n_files):
        tree[f"src/pkg{i % 10}/module_{i:03d}.py"] = (
            f"import os\nfrom src.pkg0 import module_000\n{body}\n"
        )
    tree["src/pkg0/module_000.py"] = f"import os\n{body}\n"
    return repo(tree)


class TestLLMBudget(unittest.TestCase):
    def scan_facts(self, root, runner):
        env = make_env(root, runner)
        return collect.collect(env, Config(root=root))

    def test_prompt_stays_under_forty_thousand_chars(self):
        root = big_repo()
        runner = FakeRunner(log=gitlog([]))
        facts = self.scan_facts(root, runner)
        env = make_env(root, runner)
        exc = {h["path"]: llm.excerpt(env, h["path"]) for h in facts["hotspots"][:3]}
        for kind in ("snapshot", "module_map", "hotspots", "reading_order"):
            prompt = llm.build_prompt(kind, facts, excerpts=exc)
            self.assertLessEqual(len(prompt), llm.PROMPT_LIMIT, kind)
        with self.assertRaises(OrientError):
            llm.build_prompt("nonsense", facts)

    def test_excerpt_truncates_at_120_lines(self):
        root = repo({"src/long.py": "".join(f"x = {i}\n" for i in range(500))})
        text = llm.excerpt(make_env(root), "src/long.py")
        self.assertLessEqual(len(text.splitlines()), llm.EXCERPT_LINES + 1)
        self.assertIn("src/long.py", text.splitlines()[0])

    def test_scan_issues_at_most_six_claude_calls(self):
        root = big_repo(20)
        runner = FakeRunner(log=gitlog([]))
        with yes_claude(), redirect_stdout(io.StringIO()):
            code = cli.main(["scan", str(root)], env=make_env(root, runner))
        self.assertIn(code, (0, 4))
        self.assertLessEqual(len(runner.claude_calls), llm.MAX_CALLS)
        self.assertGreater(len(runner.claude_calls), 0)

    def test_no_llm_issues_zero_claude_calls(self):
        root = big_repo(20)
        runner = FakeRunner(log=gitlog([]))
        with yes_claude(), redirect_stdout(io.StringIO()):
            cli.main(["scan", str(root), "--no-llm"], env=make_env(root, runner))
        self.assertEqual(runner.claude_calls, [])

    def test_prompt_bytes_under_five_percent_of_repo_bytes(self):
        root = big_repo()
        runner = FakeRunner(log=gitlog([]))
        with yes_claude(), redirect_stdout(io.StringIO()):
            cli.main(["scan", str(root), "--quiet"], env=make_env(root, runner))
        prompt_bytes = sum(len(c.stdin.encode()) for c in runner.claude_calls)
        repo_bytes = sum(
            (root / p).stat().st_size for p in make_env(root).walk(max_files=100000)[0]
        )
        self.assertGreater(len(runner.claude_calls), 0)
        self.assertLess(prompt_bytes, repo_bytes * 0.05, (prompt_bytes, repo_bytes))


# --------------------------------------------------------------------------------------
# CLI, output, and degradation
# --------------------------------------------------------------------------------------

SMALL_TREE = {
    "src/ledger.py": (
        "import os\n\n\ndef post_entry():\n    return 1\n\n\ndef reverse():\n    return 2\n"
    ),
    "src/api.py": (
        "from src.ledger import post_entry\n\n\ndef charge():\n    return post_entry()\n"
    ),
    "tests/test_ledger.py": (
        "from src.ledger import post_entry\n\n\n"
        "def test_post_entry():\n    assert post_entry() == 1\n"
    ),
    "Makefile": ".PHONY: run\n\nrun:\n\tpython -m src.api\n",
}


def validate(instance, schema, path="$"):
    """Minimal draft-2020-12 walker: type, required, properties, items, enum."""
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        ok = any(_is_type(instance, n) for n in names)
        if not ok:
            errors.append(f"{path}: expected {names}, got {type(instance).__name__}")
            return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors.extend(validate(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors


def _is_type(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise AssertionError(f"unknown schema type {name}")


class TestCLI(unittest.TestCase):
    def scan(self, tree=None, argv=(), runner=None, claude=False):
        root = repo(dict(tree or SMALL_TREE))
        runner = runner or FakeRunner(log=LOG)
        env = make_env(root, runner)
        ctx = yes_claude() if claude else no_claude()
        buf = io.StringIO()
        with ctx, redirect_stdout(buf):
            code = cli.main(["scan", str(root), *argv], env=env)
        return SimpleNamespace(root=root, code=code, runner=runner, out=buf.getvalue(), env=env)

    def test_markdown_report_always_has_eight_sections(self):
        res = self.scan(runner=FakeRunner(log=LOG, claude_code=1), claude=True)
        report = (res.root / ".orient" / "report.md").read_text()
        self.assertEqual(len(re.findall(r"(?m)^## ", report)), 8)
        for name in render.SECTIONS:
            self.assertIn(f"## {name}", report)
        self.assertEqual(res.code, 4)

    def test_facts_json_validates_against_committed_schema(self):
        res = self.scan(argv=["--no-llm"])
        facts = json.loads((res.root / ".orient" / "facts.json").read_text())
        self.assertEqual(validate(facts, SCHEMA), [])

    def test_repeat_scan_is_byte_identical_modulo_generated_at(self):
        root = repo(dict(SMALL_TREE))
        outs = []
        for _ in range(2):
            env = make_env(root, FakeRunner(log=LOG))
            with no_claude(), redirect_stdout(io.StringIO()):
                cli.main(["scan", str(root), "--no-llm", "--no-cache"], env=env)
            facts = json.loads((root / ".orient" / "facts.json").read_text())
            facts.pop("generated_at")
            outs.append(json.dumps(facts, sort_keys=True, indent=2))
        self.assertEqual(outs[0], outs[1])

    def test_report_without_cache_exits_3(self):
        root = repo({"a.py": "x = 1\n"})
        env = make_env(root)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["report"], env=env)
        self.assertEqual(code, 3)

    def test_missing_git_scan_exits_4_and_says_so_in_report(self):
        res = self.scan(argv=["--no-llm"], runner=FakeRunner(git_ok=False))
        self.assertEqual(res.code, 4)
        report = (res.root / ".orient" / "report.md").read_text()
        body = report.split("## Hotspots", 1)[1]
        self.assertIn("git history unavailable", body)
        who = report.split("## Who to ask", 1)[1]
        self.assertIn("git history unavailable", who)

    def test_claude_timeout_exits_4_but_writes_report(self):
        res = self.scan(
            runner=FakeRunner(log=LOG, claude_timeout=True),
            claude=True,
            argv=["--llm-timeout", "5"],
        )
        self.assertEqual(res.code, 4)
        self.assertTrue((res.root / ".orient" / "report.md").exists())
        facts = json.loads((res.root / ".orient" / "facts.json").read_text())
        self.assertTrue(any("timed out" in w for w in facts["warnings"]))

    def test_unknown_flag_exits_2(self):
        root = repo({"a.py": "x = 1\n"})
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cli.main(["scan", str(root), "--nope"], env=make_env(root))
        self.assertEqual(code, 2)

    def test_scan_writes_only_facts_json_and_report_md(self):
        root = repo(dict(SMALL_TREE))
        before = {str(p.relative_to(root)) for p in root.rglob("*")}
        env = make_env(root, FakeRunner(log=LOG))
        with no_claude(), redirect_stdout(io.StringIO()):
            cli.main(["scan", str(root), "--no-llm"], env=env)
        after = {str(p.relative_to(root)) for p in root.rglob("*")}
        self.assertEqual(
            sorted(after - before),
            [os.path.join(".orient", "facts.json"), ".orient", os.path.join(".orient", "report.md")]
            and sorted({".orient", ".orient/facts.json", ".orient/report.md"}),
        )


class TestOtherCommands(unittest.TestCase):
    """The commands that read a scan back: hotspots, owners, explain, doctor, report."""

    def prepared(self, argv, claude=False, runner=None):
        root = repo(dict(SMALL_TREE))
        runner = runner or FakeRunner(log=LOG)
        env = make_env(root, runner)
        with no_claude(), redirect_stdout(io.StringIO()):
            cli.main(["scan", str(root), "--no-llm"], env=env)
        buf = io.StringIO()
        ctx = yes_claude() if claude else no_claude()
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with ctx, redirect_stdout(buf):
                code = cli.main(argv, env=make_env(root, runner))
        finally:
            os.chdir(cwd)
        return code, buf.getvalue(), runner

    def test_hotspots_command_prints_table_without_calling_claude(self):
        code, out, runner = self.prepared(
            ["hotspots", "--top", "3", "--format", "text"], claude=True
        )
        self.assertEqual(code, 0)
        self.assertIn("src/ledger.py", out)
        self.assertIn("score", out)
        self.assertEqual(runner.claude_calls, [])

    def test_owners_command_appends_llm_note_only_with_llm(self):
        code, out, runner = self.prepared(["owners", "src/"], claude=True)
        self.assertEqual(code, 0)
        self.assertIn("dana.k", out)
        self.assertIn("canned model prose", out)
        code, out, runner = self.prepared(["owners", "src/", "--no-llm"])
        self.assertNotIn("canned model prose", out)

    def test_explain_prints_deterministic_sections_without_llm(self):
        code, out, _ = self.prepared(["explain", "src/ledger.py", "--no-llm"])
        self.assertEqual(code, 0)
        for header in ("SHAPE", "WHO CALLS IT", "WHAT IT DEPENDS ON"):
            self.assertIn(header, out)
        self.assertNotIn("WHAT IT DOES", out)
        self.assertIn("src/api.py", out)

    def test_explain_unknown_target_exits_3(self):
        code, _, _ = self.prepared(["explain", "src/nope.py", "--no-llm"])
        self.assertEqual(code, 3)

    def test_cache_is_ignored_when_the_flags_changed(self):
        root = repo(dict(SMALL_TREE))
        runner = FakeRunner(log=LOG)
        with no_llm_run() as out:
            cli.main(["scan", str(root), "--no-llm"], env=make_env(root, runner))
        self.assertIn("src/api.py", out.getvalue())
        with no_llm_run():
            cli.main(
                ["scan", str(root), "--no-llm", "--exclude", "src/api.py"],
                env=make_env(root, runner),
            )
        facts = json.loads((root / ".orient" / "facts.json").read_text())
        self.assertNotIn("src/api.py", [row["path"] for row in facts["files"]])

    def test_report_renders_cached_facts_as_json(self):
        code, out, _ = self.prepared(["report", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hotspots"][0]["path"], "src/ledger.py")

    def test_doctor_reports_tool_availability(self):
        code, out, _ = self.prepared(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("python", out)
        self.assertIn("git", out)
        self.assertIn("claude", out)
        self.assertIn("missing", out)


class TestUnits(unittest.TestCase):
    def test_parse_duration_units_and_rejection(self):
        self.assertEqual(collect.parse_duration("18mo"), 547)
        self.assertEqual(collect.parse_duration("12mo"), 365)
        self.assertEqual(collect.parse_duration("90d"), 90)
        self.assertEqual(collect.parse_duration("2y"), 730)
        with self.assertRaises(OrientError) as ctx:
            collect.parse_duration("18 months")
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_git_refuses_write_subcommands(self):
        root = repo({"a.py": "x = 1\n"})
        env = make_env(root)
        with self.assertRaises(OrientError):
            env.git(["commit", "-m", "nope"])

    def test_explain_prose_blocks_keep_their_bullets(self):
        payload = {
            "path": "src/ledger.py", "kind": "file", "lines": 40, "files": 1, "commits": 13,
            "last": None, "window_days": 365, "history_available": True, "classes": 1,
            "functions": 3, "max_nesting": 5, "defs": ["post_entry"], "test_ratio": 0.33,
            "callers": [], "depends": {"stdlib": [], "internal": [], "external": []},
            "authors": [],
        }
        prose = (
            "It posts rows.\n---\nSettle carries edge cases.\n---\n"
            "- Decimal only\n- Stay idempotent"
        )
        out = render.render_explain(payload, {"explain": prose})
        self.assertIn("- Decimal only", out)
        self.assertIn("- Stay idempotent", out)
        self.assertNotIn("---", out)

    def test_render_table_aligns_numbers_right(self):
        rows = [{"file": "a.py", "n": 5}, {"file": "bbbb.py", "n": 1234}]
        table = render.render_table(rows, ["file", "n"])
        lines = table.splitlines()
        self.assertTrue(lines[1].startswith("a.py"))
        self.assertTrue(lines[1].rstrip().endswith("5"))
        self.assertEqual(render.render_table([], ["file"]), "")
