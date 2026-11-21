"""Runs, judges and asks (and `opsquire tend --fix`) started from the UI. A run job is `opsquire run <task> [--apply |
--verify | --apply --dry-first] [-- args]` under a pseudo-terminal, so the runner and the helpers find a /dev/tty for
their confirmations; the page reads the terminal output, answers prompts and can interrupt. Arguments are those the
task's inbox item shows (handover.args_for), never the page's. The runner writes the real records as always, marked
with the console as their origin; jobs live only in this server process."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import approve, followup, handover, locks, policy
from .ask import asks_dir, configured_fresh_after
from .model import ORIGIN_CONSOLE, ORIGIN_ENV, OpsquireError, iso, now, resolve_task
from .query import runs_for
from .state import load_asks

MODES = ("read", "dry", "apply", "verify")
DRY_APPLY = "dryapply"
FLAGS = {"apply": ["--apply"], "verify": ["--verify"], DRY_APPLY: ["--apply", "--dry-first"]}
PROMPT_RE = re.compile(r"(\[y/N\] ?|Type BREAK to take the lock anyway: ?|\[Enter = [^\]]*\] > ?)$")
PROMPT_QUIET_S = 0.3
HELPER_PROMPT_RE = re.compile(rb"\n>>> ((?:(?!\n>>> ).)*?) \[y/N\] ", re.DOTALL)
HELPER_PROMPT_SCAN_BYTES = 256 * 1024
MAX_JOBS = 50
KEEP_DONE = 50
ASK_ID_RE = re.compile(rb"opsquire: ask (\S+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class Job:
    job_id: str
    task_id: str
    mode: str
    started: str
    ended: str | None = None
    status: str = "running"
    exit_code: int | None = None
    run_id: str | None = None
    ask_id: str | None = None
    prompt: str | None = None
    args: list[str] = field(default_factory=list)
    output: bytearray = field(default_factory=bytearray)
    fd: int = -1
    proc: subprocess.Popen[bytes] | None = None
    last_read: float = 0.0
    answered: int = 0
    prompt_end: int = -1
    lock: threading.Lock = field(default_factory=threading.Lock)

    def view(self) -> dict[str, Any]:
        status, prompt = self.status, self.prompt
        if self.mode == "ask" and status == "running" and self.ask_id:
            asked = unanswered(self.ask_id)
            if asked:
                status, prompt = "waiting", asked
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "args": self.args[0] if self.mode == "ask" and self.args else shlex.join(self.args),
            "started": self.started,
            "ended": self.ended,
            "status": status,
            "exit_code": self.exit_code,
            "run_id": self.run_id,
            "ask_id": self.ask_id,
            "prompt": prompt,
        }


CHAT_TAIL_BYTES = 16384


def unanswered(ask_id: str) -> str | None:
    """The approval or question an ask's chat ends on, while nobody has answered it. Its job's output cannot say: in
    a split ask the other agents keep printing their steps after the question."""
    try:
        with (asks_dir() / f"{ask_id}.chat.jsonl").open("rb") as f:
            start = max(0, f.seek(0, os.SEEK_END) - CHAT_TAIL_BYTES)
            f.seek(start)
            lines = f.read().decode("utf-8", "replace").splitlines()[1 if start else 0 :]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        kind = item.get("kind") if isinstance(item, dict) else None
        if kind in ("approve", "question"):
            return f"Allow {item.get('text')}?" if kind == "approve" else str(item.get("text") or "")
        if kind in ("you", "end", "agent"):
            return None
    return None


def last_line(output: bytes | bytearray) -> str:
    """The job's last words: the runner's or opsquire's own reason for refusing, usually."""
    lines = [ln.strip() for ln in ANSI_RE.sub("", output.decode("utf-8", "replace")).splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


class Jobs:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.problems: dict[str, dict[str, str]] = {}
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.auto_started: set[tuple[str, str, str, str]] = set()
        self.followed: set[tuple[str, str]] = set()

    def start(
        self, task_query: str, mode: str, expect: list[str] | None = None, env: dict[str, str] | None = None
    ) -> Job:
        """A job for this run, or the job already running it (a second click joins it). A start that fails leaves its
        reason on the task until a job of it starts."""
        try:
            job = self.launch(task_query, mode, expect, env)
        except (OpsquireError, OSError) as e:
            self.problem(task_query, mode, str(e).splitlines()[0] if str(e) else type(e).__name__)
            raise
        self.problems.pop(job.task_id, None)
        return job

    def start_ask(
        self, request: str, resume: str = "", model: str = "", agent: str = "", fresh: bool = False, tend: bool = False
    ) -> Job:
        """An `opsquire ask` from the page: a new request, or a follow-up in an earlier ask's conversation. With
        `tend`, the library upkeep ask (`opsquire tend --fix`), whose request opsquire writes itself."""
        request = "library upkeep" if tend else request.strip()
        if not request and not resume:
            raise OpsquireError("say what you want")
        argv = (
            [sys.executable, "-m", "opsquire", "tend", "--fix"]
            if tend
            else [
                sys.executable,
                "-m",
                "opsquire",
                "ask",
                *(["--resume", resume] if resume else []),
                *(["--fresh"] if fresh and resume else []),
                *([request] if request else []),
            ]
        )
        with self.lock:
            if len([j for j in self.jobs.values() if j.status != "done"]) >= MAX_JOBS:
                raise OpsquireError("too many jobs running")
            job = Job(
                f"{now():%Y%m%dT%H%M%S%f}"[:-3] + "Z", "", "ask", iso(now()), args=[request], ask_id=resume or None
            )
            self.add(job)
        self.spawn(job, argv)
        return job

    def add(self, job: Job) -> None:
        self.jobs[job.job_id] = job
        self.prune()

    def prune(self) -> None:
        """Of the finished jobs keep only the newest KEEP_DONE: the page gets every job kept."""
        done = sorted(j.job_id for j in self.jobs.values() if j.status == "done")
        for old in done[:-KEEP_DONE]:
            del self.jobs[old]

    def follow_up(self) -> list[Job]:
        """Resume every ask conversation whose handed-over runs finished since its last answer (see followup)."""
        asks = load_asks()
        conv = {str(a.get("ask_id")): followup.conversation_of(a) for a in asks}
        with self.lock:
            live = [j for j in self.jobs.values() if j.status != "done"]
        busy_conversations = {conv.get(str(j.ask_id), str(j.ask_id)) for j in live if j.mode == "ask" and j.ask_id}
        busy_tasks = {j.task_id for j in live if j.task_id}
        at = now()
        held = followup.held_agents(asks, at)
        limit = configured_fresh_after()
        cost = {str(a.get("ask_id")): a.get("cost_usd") for a in asks}

        def long(ask_id: str) -> bool:
            spent = cost.get(ask_id)
            return limit is not None and isinstance(spent, (int, float)) and spent >= limit

        wakes = [
            (ask_id, followup.report(runs), long(ask_id))
            for ask_id, runs in followup.due(asks, runs_for, busy_conversations, busy_tasks, at, held)
        ]
        wakes += followup.continuations(asks, runs_for, busy_conversations, held, at)
        started = []
        for ask_id, message, fresh in wakes:
            if (ask_id, message) in self.followed:
                continue
            self.followed.add((ask_id, message))
            try:
                started.append(self.start_ask(message, ask_id, fresh=fresh))
            except OpsquireError:
                break
        return started

    def auto_run(self, inbox: list[dict[str, Any]]) -> list[Job]:
        """Start each waiting item the operator's policy lets the console start by itself (state marks it `auto`),
        once per item and run: a run that leaves it waiting again, or fails to start, is not repeated until the item
        changes. Its record names the rule (`policy`)."""
        started = []
        applying = [(t, h) for t, hs in locks.holders().items() for h in hs if locks.is_apply(h) and h.alive()]
        for item in inbox:
            if not item.get("auto") or item.get("applied_already"):
                continue
            mine = [str(x) for x in item.get("targets") or []]
            if item["next"] == "apply" and any(locks.overlaps(t, x) for t, _ in applying for x in mine):
                continue
            key = (str(item["task_id"]), str(item["next"]), str(item.get("args", "")), str(item.get("waiting_since")))
            if key in self.auto_started:
                continue
            self.auto_started.add(key)
            with contextlib.suppress(OpsquireError, OSError):
                started.append(self.start(key[0], key[1], env=policy.env_for(str(item["tier"]), key[1])))
        return started

    def problem(self, task: str, mode: str, why: str) -> None:
        with contextlib.suppress(OpsquireError):
            task = resolve_task(task).id
        self.problems[task] = {"mode": mode, "why": why, "at": iso(now())}
        self.changed.set()

    def launch(
        self, task_query: str, mode: str, expect: list[str] | None = None, env: dict[str, str] | None = None
    ) -> Job:
        """A run from the page (mode read, dry, apply, verify or dryapply), or `judge`: the agent that decides how the
        task ended. A run takes the arguments its inbox item shows, never any the page sends; a chat's RUN line sends
        the ones it shows as `expect`, and a run that would take others is refused rather than started with them."""
        task = resolve_task(task_query)
        if task.meta is None:
            raise OpsquireError(f"{task.id} has invalid meta.json: " + "; ".join(task.errors))
        if mode == "judge":
            if task.kind == "general":
                raise OpsquireError("general scripts are not closed; judge the tasks that use them")
        elif mode == DRY_APPLY:
            if "apply" not in task.meta.modes:
                raise OpsquireError(f"{task.id} changes nothing; it has no apply to follow its run")
        elif mode not in MODES or (mode not in task.meta.modes and not (task.kind == "plan" and mode == "verify")):
            raise OpsquireError(f"{task.id} runs in {', '.join(task.meta.modes)} mode, not {mode}")
        args = handover.args_for(task, runs_for(task.id), "dry" if mode == DRY_APPLY else mode)
        if expect is not None and list(expect) != args:
            raise OpsquireError(
                f"the console would run it with `{shlex.join(args)}`, not `{shlex.join(expect)}`; "
                f"run it in a terminal: {handover.command(task.id, mode, list(expect))}"
            )
        with self.lock:
            for j in self.jobs.values():
                if j.task_id == task.id and j.status != "done" and j.proc is not None and j.proc.poll() is not None:
                    j.status, j.ended = "done", j.ended or iso(now())
            live = [j for j in self.jobs.values() if j.task_id == task.id and j.status != "done"]
            if live:
                if live[0].mode == mode:
                    return live[0]
                raise OpsquireError(f"{task.id} already has a {live[0].mode} job running; open it from Jobs")
            if len([j for j in self.jobs.values() if j.status != "done"]) >= MAX_JOBS:
                raise OpsquireError("too many jobs running")
            job = Job(f"{now():%Y%m%dT%H%M%S%f}"[:-3] + "Z", task.id, mode, iso(now()), args=args)
            self.add(job)
        if mode == "judge":
            argv = [sys.executable, "-m", "opsquire", "judge", task.id]
        else:
            argv = [sys.executable, "-m", "opsquire", "run", task.id, *FLAGS.get(mode, [])]
            argv += ["--", *args] if args else []
        self.spawn(job, argv, env)
        return job

    def spawn(self, job: Job, argv: list[str], extra: dict[str, str] | None = None) -> None:
        master, slave = pty.openpty()
        env = {
            **{k: v for k, v in os.environ.items() if k != policy.POLICY_ENV},
            "OPSQUIRE_AUTHOR": os.environ.get("OPSQUIRE_AUTHOR", "opsquire ui"),
            ORIGIN_ENV: ORIGIN_CONSOLE,
            "TERM": "dumb",
            **(extra or {}),
        }
        try:
            job.proc = subprocess.Popen(
                on_tty(argv), stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
            )
        finally:
            os.close(slave)
        job.fd = master
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        threading.Thread(target=self.pump, args=(job,), daemon=True).start()
        self.changed.set()

    def pump(self, job: Job) -> None:
        try:
            self.read_until_exit(job)
        finally:
            self.finish(job)

    def read_until_exit(self, job: Job) -> None:
        proc = job.proc
        assert proc is not None
        while True:
            try:
                chunk = os.read(job.fd, 65536)
            except BlockingIOError:
                chunk = b""
            except OSError:
                chunk = None
            if chunk:
                with job.lock:
                    job.output += chunk
                    if job.mode == "ask" and not job.ask_id:
                        found = ASK_ID_RE.search(job.output)
                        if found:
                            job.ask_id = found.group(1).decode()
                            self.changed.set()
                    job.last_read = time.monotonic()
                    if job.prompt and job.prompt_end < 0:
                        job.prompt, job.status = None, "running"
                        self.changed.set()
                self.detect_helper_prompt(job)
            elif chunk is None or proc.poll() is not None:
                if proc.poll() is None:
                    time.sleep(0.05)
                    continue
                break
            else:
                self.detect_prompt(job)
                time.sleep(0.05)

    def detect_helper_prompt(self, job: Job) -> None:
        """A helper's prompt stands until it is answered, wherever it landed in the output: a script's own prompt
        can overtake lines the runner is still echoing, or be followed by another process's output."""
        with job.lock:
            if job.prompt_end >= 0:
                return
            start = max(job.answered, len(job.output) - HELPER_PROMPT_SCAN_BYTES)
            found = HELPER_PROMPT_RE.search(job.output, start)
            if not found:
                return
            question = found.group(1).decode("utf-8", "replace").replace("\r", "")
            job.prompt, job.status, job.prompt_end = f">>> {question} [y/N]", "waiting", found.end()
        self.changed.set()

    def clear_prompt(self, job: Job) -> None:
        with job.lock:
            job.answered = job.prompt_end if job.prompt_end >= 0 else len(job.output)
            job.prompt, job.status, job.prompt_end = None, "running", -1

    def detect_prompt(self, job: Job) -> None:
        with job.lock:
            if job.prompt or not job.output or time.monotonic() - job.last_read < PROMPT_QUIET_S:
                return
            tail = ANSI_RE.sub("", job.output[-400:].decode("utf-8", "replace"))
            if tail.endswith("\n"):
                return
            last = tail.rsplit("\n", 1)[-1]
            if PROMPT_RE.search(last):
                job.prompt, job.status = last.strip(), "waiting"
                self.changed.set()

    def finish(self, job: Job) -> None:
        proc = job.proc
        assert proc is not None
        code = proc.wait()
        with job.lock:
            job.exit_code = code
            job.status, job.prompt, job.prompt_end = "done", None, -1
            job.ended = iso(now())
            try:
                os.close(job.fd)
            except OSError:
                pass
        with self.lock:
            self.prune()
        runs = [r for r in runs_for(job.task_id) if str(r.get("started", "")) >= job.started] if job.task_id else []
        job.run_id = str(runs[-1]["run_id"]) if runs and job.mode != "judge" else None
        if code and job.mode not in ("judge", "ask"):
            started = any(r.get("mode") == job.mode and r.get("status") != "blocked" for r in runs)
            refused = runs[-1] if runs and runs[-1].get("status") == "blocked" and not started else None
            if refused or not runs:
                why = str(refused.get("last_error")) if refused else last_line(job.output)
                self.problem(job.task_id, job.mode, why or f"exited {code} before any run started")
        self.changed.set()

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise OpsquireError(f"no job {job_id}")
        return job

    def read(self, job_id: str, offset: int) -> dict[str, Any]:
        job = self.get(job_id)
        with job.lock:
            data = bytes(job.output[max(0, offset) :])
            view = job.view()
        return {**view, "text": data.decode("utf-8", "replace"), "offset": max(0, offset) + len(data)}

    def answer(self, job_id: str, text: str) -> None:
        job = self.get(job_id)
        if job.status == "done":
            raise OpsquireError(f"job {job_id} has finished")
        os.write(job.fd, (text.strip() + "\n").encode())
        self.clear_prompt(job)
        self.detect_helper_prompt(job)
        self.changed.set()

    def approvals(self, job_id: str) -> list[dict[str, Any]]:
        """The tool requests waiting in an ask job, the first being the one its terminal asks now."""
        job = self.get(job_id)
        box = approve.box_of(job.proc.pid) if job.mode == "ask" and job.proc and job.status != "done" else None
        return approve.pending(box) if box else []

    def approve(self, job_id: str, req_id: str, allow: bool) -> None:
        """Answer one waiting request: the one the terminal asks through the terminal, any other in place."""
        job = self.get(job_id)
        box = approve.box_of(job.proc.pid) if job.mode == "ask" and job.proc and job.status != "done" else None
        queue = approve.pending(box) if box else []
        if box is None or not any(r["id"] == req_id for r in queue):
            raise OpsquireError(f"no waiting request {req_id} in job {job_id}")
        if queue[0]["id"] == req_id:
            self.answer(job_id, "y" if allow else "n")
            return
        chat = asks_dir() / f"{job.ask_id}.chat.jsonl" if job.ask_id else None
        approve.answer_queued(box, req_id, allow, chat)
        self.changed.set()

    def interrupt(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.status == "done" or job.proc is None:
            raise OpsquireError(f"job {job_id} has finished")
        self.clear_prompt(job)
        os.killpg(job.proc.pid, signal.SIGINT)

    def stop_all(self) -> None:
        for job in list(self.jobs.values()):
            if job.status != "done" and job.proc is not None:
                try:
                    os.killpg(job.proc.pid, signal.SIGINT)
                except OSError:
                    pass
        deadline = time.monotonic() + 10
        for job in list(self.jobs.values()):
            if job.proc is not None and job.status != "done":
                try:
                    job.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    job.proc.kill()

    def views(self) -> list[dict[str, Any]]:
        return [j.view() for j in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)]


TTY_EXEC = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"


def on_tty(argv: list[str]) -> list[str]:
    """argv, started so its stdin pty becomes its controlling terminal (so /dev/tty opens it). The console runs
    threads, and code between fork and exec (a preexec_fn) can deadlock there; this does the ioctl after a clean
    exec, in the new session the child already leads."""
    return [sys.executable, "-c", TTY_EXEC, *argv]
