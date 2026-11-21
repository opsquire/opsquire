"""Which run records each agent has read. An agent reads a record through `wait`, `log`, `runs <task>`, `last` or
`show`, or by running it itself; `handover` and `new` refuse an agent that has not read the task's latest run or
the results of the runs it handed over, and the stop hook keeps a session from ending its turn on them, and reminds
it of tasks it wrote and left unhanded. The ledgers, and which unhanded edits each session heard about, live in the
home, outside `var/`, which agents cannot write."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import handover, homegit
from .model import (
    Task,
    all_task_folders,
    author_key,
    load_task,
    main_home,
    names_session,
    now,
    read_json,
    var_dir,
    write_json,
)
from .query import all_runs, runs_for

FINISHED = ("ok", "failed", "partial", "aborted", "interrupted", "blocked")


def ledger(key: str) -> Path:
    return main_home() / homegit.READ_LEDGERS / f"{key.replace(' ', '_')}.json"


def legacy_ledger(key: str) -> Path:
    return var_dir() / "reads" / f"{key.replace(' ', '_')}.json"


def load(path: Path) -> set[str]:
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return set()
    return {str(r) for r in data} if isinstance(data, list) else set()


def read_ids(author: str) -> set[str]:
    key = author_key(author)
    if not key:
        return set()
    return load(ledger(key)) | load(legacy_ledger(key))


def mark(author: str, run_ids: Iterable[str]) -> None:
    key = author_key(author)
    ids = {str(r) for r in run_ids if r}
    if not key or not ids:
        return
    have = read_ids(author)
    if ids <= have:
        return
    path = ledger(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, sorted(have | ids))
    except OSError as e:
        print(
            f"opsquire: could not record that {author} read {', '.join(sorted(ids - have))} "
            f"({path}: {e.strerror or e}); `handover` and `new` will refuse as if it were unread",
            file=sys.stderr,
        )


def mark_records(author: str, recs: Iterable[dict[str, Any]]) -> None:
    mark(author, (str(r.get("run_id")) for r in recs if r.get("status") in FINISHED))


def unread_results(author: str) -> list[dict[str, Any]]:
    """Finished runs this author handed over and has not read."""
    key = author_key(author)
    if not key:
        return []
    seen = read_ids(author)
    return [
        r
        for r in all_runs()
        if r.get("status") in FINISHED
        and author_key(str(r.get("handed_by") or "")) == key
        and str(r.get("run_id")) not in seen
    ]


def unread_latest(task_id: str, author: str) -> dict[str, Any] | None:
    """The task's latest finished run when this author has not read it."""
    if not author_key(author):
        return None
    done = [r for r in runs_for(task_id) if r.get("status") in FINISHED]
    if done and str(done[-1].get("run_id")) not in read_ids(author):
        return done[-1]
    return None


def describe(recs: list[dict[str, Any]]) -> str:
    return "; ".join(f"{r.get('run_id')} of {r.get('task_id')} ({r.get('mode')} {r.get('status')})" for r in recs)


def session_unread(session_id: str) -> list[dict[str, Any]]:
    """Unread results of the agents whose author names this Claude session (its id or the start of it)."""
    out = []
    seen_by: dict[str, set[str]] = {}
    for r in all_runs():
        by = str(r.get("handed_by") or "")
        key = author_key(by)
        if r.get("status") not in FINISHED or not names_session(by, session_id):
            continue
        if key not in seen_by:
            seen_by[key] = read_ids(by)
        if str(r.get("run_id")) not in seen_by[key]:
            out.append(r)
    return out


def unhanded(session_id: str) -> list[tuple[Task, list[str]]]:
    """Tasks this Claude session wrote (it created them or handed them over last) whose own code no handover or run
    has seen: each with its files changed since, [] when it was never handed over nor run. Closed, superseded,
    archived and held tasks are left out, and so are general scripts and a plan's steps, which go with their plan."""
    tasks = [load_task(f) for f in all_task_folders()]
    steps = {str(step.get("task")) for t in tasks if t.meta for step in t.meta.steps or []}
    out = []
    for t in tasks:
        m = t.meta
        if m is None or t.kind == "general" or t.id in steps:
            continue
        if m.outcome or m.superseded_by or m.abandoned_reason or m.archived or handover.held(t, now()):
            continue
        last = t.raw.get("handover")
        by = [m.author, str(last.get("by") or "") if isinstance(last, dict) else ""]
        if not any(names_session(b, session_id) for b in by):
            continue
        ready = handover.readiness(t, runs_for(t.id))
        if not ready["ready"]:
            out.append((t, ready["own"]))
    return out


def reminded_file(session_id: str) -> Path:
    return main_home() / homegit.READ_LEDGERS / f"session-{re.sub(r'[^0-9a-z-]', '', session_id.lower())[:64]}.json"


def unhanded_news(session_id: str) -> list[tuple[Task, list[str]]]:
    """The session's unhanded tasks it was not yet reminded of as they are now, recorded as reminded: an agent waiting
    on the operator mid-task hears once per edit, not at every stop."""
    found = unhanded(session_id)
    if not found:
        return []
    path = reminded_file(session_id)
    try:
        seen = read_json(path)
    except (OSError, ValueError):
        seen = {}
    seen = seen if isinstance(seen, dict) else {}
    now_hash = {t.id: t.content_hash() for t, _ in found}
    news = [(t, own) for t, own in found if seen.get(t.id) != now_hash[t.id]]
    if news:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, {**seen, **{t.id: now_hash[t.id] for t, _ in news}})
        except OSError:
            pass
    return news


def stop_hook(stdin: str) -> dict[str, Any] | None:
    """The Claude Code Stop hook's answer: block once per stop attempt while the session has unread results, and once
    per edit while a task it wrote is neither handed over, held nor closed."""
    try:
        event = json.loads(stdin or "{}")
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("stop_hook_active"):
        return None
    session = str(event.get("session_id") or "")
    if not session:
        return None
    reasons = []
    unread = session_unread(session)
    if unread:
        first = unread[0]
        reasons.append(
            f"opsquire: runs you handed over finished and you have not read them: {describe(unread)}. Read each "
            f"(`opsquire wait {first.get('task_id')} --json` or `opsquire log {first.get('run_id')}`) and act on it "
            "before ending your turn."
        )
    news = unhanded_news(session)
    if news:
        listed = "; ".join(
            f"{t.id} ({', '.join(own) + ' changed since its last handover or run' if own else 'never handed over'})"
            for t, own in news
        )
        reasons.append(
            f"opsquire: tasks you wrote are not handed over: {listed}. Hand each over once it is ready (`opsquire "
            "handover <task> --why ...`), hold it while it waits on something (`opsquire hold <task> --why ...`), or "
            "close one you dropped (`opsquire close <task> --outcome abandoned --note ...`). If you are still writing "
            "it, say so and end your turn again."
        )
    return {"decision": "block", "reason": " ".join(reasons)} if reasons else None
