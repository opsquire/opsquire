"""Structured events for scripts run by `opsquire run`. Remote payloads can't import this; they print the same
`@@OPSQUIRE {json}` lines themselves (see AGENTS.md)."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import re
import select
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any, TextIO

MODE = os.environ.get("OPSQUIRE_MODE", "dry")
APPLY = MODE == "apply"
TARGETS = os.environ.get("OPSQUIRE_TARGETS", "").split()
HOSTS = list(dict.fromkeys(t.split("/", 1)[0] for t in TARGETS))


def emit(event: str, msg: str = "", **data: Any) -> None:
    sys.stdout.write("@@OPSQUIRE " + json.dumps({"event": event, "msg": msg, **data}) + "\n")
    sys.stdout.flush()


def step(msg: str, **data: Any) -> None:
    emit("step", msg, **data)


def target(name: str, status: str, msg: str = "", **data: Any) -> None:
    emit("target", msg, target=name, status=status, **data)


def result(msg: str, **data: Any) -> None:
    emit("result", msg, **data)


def warn(msg: str, **data: Any) -> None:
    emit("warn", msg, **data)


def error(msg: str, **data: Any) -> None:
    emit("error", msg, **data)


def data(msg: str, **values: Any) -> None:
    emit("data", msg, **values)


def lock_token(target: str) -> int | None:
    """This run's fencing token on target, for a guard on the target to compare; None when it holds no lock there."""
    for pair in os.environ.get("OPSQUIRE_LOCK_TOKENS", "").split():
        name, _, token = pair.rpartition("=")
        if name == target:
            return int(token)
    return None


INPUT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def save(name: str, obj: Any) -> bool:
    """In a dry run, keep an expensive input (JSON-able: what took long to read and barely changes before the apply)
    for the apply this dry run unlocks; `load` gives it back there. False, and nothing kept, in any other run."""
    folder = os.environ.get("OPSQUIRE_INPUTS_SAVE")
    if not INPUT_NAME.fullmatch(name):
        raise ValueError(f"input name {name!r}: lowercase letters, digits, _ and -")
    if not folder:
        return False
    data = gzip.compress(json.dumps(obj, separators=(",", ":")).encode())
    path = Path(folder) / f"{name}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_bytes(data)
    tmp.replace(path)
    emit("saved", name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    return True


def load(name: str, max_age: str | float | None = None) -> Any:
    """In an apply, the input its dry run kept with `save`: None when that dry run kept none, it is older than
    `max_age` (seconds, or "30m", "2h", "1d"), or it is not what the dry run's record says it kept. On None, read it
    fresh. The apply's record lists what it used and how old it was."""
    folder, source = os.environ.get("OPSQUIRE_INPUTS"), os.environ.get("OPSQUIRE_INPUTS_FROM", "")
    path = Path(folder or "/nonexistent") / f"{name}.json.gz"
    if not folder or not path.is_file():
        return None
    try:
        data = path.read_bytes()
        kept = json.loads((path.parent.parent / "run.json").read_text()).get("inputs") or {}
    except (OSError, ValueError):
        return None
    sha = hashlib.sha256(data).hexdigest()
    age = time.time() - path.stat().st_mtime
    limit = max_age if isinstance(max_age, (int, float)) or max_age is None else _seconds(max_age)
    why = (
        "not what its dry run recorded"
        if not any(s.get("name") == name and s.get("sha256") == sha for s in kept.get("saved") or [])
        else f"older than {max_age}"
        if limit is not None and age > limit
        else ""
    )
    emit("input", name, from_run=source, age_s=round(age), sha256=sha, used=not why, **({"why": why} if why else {}))
    return None if why else json.loads(gzip.decompress(data))


def _seconds(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text.strip())
    if not m:
        raise ValueError(f"max_age {text!r}: a number of seconds, or like 30m, 2h, 1d")
    return float(m.group(1)) * AGE_UNITS[m.group(2)]


def use(general: str, module: str = "main") -> ModuleType:
    """A general script's `module` (its main.py by default) as a module, loaded once per process, with the general's
    folder first on sys.path so its own sibling imports work. The task lists the general in `uses`, as for any general
    script it calls; one it does not list is not in the run."""
    folder = Path(os.environ.get("OPSQUIRE_GENERAL", "")) / general
    path = folder / f"{module}.py"
    if "OPSQUIRE_GENERAL" not in os.environ or not path.is_file():
        raise ImportError(f"{general}/{module}.py is not in this run: list {general} in the task's uses")
    name = "opsquire_general_" + re.sub(r"\W", "_", f"{general}_{module}")
    if name in sys.modules:
        return sys.modules[name]
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    try:
        spec.loader.exec_module(loaded)
    except BaseException:
        del sys.modules[name]
        raise
    return loaded


_LOADER = """import linecache as _lc, sys as _sys, types as _types
def _opsquire_load(name, path, source, main=False):
    _lc.cache[path] = (len(source), None, source.splitlines(True), path)
    code = compile(source, path, "exec", dont_inherit=True)
    if main:
        exec(code, {"__name__": "__main__", "__file__": path, "__builtins__": __builtins__})
        return
    module = _types.ModuleType(name)
    module.__file__ = path
    _sys.modules[name] = module
    exec(code, module.__dict__)
"""


def bundle(payload: str | Path, *modules: str | Path) -> str:
    """The text to pipe to a remote `python -u -` so the payload can `import <module>` there as it does locally.
    Each module file (for example a general script's library: `$OPSQUIRE_GENERAL/<id>/<name>.py`) is registered
    under its file name before the payload runs as __main__; tracebacks keep each file's own name and line numbers.
    Nothing is written on the host."""
    parts = [_LOADER]
    for m in map(Path, modules):
        parts.append(f"_opsquire_load({m.stem!r}, {m.name!r}, {m.read_text()!r})\n")
    path = Path(payload)
    parts.append(f"_opsquire_load('__main__', {path.name!r}, {path.read_text()!r}, main=True)\n")
    return "".join(parts)


def _announce_prompt(question: str) -> None:
    """Record that the run now waits for the operator, and hold the prompt until everything printed so far is on
    screen (the runner echoes stdout via a pipe). A path, not a file descriptor: subprocess closes inherited
    descriptors by default, and a general script run by a task must get this without the task knowing."""
    emit("waiting", question)
    path = os.environ.get("OPSQUIRE_SYNC_PATH")
    if not path:
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        ready, _, _ = select.select([fd], [], [], 10)
        if ready:
            os.read(fd, 1)
    except OSError:
        pass
    finally:
        os.close(fd)


_last_progress = [0.0]


def progress(label: str, done: float, total: float, unit: str = "") -> None:
    """Report progress; opsquire draws one updating bar and logs every 10%. unit "B" means bytes (shown as
    KB/MB/GB with speed). Calls closer than 0.2 s apart are dropped, except the final one."""
    now = time.monotonic()
    if done < total and now - _last_progress[0] < 0.2:
        return
    _last_progress[0] = now
    emit("progress", label, done=done, total=total, unit=unit)


def confirm(question: str) -> None:
    _announce_prompt(question)
    try:
        with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
            answer = _ask(out, inp, f"\n>>> {question} [y/N] ").strip().lower()
    except OSError:
        answer = ""
    emit("confirm", question, answer="y" if answer == "y" else "n")
    if answer != "y":
        sys.exit(3)


def _ask(out: TextIO, inp: TextIO, prompt: str) -> str:
    """Print the prompt and read the reply. A Ctrl-C handled just before a blocking read starts never ends that read,
    so the prompt would hang: the reply is awaited in select, which the signal's wakeup byte ends too, and the handler
    (KeyboardInterrupt for Ctrl-C) runs before it waits again."""
    woken, wake = os.pipe()
    os.set_blocking(wake, False)
    previous = signal.set_wakeup_fd(wake) if threading.current_thread() is threading.main_thread() else None
    try:
        out.write(prompt)
        out.flush()
        while inp.fileno() not in select.select([inp.fileno(), woken], [], [])[0]:
            os.read(woken, 64)
        return inp.readline()
    finally:
        if previous is not None:
            signal.set_wakeup_fd(previous)
        os.close(woken)
        os.close(wake)


def _to_interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def run(main: Callable[[], None], cleanup: Callable[[], None] | None = None) -> None:
    """Run main with graceful termination: Ctrl-C, SIGTERM or SIGHUP run cleanup, report, and exit 130. An exception
    runs cleanup too and is reported as an error event with its traceback (the record's last_error names it)."""
    signal.signal(signal.SIGTERM, _to_interrupt)
    signal.signal(signal.SIGHUP, _to_interrupt)
    code = 0
    try:
        main()
    except KeyboardInterrupt:
        code = 130
    except Exception:
        error(traceback.format_exc().rstrip())
        code = 1
    finally:
        if cleanup:
            cleanup()
    if code == 130:
        warn("interrupted by the operator")
    sys.exit(code)
