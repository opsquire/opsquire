"""`opsquire judge <task>`: an agent reads the task's record (purpose, design, runs, the last output) and decides
how it ended: done, partial or abandoned with a note, written with `close` and marked as the judge's, so the operator
ticks nothing off by hand and can still amend it. When the record does not say, it keeps the task open, naming the
read-only check that would settle it (verify) or what is missing (keep), written to meta.json `judged`."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ledger, store
from .ask import Agents, Budget, Conversation, agent_env, model_for, pick_agent
from .close import close_task
from .model import OpsquireError, home_dir, iso, now, resolve_task, write_json
from .query import runs_for
from .state import state

CLOSE_RE = re.compile(r"^CLOSE:\s*(done|partial|abandoned)\s*\|\s*(.+?)\s*$", re.M)
KEEP_RE = re.compile(r"^KEEP:\s*(.+?)\s*$", re.M)
VERIFY_RE = re.compile(r"^VERIFY:\s*(.+?)\s*$", re.M)
PR_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
JUDGE_BUDGET_USD = 2.0
RECORDING = threading.Lock()
DUE_JOBS = 4
DUE_MAX = 10
LOG_TAIL_LINES = 80

SYSTEM = """You judge how an operations task ended, from its record only. You change nothing and run nothing.
Decide from the evidence: the purpose says what was wanted; the runs, their target statuses, results, errors and
the last output say what happened; the design says what was decided. Rules:
- done: the purpose was achieved on every target (an ok apply, or later evidence that the hosts ended up right).
- partial: some of it was achieved and the rest will not be done by this task.
- abandoned: nothing of it will happen through this task (superseded, not needed any more, given up).
- verify: the record does not say, but a read-only check on the targets would (the task's verify check, or one
  to be written): say exactly what the check must establish.
- keep: neither the record nor a check can settle it; name what is missing.
Pull requests linked from the task are listed with their state on GitHub (merged, open, closed): merged means the
code or config landed in the repository, not that hosts run it; the runs and the verify check say that.
End your answer with exactly one line, nothing after it:
CLOSE: done|partial|abandoned | <one or two sentences: what actually ended up live, with the run id as evidence>
or
VERIFY: <what a read-only check on the targets must establish to decide>
or
KEEP: <what is missing to decide>"""


def brief(task_id: str) -> str:
    task = resolve_task(task_id)
    m = task.meta
    if m is None:
        raise OpsquireError(f"{task.id} has invalid meta.json: " + "; ".join(task.errors))
    lines = [f"Task {task.id} (area {m.area}, {'changes production' if m.changes_prod else 'read-only'})"]
    lines.append(f"Purpose: {m.purpose}")
    lines.append(f"Targets: {', '.join(m.targets)}; created {m.created} by {m.author}; today is {iso(now())}")
    if m.superseded_by:
        lines.append(f"Marked superseded by {m.superseded_by}")
    lines.append(
        f"Verify check: {m.verify} (run it with `opsquire run {task.id} --verify`)"
        if m.verify
        else "Verify check: none"
    )
    for link in m.links:
        lines.append(f"Link: {link}" + (f" -> {pr_state(link)}" if PR_RE.match(link) else ""))
    if m.outcome:
        lines.append(
            f"Already closed {m.outcome.get('state')} by {m.outcome.get('closed_by')}: {m.outcome.get('note')}"
        )
    design = task.folder / "design.md"
    if design.is_file():
        lines += ["", "design.md:", design.read_text()[:6000]]
    runs = runs_for(task.id)
    lines += ["", f"Runs ({len(runs)}), oldest first:"]
    for r in runs[-12:]:
        targets = ", ".join(
            f"{t}={v.get('status') if isinstance(v, dict) else v}" for t, v in (r.get("targets") or {}).items()
        )
        lines.append(
            f"- {r.get('run_id')} {r.get('mode')} {r.get('status')} ({r.get('duration_s')} s) targets: {targets}"
            + (f" error: {r.get('last_error')}" if r.get("last_error") else "")
            + (f" results: {' | '.join(str(x) for x in r.get('results') or [])[:600]}" if r.get("results") else "")
        )
    if runs:
        last = runs[-1]
        try:
            text = store.read_log(Path(str(last["paths"]["dir"])), "output.log").decode("utf-8", "replace")
            tail = text.splitlines()[-LOG_TAIL_LINES:]
            lines += ["", f"Last {len(tail)} lines of the last run's output ({last.get('run_id')}):", *tail]
        except (OSError, KeyError):
            pass
    return "\n".join(lines)


def pr_state(url: str) -> str:
    """What GitHub says about a linked pull request, through the operator's `gh` login; one line, or why not."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state,mergedAt,mergeCommit,headRefName,baseRefName,title"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"state unknown ({type(e).__name__})"
    if r.returncode:
        return "state unknown (" + (r.stderr.strip().splitlines() or ["gh failed"])[-1][:120] + ")"
    try:
        pr = json.loads(r.stdout)
    except ValueError:
        return "state unknown (bad gh output)"
    merged = pr.get("mergedAt")
    commit = (pr.get("mergeCommit") or {}).get("oid", "")[:10]
    state = f"merged {merged} as {commit}" if merged else str(pr.get("state", "?")).lower()
    return f"{state}; {pr.get('headRefName')} -> {pr.get('baseRefName')}; {pr.get('title')}"


def judge(task_id: str, agent_name: str | None = None, tier: str = "") -> dict[str, Any]:
    """Ask the agent (on the retinue tier given, else the agent's own default model); close the task when it
    decides, or report why it kept it open."""
    name, agent = pick_agent(agent_name)
    task = resolve_task(task_id)
    text = brief(task.id)
    env = agent_env(name, f"judge-{task.id}", {"OPSQUIRE_AUTHOR": f"opsquire judge ({name})"})
    model = model_for(tier, agent) if tier else ""
    conv = Conversation(
        name, agent, str(home_dir()), SYSTEM, env, label=f"[judge {task.id}]", model=model, boundary_check=False
    )
    res = conv.turn(text, Budget(JUDGE_BUDGET_USD, True))
    if res.exit_code:
        raise OpsquireError(f"the judge agent failed (exit {res.exit_code}): {res.text[-300:]}")
    with RECORDING:
        return record(task.id, name, res.text, conv.cost)


def record(task_id: str, name: str, text: str, cost: float) -> dict[str, Any]:
    """Write the judge's decision; one at a time, so concurrent judges do not race for the home's git index."""
    task = resolve_task(task_id)
    close = CLOSE_RE.search(text)
    if close:
        os.environ["OPSQUIRE_AUTHOR"] = f"opsquire judge ({name})"
        closed = close_task(task.id, close.group(1), close.group(2))
        return {"id": task.id, "decision": close.group(1), "note": close.group(2), "cost_usd": cost, **closed}
    keep = KEEP_RE.search(text)
    verify = VERIFY_RE.search(text)
    if verify:
        decision, note = "verify", verify.group(1)
        if not (task.meta and task.meta.verify):
            note = f"no verify check yet; an agent must add one that establishes: {note}"
    else:
        decision, note = "keep", keep.group(1) if keep else "the judge gave no decision line"
    raw = dict(task.raw)
    raw["judged"] = {"decision": decision, "note": note, "by": f"opsquire judge ({name})", "when": iso(now())}
    write_json(task.folder / "meta.json", raw)
    ledger.append("judge", {"task": task.id, "judged": raw["judged"]})
    return {"id": task.id, "decision": decision, "note": note, "cost_usd": cost, "text": text}


def due(limit: int) -> list[str]:
    """The tasks that need a decision, newest first, at most `limit`."""
    return sorted((tid for tid, v in state()["tasks"].items() if v["decide"]), reverse=True)[:limit]


def judge_all(
    ids: list[str],
    jobs: int,
    report: Callable[[dict[str, Any]], None],
    agent_name: str | None = None,
    tier: str = "",
) -> list[dict[str, Any]]:
    """Judge every task, `jobs` at a time; a task that fails is reported as `failed` and the rest go on. Each result
    reaches `report` as it comes, on this thread. Ctrl-C stops every agent, waits for the workers, and re-raises."""
    todo: queue.Queue[str] = queue.Queue()
    for tid in ids:
        todo.put(tid)
    done: queue.Queue[dict[str, Any]] = queue.Queue()
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            try:
                tid = todo.get_nowait()
            except queue.Empty:
                return
            try:
                done.put(judge(tid, agent_name, tier))
            except Exception as e:
                done.put({"id": tid, "decision": "failed", "note": str(e), "cost_usd": 0.0})

    workers = [threading.Thread(target=work, daemon=True) for _ in range(min(jobs, len(ids)))]
    for w in workers:
        w.start()
    results: list[dict[str, Any]] = []
    try:
        while any(w.is_alive() for w in workers) or not done.empty():
            try:
                results.append(done.get(timeout=0.2))
            except queue.Empty:
                continue
            report(results[-1])
    except KeyboardInterrupt:
        stop.set()
        while any(w.is_alive() for w in workers):
            try:
                Agents.stop_all()
                for w in workers:
                    w.join(0.2)
            except KeyboardInterrupt:
                continue
        raise
    return results
