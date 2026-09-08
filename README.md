# orient

A command-line tool that reads a codebase and its git history and writes an
orientation report — entry points, module map, churn hotspots, risk flags, who
to ask, what to read first — for an engineer who has just been handed a
repository they did not write.

## Why this exists

You cloned a service on Monday. The README covers a deploy process that
changed, `utils/` is nine thousand lines, the person who wrote the retry logic
left in March, and you are three Slack round-trips deep into "who owns
`billing/`?" The information that would answer most of those questions —
the file tree, the import graph, the commit log — is already sitting in the
checkout; nobody assembles it because doing so by hand is a day of `git log`
incantations. `orient` assembles it in one command.

## Install

```
pip install -e .
orient doctor
```

Python 3.11 or newer. No dependencies outside the standard library. `git` and
the `claude` CLI are optional — the tool runs without either and says so.

## Quick start

From the root of the repository you want to understand:

```
orient scan . --no-llm
```

It walks the tree, parses the Python with `ast`, reads the last 12 months of
`git log`, and writes `.orient/facts.json` and `.orient/report.md`. The
terminal gets a summary: file and line counts, a "Start here" list, the top
five hotspots, and any risk flags. On a 1,200-file, 456k-line tree this takes
about three seconds. Drop `--no-llm` to have the `claude` CLI on your machine
write the prose sections too.

## Example

Run against a small Python repository — here, orient's own source tree in a
git checkout with a short history:

```
$ orient scan . --no-llm
orient 0.1.0  ·  /private/tmp/orient-demo

  files       12 tracked   ·  8 analyzed (python), 4 counted (other)
  lines       3,745
  history     12mo · 8 commits · 2 authors
  llm         disabled (--no-llm) · 0 calls · 0.0s

Wrote .orient/facts.json  (17 KB)
Wrote .orient/report.md   (2 KB)

  Start here
  ──────────────────────────────────────────────────────────────────
  1. pyproject.toml — console script: orient = orient.cli:main
  2. tests/test_orient.py — web route: 1 route(s), first /charges
  3. src/orient/collect.py — 6 commits, 1235 lines, 4 importers
  4. src/orient/render.py — 3 commits, 516 lines, 2 importers

  Top hotspots (churn x complexity, 12mo)
  ──────────────────────────────────────────────────────────────────
  score  file                   commits  lines  fan_in  test_ratio
     93  src/orient/collect.py        6  1,235       4        0.09
     69  src/orient/render.py         3    516       2        0.11
     32  src/orient/env.py            1    190       5        1.00
     23  src/orient/cli.py            1    338       1        0.00
     19  src/orient/llm.py            1    165       2        0.00

  Flags
  ──────────────────────────────────────────────────────────────────
  config-surface tests/test_orient.py  · 2 credential-shaped strings (values are never read or printed)
  god-file       src/orient/collect.py  · 1235 lines, 4 importers, 37 top-level definitions
```

Two things in that output are worth reading as a warning label rather than a
result: line 2 of "Start here" is the test file, flagged as a web route because
it contains a `@app.route(...)` string in a fixture, and `env.py` scores a
`test_ratio` of 1.00 because its function names all appear somewhere in the
test file's text. Both are the detectors working exactly as written. See
[Limitations](#limitations).

The other commands read the same facts:

```
$ orient explain src/orient/llm.py --no-llm
src/orient/llm.py  ·  165 lines  ·  1 commits/365d  ·  2 importers

SHAPE
  0 classes, 5 functions, max nesting 3, test ratio 0.00
  public: available, build_prompt, excerpt, narrate

WHO CALLS IT
  src/orient/cli.py                        module import
  tests/test_orient.py                     module import

WHAT IT DEPENDS ON
  stdlib:   __future__, collections, json, typing
  internal: src/orient/collect.py, src/orient/env.py
  external: -
```

```
$ orient owners src/orient --no-llm
src/orient  (6 files, 13 commits in window)

  author  share  commits  last        status
  dana.k  62%          8  2026-09-08
  s.ito   38%          5  2026-09-08
```

## How it works

1. **Walk and read.** Every regular file under the root, minus a fixed prune
   list (`.git`, `node_modules`, `__pycache__`, `vendor`, `dist`, …) and minus
   your `--exclude` globs. Each file is read once.
2. **Analyze.** Python goes through `ast` for imports, top-level classes and
   functions, and nesting depth. Fifteen other languages (JavaScript,
   TypeScript, Go, Ruby, Java, Rust, PHP, C#, Kotlin, Swift, Scala, C, C++,
   shell, Elixir) get a per-language regex list for the same fields.
   Everything else is counted, not parsed.
3. **Couple.** Python imports are resolved against a module index to produce
   real fan-in and fan-out, plus a stdlib/external/internal split per file.
4. **Read history.** One `git log --numstat -M --since=<window>` pass yields
   commits, authors, last-touched date and added+deleted lines per file.
5. **Score and render.** Hotspots are a percentile blend — 45% churn, 20%
   fan-in, 15% size, 10% nesting depth, 10% test thinness — and the eight
   report sections are always all rendered, falling back to deterministic
   tables wherever the model did not speak.

Where prose helps, `orient` pipes derived facts (never the repository) into
`claude -p --output-format text` on stdin: at most four calls per scan
(snapshot, module map, hotspots, reading order), each prompt hard-truncated at
40,000 characters, with at most 120 lines excerpted from each of the three top
hotspot files. No API key is read and `orient` itself opens no sockets. The
git surface is read-only and enforced: `Env.git` refuses any subcommand outside
`log`, `ls-files`, `rev-parse`, `shortlog`, `blame`.

The report always has these eight `## ` sections, in order: Snapshot · Entry
points · Module map · Hotspots · Risk flags · Who to ask · Reading order ·
Suggested first tasks.

## Configuration

| Command | Arguments |
| --- | --- |
| `orient scan [PATH]` | Analyze and write. `PATH` defaults to `.` and is the only place the repo root can be set. |
| `orient report` | Re-render `.orient/facts.json`. Exits 3 if it is missing. |
| `orient hotspots` | The ranked table alone. Never calls `claude`. |
| `orient owners [PATH]` | Authorship for a path prefix. |
| `orient explain PATH` | One file or directory in depth. Exits 3 if the path is not in the scan. |
| `orient doctor` | Versions of python/git/claude, repo state, cache age. |

| Flag | Commands | Default | Effect |
| --- | --- | --- | --- |
| `--since DURATION` | scan, hotspots, owners, explain | `12mo` | History window. Units `d`, `w`, `mo`, `m` (= months), `y`. A bad value exits 2. |
| `--top N` | scan, hotspots, owners, explain | `10` | Rows kept in ranked tables. Must be ≥ 1. |
| `--include GLOB` | scan, hotspots, owners, explain | none | Repeatable. If any are given, a file must match one. `**` spans directories. |
| `--exclude GLOB` | scan, hotspots, owners, explain | none | Repeatable. Applied after `--include`; excludes win. |
| `--max-files N` | scan, hotspots, owners, explain | `20000` | Hard stop on the walk. Hitting it adds a warning and makes the run exit 4. |
| `--no-cache` | scan, hotspots, owners, explain | off | Ignore `.orient/facts.json` and recompute. |
| `--no-llm` | scan, owners, explain | off | Zero `claude` invocations. Every section still renders, reduced. |
| `--llm-timeout SECONDS` | scan, owners, explain | `90` | Per-call timeout. A timeout is a warning, not a failure. |
| `--format md\|json\|text` | scan, report, hotspots | `md` (`text` for `hotspots`) | Output format. |
| `-o PATH` | scan, report | `.orient/report.md` | Report destination. `-` writes to stdout (and suppresses the terminal summary). |
| `--quiet` | scan, report, hotspots, owners, explain | off | Suppresses the scan summary and `report`'s "Wrote" line only. Accepted but ignored by `hotspots`, `owners` and `explain`; never suppresses stderr warnings. |
| `--version`, `-h/--help` | all | — | |

| Environment variable | Effect |
| --- | --- |
| `ORIENT_DEBUG` | If set to any non-empty value, unexpected exceptions re-raise with a traceback instead of being turned into `orient: internal error: …` and exit 1. |

Exit codes: `0` success · `1` unexpected error · `2` bad usage · `3` missing
cache or unreadable path · `4` degraded run (output was written, but git or
`claude` was unavailable, or the walk was truncated).

`orient` writes exactly two files, both under `.orient/` in the scanned
repository unless `-o` says otherwise. It never edits source, never creates
branches, and never touches `.gitignore`.

## Limitations

This is a heuristic tool run over an unfamiliar repository. Treat the report as
a starting map, not a source of truth. The known rough edges:

- **Cached warnings leak into later runs, and with them exit code 4.** `scan`
  appends LLM warnings to the facts it then saves, and it exits 4 whenever the
  facts carry any warning. So a run where `claude` failed poisons the cache: the
  next `orient scan .` at the same commit reuses those facts, re-prints
  `warning: claude exited 1 (snapshot) — section reduced`, and exits 4 even
  though nothing went wrong. `--no-cache` (or a new commit) clears it. This is a
  bug, verified on this build; do not gate anything on `scan`'s exit code.
- **Only `scan` takes a path.** `owners`, `explain` and `hotspots` always root
  themselves at the current working directory. Running `orient owners
  /tmp/other-repo/src` from `/tmp` analyzes `/tmp`, prints a near-empty result,
  and exits 0. `cd` into the repository first.
- **Fan-in and fan-out are Python-only.** Other languages get their import
  lines extracted by regex and listed as external dependencies, but nothing
  resolves them to files, so every non-Python file has fan-in 0 and is ranked
  on size, nesting and churn alone. A TypeScript repo gets a much weaker
  hotspot ranking than a Python one.
- **Entry-point detection over-reports.** It is regex over file text with no
  context: a `@app.route("/charges")` inside a test fixture or a docstring is
  reported as a web route, and every `.github/workflows/*.yml` is listed. Every
  `if __name__ ==` guard counts, including the ones in throwaway scripts.
- **`test_ratio` is a substring heuristic, not coverage.** It is the fraction
  of a file's public names that appear anywhere in the concatenated text of all
  test files. A function called `run` or `main` will look tested because some
  test mentions that word; a well-tested file whose tests import a wrapper will
  look untested. It is not instrumented, and it is not `coverage.py`.
- **Ownership is commit counts, not surviving lines.** `git blame` is on the
  allowed list but never called. Someone who made 200 one-line commits outranks
  someone who wrote the file in one. Authors are keyed on the git author name
  string, so the same person under two names is two people.
- **`.gitignore` is not read.** The walk prunes a fixed directory list. A
  generated or vendored tree outside that list — `target/`, `.next/`,
  `coverage/`, a checked-in SDK — is counted, analyzed, and can dominate the
  line counts and the module map. Use `--exclude`. Symlinks are always skipped,
  and files over 2 MB or containing a NUL byte in the first 8 KB read as zero
  lines.
- **The LLM prompt budget is absolute, not proportional.** The 40,000-character
  cap and 120-line excerpts hold regardless of repository size, which is the
  right guarantee for a large repo and a weak one for a small one: on the
  12-file example above, the four prompts totalled 21 KB against 135 KB of
  source — 16% of the repository, not the under-5% the design targets. On a
  repo where source is measured in megabytes the share is negligible.
- **`facts.json` grows with the repository.** It carries one row per file,
  including every import, def and importer list. 17 KB for 12 files; 1.8 MB for
  a 1,200-file, 456k-line tree. It is a cache for the current run, overwritten
  by the next one — not a time series.
- **Rename tracking is window-bounded.** History comes from a single
  `git log -M --since=…` pass, so a file renamed before the window opened
  appears to have no history, and a shallow clone yields whatever commits it
  has without complaint.
- **Without git, half the report is gone.** No `.git` means hotspots rank on
  size and nesting only and "Who to ask" says so explicitly. The run still
  writes both files and exits 4.
- **Not a linter, scanner, or PR reviewer.** The `config_surface` flag reports
  that a file contains credential-shaped strings and how many; it never reads,
  prints, classifies or scores them, and it is a "this repo keeps config here"
  signal, not a finding. There is no `--fail-on`, no severities, no CVE
  lookups, no diff mode, no watch mode, no HTML.

Verification on this build: 51 tests pass (`python -m unittest discover`) and
`ruff check .` is clean, on Python 3.12. The tests use a fake subprocess runner
and a fixed clock, so nothing in the suite exercises a real `git` binary, a
real `claude`, or a real repository — the behaviours listed above were checked
by hand against actual runs.

## License

MIT.
