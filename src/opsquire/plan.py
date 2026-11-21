from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import IO, Any

from . import handover, ledger, locks, policy, runner, schedule
from .creds import Redactor, Secrets
from .lint import lint_task, save_lint
from .model import (
    SCHEMA,
    Heartbeat,
    Meta,
    OpsquireError,
    RunClock,
    Task,
    compact,
    home_dir,
    host_name,
    iso,
    load_task,
    now,
    pid_namespace,
    resolve_task,
    run_origin,
    runs_dir,
    task_folder,
    user_name,
    write_json,
)
from .query import runs_for
from .runner import EXIT_CODES, Run, style, tty_prompt


def plan_meta(
    name: str,
    t: dt.datetime,
    area: str,
    purpose: str,
    author: str,
    steps: list[dict[str, Any]],
    rollback: str | None = None,
    links: list[str] | None = None,
    thread: str | None = None,
) -> Meta:
    """Meta for a plan over existing tasks; targets and changes_prod follow from the steps."""
    metas = []
    for step in steps:
        task = resolve_task(str(step["task"]))
        if task.meta is None or task.kind != "task":
            raise OpsquireError(f"step {task.id} is not a valid task")
        metas.append(task.meta)
    changes = any(m.changes_prod for m in metas)
    return Meta(
        id=name,
        kind="plan",
        created=iso(t),
        author=author,
        purpose=purpose,
        area=area,
        entry="",
        changes_prod=changes,
        targets=sorted({x for m in metas for x in m.targets}),
        lock="exclusive" if changes else "shared",
        lock_reason="",
        credentials={},
        uses=[],
        rollback=rollback or ("each step's own rollback, see steps" if changes else "read-only"),
        links=links or [],
        steps=steps,
        thread=thread,
    )


STOPPING = {
    "failed": "failed",
    "partial": "failed",
    "aborted": "aborted",
    "interrupted": "interrupted",
    "blocked": "blocked",
}


def lock_mode(metas: list[Meta]) -> str:
    modes = {m.lock for m in metas}
    if "exclusive" in modes:
        return "exclusive"
    return "shared" if "shared" in modes else "none"


class PlanRun:
    def __init__(
        self,
        plan: Task,
        meta: Meta,
        apply: bool,
        break_lock: bool,
        skip_dry: str = "",
        verify: bool = False,
        from_start: bool = False,
        wait: bool = True,
    ) -> None:
        self.plan = plan
        self.wait = wait
        self.meta = meta
        self.apply = apply
        self.verify = verify
        self.from_start = from_start
        self.break_lock = break_lock
        self.skip_dry = skip_dry
        self.mode = "verify" if verify else "apply" if apply else meta.modes[0]
        self.current: int | None = None
        self.resumed: dict[int, dict[str, Any]] = {}
        self.cut: dict[str, Any] | None = None
        self.rollbacks: dict[str, Task] = {}
        self.prerun: dict[str, Any] | None = None
        self.clock = RunClock()
        self.run_id = f"{compact(self.started)}_{self.mode}"
        self.dir = runs_dir() / plan.id / self.run_id
        self.redact = Redactor([])
        self.out: IO[str] | None = None
        self.events: IO[str] | None = None
        self.seq = 0
        self.steps: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.last_error = ""
        self.locked: list[str] = []
        self.lock_info: dict[str, Any] = {
            "mode": "none",
            "targets": meta.targets,
            "stale_reclaimed": [],
            "broken": [],
            "tokens": {},
        }

    def event(self, event: str, msg: str = "", level: str = "info", **extra: Any) -> None:
        self.seq += 1
        ts = iso(self.clock.now())
        rec = {"ts": ts, "seq": self.seq, "run_id": self.run_id, "src": "plan", "event": event, "level": level}
        rec |= {"msg": self.redact(msg), **extra}
        if self.events:
            self.events.write(json.dumps(rec) + "\n")
            self.events.flush()
        line = f"{ts} plan | [{event}] {self.redact(msg)}"
        if self.out:
            self.out.write(line + "\n")
            self.out.flush()
        shown = ("plan_start", "step_start", "step_end", "plan_end", "lock_wait")
        if runner.VERBOSE or event in shown or level != "info":
            sys.stderr.write(
                style(line if runner.VERBOSE else f"opsquire plan: {self.redact(msg)}", "1", sys.stderr) + "\n"
            )
            sys.stderr.flush()
        for move in self.clock.fresh():
            self.event("warn", self.clock.warning(move), level="warn")

    @property
    def started(self) -> dt.datetime:
        return self.clock.start

    def wait_for(self, desc: str) -> None:
        self.lock_info["waiting_for"] = desc
        self.lock_info.setdefault("waiting_since", iso(self.clock.now()))
        self.write_record("running")
        self.event("lock_wait", f"waiting in line for {desc}; Ctrl-C gives up")

    def write_record(self, status: str) -> None:
        ended = self.clock.now() if status != "running" else None
        targets: dict[str, dict[str, str]] = {}
        for target in self.meta.targets:
            seen = [s["targets"].get(target, {}).get("status") for s in self.steps if s.get("targets")]
            seen = [x for x in seen if x]
            state = (
                "failed"
                if "failed" in seen
                else "ok"
                if seen and all(x in ("ok", "unchanged") for x in seen)
                else "skipped"
            )
            targets[target] = {"status": state, "msg": ""}
        write_json(
            self.dir / "run.json",
            {
                "schema": SCHEMA,
                "run_id": self.run_id,
                "task_id": self.plan.id,
                "task_kind": "plan",
                "purpose": self.meta.purpose,
                "area": self.meta.area,
                "mode": self.mode,
                "status": status,
                "started": iso(self.started),
                "ended": iso(ended) if ended else None,
                "duration_s": round((ended - self.started).total_seconds(), 3) if ended else None,
                "user": user_name(),
                "uid": os.geteuid(),
                "host": host_name(),
                "pid": os.getpid(),
                "pid_ns": pid_namespace(),
                "origin": run_origin(),
                "policy": policy.started_by(),
                "files": self.plan.file_hashes(),
                "steps": self.steps,
                "total": len(self.meta.steps or []),
                "current": self.current,
                "resumed_after": sorted(i + 1 for i in self.resumed) or None,
                "interrupted_step": self.cut,
                "prerun": self.prerun.get("run_id") if self.prerun else None,
                "targets": targets,
                "lock": self.lock_info,
                "warnings": self.warnings,
                **({"clock": list(self.clock.moves.values())} if self.clock.moves else {}),
                "results": [r for s in self.steps for r in s.get("results", [])],
                "last_error": self.last_error,
                "paths": {
                    "dir": str(self.dir),
                    "output": str(self.dir / "output.log"),
                    "events": str(self.dir / "events.jsonl"),
                },
            },
        )

    def finish(self, status: str, reason: str = "") -> int:
        if reason:
            self.last_error = reason
        self.current = None
        for i, step in enumerate(self.meta.steps or []):
            if i >= len(self.steps):
                self.steps.append({"i": i + 1, "task": step["task"], "args": step.get("args", []), "status": "skipped"})
        self.write_record(status)
        self.event(
            "plan_end", f"status={status}" + (f": {reason}" if reason else ""), "info" if status == "ok" else "error"
        )
        return EXIT_CODES[status]

    def execute(self) -> int:
        try:
            self.dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise OpsquireError(f"run directory already exists: {self.dir}") from None
        except OSError:
            raise OpsquireError(
                f"cannot write {self.dir}: run records are written only by opsquire run in your own terminal"
            ) from None
        try:
            with (self.dir / "events.jsonl").open("a") as ev, (self.dir / "output.log").open("a") as out:
                self.events, self.out = ev, out
                try:
                    with Heartbeat(self.dir):
                        return self._execute()
                except KeyboardInterrupt:
                    return self.finish("interrupted", "interrupted by the operator outside a step")
                finally:
                    if self.locked:
                        locks.release(self.locked, self.run_id)
                        self.locked = []
                    self.events = self.out = None
                    ledger.run_ended(self.dir)
        except BaseException:
            if not (self.dir / "run.json").exists():
                shutil.rmtree(self.dir, ignore_errors=True)
                with contextlib.suppress(OSError):
                    self.dir.parent.rmdir()
            raise

    def _execute(self) -> int:
        self.write_record("running")
        ledger.observe()
        ledger.run_started(self.dir, self.plan.content_hash())
        specs = self.meta.steps or []
        self.event("plan_start", f"{self.plan.id} mode={self.mode} steps={len(specs)}", purpose=self.meta.purpose)
        tasks: list[Task] = []
        for spec in specs:
            folder = task_folder(spec["task"])
            task = load_task(folder) if folder else None
            if task is None or task.meta is None:
                return self.finish("blocked", f"step {spec['task']} is missing or invalid")
            tasks.append(task)

        earlier = [r for r in runs_for(self.plan.id) if r.get("run_id") != self.run_id]
        if not self.verify and not self.from_start:
            self.resumed, self.cut = schedule.recovered(self.plan, earlier)
            if self.resumed:
                left = min(set(range(len(specs))) - set(self.resumed), default=len(specs))
                which = ", ".join(str(i + 1) for i in sorted(self.resumed))
                by = next(iter(self.resumed.values()))["plan_run"]
                self.event("resume", f"resumes from step {left + 1}: steps {which} finished in run {by}")
            if self.cut:
                note = f"{self.cut['text']}; it runs again"
                self.warnings.append(note)
                self.event("interrupted_step", note, "warn")

        metas: dict[int, Task] = {}
        if self.apply and not self.skip_dry:
            self.prerun = schedule.prerun(self.plan, earlier, set(self.resumed))
            if self.prerun is None:
                again = f"opsquire run {self.plan.id}" + (" --from-start" if self.from_start else "")
                return self.finish(
                    "blocked",
                    f"no pre-run covers this apply: no dry run of the plan since its last apply ran every step left "
                    f"ok (step {self._first_left(tasks).id} onward). Run `{again}` first, "
                    'or pass --skip-dry "why" to apply without one',
                )
            for i, frozen in self.prerun["steps"].items():
                copy = schedule.frozen_task(tasks[i], Path(frozen["root"]))
                if copy is None:
                    return self.finish(
                        "blocked", f"the pre-run {self.prerun['run_id']} kept no copy of step {tasks[i].id}"
                    )
                metas[i] = copy
            changed = schedule.changed_since(self.prerun["steps"])
            note = f"runs the files pre-run {self.prerun['run_id']} ran"
            if changed:
                note += f"; {len(changed)} changed since and wait for the next pre-run: {', '.join(changed[:5])}"
                self.warnings.append(note)
            self.event("frozen", note, "warn" if changed else "info")

        def meta_of(i: int) -> Meta:
            m = (metas[i] if i in metas else tasks[i]).meta
            assert m
            return m

        try:
            secrets = Secrets()
        except OpsquireError as e:
            return self.finish("blocked", str(e))
        self.redact = secrets.redactor()
        for i, task in enumerate(tasks):
            if i in self.resumed:
                continue
            _, missing = secrets.resolve(meta_of(i).credentials)
            if missing:
                return self.finish("blocked", f"step {task.id}: missing credential keys: {', '.join(missing)}")

        if not metas:
            self.event("lint", f"checking all {len(tasks)} steps before running any")
            for i, task in enumerate(tasks):
                if i not in self.resumed and not lint_task(task).ok:
                    return self.finish("blocked", f"step {task.id} fails lint; run opsquire lint {task.id}")

        if self.apply:
            for i, spec in enumerate(specs):
                folder = task_folder(spec["on_failure"]) if i not in self.resumed and spec.get("on_failure") else None
                undo = load_task(folder) if folder else None
                if undo is not None and undo.meta is not None:
                    self.rollbacks[undo.id] = undo
        running = [meta_of(i) for i in range(len(tasks)) if i not in self.resumed]
        running += [t.meta for t in self.rollbacks.values() if t.meta]
        mode = lock_mode(running) if self.apply else locks.run_lock(self.mode, lock_mode(running))
        targets = sorted({t for m in running for t in m.targets})
        self.lock_info |= {"mode": mode, "targets": targets}
        if mode != "none":
            me = locks.Holder(
                self.run_id,
                self.plan.id,
                os.getpid(),
                host_name(),
                mode,
                iso(self.started),
                pid_namespace(),
                user=user_name(),
                uid=os.geteuid(),
            )
            conflicts: list[locks.Holder] = []
            waited: list[locks.Holder] = []
            try:
                if self.wait and not self.break_lock:
                    conflicts, stale, tokens, waited = locks.wait_turn(targets, me, self.wait_for)
                else:
                    conflicts, stale, _, tokens = locks.acquire(targets, me, break_lock=False)
            except OSError as e:
                return self.finish("blocked", f"cannot write locks ({e.strerror}); run opsquire in your own terminal")
            self.lock_info["stale_reclaimed"] = [h.__dict__ for h in stale]
            if waited:
                self.lock_info.pop("waiting_for", None)
                self.lock_info.pop("waiting_since", None)
                self.lock_info["waited_for"] = [h.run_id for h in waited]
                self.event("lock", f"waited for {', '.join(h.run_id for h in waited)}")
            if conflicts:
                desc = "; ".join(h.described() for h in conflicts)
                if self.wait and not self.break_lock:
                    return self.finish(
                        "blocked",
                        f"another apply is changing these targets: {desc}. Run the plan again after it ends, so its"
                        " apply starts from what that one left",
                    )
                if not self.break_lock:
                    return self.finish(
                        "blocked",
                        f"targets locked by {desc}. Re-run without --no-wait to wait in line, or with --break-lock",
                    )
                if tty_prompt(f"Locked by {desc}.\nType BREAK to take the lock anyway: ") != "BREAK":
                    return self.finish("blocked", f"lock override declined; held by {desc}")
                _, _, broken, tokens = locks.acquire(targets, me, break_lock=True)
                self.lock_info["broken"] = [h.__dict__ for h in broken]
            self.locked = targets
            self.lock_info["tokens"] = tokens
            self.event("lock", f"{mode} lock on {', '.join(targets)} for the whole plan")

        if not self.apply and not self.verify:
            for spec in specs:
                if spec.get("verify"):
                    self.warnings.append(
                        f"{spec['task']} is a verify step: in a dry run it checks the state before any change"
                    )

        for i, (spec, task) in enumerate(zip(specs, tasks, strict=True)):
            if i in self.resumed:
                done = self.resumed[i]
                self.steps.append(
                    {
                        "i": i + 1,
                        "task": task.id,
                        "args": spec.get("args", []),
                        "status": "ok",
                        "run_id": done.get("run_id"),
                        "done_in": done.get("plan_run"),
                        "mode": "apply",
                    }
                )
                self.event("step_done", f"step {i + 1}: {task.id} finished in run {done.get('plan_run')}")
                continue
            status = self.run_step(i + 1, spec, task, meta_of(i), (self.prerun or {}).get("steps", {}).get(i))
            if status != "ok":
                reason = f"step {i + 1} ({task.id}) ended {status}"
                if self.apply and status in ("failed", "partial") and spec.get("on_failure"):
                    self.rollback(i + 1, spec["on_failure"])
                return self.finish(STOPPING.get(status, "failed"), reason)
        return self.finish("ok")

    def _first_left(self, tasks: list[Task]) -> Task:
        return next((t for i, t in enumerate(tasks) if i not in self.resumed), tasks[0])

    def run_step(
        self, i: int, spec: dict[str, Any], task: Task, meta: Meta, frozen: dict[str, Any] | None = None
    ) -> str:
        if self.verify:
            if not meta.verify:
                self.steps.append(
                    {
                        "i": i,
                        "task": task.id,
                        "args": spec.get("args", []),
                        "status": "ok",
                        "mode": "verify",
                        "results": ["no verify check"],
                    }
                )
                return "ok"
            mode = "verify"
        else:
            mode = "apply" if self.apply and meta.changes_prod else meta.modes[0]
        self.current = i
        self.event("step_start", f"step {i}/{len(self.meta.steps or [])}: {task.id} mode={mode}")
        self.write_record("running")
        args = list(spec.get("args", []))
        entry = self._one(task, meta, mode, args, frozen, i)
        if entry is None:
            return "failed"
        status = str(entry["status"])
        if status == "ok" and mode == "apply" and meta.verify:
            self.event("step_verify", f"step {i}: apply ok; running its verify check")
            check = self._one(task, meta, "verify", [] if meta.stages else args, frozen, i, record=False)
            entry["verify"] = (
                {k: check.get(k) for k in ("run_id", "status", "last_error", "results")}
                if check
                else {"status": "failed"}
            )
            if not check or check.get("status") != "ok":
                status = entry["status"] = "failed"
                entry["last_error"] = f"applied ok, but its verify check ended {(check or {}).get('status', 'failed')}"
        self.write_record("running")
        self.event("step_end", f"step {i}: {task.id} {status}", "info" if status == "ok" else "error")
        return status

    def _one(
        self,
        task: Task,
        meta: Meta,
        mode: str,
        args: list[str],
        frozen: dict[str, Any] | None,
        i: int,
        record: bool = True,
    ) -> dict[str, Any] | None:
        run = Run(
            task,
            meta,
            mode,
            args,
            False,
            plan_run=self.run_id,
            skip_dry=self.skip_dry,
            frozen=frozen,
            plan_lock=self.lock_info["mode"],
            plan_tokens=self.lock_info["tokens"],
        )
        try:
            run.execute()
        except OpsquireError as e:
            if record:
                self.steps.append({"i": i, "task": task.id, "args": args, "status": "failed", "error": str(e)})
            return None
        rec = run.record
        entry = {
            "i": i,
            "task": task.id,
            "args": args,
            "run_id": rec.get("run_id"),
            "mode": mode,
            "status": rec.get("status"),
            "targets": rec.get("targets", {}),
            "results": rec.get("results", []),
            "last_error": rec.get("last_error", ""),
            "dir": rec.get("paths", {}).get("dir"),
            **({"frozen": frozen.get("run_id")} if frozen else {}),
        }
        if record:
            self.steps.append(entry)
        return entry

    def rollback(self, i: int, task_id: str) -> None:
        """Run step i's rollback task as it was when the plan took its lock, which covers the rollback's targets."""
        task = self.rollbacks.get(task_id)
        if task is None or task.meta is None:
            self.warnings.append(f"rollback task {task_id} is missing or invalid; nothing was rolled back")
            return
        self.event("rollback", f"step {i} failed: running its rollback task {task_id}", "warn")
        mode = "apply" if task.meta.changes_prod else task.meta.modes[0]
        why = f"rollback of step {i} of {self.plan.id}"
        run = Run(
            task,
            task.meta,
            mode,
            [],
            False,
            plan_run=self.run_id,
            skip_dry=why,
            plan_lock=self.lock_info["mode"],
            plan_tokens=self.lock_info["tokens"],
        )
        try:
            run.execute()
        except OpsquireError as e:
            self.warnings.append(f"rollback {task_id} could not start: {e}")
            return
        self.steps[-1]["rollback"] = {
            "task": task_id,
            "run_id": run.record.get("run_id"),
            "status": run.record.get("status"),
        }


def create_schedule(steps: list[dict[str, Any]], purpose: str, author: str) -> Task:
    """A plan over these steps ({task, args}, in order) made from the console's inbox: the operator linked them into
    one schedule. Its area is the steps' common area, else `group`; its thread theirs when they share one."""
    if len(steps) < 2:
        raise OpsquireError("link at least two items into a schedule")
    tasks = [resolve_task(str(s["task"])) for s in steps]
    if len({t.id for t in tasks}) != len(tasks):
        raise OpsquireError("a schedule runs each task once; link it in one place")
    for t in tasks:
        if t.kind != "task" or t.meta is None:
            raise OpsquireError(f"{t.id} is not a task: only tasks link into a schedule")
    areas = {t.meta.area for t in tasks if t.meta}
    threads = {t.meta.thread for t in tasks if t.meta}
    area = areas.pop() if len(areas) == 1 else "group"
    t0 = now()
    base = f"{t0:%Y%m%d-%H%M}_{area}_schedule"
    name, n = base, 1
    while task_folder(name) is not None:
        n += 1
        name = f"{base}-{n}"
    specs = [
        {"task": t.id, **({"args": list(s["args"])} if s.get("args") else {})}
        for t, s in zip(tasks, steps, strict=True)
    ]
    text = purpose.strip() or "linked in the console: " + ", then ".join(t.meta.purpose[:60] for t in tasks if t.meta)
    thread = threads.pop() if len(threads) == 1 else None
    meta = plan_meta(name, t0, area, text, author, specs, None, [], thread)
    folder = home_dir() / "tasks" / f"{t0:%Y-%m}" / name
    folder.mkdir(parents=True)
    write_json(folder / "meta.json", meta.to_json())
    plan = load_task(folder)
    result = lint_task(plan)
    save_lint(plan, result)
    if result.ok:
        handover.record(plan, "dry", [], "linked in the console", author)
    return load_task(folder)


def run_plan(
    plan: Task,
    apply: bool,
    break_lock: bool,
    skip_dry: str = "",
    verify: bool = False,
    from_start: bool = False,
    wait: bool = True,
) -> int:
    if plan.meta is None:
        raise OpsquireError(f"{plan.id} has invalid meta.json:\n  " + "\n  ".join(plan.errors))
    if plan.meta.archived or plan.meta.superseded_by or plan.meta.abandoned_reason:
        raise OpsquireError(f"{plan.id} is retired; see its meta.json")
    if apply and not plan.meta.changes_prod:
        raise OpsquireError(f"{plan.id} only reads (no step changes prod); run it without --apply")
    return PlanRun(plan, plan.meta, apply, break_lock, skip_dry, verify, from_start, wait).execute()
