from __future__ import annotations

import fcntl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import ledger
from .model import (
    HEARTBEAT_FILE,
    host_name,
    left_running,
    locks_dir,
    pid_exists,
    pid_namespace,
    read_json,
    run_alive,
    runs_dir,
    write_json,
)

Held = dict[str, list[dict[str, Any]]]
READ = "read"
QUEUE_FILE = "queue.json"
CHANGES_FILE = "changes.json"


@dataclass
class Holder:
    run_id: str
    task_id: str
    pid: int
    host: str
    mode: str
    since: str
    pid_ns: str = ""
    token: int = 0
    user: str = ""
    uid: int = -1

    def alive(self) -> bool:
        return run_alive(self.pid, self.host, runs_dir() / self.task_id / self.run_id, self.pid_ns)

    def gone(self) -> str:
        """Why a holder that is not alive counts as dead."""
        if self.pid_ns != pid_namespace() and (runs_dir() / self.task_id / self.run_id / HEARTBEAT_FILE).exists():
            return "its heartbeat stopped"
        return f"pid {self.pid} is gone"

    def orphans(self) -> list[int]:
        """Processes of this holder's script still running after its runner died (same host and pid namespace)."""
        if self.host != host_name() or self.pid_ns != pid_namespace() or pid_exists(self.pid):
            return []
        return left_running(runs_dir() / self.task_id / self.run_id)

    def described(self) -> str:
        by = f", {self.user}" if self.user else ""
        left = self.orphans()
        tail = f"; its runner is gone but its script still runs as pid {', '.join(map(str, left))}" if left else ""
        return f"{self.task_id} run {self.run_id} ({self.mode}{by}, since {self.since}){tail}"

    def who(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "token"}


def overlaps(a: str, b: str) -> bool:
    """Equal targets, or one names a part of the other ("node1" and "node1/nginx")."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def clashes(mine: str, other: str) -> bool:
    """Whether a run asking for a `mine` hold must wait for an `other` hold on an overlapping target. Nobody waits for
    a read, and a read waits only for a change under way (an exclusive hold); otherwise an exclusive side clashes."""
    if other == READ:
        return False
    if mine == READ:
        return other == "exclusive"
    return "exclusive" in (mine, other)


def run_lock(mode: str, task_lock: str) -> str:
    """Only an apply takes its task's own lock, and a read task that asked for `exclusive`. A dry run or verify
    changes nothing, so it shares; any other read takes a `read` hold (see `clashes`)."""
    if task_lock == "none" or mode == "apply" or (mode == "read" and task_lock == "exclusive"):
        return task_lock
    return READ if mode == "read" else "shared"


def is_apply(h: Holder) -> bool:
    return h.run_id.endswith("_apply")


def _load(path: Path) -> tuple[Held, dict[str, int]]:
    """locks.json holds {"holders": {target: [holder]}, "tokens": {target: last token}}; a file from before tokens is
    the holders map alone."""
    data: dict[str, Any] = read_json(path) if path.is_file() else {}
    if isinstance(data.get("holders"), dict):
        return data["holders"], data.get("tokens") or {}
    return data, {}


@contextmanager
def _state() -> Iterator[tuple[Held, dict[str, int], list[dict[str, Any]]]]:
    """The lock state under the mutex; the events collected go to the ledger once the new state is written."""
    d = locks_dir()
    d.mkdir(parents=True, exist_ok=True)
    with (d / ".mutex").open("a") as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        path = d / "locks.json"
        held, tokens = _load(path)
        events: list[dict[str, Any]] = []
        yield held, tokens, events
        write_json(
            path, {"holders": {k: v for k, v in sorted(held.items()) if v}, "tokens": dict(sorted(tokens.items()))}
        )
        for event in events:
            ledger.append("lock", event)


def holders() -> dict[str, list[Holder]]:
    held, _ = _load(locks_dir() / "locks.json")
    return {t: [Holder(**h) for h in hs] for t, hs in held.items()}


def acquire(
    targets: list[str], me: Holder, break_lock: bool, wait: bool = False
) -> tuple[list[Holder], list[Holder], list[Holder], dict[str, int]]:
    """Returns (conflicts, stale_reclaimed, broken, tokens). Only overlapping targets are compared, and a run waiting
    in line ahead of `me` that `me`'s hold would make wait counts as a conflict too. Acquires only when there are no
    conflicts or break_lock is set; each target then gets a fencing token above every token issued before on a target
    that overlaps it (a read writes nothing, so it gets none). With `wait`, a refused `me` keeps its place in line."""
    with _state() as (held, tokens, events):
        mine = [t for t in held if any(overlaps(t, x) for x in targets)]
        stale: list[Holder] = []
        conflicts: list[Holder] = []
        for target in mine:
            live: list[Holder] = []
            for h in (Holder(**x) for x in held[target]):
                if h.alive():
                    live.append(h)
                    continue
                stale.append(h)
                events.append(
                    {"event": "reclaim", "target": target, "holder": h.__dict__, "why": h.gone(), "by": me.who()}
                )
            held[target] = [h.__dict__ for h in live]
            conflicts += [h for h in live if clashes(me.mode, h.mode)]
        queue = locks_dir() / QUEUE_FILE
        line = [
            w
            for w in (read_json(queue) if queue.is_file() else [])
            if w["holder"]["run_id"] == me.run_id or Holder(**w["holder"]).alive()
        ]
        place = next((i for i, w in enumerate(line) if w["holder"]["run_id"] == me.run_id), len(line))
        conflicts += [
            Holder(**w["holder"])
            for w in line[:place]
            if clashes(w["holder"]["mode"], me.mode) and any(overlaps(t, x) for t in w["targets"] for x in targets)
        ]
        if conflicts and not break_lock:
            if wait and place == len(line):
                line.append({"holder": me.__dict__, "targets": targets})
            write_json(queue, line)
            return conflicts, stale, [], {}
        write_json(queue, [w for w in line if w["holder"]["run_id"] != me.run_id])
        broken_ids = {h.run_id for h in conflicts}
        for target in mine:
            for x in held[target]:
                if x["run_id"] in broken_ids:
                    events.append({"event": "break", "target": target, "holder": x, "by": me.who()})
            held[target] = [x for x in held[target] if x["run_id"] not in broken_ids]
        issued: dict[str, int] = {}
        for target in targets:
            if me.mode != READ:
                above = max((n for t, n in tokens.items() if overlaps(t, target)), default=0)
                issued[target] = tokens[target] = above + 1
            held.setdefault(target, []).append(replace(me, token=issued.get(target, 0)).__dict__)
        if me.mode == "exclusive":
            changes = locks_dir() / CHANGES_FILE
            counts: dict[str, int] = read_json(changes) if changes.is_file() else {}
            write_json(changes, counts | {t: counts.get(t, 0) + 1 for t in targets})
        events.append({"event": "acquire", "targets": targets, "mode": me.mode, "tokens": issued, "holder": me.who()})
        return [], stale, conflicts, issued


def wait_turn(
    targets: list[str], me: Holder, waiting: Callable[[str], None], poll: float = 1.0
) -> tuple[list[Holder], list[Holder], dict[str, int], list[Holder]]:
    """Acquire, waiting in line first come first served while clashing runs hold or wait ahead for the targets;
    `waiting` hears who it waits for whenever that changes. An apply never waits for another apply: it leaves the
    line and gets those applies back as conflicts. Returns (conflicts, stale_reclaimed, tokens, the runs waited for).
    Interrupted, it leaves the line."""
    stale: list[Holder] = []
    waited: dict[str, Holder] = {}
    told = ""
    try:
        while True:
            conflicts, reclaimed, _, tokens = acquire(targets, me, break_lock=False, wait=True)
            stale += reclaimed
            if not conflicts:
                return [], stale, tokens, list(waited.values())
            applying = [h for h in conflicts if is_apply(h)] if is_apply(me) else []
            if applying:
                leave(me.run_id)
                return applying, stale, {}, list(waited.values())
            waited |= {h.run_id: h for h in conflicts}
            desc = "; ".join(h.described() for h in conflicts)
            if desc != told:
                waiting(desc)
                told = desc
            time.sleep(poll)
    except BaseException:
        leave(me.run_id)
        raise


def leave(run_id: str) -> None:
    with _state():
        queue = locks_dir() / QUEUE_FILE
        if queue.is_file():
            write_json(queue, [w for w in read_json(queue) if w["holder"]["run_id"] != run_id])


def change_counts(targets: list[str]) -> dict[str, int]:
    """How many exclusive holds were ever taken on each target or a target overlapping it: a read compares its start
    and end to tell whether a change ran on what it read."""
    changes = locks_dir() / CHANGES_FILE
    counts: dict[str, int] = read_json(changes) if changes.is_file() else {}
    return {t: sum(n for k, n in counts.items() if overlaps(k, t)) for t in targets}


def release(targets: list[str], run_id: str) -> None:
    with _state() as (held, _, events):
        released = [t for t in targets if any(h["run_id"] == run_id for h in held.get(t, []))]
        for target in released:
            held[target] = [h for h in held[target] if h["run_id"] != run_id]
        if released:
            events.append({"event": "release", "targets": released, "run_id": run_id})


def remove(target: str) -> list[Holder]:
    with _state() as (held, _, events):
        removed = [Holder(**h) for h in held.get(target, [])]
        held[target] = []
        events.append({"event": "remove", "target": target, "holders": [h.__dict__ for h in removed]})
        return removed
