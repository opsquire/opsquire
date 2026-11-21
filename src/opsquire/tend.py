"""Library upkeep: how much each general script is used, which code tasks keep copying instead of sharing, which
scripts still load shared code by hand, and which scripts nothing uses any more. `opsquire tend` reports it from the
files and run records alone, for free. Fixing is an agent's job, started only by the operator (`opsquire tend --fix`
or the console's Library tab): it goes through `opsquire ask` under a cost cap, and since every task whose scripts
change needs a fresh dry run before its next apply, nothing it changes reaches a live system unseen. Unused scripts
are only reported: general scripts never leave on their own (see offboard)."""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import (
    Task,
    all_task_folders,
    iso,
    load_task,
    now,
    offboarded_folders,
    parse_iso,
    read_json,
    var_dir,
    write_json,
)
from .query import runs_for, task_status
from .state import CLOSED

COPY_MIN_LINES = 6
COPY_MIN_FOLDERS = 3
UNUSED_DAYS = 30
FIX_BUDGET_USD = 10.0
UPKEEP = "Library upkeep of this home's general scripts"
FIX_MAX_AGENTS = 4
HAND_IMPORT = re.compile(r"spec_from_file_location|sys\.path\.(?:insert|append)")


@dataclass
class Copy:
    """One function body found, statement for statement, in several folders."""

    names: list[str]
    lines: int
    folders: list[str]
    generals: list[str] = field(default_factory=list)
    open: list[str] = field(default_factory=list)
    fixable: bool = True


def cache_path() -> Path:
    return var_dir() / "tend-cache.json"


def is_open(task: Task, status: str) -> bool:
    """Code that still runs: every general script, and tasks whose end is not recorded."""
    return task.kind == "general" or status not in CLOSED


def scripts(task: Task) -> list[Path]:
    """The task's own Python scripts; files() also lists those of the general scripts it uses."""
    return [
        p
        for p in task.files()
        if p.is_relative_to(task.folder) and p.suffix == ".py" and not p.name.startswith("test_")
    ]


def bodies(path: Path, old: dict[str, Any], used: dict[str, Any]) -> list[tuple[str, str, int]]:
    """(name, fingerprint, lines) of every function of at least COPY_MIN_LINES lines; the fingerprint ignores a
    docstring, comments and layout, nothing else. Loading shared code by hand is reported apart, so it is left out.
    Cached by the file's content (`old` as read, `used` what this report needs), as parsing a home takes seconds."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest not in used:
        hit = old.get(digest)
        used[digest] = hit if isinstance(hit, list) else [list(b) for b in parse_bodies(text)]
    return [(str(n), str(k), int(n_lines)) for n, k, n_lines in used[digest]]


def parse_bodies(text: str) -> list[tuple[str, str, int]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        lines = (node.end_lineno or node.lineno) - node.lineno + 1
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            body = body[1:]
        if lines < COPY_MIN_LINES or not body or HAND_IMPORT.search(ast.get_source_segment(text, node) or ""):
            continue
        dump = ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)
        out.append((node.name, hashlib.sha256(dump.encode()).hexdigest(), lines))
    return out


def copies(tasks: list[tuple[Task, str]], old: dict[str, Any], used: dict[str, Any]) -> list[Copy]:
    """Function bodies copied into COPY_MIN_FOLDERS or more folders, the most copied lines first. Settled tasks count
    too: what agents copied there, they copy again next time, so it belongs in a library. A library's own copy drifts
    (typing, tidying), so a general script defining a function of the same name counts as its home too (named when it
    is the only one). It is left for an agent only while no general script holds it, two hold it statement for
    statement, or an open folder outside them keeps a copy: settled tasks stay as they are."""
    found: dict[str, list[tuple[str, str, int, Task, bool]]] = defaultdict(list)
    for task, status in tasks:
        for path in scripts(task):
            for name, key, lines in bodies(path, old, used):
                found[key].append((name, task.id, lines, task, is_open(task, status)))
    homes: dict[str, set[str]] = defaultdict(set)
    for hits in found.values():
        for name, tid, _, task, _ in hits:
            if task.kind == "general" and name != "main":
                homes[name].add(tid)
    out = []
    for hits in found.values():
        folders = sorted({h[1] for h in hits})
        if len(folders) < COPY_MIN_FOLDERS:
            continue
        names = sorted({h[0] for h in hits})
        same = sorted({h[1] for h in hits if h[3].kind == "general"})
        named = sorted({g for n in names for g in homes[n]})
        generals = same or (named if len(named) == 1 else [])
        open_ = sorted({h[1] for h in hits if h[4]})
        fixable = len(same) > 1 or not (same or named) or any(f not in same + named for f in open_)
        out.append(Copy(names, max(h[2] for h in hits), folders, generals, open_, fixable))
    return sorted(out, key=lambda c: (-len(c.folders) * c.lines, c.names))


def hand_imports(tasks: list[tuple[Task, str]]) -> list[dict[str, Any]]:
    """Open code that loads a general script by hand instead of with sq.use."""
    out = []
    for task, status in tasks:
        if not is_open(task, status):
            continue
        files = [
            p.relative_to(task.folder).as_posix()
            for p in scripts(task)
            if "OPSQUIRE_GENERAL" in (text := p.read_text(errors="replace")) and HAND_IMPORT.search(text)
        ]
        if files:
            out.append({"id": task.id, "kind": task.kind, "files": files})
    return out


def usage(
    tasks: list[tuple[Task, str]], gone: list[Task], at: dt.datetime, runs: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Per general script: who uses it (open users, all users including offboarded tasks), how often it ran in the
    last UNUSED_DAYS days, alone or in a task using it, how its applies went, and when it last ran."""
    every = [t for t, _ in tasks] + gone
    open_ids = {t.id for t, s in tasks if is_open(t, s)}
    since = at - dt.timedelta(days=UNUSED_DAYS)
    rows = []
    for g, _ in tasks:
        if g.kind != "general":
            continue
        users = [t for t in every if t.id != g.id and g.id in (t.raw.get("uses") or [])]
        mine = [r for t in [g, *users] for r in runs[t.id]]
        stamps = sorted(str(r.get("started")) for r in mine if r.get("started"))
        recent = [r for r in mine if r.get("started") and parse_iso(str(r["started"])) >= since]
        applies = [r for r in mine if r.get("mode") == "apply"]
        rows.append(
            {
                "id": g.id,
                "purpose": str(g.raw.get("purpose") or ""),
                "users_open": sorted(t.id for t in users if t.id in open_ids),
                "users": len(users),
                "runs_recent": len(recent),
                "applies": len(applies),
                "applies_ok": len([r for r in applies if r.get("status") == "ok"]),
                "last_used": stamps[-1] if stamps else None,
                "created": str(g.raw.get("created") or ""),
                "tested": any(p.parent == g.folder and p.name.startswith("test_") for p in g.files()),
            }
        )
    return sorted(rows, key=lambda r: (-r["runs_recent"], -r["users"], r["id"]))


def unused(rows: list[dict[str, Any]], at: dt.datetime) -> list[str]:
    """General scripts no open task uses and nothing ran for UNUSED_DAYS days (counting from creation when it never
    ran): candidates for `opsquire offboard <script>`."""
    since = at - dt.timedelta(days=UNUSED_DAYS)
    out = []
    for r in rows:
        stamp = r["last_used"] or r["created"]
        try:
            idle = parse_iso(stamp) < since
        except ValueError:
            idle = False
        if not r["users_open"] and idle:
            out.append(r["id"])
    return out


def report(at: dt.datetime | None = None) -> dict[str, Any]:
    at = at or now()
    tasks = []
    runs = {}
    for folder in all_task_folders():
        task = load_task(folder)
        runs[task.id] = runs_for(task.id)
        tasks.append((task, task_status(task, runs[task.id])))
    gone = [load_task(f) for f in offboarded_folders()]
    rows = usage(tasks, gone, at, runs | {t.id: runs_for(t.id) for t in gone})
    try:
        old = read_json(cache_path())
    except (OSError, ValueError):
        old = {}
    used: dict[str, Any] = {}
    found = copies(tasks, old if isinstance(old, dict) else {}, used)
    with contextlib.suppress(OSError):
        cache_path().parent.mkdir(parents=True, exist_ok=True)
        write_json(cache_path(), used)
    return {
        "generated": iso(at),
        "usage": rows,
        "copies": [c.__dict__ for c in found],
        "hand_imports": hand_imports(tasks),
        "unused": unused(rows, at),
        "untested": [r["id"] for r in rows if not r["tested"]],
    }


def fixable(rep: dict[str, Any]) -> int:
    return len([c for c in rep["copies"] if c.get("fixable", True)]) + len(rep["hand_imports"]) + len(rep["untested"])


def unfinished_upkeep() -> dict[str, Any] | None:
    """The latest upkeep ask when it stopped short (parked pieces, interrupted, at a usage limit): a new one would
    redo its work, as the findings it fixes stay until it lands."""
    from .ask import ask_records

    latest = next((r for r in reversed(ask_records()) if str(r.get("prompt", "")).startswith(UPKEEP)), None)
    return latest if latest and latest.get("status") in ("waiting", "interrupted", "limited") else None


def brief(rep: dict[str, Any]) -> str:
    """The request `tend --fix` hands to `opsquire ask`: what to change, and the rules that keep it a refactor."""
    parts = [
        UPKEEP + " (from `opsquire tend`). Change how code is shared, never what"
        " it does. Settled: findings that touch the same general script go into one piece; edit only general scripts"
        " and tasks whose end is not recorded (settled tasks stay as they are); keep every changed script's dry run"
        " output the same; add or extend tests for what you move; `opsquire lint` must pass on every script you touch"
        " and on each open task using it. Hand over one RUN line per open task whose scripts changed, its dry or read"
        " run, and a CHANGED line saying what moved.",
    ]
    for c in (c for c in rep["copies"] if c.get("fixable", True)):
        where = ", ".join(c["folders"][:8]) + (f" and {len(c['folders']) - 8} more" if len(c["folders"]) > 8 else "")
        if c["generals"]:
            what = (
                f"{'/'.join(c['names'])} ({c['lines']} lines) is defined in {', '.join(c['generals'])} and copied"
                f" into {where}. Make the open copies ({', '.join(c['open']) or 'none'}) call it via sq.use."
            )
        else:
            what = (
                f"{'/'.join(c['names'])} ({c['lines']} lines) is copied into {where}. Put it in a library general"
                f" script (a general whose main.py holds functions, with tests), and make the open copies"
                f" ({', '.join(c['open']) or 'none'}) use it via sq.use."
            )
        parts.append("- " + what)
    for h in rep["hand_imports"]:
        parts.append(
            f"- {h['id']} loads general scripts by hand (importlib or sys.path) in {', '.join(h['files'])}:"
            " replace that with sq.use."
        )
    for g in rep["untested"]:
        parts.append(f"- {g} has no tests: add test_main.py covering its decisions without touching any host.")
    return "\n".join(parts)


def ago(stamp: str | None, at: dt.datetime) -> str:
    if not stamp:
        return "never"
    hours = (at - parse_iso(stamp)).total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def text(rep: dict[str, Any]) -> str:
    at = parse_iso(rep["generated"])
    rows = rep["usage"]
    out = [f"General scripts ({len(rows)}), runs in the last {UNUSED_DAYS} days, alone or in a task using them:"]
    out.append(f"  {'runs':>5}  {'applies ok':>10}  {'users open/all':>14}  {'last used':>9}  script")
    for r in rows:
        applies = f"{r['applies_ok']}/{r['applies']}" if r["applies"] else "-"
        users = f"{len(r['users_open'])}/{r['users']}"
        out.append(
            f"  {r['runs_recent']:>5}  {applies:>10}  {users:>14}  {ago(r['last_used'], at):>9}  {r['id']}"
            + ("  (no tests)" if not r["tested"] else "")
        )
    out.append(f"\nCopied code ({len(rep['copies'])}): the same function in {COPY_MIN_FOLDERS}+ folders")
    for c in rep["copies"]:
        home = f"; defined in {', '.join(c['generals'])}" if c["generals"] else ""
        left = "" if c.get("fixable", True) else " (only settled tasks keep their copy)"
        out.append(f"  {'/'.join(c['names'])} ({c['lines']} lines) in {len(c['folders'])} folders{home}{left}")
    out.append(f"\nLoading general scripts by hand instead of sq.use ({len(rep['hand_imports'])}):")
    out += [f"  {h['id']}: {', '.join(h['files'])}" for h in rep["hand_imports"]]
    out.append(f"\nUnused for {UNUSED_DAYS}+ days, no open task uses them ({len(rep['unused'])}):")
    out += [f"  {g}   (opsquire offboard {g})" for g in rep["unused"]]
    n = fixable(rep)
    out.append(
        f"\n{n} findings an agent can fix: opsquire tend --fix (at most ${FIX_BUDGET_USD:g})"
        if n
        else "\nNothing for an agent to fix."
    )
    return "\n".join(out)
