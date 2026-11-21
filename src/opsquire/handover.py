"""A handover is the agent's word that a task is ready, and its recommendation to run it now.
`opsquire handover <task> [--apply|--verify] [--why ...] [--changed ...] [-- args]` writes it into the task's
meta.json as an annotation (outside the content hash, like `outcome`) with the code it covers, and asks record one
for every RUN line their agents end with. A task reaches the inbox, and the console's auto-run, only once it is
ready: handed over, or run, with the code its own folders hold now. An edit to them after that sends it back to
being prepared until it is handed over again, so a task an agent is still writing never waits on the operator. The
handed-over run itself shows, with the reason, until a run of the task starts after it."""

from __future__ import annotations

import datetime as dt
import os
import re
import shlex
import sys
from typing import Any

from . import ledger
from .homegit import commit_quietly
from .model import OpsquireError, Task, code_version, home_dir, iso, next_stage, now, resolve_task, write_json

RUN_LINE_RE = re.compile(r"^opsquire run (?P<task>\S+)(?P<flags>(?:\s+--\w[\w-]*)*)(?:\s+--\s+(?P<args>.*))?\s*$")


def mode_for(task: Task, apply: bool, verify: bool) -> str:
    m = task.meta
    if m is None:
        raise OpsquireError("cannot hand over a task with invalid meta.json: " + "; ".join(task.errors))
    if task.kind == "general":
        raise OpsquireError(f"{task.id} is a general script; hand over a task that uses it")
    if apply and verify:
        raise OpsquireError("--apply or --verify, not both")
    if verify:
        if not m.verify:
            raise OpsquireError(f"{task.id} has no verify check")
        return "verify"
    if apply:
        if not m.changes_prod:
            raise OpsquireError(f"{task.id} is read-only; it has no apply run")
        return "apply"
    return "dry" if m.changes_prod else "read"


def code_files(files: dict[str, str]) -> dict[str, str]:
    """The files of `file_hashes` (or a run record's `files`) that hold code: not the Markdown, which closing a task
    appends to."""
    return {name: digest for name, digest in files.items() if not name.endswith(".md")}


def own_roots(task: Task) -> tuple[str, ...]:
    """The folders the task's author writes, home-relative with a trailing slash: the task's own and a plan's steps'.
    General scripts it uses are shared, edited for other tasks too."""
    home = home_dir()
    return tuple(
        f"{r.relative_to(home).as_posix()}/"
        for r in task.roots()
        if (r == task.folder or r.parent.name != "general") and r.is_relative_to(home)
    )


def readiness(task: Task, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether the task is ready: the latest of its handover and its last run (a blocked run ran nothing) saw the code
    its own folders hold now. `shared` lists the general script files changed since then: the task stays ready, but
    nothing runs it by itself. A handover recorded before handovers carried their code counts as ready."""
    marks: list[tuple[str, Any, str]] = []
    h = task.raw.get("handover")
    if isinstance(h, dict) and isinstance(h.get("at"), str):
        marks.append((h["at"], h.get("files"), "handover"))
    ran = [r for r in runs if r.get("status") != "blocked" and r.get("started")]
    if ran:
        marks.append((str(ran[-1]["started"]), ran[-1].get("files") or {}, "run"))
    if not marks:
        return {"ready": False, "by": None, "at": None, "own": [], "shared": []}
    at, seen, by = max(marks, key=lambda m: m[0])
    if not isinstance(seen, dict):
        return {"ready": True, "by": by, "at": at, "own": [], "shared": []}
    now_code, seen_code = code_files(task.file_hashes()), code_files(seen)
    changed = sorted(n for n in now_code.keys() | seen_code.keys() if now_code.get(n) != seen_code.get(n))
    roots = own_roots(task)
    own = [n for n in changed if n.startswith(roots)]
    return {"ready": not own, "by": by, "at": at, "own": own, "shared": [n for n in changed if n not in own]}


def changed_since_run(task: Task, runs: list[dict[str, Any]]) -> list[str]:
    """Code files that differ from the task's last run; [] when it never ran."""
    ran = [r for r in runs if r.get("status") != "blocked"]
    if not ran:
        return []
    now_code, seen = code_files(task.file_hashes()), code_files(ran[-1].get("files") or {})
    return sorted(n for n in now_code.keys() | seen.keys() if now_code.get(n) != seen.get(n))


def lint_ok(task: Task) -> str | None:
    """Why the task cannot be handed over as ready, None when its lint passes on the files as they are (the cached
    result, else a fresh lint)."""
    from .lint import cached_lint_ok, lint_task, save_lint

    if cached_lint_ok(task):
        return None
    result = lint_task(task)
    save_lint(task, result)
    return None if result.ok else f"its lint fails; run `opsquire lint {task.id}` and fix it first"


def record(
    task: Task, mode: str, args: list[str], why: str, by: str, again: bool = False, changed: str = ""
) -> dict[str, Any]:
    entry: dict[str, Any] = {"at": iso(now()), "by": by, "mode": mode, "args": list(args), "why": why.strip()}
    if changed.strip():
        entry["changed"] = changed.strip()
    entry["files"] = code_files(task.file_hashes())
    if again:
        entry["again"] = True
    raw = dict(task.raw)
    raw["handover"] = entry
    write_json(task.folder / "meta.json", raw)
    ledger.append("handover", {"task": task.id, "handover": entry})
    commit_quietly(home_dir(), [task.folder], f"handover: {task.id}")
    return entry


def hold(task: Task, why: str, by: str, until: dt.datetime | None = None) -> dict[str, Any]:
    """Keep the task, and a plan's steps, out of the inbox until it is handed over or `until` passes.

    A handover made before the hold is kept: it is what the inbox shows once the hold ends."""
    entry: dict[str, Any] = {"at": iso(now()), "by": by, "why": why.strip()}
    if until:
        entry["until"] = iso(until)
    raw = dict(task.raw)
    raw["hold"] = entry
    write_json(task.folder / "meta.json", raw)
    ledger.append("hold", {"task": task.id, "hold": entry})
    return entry


def release(task: Task) -> bool:
    if "hold" not in task.raw:
        return False
    write_json(task.folder / "meta.json", {k: v for k, v in task.raw.items() if k != "hold"})
    ledger.append("release", {"task": task.id})
    return True


def held(task: Task, at: dt.datetime) -> dict[str, Any] | None:
    """The task's hold while it lasts: not handed over since, and before its `until`."""
    h = task.raw.get("hold")
    if not isinstance(h, dict) or not isinstance(h.get("at"), str):
        return None
    handed = task.raw.get("handover")
    if isinstance(handed, dict) and str(handed.get("at") or "") > h["at"]:
        return None
    until = h.get("until")
    if isinstance(until, str) and until <= iso(at):
        return None
    return h


def withdraw(task: Task) -> bool:
    """Take the task's handover out of the inbox; False when there was none."""
    if "handover" not in task.raw:
        return False
    raw = {k: v for k, v in task.raw.items() if k != "handover"}
    write_json(task.folder / "meta.json", raw)
    ledger.append("withdraw", {"task": task.id})
    commit_quietly(home_dir(), [task.folder], f"handover withdrawn: {task.id}")
    return True


def same_apply(task: Task, runs: list[dict[str, Any]], args: list[str]) -> dict[str, Any] | None:
    """The latest apply, finished ok or still running, with these arguments and the task's current scripts: running
    it again changes production a second time for no new reason. Interrupted, failed and aborted applies do not
    count."""
    applies = [
        r
        for r in runs
        if r.get("mode") == "apply" and r.get("status") in ("ok", "running") and list(r.get("argv") or []) == list(args)
    ]
    if not applies:
        return None
    current = code_version(task.file_hashes())
    return next((r for r in reversed(applies) if code_version(r.get("files") or {}) == current), None)


def covering_dry(task: Task, runs: list[dict[str, Any]], args: list[str]) -> tuple[dict[str, Any] | None, str]:
    """The dry run that covers an apply now: finished ok with these arguments and the task's current scripts. Without
    one, why: an apply of scripts no dry run has seen is a first try on production."""
    current = code_version(task.file_hashes())
    dries = [r for r in runs if r.get("mode") == "dry"]
    same_args = [r for r in dries if list(r.get("argv") or []) == list(args)]
    same = [r for r in same_args if code_version(r.get("files") or {}) == current]
    ok = next((r for r in reversed(same) if r.get("status") == "ok"), None)
    if ok:
        return ok, ""
    if same:
        return None, f"its dry run {same[-1].get('run_id')} of these scripts ended {same[-1].get('status')}"
    if same_args:
        return None, f"the scripts changed since its last dry run, {same_args[-1].get('run_id')}"
    if dries:
        return None, f"its last dry run, {dries[-1].get('run_id')}, had other arguments"
    return None, "it never had a dry run"


def needs_dry(task: Task, runs: list[dict[str, Any]], args: list[str]) -> str | None:
    """Why an apply of this task cannot be handed over yet, None when a dry run covers it. A plan's apply is checked
    for its pre-run when it runs, and a staged task's apply with no arguments makes each stage's dry run itself."""
    if task.kind == "plan" or (task.meta and task.meta.stages and not args):
        return None
    covered, why = covering_dry(task, runs, args)
    if covered:
        return None
    return f"no dry run covers this apply ({why}); the dry run comes first: `{command(task.id, 'dry', args)}`"


def applied_text(run: dict[str, Any]) -> str:
    verb = "is running" if run.get("status") == "running" else "finished ok"
    return f"apply {run.get('run_id')}, started {run.get('started')}, {verb} with the same scripts and arguments"


def command(task_id: str, mode: str, args: list[str]) -> str:
    flag = {"apply": " --apply", "verify": " --verify"}.get(mode, "")
    return f"opsquire run {task_id}{flag}" + (" -- " + shlex.join(args) if args else "")


def pending(task: Task, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The task's handover while no run of it has started since; runs are the task's run records. A blocked run
    started nothing, so the handover outlives it."""
    h = task.raw.get("handover")
    if not isinstance(h, dict) or not isinstance(h.get("at"), str):
        return None
    if any(str(r.get("started") or "") >= str(h["at"]) for r in runs if r.get("status") != "blocked"):
        return None
    return h


def args_for(task: Task, runs: list[dict[str, Any]], mode: str) -> list[str]:
    """What a run in this mode takes from the inbox, and the console runs: the arguments of the handed-over run it
    shows; for a staged task's apply nobody handed arguments to, none, so it walks every stage left with each stage's
    own dry run; for any other apply nobody handed over, those of the dry run it follows (an apply is covered only by
    a dry run with its arguments); a staged task's dry run is of its next stage; a verify check those of the apply
    it checks (a staged task's none: its arguments name stages). Any other run of the task has none."""
    h = pending(task, runs)
    if h:
        return [str(x) for x in h.get("args") or []] if h.get("mode") == mode else []
    if mode in ("apply", "verify") and task.meta and task.meta.stages:
        return []
    ran = [r for r in runs if r.get("status") != "blocked"]
    if mode == "verify":
        applied = [r for r in ran if r.get("mode") == "apply"]
        return [str(x) for x in applied[-1].get("argv") or []] if applied else []
    if mode == "apply" and ran and ran[-1].get("mode") == "dry":
        return [str(x) for x in ran[-1].get("argv") or []]
    stage = next_stage(task.meta, ran) if task.meta and mode == "dry" else None
    return [stage] if stage else []


def record_run_lines(actions: list[tuple[str, str]], why: str, by: str) -> list[str]:
    """Record a handover for every `RUN: opsquire run ...` line an agent ended with; lines that are not such a
    command, or name no task, are left alone (they are the agent's text, not a contract). An apply that already ran
    the same way or that no dry run covers, or a task whose lint fails, stays out of the inbox and is reported. A
    `CHANGED:` line says what changed in the code, for every handover it records."""
    from .query import runs_for

    changed = next((text for kind, text in actions if kind == "CHANGED"), "")
    handed: list[str] = []
    for kind, text in actions:
        m = RUN_LINE_RE.match(text.strip()) if kind == "RUN" else None
        if not m:
            continue
        try:
            task = resolve_task(m.group("task"))
            flags = set((m.group("flags") or "").split())
            mode = mode_for(task, "--apply" in flags, "--verify" in flags)
            args = shlex.split(m.group("args") or "")
        except (OpsquireError, ValueError):
            continue
        if task.id in handed:
            continue
        runs = runs_for(task.id) if mode == "apply" else []
        same = same_apply(task, runs, args) if mode == "apply" else None
        if same:
            print(f"opsquire: {task.id} not put in the inbox: {applied_text(same)}", file=sys.stderr)
            continue
        uncovered = needs_dry(task, runs, args) if mode == "apply" else None
        if uncovered:
            print(f"opsquire: {task.id} not put in the inbox: {uncovered}", file=sys.stderr)
            continue
        unready = lint_ok(task)
        if unready:
            print(f"opsquire: {task.id} not put in the inbox: {unready}", file=sys.stderr)
            continue
        record(task, mode, args, why, by, changed=changed)
        handed.append(task.id)
    return handed


def author() -> str:
    return os.environ.get("OPSQUIRE_AUTHOR", "unknown")
