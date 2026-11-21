"""A schedule is a plan seen as one unit. Its pre-run is the plan's dry run: every step that changes production runs dry
and every reading step reads, and each step's run keeps a snapshot of the exact files it ran. The apply runs each step
from that snapshot, so scripts edited after the pre-run wait for the next one instead of reaching production unseen. An
apply that stops leaves its finished steps done: the next run of the plan resumes after them. That holds for an apply
killed outright too: its record keeps the steps it finished, and the step it was in counts as done only when that step's
own run (and its verify check) ended ok; otherwise it was interrupted mid-run and runs again after a fresh pre-run.
Whatever its steps would owe alone, the group owes the operator one run: the pre-run, the confirmed apply, or the steps'
verify checks."""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from .model import Task, content_digest, home_dir, load_task, read_json, runs_dir, task_folder
from .runner import load_run

SNAPSHOT = "snapshot"


def specs(plan: Task) -> list[dict[str, Any]]:
    return list(plan.meta.steps or []) if plan.meta else []


def step_args(step: dict[str, Any]) -> list[str]:
    """The arguments a plan's step record ran with; records from before they were kept say so in the step's own run."""
    if "args" in step:
        return [str(x) for x in step.get("args") or []]
    folder = step.get("dir")
    if folder:
        try:
            return [str(x) for x in load_run(Path(folder) / "run.json").get("argv") or []]
        except (OSError, ValueError):
            pass
    return []


def finished(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in runs if r.get("status") != "running"]


def ran_a_step(run: dict[str, Any]) -> bool:
    """Whether a plan's run started any step itself: one refused before its first step (a lock, a missing pre-run)
    changed nothing, so it neither stops a schedule nor uses its pre-run up. A run killed in a step still names it as
    current."""
    return bool(run.get("current")) or any(s.get("run_id") and not s.get("done_in") for s in run.get("steps") or [])


def applies(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in finished(runs) if r.get("mode") == "apply" and ran_a_step(r)]


def same_step(spec: dict[str, Any], step: dict[str, Any]) -> bool:
    return str(step.get("task")) == str(spec.get("task")) and step_args(step) == [str(x) for x in spec.get("args", [])]


def step_runs(task_id: str, plan_run: str) -> list[dict[str, Any]]:
    """The runs a plan's run started for one of its steps, by their own records: the step's run, then its verify."""
    out = []
    for path in sorted((runs_dir() / task_id).glob("*/run.json")):
        try:
            rec = load_run(path)
        except (OSError, ValueError):
            continue
        if rec.get("plan_run") == plan_run:
            out.append(rec)
    return out


def checks_after_apply(rec: dict[str, Any]) -> bool | None:
    """Whether the step this apply ran has a verify check, by the meta.json in the apply's snapshot; None when that
    cannot be read."""
    if rec.get("mode") != "apply":
        return False
    name = next((n for n in rec.get("files") or {} if n.endswith(f"/{rec.get('task_id')}/meta.json")), None)
    snapshot = (rec.get("paths") or {}).get("snapshot")
    if not name or not snapshot:
        return None
    try:
        raw = read_json(Path(snapshot) / name)
    except (OSError, ValueError):
        return None
    return bool(raw.get("verify")) if isinstance(raw, dict) else None


def recovered(plan: Task, runs: list[dict[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[str, Any] | None]:
    """What the plan's last apply finished before it stopped, and the step it was in when it was killed if that step
    did not finish. A record cut short by a kill keeps the steps it finished; each must still match the plan's step
    at its place (task and arguments), else nothing counts. The step it was in counts as done only when its own run
    ended ok with the arguments the plan gives it, and so did its verify check when it has one."""
    ran = applies(runs)
    if not ran or ran[-1].get("status") == "ok":
        return {}, None
    last = ran[-1]
    plan_run = str(last.get("run_id"))
    plan_specs, steps = specs(plan), last.get("steps") or []
    if len(steps) > len(plan_specs):
        return {}, None
    done: dict[int, dict[str, Any]] = {}
    for i, step in enumerate(steps):
        if not same_step(plan_specs[i], step):
            return {}, None
        if step.get("status") == "ok":
            done[i] = {"task": step["task"], "run_id": step.get("run_id"), "plan_run": step.get("done_in") or plan_run}
    at = len(steps)
    if at == len(plan_specs) or last.get("current") != at + 1:
        return done, None
    spec = plan_specs[at]
    task_id, args = str(spec.get("task")), [str(x) for x in spec.get("args", [])]
    own = step_runs(task_id, plan_run)
    main = next((r for r in own if r.get("mode") != "verify"), None)
    check = next((r for r in own if r.get("mode") == "verify"), None)
    if main and [str(x) for x in main.get("argv") or []] != args:
        main = check = None
    if main and main.get("status") == "ok":
        wants = checks_after_apply(main)
        if (check.get("status") == "ok") if check else wants is False:
            done[at] = {"task": task_id, "run_id": main.get("run_id"), "plan_run": plan_run}
            return done, None
    if main is None:
        how = "its run never started"
    elif main.get("status") != "ok":
        how = f"its run {main.get('run_id')} ended {main.get('status')}"
    else:
        how = f"its run {main.get('run_id')} ended ok but its verify check did not finish ok"
    return done, {
        "i": at + 1,
        "task": task_id,
        "plan_run": plan_run,
        "run_id": main.get("run_id") if main else None,
        "status": main.get("status") if main else None,
        "text": f"step {at + 1} ({task_id}) was interrupted mid-run last time, in plan run {plan_run}: {how}",
    }


def done_steps(plan: Task, runs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Steps (0-based) that the plan's last apply finished before it stopped, with the run that did each: the next run
    resumes after them. Empty when the last apply ended ok, when there is none, or when the plan's steps changed."""
    return recovered(plan, runs)[0]


def stopped(plan: Task, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The plan's last apply when it stopped before its end: where, and why. A run killed outright stopped at the
    step it was in, or after it when that step's own run finished ok."""
    ran = applies(runs)
    if not ran or ran[-1].get("status") == "ok":
        return None
    last = ran[-1]
    steps = last.get("steps") or []
    at = next((i for i, s in enumerate(steps) if s.get("status") != "ok"), None)
    why = (steps[at].get("last_error") or steps[at].get("error")) if at is not None else None
    reason = f"{steps[at].get('status')}: {why}" if at is not None and why else last.get("last_error")
    if at is None and len(steps) < len(specs(plan)):
        done, cut = recovered(plan, runs)
        at = cut["i"] - 1 if cut else min(set(range(len(specs(plan)))) - set(done), default=None)
        reason = cut["text"] if cut else reason
    return {"run_id": last.get("run_id"), "status": last.get("status"), "reason": reason, "at": at}


def snapshot_root(step: dict[str, Any]) -> Path | None:
    folder = step.get("dir")
    if not folder:
        return None
    root = Path(folder) / SNAPSHOT
    return root if root.is_dir() else None


def step_files(step: dict[str, Any], key: str = "files") -> dict[str, str]:
    """What the step's own run record keeps of the files it ran: their digests, or with key `modes` their modes."""
    folder = step.get("dir")
    if not folder:
        return {}
    try:
        files = load_run(Path(folder) / "run.json").get(key) or {}
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in files.items()}


def prerun(plan: Task, runs: list[dict[str, Any]], skip: set[int]) -> dict[str, Any] | None:
    """The pre-run an apply of this plan runs from: the latest dry run of the plan since its last apply that ran a
    step, in which every step left ran ok with the arguments the plan gives it now and kept its snapshot. An apply
    uses its pre-run up: after one stops, the steps left get a fresh pre-run. None when there is none."""
    plan_specs = specs(plan)
    done = finished(runs)
    last = max((i for i, r in enumerate(done) if r.get("mode") == "apply" and ran_a_step(r)), default=-1)
    for r in reversed(done[last + 1 :]):
        if r.get("mode") != "dry" or r.get("status") != "ok":
            continue
        steps = r.get("steps") or []
        if len(steps) != len(plan_specs):
            continue
        frozen: dict[int, dict[str, Any]] = {}
        for i, (spec, step) in enumerate(zip(plan_specs, steps, strict=True)):
            if i in skip:
                continue
            root = snapshot_root(step)
            if step.get("status") != "ok" or step.get("skipped") or not same_step(spec, step) or root is None:
                break
            frozen[i] = {
                "run_id": step.get("run_id"),
                "root": str(root),
                "files": step_files(step),
                "modes": step_files(step, "modes"),
            }
        else:
            return {"run_id": r.get("run_id"), "started": r.get("started"), "steps": frozen}
    return None


def changed_since(prerun_steps: dict[int, dict[str, Any]]) -> list[str]:
    """Files a step of the pre-run ran that differ now: the apply still runs the pre-run's copy of them."""
    out: list[str] = []
    for step in prerun_steps.values():
        out += [n for n in verify_digest(home_dir(), step.get("files") or {}) if n not in out]
    return out


def frozen_task(task: Task, root: Path) -> Task | None:
    """The step as its pre-run ran it: its meta.json and scripts from the snapshot."""
    try:
        rel = task.folder.relative_to(home_dir())
    except ValueError:
        return None
    folder = root / rel
    if not (folder / "meta.json").is_file():
        return None
    copy = load_task(folder)
    return copy if copy.meta is not None else None


def verify_digest(root: Path, files: dict[str, str]) -> list[str]:
    """Files under `root` whose content no longer matches its recorded digest."""
    bad = []
    for name, digest in files.items():
        try:
            ok = content_digest(root / name) == digest
        except OSError:
            ok = False
        if not ok:
            bad.append(name)
    return bad


def typical_s(runs: list[dict[str, Any]], mode: str, exclude: str = "") -> float | None:
    """How long this task's runs in this mode usually take: the median of its ok ones, None when it has none."""
    took = [
        float(r["duration_s"])
        for r in runs
        if r.get("mode") == mode and r.get("status") == "ok" and r.get("duration_s") and r.get("plan_run") != exclude
    ]
    return statistics.median(took) if took else None


def live(plan: Task, run: dict[str, Any], by_task: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """What the console needs to fill a running plan's bar: each step's mode and usual length from its own run
    history, and the step running now: when it started and the progress its script last reported."""
    rid, mode = str(run.get("run_id")), str(run.get("mode"))
    steps = []
    for spec in specs(plan):
        folder = task_folder(str(spec.get("task")))
        task = load_task(folder) if folder else None
        meta = task.meta if task else None
        runs = by_task.get(str(spec.get("task")), [])
        if mode == "verify":
            m = "verify"
        elif meta is None:
            m = "dry"
        else:
            m = ("apply" if mode == "apply" else "dry") if meta.changes_prod else "read"
        expect = typical_s(runs, m, rid)
        if m == "apply" and meta and meta.verify and expect is not None:
            expect += typical_s(runs, "verify", rid) or 0.0
        steps.append({"mode": m, "expect_s": round(expect, 1) if expect is not None else None})
    now = None
    cur = run.get("current")
    if cur and 0 < int(cur) <= len(steps):
        sid = str(specs(plan)[int(cur) - 1].get("task"))
        mine = [r for r in by_task.get(sid, []) if r.get("plan_run") == rid]
        if mine:
            last = mine[-1]
            now = {"started": last.get("started"), "progress": last.get("progress"), "mode": last.get("mode")}
    return {"steps": steps, "now": now}
