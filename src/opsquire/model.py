from __future__ import annotations

import contextlib
import datetime as dt
import functools
import hashlib
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = 1
PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
HELPERS_DIR = DATA_DIR / "helpers"
TEMPLATES_DIR = DATA_DIR / "templates"
DEFAULT_HOME = Path.home() / ".local" / "share" / "opsquire"

TASK_ID_RE = re.compile(r"^\d{8}-\d{4}_[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*$")
GENERAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TARGET_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.:-]*)*")
LOCK_MODES = ("exclusive", "shared", "none")
NO_UNDO = frozenset({"read-only", "readonly", "none", "n/a", "na", "-", "nothing", "no"})


def bash_path() -> str:
    """The bash that runs .sh tasks: the system's, but on a Mac, whose /bin/bash is 3.2 from 2007, Homebrew's ("" when
    it is not installed: lint then refuses bash tasks)."""
    if sys.platform != "darwin":
        return "/bin/bash"
    return next((p for p in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash") if os.access(p, os.X_OK)), "")


INTERPRETERS = {".py": [sys.executable, "-u"], ".sh": [bash_path()]}
IGNORED_PARTS = {"__pycache__", ".mypy_cache", ".ruff_cache", ".claude"}
LINT_FILE = ".lint.json"
OFFBOARDED = "offboarded"
ORIGIN_ENV = "OPSQUIRE_ORIGIN"
ORIGIN_CONSOLE = "console"
ORIGIN_TERMINAL = "terminal"
ORIGIN_UNATTENDED = "unattended"


class OpsquireError(Exception):
    pass


HEARTBEAT_FILE = "heartbeat"
SCRIPT_FILE = "script.json"
HEARTBEAT_EVERY = 5.0
HEARTBEAT_STALE = 30.0


class Heartbeat:
    """Refreshes <run dir>/heartbeat while a run is in progress, so any reader (even in another PID namespace,
    like an agent sandbox) can tell a live run from a dead one."""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / HEARTBEAT_FILE
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.beat, daemon=True)

    def beat(self) -> None:
        while True:
            with contextlib.suppress(OSError):
                self.path.write_text(iso(now()) + "\n")
            if self.stop.wait(HEARTBEAT_EVERY):
                return

    def __enter__(self) -> Heartbeat:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(timeout=2)


def run_origin() -> str:
    """Where this run was started, for its record: the console's jobs say so in the environment; anything else is a
    command line, typed in a terminal when stdin is one and unattended (cron, a pipe) when not. OPSQUIRE_AUTHOR cannot
    tell the console from a command line, since a console started with it set passes it on."""
    if os.environ.get(ORIGIN_ENV) == ORIGIN_CONSOLE:
        return ORIGIN_CONSOLE
    return ORIGIN_TERMINAL if os.isatty(0) else ORIGIN_UNATTENDED


def pid_namespace() -> str:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


def user_name() -> str:
    """Who runs this process: the name of its effective uid, which no environment variable can claim."""
    uid = os.geteuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


SHARED_BITS = stat.S_IWGRP | stat.S_ISGID


def shared_home(home: Path | None = None) -> bool:
    """Whether operators share the home (`opsquire share`): its root is group-writable with setgid."""
    try:
        return (home or main_home()).stat().st_mode & SHARED_BITS == SHARED_BITS
    except OSError:
        return False


def group_mode(mode: int) -> int:
    """The mode for a file opsquire creates with one of its own: in a shared home the group gets the owner's bits."""
    return mode | ((mode & 0o700) >> 3) if shared_home() else mode


@functools.cache
def host_name() -> str:
    """The name that ties runs and locks to this machine. A Mac's hostname follows the network it joins, so there it
    is the LocalHostName set once in its sharing settings."""
    if sys.platform == "darwin":
        name = ps_like(["scutil", "--get", "LocalHostName"]).strip()
        if name:
            return name
    return socket.gethostname()


def ps_like(cmd: list[str]) -> str:
    """The output of a short system query, "" when it fails."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def proc_start(pid: int) -> str:
    """When process `pid` started (clock ticks since boot; on a Mac, as `ps` prints it); "" when there is no such
    process. A pid and its start name one process: a pid alone may since belong to another."""
    if sys.platform == "darwin":
        return ps_like(["ps", "-o", "lstart=", "-p", str(pid)]).strip()
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    fields = stat_line.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else ""


def exited(pid: int) -> bool:
    """Whether /proc shows process `pid` as a zombie or dead: it has exited, and its parent has yet to reap it."""
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat_line.rpartition(")")[2].split()[:1] in (["Z"], ["X"])


def left_running(run_dir: Path) -> list[int]:
    """Processes of a run still alive after its runner: the script it started (`SCRIPT_FILE`, by pid and start time)
    and any process carrying the run's ids in its environment, which is everything the script started that kept
    it (from /proc, on a Mac from `ps -E`); processes of other users are seen only through the script's own pid."""
    found: set[int] = set()
    script = run_dir / SCRIPT_FILE
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
        rec = json.loads(script.read_text())
        if rec.get("start") and proc_start(int(rec["pid"])) == str(rec["start"]) and not exited(int(rec["pid"])):
            found.add(int(rec["pid"]))
    marks = (f"OPSQUIRE_RUN_ID={run_dir.name}", f"OPSQUIRE_TASK_ID={run_dir.parent.name}")
    if sys.platform == "darwin":
        for line in ps_like(["ps", "-A", "-ww", "-E", "-o", "pid=,command="]).splitlines():
            pid, _, rest = line.strip().partition(" ")
            if pid.isdigit() and int(pid) != os.getpid() and all(m in rest.split() for m in marks):
                found.add(int(pid))
        return sorted(found)
    with contextlib.suppress(OSError):
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == os.getpid():
                continue
            with contextlib.suppress(OSError):
                environ = b"\0" + Path(f"/proc/{name}/environ").read_bytes()
                if all(f"\0{m}\0".encode() in environ for m in marks):
                    found.add(int(name))
    return sorted(found)


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_alive(pid: int, host: str, run_dir: Path, pid_ns: str = "") -> bool:
    """A run on another host counts as alive. Same host and PID namespace: ask the kernel (instant, exact). A run
    whose runner is gone stays alive while its script, or anything the script started, still runs: it can still
    change its targets. Otherwise (e.g. reading from an agent sandbox): trust the heartbeat; runs from before
    heartbeats existed fall back to the pid check."""
    if host != host_name():
        return True
    beat = run_dir / HEARTBEAT_FILE
    if beat.exists() and pid_ns != pid_namespace():
        try:
            return time.time() - beat.stat().st_mtime < HEARTBEAT_STALE
        except OSError:
            return False
    return pid_exists(pid) or bool(left_running(run_dir))


GENERIC_AUTHOR_WORDS = {"claude", "code", "codex", "chat", "opsquire", "ask", "migrated", "by", "unknown", "test"}


def author_key(author: str) -> str:
    """What identifies the session in an author string, or "" when it names only the tool ("claude-code (chat)")."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", author.lower())
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in GENERIC_AUTHOR_WORDS]
    return " ".join(words)


def names_session(author: str, session_id: str) -> bool:
    """Whether an author string names this Claude session: one of its words is the session id or its start."""
    sid = session_id.lower()
    return bool(sid) and any(len(word) >= 6 and sid.startswith(word) for word in author_key(author).split())


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opsquire" / "config.json"


def temporary(path: Path) -> bool:
    """Under the temporary folder; on a Mac, whose TMPDIR is a private folder, /tmp counts too."""
    dirs = [tempfile.gettempdir(), *(["/tmp"] if sys.platform == "darwin" else [])]
    return any(path.resolve().is_relative_to(resolved(d)) for d in dirs)


def check_config_writable(home: Path) -> None:
    """A scratch home under the temporary folder never writes a config outside it: every later shell reads it."""
    if temporary(home) and not temporary(config_path()):
        raise OpsquireError(
            f"not writing {config_path()} for the temporary home {home}: every shell reads that file; "
            "set XDG_CONFIG_HOME to a scratch folder too"
        )


def update_config(**values: Any) -> None:
    """Merge values into the config file, creating it when needed."""
    check_config_writable(Path(values["home"]) if "home" in values else home_dir())
    try:
        current = read_json(config_path())
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    config_path().parent.mkdir(parents=True, exist_ok=True)
    write_json(config_path(), {**current, **values})


@functools.lru_cache(maxsize=16)
def resolved(path: str) -> Path:
    return Path(path).expanduser().resolve()


def home_dir() -> Path:
    env = os.environ.get("OPSQUIRE_HOME")
    if env:
        return resolved(env)
    try:
        configured = by_file("config", config_path(), functools.partial(read_json, config_path())).get("home")
    except (OSError, ValueError, AttributeError):
        configured = None
    return resolved(configured) if isinstance(configured, str) else DEFAULT_HOME


PINNED_FROM: dict[str, str] = {}


def home_source() -> str:
    """Where home_dir() came from: OPSQUIRE_HOME, the config file's path, or default. pin_home sets OPSQUIRE_HOME
    itself, so a pinned home keeps the source it had before."""
    env = os.environ.get("OPSQUIRE_HOME")
    if env:
        return PINNED_FROM.get(env, "OPSQUIRE_HOME")
    return str(config_path()) if home_dir() != DEFAULT_HOME else "default"


def no_home(home: Path) -> str:
    return (
        f"no home at {home} (from {home_source()}); `opsquire init {home}` creates it, or point opsquire at an "
        "existing home with `opsquire home <path>` or OPSQUIRE_HOME"
    )


def pin_home() -> Path:
    """Resolve the home once for this process and every process it starts: a run, the console and the jobs it starts,
    or an ask keep working in the home they began in when the config's `home` changes meanwhile (a run that switched
    homes halfway lost its record)."""
    home = home_dir()
    PINNED_FROM[str(home)] = home_source()
    os.environ["OPSQUIRE_HOME"] = str(home)
    return home


def main_home() -> Path:
    """The operator's home even inside a parallel piece's worktree (whose OPSQUIRE_VAR points back at it)."""
    env = os.environ.get("OPSQUIRE_VAR")
    return Path(env).parent if env else home_dir()


def var_dir() -> Path:
    """Run records live in the main home; agents of a parallel ask work in worktrees and get OPSQUIRE_VAR."""
    env = os.environ.get("OPSQUIRE_VAR")
    return Path(env) if env else home_dir() / "var"


def runs_dir() -> Path:
    return var_dir() / "runs"


def locks_dir() -> Path:
    return var_dir() / "locks"


def creds_path() -> Path:
    return Path(os.environ.get("OPSQUIRE_CREDS", str(Path.home() / ".creds"))).expanduser()


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


CLOCK_SLACK_S = 60.0


def steady_seconds() -> float:
    """A timer that never jumps and keeps counting while the machine sleeps (a Mac's CLOCK_MONOTONIC does)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME if hasattr(time, "CLOCK_BOOTTIME") else time.CLOCK_MONOTONIC)


def local_offset_s() -> float:
    return float(time.localtime().tm_gmtoff)


class RunClock:
    """A run's times: the wall clock at its start plus a timer that cannot jump, checked against the wall clock at
    every stamp. A wall reading off by the machine's UTC offset is the clock taking local time for UTC (seen on WSL,
    flapping within seconds): it is ignored. Any other disagreement beyond CLOCK_SLACK_S (a resynced clock, or a
    start read during such a slip) moves the start to the wall clock. `moves` keeps each kind seen; `fresh()` hands
    each new kind out once, for a warning."""

    def __init__(
        self,
        wall: Callable[[], dt.datetime] = now,
        timer: Callable[[], float] = steady_seconds,
        offset: Callable[[], float] = local_offset_s,
    ) -> None:
        self.wall, self.timer, self.offset = wall, timer, offset
        self.lock = threading.Lock()
        self.start = wall()
        self.t0 = timer()
        self.moves: dict[str, dict[str, Any]] = {}
        self.unwarned: list[str] = []

    def now(self) -> dt.datetime:
        with self.lock:
            wall, elapsed = self.wall(), self.timer() - self.t0
            moved = (wall - self.start).total_seconds() - elapsed
            if abs(moved) <= CLOCK_SLACK_S:
                return wall
            off = self.offset()
            if off and abs(moved - off) <= CLOCK_SLACK_S:
                self.note("local-as-utc", moved, wall)
                return self.start + dt.timedelta(seconds=elapsed)
            self.note("jump", moved, wall)
            self.start = wall - dt.timedelta(seconds=elapsed)
            return wall

    def note(self, kind: str, moved: float, wall: dt.datetime) -> None:
        seen = self.moves.get(kind)
        if seen:
            seen["count"] += 1
            return
        self.moves[kind] = {"kind": kind, "moved_s": round(moved), "first_wall": iso(wall), "count": 1}
        self.unwarned.append(kind)

    def fresh(self) -> list[dict[str, Any]]:
        with self.lock:
            out = [self.moves[k] for k in self.unwarned]
            self.unwarned = []
            return out

    def warning(self, move: dict[str, Any]) -> str:
        if move["kind"] == "local-as-utc":
            return (
                f"the system clock read {move['moved_s']:+d}s off, the machine's UTC offset (local time taken for "
                "UTC); this run's times come from its start and its own timer while that lasts"
            )
        return f"the system clock moved by {move['moved_s']:+d}s during this run; its start is moved to match"


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def compact(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%S") + f"{t.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC)


DEFAULT_TIME_ZONE = "UTC"


def valid_time_zone(name: object) -> bool:
    if name in ("UTC", "local"):
        return True
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def configured_time_zone() -> str:
    """Where people read times: `time_zone` in the config file, `UTC`, `local` (this machine's zone) or a zone name
    such as `America/New_York`. Records and JSON stay in UTC whatever it says."""
    try:
        value = read_json(config_path()).get("time_zone")
    except (OSError, ValueError, AttributeError):
        value = None
    return str(value) if valid_time_zone(value) else DEFAULT_TIME_ZONE


def in_zone(t: dt.datetime, zone: str) -> dt.datetime:
    if zone == "UTC":
        return t.astimezone(dt.UTC)
    if zone == "local":
        return t.astimezone()
    return t.astimezone(ZoneInfo(zone))


def zone_label(t: dt.datetime) -> str:
    return "UTC" if t.utcoffset() == dt.timedelta(0) and t.tzname() in ("UTC", "GMT", None) else str(t.tzname())


def shown(stamp: object, zone: str, seconds: bool = True) -> str:
    """A recorded UTC time as people read it, in `zone`, with the zone named; anything unparsable as given."""
    text = str(stamp or "")
    try:
        t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    t = in_zone(t, zone)
    return f"{t:%Y-%m-%d %H:%M}" + (f":{t:%S}" if seconds else "") + f" {zone_label(t)}"


def wall_clock(day: dt.datetime, hour: int, minute: int, zone: str) -> dt.datetime:
    """`hour:minute` on the date of `day` in `zone`, as an aware time."""
    local = in_zone(day, zone)
    return attach_zone(dt.datetime(local.year, local.month, local.day, hour, minute), zone)


def attach_zone(naive: dt.datetime, zone: str) -> dt.datetime:
    """A time typed without a zone, read in `zone`."""
    if zone == "UTC":
        return naive.replace(tzinfo=dt.UTC)
    if zone == "local":
        return naive.astimezone()
    return naive.replace(tzinfo=ZoneInfo(zone))


def read_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def write_text(path: Path, text: str) -> None:
    """Atomic: a temporary file of this thread's own, then a rename, so threads writing one file never share it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, indent=2) + "\n")


@dataclass
class Meta:
    id: str
    kind: str
    created: str
    author: str
    purpose: str
    area: str
    entry: str
    changes_prod: bool
    targets: list[str]
    lock: str
    lock_reason: str
    credentials: dict[str, str]
    uses: list[str]
    rollback: str
    links: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    abandoned_reason: str | None = None
    archived: dict[str, Any] | None = None
    interpreter: str | None = None
    steps: list[dict[str, Any]] | None = None
    outcome: dict[str, Any] | None = None
    thread: str | None = None
    verify: str | None = None
    stages: list[str] | None = None

    def command(self, entry: Path) -> list[str]:
        if self.interpreter and entry.suffix == ".py":
            return [str(Path(self.interpreter).expanduser()), "-u"]
        return list(INTERPRETERS[entry.suffix])

    @property
    def modes(self) -> list[str]:
        if not self.changes_prod:
            return ["read"]
        return ["dry", "apply", "verify"] if self.verify else ["dry", "apply"]

    def to_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA, **self.__dict__}


STAGE_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def stage_of(meta: Meta, argv: list[str]) -> str | None:
    """The stage a run with these arguments belongs to: its first argument, or the first stage when it has none."""
    if not meta.stages:
        return None
    if not argv:
        return meta.stages[0]
    return argv[0] if argv[0] in meta.stages else None


def is_last_stage(meta: Meta, argv: list[str]) -> bool:
    return not meta.stages or stage_of(meta, argv) == meta.stages[-1]


def next_stage(meta: Meta, runs: list[dict[str, Any]]) -> str | None:
    """The stage the task's next run is for: the one after its latest ok apply, the first when none applied yet, and
    the last once the last applied."""
    if not meta.stages:
        return None
    for r in reversed(runs):
        if r.get("mode") == "apply" and r.get("status") == "ok":
            done = stage_of(meta, [str(x) for x in r.get("argv") or []])
            if done is not None:
                i = meta.stages.index(done)
                return meta.stages[min(i + 1, len(meta.stages) - 1)]
    return meta.stages[0]


REQUIRED: dict[str, type] = {
    "id": str,
    "kind": str,
    "created": str,
    "author": str,
    "purpose": str,
    "area": str,
    "entry": str,
    "changes_prod": bool,
    "targets": list,
    "lock": str,
    "lock_reason": str,
    "credentials": dict,
    "uses": list,
    "rollback": str,
}


def validate_meta(raw: Any, folder: Path) -> tuple[Meta | None, list[str]]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["meta.json must be a JSON object"]
    if raw.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    for key, typ in REQUIRED.items():
        if not isinstance(raw.get(key), typ):
            errors.append(f"{key} missing or not {typ.__name__}")
    if errors:
        return None, errors
    meta = Meta(
        **{k: raw[k] for k in REQUIRED},
        links=list(raw.get("links") or []),
        superseded_by=raw.get("superseded_by"),
        abandoned_reason=raw.get("abandoned_reason"),
        archived=raw.get("archived"),
        interpreter=raw.get("interpreter"),
        steps=raw.get("steps"),
        outcome=raw.get("outcome"),
        thread=raw.get("thread"),
        verify=raw.get("verify"),
        stages=raw.get("stages"),
    )
    if meta.stages is not None:
        names = meta.stages
        good = isinstance(names, list) and all(isinstance(n, str) and STAGE_RE.match(n) for n in names)
        if not good or len(names) < 2 or len(set(names)) != len(names) or not meta.changes_prod:
            errors.append(
                'stages names the first argument of each stage of a changing task, in order: ["build", "deploy"]'
            )
    if meta.verify is not None:
        check = folder / str(meta.verify)
        if not isinstance(meta.verify, str) or not meta.changes_prod:
            errors.append("verify names the check script of a changing task")
        elif not check.is_file() or check.suffix not in INTERPRETERS:
            errors.append(f"verify {meta.verify!r} not found or not .py/.sh")
    if meta.thread is not None and not (isinstance(meta.thread, str) and THREAD_RE.match(meta.thread)):
        errors.append("thread must be a lowercase kebab-case name of at most 40 characters")
    if meta.outcome is not None:
        errors += validate_outcome(meta.outcome)
    if meta.archived is not None and not (
        isinstance(meta.archived, dict)
        and isinstance(meta.archived.get("note"), str)
        and meta.archived["note"].strip()
        and isinstance(meta.archived.get("history"), list)
    ):
        errors.append("archived must be an object with a non-empty note and a history list")
    if meta.interpreter is not None:
        interp = Path(str(meta.interpreter)).expanduser()
        if not isinstance(meta.interpreter, str) or not meta.entry.endswith(".py"):
            errors.append("interpreter is a path string and only applies to main.py tasks")
        elif not (interp.is_file() and os.access(interp, os.X_OK)):
            errors.append(f"interpreter {meta.interpreter!r} is not an executable file")
    if meta.kind not in ("task", "general", "plan"):
        errors.append("kind must be task, general or plan")
    id_re = GENERAL_ID_RE if meta.kind == "general" else TASK_ID_RE
    if not id_re.match(meta.id):
        errors.append(f"id {meta.id!r} does not match the {meta.kind} naming convention")
    elif meta.id.split("_")[-2] != meta.area:
        errors.append(f"area {meta.area!r} differs from the area in the id")
    if meta.id != folder.name:
        errors.append(f"id {meta.id!r} differs from folder name {folder.name!r}")
    try:
        parse_iso(meta.created)
    except ValueError:
        errors.append("created must look like 2026-10-30T17:20:00.000Z")
    if not meta.purpose.strip():
        errors.append("purpose is empty")
    if not meta.rollback.strip():
        errors.append('rollback is empty (use "read-only" when nothing changes)')
    elif meta.changes_prod and meta.rollback.strip().lower().rstrip(".") in NO_UNDO:
        errors.append("a changing task needs a rollback that says how to undo it, not " + repr(meta.rollback.strip()))
    if meta.kind == "plan":
        errors += validate_plan(meta)
    else:
        entry = folder / meta.entry
        if not entry.is_file():
            errors.append(f"entry {meta.entry!r} not found")
        elif entry.suffix not in INTERPRETERS:
            errors.append(f"entry must end in one of {sorted(INTERPRETERS)}")
        if meta.steps is not None:
            errors.append("steps are only allowed on plans")
    if not meta.targets or not all(isinstance(t, str) and TARGET_RE.fullmatch(t) for t in meta.targets):
        errors.append(
            "targets must be a non-empty list of lowercase names (hosts, services, systems), a part of one after a /"
            " (node1/nginx, node1/worker:api)"
        )
    if meta.lock not in LOCK_MODES:
        errors.append(f"lock must be one of {LOCK_MODES}")
    if meta.lock == "none" and not meta.lock_reason.strip():
        errors.append("lock none needs a lock_reason")
    if meta.changes_prod and meta.lock == "shared" and not meta.lock_reason.strip():
        errors.append("a prod-changing task with a shared lock needs a lock_reason")
    for env, key in meta.credentials.items():
        if not ENV_NAME_RE.match(env) or not isinstance(key, str) or not key:
            errors.append(f"credentials entry {env!r} must map an UPPER_CASE env name to a dotted key")
    for use in meta.uses:
        if not isinstance(use, str) or not (home_dir() / "general" / use / "meta.json").is_file():
            gone = isinstance(use, str) and (home_dir() / OFFBOARDED / "general" / use).is_dir()
            hint = f" (it is offboarded: `opsquire restore {use}` brings it back)" if gone else ""
            errors.append(f"uses {use!r} is not a general script{hint}")
    return (meta if not errors else None), errors


OUTCOME_STATES = ("done", "partial", "abandoned")
THREAD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def decision(raw: dict[str, Any]) -> dict[str, Any] | None:
    """The recorded decision that no run of this task is coming (abandoned or superseded): who, when and why. None
    for an open task, a task that ended done or partial, and a supersession written by hand with no record."""
    outcome = raw.get("outcome")
    if raw.get("abandoned_reason") and isinstance(outcome, dict) and outcome.get("state") == "abandoned":
        return {
            "state": "abandoned",
            "by": str(outcome.get("closed_by") or "unknown"),
            "at": str(outcome.get("closed") or ""),
            "note": str(outcome.get("note") or ""),
        }
    record = raw.get("superseded")
    if raw.get("superseded_by") and isinstance(record, dict):
        return {
            "state": "superseded",
            "by": str(record.get("by") or "unknown"),
            "at": str(record.get("at") or ""),
            "note": str(record.get("note") or ""),
            "superseded_by": str(raw["superseded_by"]),
        }
    return None


def decision_text(task_id: str, d: dict[str, Any]) -> str:
    what = f"superseded by {d['superseded_by']}" if d["state"] == "superseded" else "abandoned"
    return f"{task_id} was {what} ({d['by']}, {d['at']})" + (f": {d['note']}" if d["note"] else "")


def validate_outcome(outcome: Any) -> list[str]:
    """Errors in a task's closing `outcome` (written by `opsquire close`): a state, a note, who and when."""
    if not isinstance(outcome, dict):
        return ["outcome must be an object"]
    errors: list[str] = []
    if outcome.get("state") not in OUTCOME_STATES:
        errors.append(f"outcome.state must be one of {OUTCOME_STATES}")
    for key in ("note", "closed_by", "closed"):
        if not isinstance(outcome.get(key), str) or not outcome[key].strip():
            errors.append(f"outcome.{key} missing or empty")
    return errors


STEP_KEYS = {"task": str, "args": list, "on_failure": str, "verify": bool}


def validate_plan(meta: Meta) -> list[str]:
    errors: list[str] = []
    if meta.entry:
        errors.append('a plan has no entry script; set entry to ""')
    if meta.credentials or meta.uses or meta.interpreter:
        errors.append("credentials, uses and interpreter belong on the step tasks, not the plan")
    if not isinstance(meta.steps, list) or not meta.steps:
        return errors + ["a plan needs a non-empty steps list"]
    targets: set[str] = set()
    changes = False
    for i, step in enumerate(meta.steps, 1):
        if not isinstance(step, dict) or not isinstance(step.get("task"), str):
            errors.append(f"step {i} must be an object with a task id")
            continue
        for key, value in step.items():
            if key not in STEP_KEYS or not isinstance(value, STEP_KEYS[key]):
                errors.append(f"step {i}: {key!r} is not one of {sorted(STEP_KEYS)} with the right type")
        for field_name in ("task", "on_failure"):
            ref = step.get(field_name)
            if not isinstance(ref, str):
                continue
            folder = task_folder(ref)
            if folder and peek_kind(folder) == "plan":
                errors.append(f"step {i}: {ref!r} must be a live task, not a plan (plans do not nest)")
                continue
            sub = load_task(folder) if folder else None
            if sub is None or sub.meta is None:
                errors.append(f"step {i}: {field_name} {ref!r} is not a valid task (use the full task id)")
            elif sub.meta.kind != "task" or sub.meta.archived or sub.meta.superseded_by or sub.meta.abandoned_reason:
                errors.append(f"step {i}: {ref!r} must be a live task, not a {sub.meta.kind} or retired task")
            elif field_name == "task":
                targets |= set(sub.meta.targets)
                changes |= sub.meta.changes_prod
    if not errors:
        if sorted(meta.targets) != sorted(targets):
            errors.append(f"plan targets must be the union of its steps' targets: {sorted(targets)}")
        if meta.changes_prod != changes:
            errors.append(f"plan changes_prod must be {changes} (true if any step changes prod)")
    return errors


def peek_kind(folder: Path) -> str:
    try:
        raw = read_json(folder / "meta.json")
    except (OSError, ValueError):
        return ""
    return str(raw.get("kind", "")) if isinstance(raw, dict) else ""


def task_folder(task_id: str) -> Path | None:
    matches = [p for p in (home_dir() / "tasks").glob(f"*/{task_id}") if p.is_dir()]
    return matches[0] if len(matches) == 1 else None


@dataclass
class Task:
    folder: Path
    raw: dict[str, Any]
    meta: Meta | None
    errors: list[str]

    @property
    def id(self) -> str:
        return self.folder.name

    @property
    def kind(self) -> str:
        if self.folder.parent.name == "general":
            return "general"
        return "plan" if self.raw.get("kind") == "plan" else "task"

    def roots(self) -> list[Path]:
        roots = [self.folder]
        if self.meta:
            roots += [home_dir() / "general" / u for u in self.uses_all()]
            for step in self.meta.steps or []:
                folder = task_folder(str(step.get("task")))
                if folder:
                    roots.append(folder)
        return roots

    def _walk(self) -> tuple[list[Path], list[Path]]:
        """The regular files under the roots, and the symlinks there. A link is never followed: hashing, lint and
        snapshots would read whatever it points at, the operator's credentials included."""
        files: list[Path] = []
        links: list[Path] = []
        for root in self.roots():
            if root.is_symlink():
                links.append(root)
                continue
            for top, dirs, names in os.walk(root):
                here = Path(top)
                if IGNORED_PARTS & set(here.relative_to(root).parts):
                    continue
                for name in sorted([*dirs, *names]):
                    p = here / name
                    if p.is_symlink():
                        links.append(p)
                    elif name in names and name != LINT_FILE and name not in IGNORED_PARTS and p.is_file():
                        files.append(p)
        return sorted(files), sorted(links)

    def files(self) -> list[Path]:
        return self._walk()[0]

    def links(self) -> list[Path]:
        return self._walk()[1]

    def uses_all(self) -> list[str]:
        """The general scripts a run of this task brings: those its `uses` lists and, in turn, those theirs list."""
        out: list[str] = []
        todo = list(self.meta.uses) if self.meta else []
        while todo:
            use = todo.pop(0)
            if use in out:
                continue
            out.append(use)
            folder = home_dir() / "general" / use
            if folder.is_dir():
                general = load_task(folder)
                todo += general.meta.uses if general.meta else []
        return out

    def file_hashes(self) -> dict[str, str]:
        """Content hashes of every file that runs. meta.json is hashed without its ANNOTATIONS: closing, judging,
        handing over or holding a task does not change what it does, so it must not make it draft."""
        home = home_dir()
        return {
            str(p.relative_to(home)): by_file("sha256", p, functools.partial(content_digest, p)) for p in self.files()
        }

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for name, digest in self.file_hashes().items():
            h.update(f"{name}\0{digest}\n".encode())
        return h.hexdigest()


ANNOTATIONS = ("outcome", "judged", "handover", "hold")


FILE_CACHE: dict[tuple[str, str], tuple[tuple[int, int], Any]] = {}


def by_file[T](kind: str, path: Path, build: Callable[[], T]) -> T:
    """What build() derives from one file, computed again only when the file's mtime or size changed: the console
    rebuilds its state from the same thousands of unchanged files on every refresh."""
    st = os.stat(path)
    stamp = (st.st_mtime_ns, st.st_size)
    hit = FILE_CACHE.get((kind, str(path)))
    if hit and hit[0] == stamp:
        value: T = hit[1]
        return value
    fresh = build()
    FILE_CACHE[(kind, str(path))] = (stamp, fresh)
    return fresh


def code_version(files: dict[str, str]) -> str:
    """What a task's run record or current files do, for comparing runs: the hashes of `file_hashes` without the
    Markdown, since closing a task appends its `## Result` to design.md. "" when the record has no files."""
    code = {name: digest for name, digest in files.items() if not name.endswith(".md")}
    if not code:
        return ""
    return hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()


def content_digest(path: Path) -> str:
    return hashlib.sha256(content_for_hash(path)).hexdigest()


def content_for_hash(path: Path) -> bytes:
    data = path.read_bytes()
    if path.name != "meta.json":
        return data
    try:
        raw = json.loads(data)
    except ValueError:
        return data
    if not isinstance(raw, dict):
        return data
    return json.dumps({k: v for k, v in raw.items() if k not in ANNOTATIONS}, sort_keys=True).encode()


GENERAL_REF = re.compile(r"[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*")
SCRIPT_SUFFIXES = (".py", ".sh")


def undeclared_generals(task: Task) -> list[str]:
    """General scripts that the task's scripts, or the general scripts they bring, name although no `uses` lists them
    (the task's own or that of a general script it uses). A run carries only declared general scripts, so the
    runner refuses such a task and lint fails it."""
    general = home_dir() / "general"
    known = {p.name for p in general.iterdir() if p.is_dir()} if general.is_dir() else set()
    have = set(task.uses_all())
    found: set[str] = set()
    queue = [p for p in task.files() if p.suffix in SCRIPT_SUFFIXES]
    while queue:
        path = queue.pop()
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for name in set(GENERAL_REF.findall(text)) & known:
            if name not in have and name not in found and name != task.id:
                found.add(name)
                queue.extend(p for p in (general / name).rglob("*") if p.is_file() and p.suffix in SCRIPT_SUFFIXES)
    return sorted(found)


def load_task(folder: Path) -> Task:
    """A task as its folder holds it now. Parsed again when meta.json or the folder's file list changed (a verify
    script appearing, say), or a general script or task folder was added or removed, since validation checks that
    its uses and a plan's steps exist; each caller gets its own copy of the raw meta."""
    try:
        home = home_dir()
        dirs = [folder, home / "general", *sorted((home / "tasks").glob("*/"))]
        stamp = ",".join(str(os.stat(d).st_mtime_ns) for d in dirs if d.is_dir())
        task = by_file(f"task:{stamp}", folder / "meta.json", functools.partial(read_task, folder))
    except OSError:
        task = read_task(folder)
    return Task(task.folder, dict(task.raw), task.meta, list(task.errors))


def read_task(folder: Path) -> Task:
    meta_path = folder / "meta.json"
    try:
        raw = read_json(meta_path)
    except (OSError, json.JSONDecodeError) as e:
        return Task(folder, {}, None, [f"meta.json unreadable: {e}"])
    meta, errors = validate_meta(raw, folder)
    return Task(folder, raw if isinstance(raw, dict) else {}, meta, errors)


def all_task_folders() -> list[Path]:
    home = home_dir()
    tasks = sorted(p for p in (home / "tasks").glob("*/*") if p.is_dir() and not p.name.startswith("."))
    general = sorted(p for p in (home / "general").glob("*") if p.is_dir() and not p.name.startswith("."))
    return tasks + general


def offboarded_folders() -> list[Path]:
    """Settled tasks that left tasks/ (and general scripts taken out by hand): same layout, under offboarded/."""
    root = home_dir() / OFFBOARDED
    return sorted(p for p in root.glob("*/*") if p.is_dir() and not p.name.startswith("."))


def is_offboarded(folder: Path) -> bool:
    return folder.parent.parent.name == OFFBOARDED


def match_folder(folders: list[Path], query: str) -> Path | None:
    q = query.rstrip("/").split("/")[-1]
    exact = [f for f in folders if f.name == q]
    if exact:
        return exact[0]
    matches = [f for f in folders if q in f.name]
    if len(matches) > 1:
        names = "\n  ".join(m.name for m in matches)
        raise OpsquireError(f"{query!r} matches several tasks:\n  {names}")
    return matches[0] if matches else None


def resolve_task(query: str, offboarded: bool = False) -> Task:
    """The task a command names. Commands that only read a task (show, log, runs), restore and offboard pass
    offboarded=True; for every other command an offboarded task is not there to work on, and the error says how to
    bring it back."""
    folder = match_folder(all_task_folders(), query)
    if folder:
        return load_task(folder)
    folder = match_folder(offboarded_folders(), query)
    if folder is None:
        home = home_dir()
        raise OpsquireError(f"no task matches {query!r}" + ("" if home.is_dir() else f": there is {no_home(home)}"))
    if not offboarded:
        raise OpsquireError(
            f"{folder.name} is offboarded (settled and idle): `opsquire restore {folder.name}` brings it back, "
            f"`opsquire show {folder.name}` reads it"
        )
    return load_task(folder)
