from __future__ import annotations

import ast
import contextlib
import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import isolate, share
from .creds import Secrets
from .model import (
    DATA_DIR,
    HELPERS_DIR,
    INTERPRETERS,
    LINT_FILE,
    OpsquireError,
    Task,
    by_file,
    home_dir,
    iso,
    load_task,
    now,
    read_json,
    task_folder,
    undeclared_generals,
    write_json,
)

RUFF_CONFIG = DATA_DIR / "ruff.toml"
MYPY_CONFIG = DATA_DIR / "mypy.ini"


@dataclass
class LintResult:
    ok: bool
    content_hash: str
    output: str
    tests: int = 0
    interrupted: bool = False
    isolation: str = ""


class Interrupted(Exception):
    """A lint tool was stopped by the operator's Ctrl-C (the signal reaches the whole process group), so its exit
    says nothing about the files."""


TEST_TIMEOUT = 600


def find_tool(name: str) -> str | None:
    override = os.environ.get(f"OPSQUIRE_{name.upper()}")
    if override:
        return override
    candidates = [Path(sys.executable).parent / name, Path(sysconfig.get_path("scripts", f"{os.name}_user")) / name]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which(name)


def _run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    # A Python tool hit by Ctrl-C while it starts up aborts with a fatal error ending in KeyboardInterrupt instead.
    if proc.returncode == -signal.SIGINT or (proc.returncode and proc.stderr.rstrip().endswith("KeyboardInterrupt")):
        raise Interrupted(Path(cmd[0]).name)
    text = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, f"$ {' '.join(cmd)}\n{text}" if text else f"$ {' '.join(cmd)}\nok"


def lint_task(task: Task) -> LintResult:
    """Lint the task's files, run its tests and say whether it is ready. Ctrl-C while a tool runs is an interruption,
    never a verdict: the main thread gets its KeyboardInterrupt as usual, a worker thread gets a result marked
    interrupted that callers report as such and never save."""
    try:
        result = _lint_task(task)
        result.output = redacted(result.output)
        return result
    except Interrupted as e:
        if threading.current_thread() is threading.main_thread():
            raise KeyboardInterrupt from e
        return LintResult(False, task.content_hash(), f"interrupted: {e} was stopped by Ctrl-C", interrupted=True)


def redacted(text: str) -> str:
    """Lint output with the operator's known credential values masked: a tool's diagnostic may quote a file."""
    try:
        return Secrets(required=False).redactor()(text)
    except OpsquireError:
        return text


PY_CONFIRM = re.compile(r"(?<!def )(?<![\w.])(?:sq\.)?confirm\(")
SH_CONFIRM = re.compile(r"^[^#\n]*\bsq_confirm\b(?!\s*\(\))", re.M)


def asks_in(path: Path) -> bool:
    return bool((PY_CONFIRM if path.suffix == ".py" else SH_CONFIRM).search(path.read_text()))


def asks_first(task: Task) -> bool:
    """Whether the task's scripts, or the general scripts it uses, ask the operator for a yes somewhere. Tests and
    the verify check (read-only) do not count."""
    if task.meta is None or task.kind != "task":
        return False
    folders = [task.folder, *(home_dir() / "general" / u for u in task.uses_all())]
    for folder in folders:
        for f in sorted(folder.glob("*.py")) + sorted(folder.glob("*.sh")):
            if f.name.startswith("test_") or (folder == task.folder and f.name == task.meta.verify):
                continue
            with contextlib.suppress(OSError, UnicodeDecodeError):
                if by_file("asks", f, functools.partial(asks_in, f)):
                    return True
    return False


def asking_note(task: Task) -> str:
    if task.meta is None or task.kind != "task" or not task.meta.changes_prod or asks_first(task):
        return ""
    return "note: the scripts never ask before changing production; the console asks for a second click instead"


TARGETS_READ = re.compile(r"\bsq\.TARGETS\b|\bOPSQUIRE_TARGETS\b")


def host_names_note(task: Task) -> str:
    """A target naming part of a machine (`node1/nginx`) reaches `sq.TARGETS` as that part, so a script that reads
    it as machine names looks for a machine that does not exist."""
    parts = [t for t in (task.meta.targets if task.meta else []) if "/" in t]
    if not parts:
        return ""
    folders = [task.folder, *(home_dir() / "general" / u for u in task.uses_all())]
    readers = [
        f"{folder.name}/{f.name}"
        for folder in folders
        if folder.is_dir()
        for f in sorted(folder.iterdir())
        if f.suffix in (".py", ".sh")
        and not f.name.startswith("test_")
        and TARGETS_READ.search(f.read_text(errors="replace"))
    ]
    if not readers:
        return ""
    return (
        f"warning: {', '.join(parts)} names part of a machine, and {', '.join(readers)} reads sq.TARGETS, which holds"
        " that part rather than a machine name: connect through sq.HOSTS (bash sq_hosts) and keep sq.TARGETS for"
        " per-target status"
    )


SH_READ = re.compile(r"^(?:[^#\n]*[;&]\s*)?\s*read\b([^\n]*)$", re.M)


def typed_input(task: Task) -> tuple[list[str], list[str]]:
    """Where the task's scripts wait for the operator to type: plain text (`input()`, bash `read` from the terminal) is
    refused, since a value the run acts on belongs in its arguments and the record; a hidden secret (getpass, `read -s`)
    may be typed, but only in a terminal."""
    plain: list[str] = []
    hidden: list[str] = []
    for f in task.files():
        if f.name.startswith("test_") or f.suffix not in (".py", ".sh"):
            continue
        text = f.read_text(errors="replace")
        if f.suffix == ".py":
            with contextlib.suppress(SyntaxError):
                for node in ast.walk(ast.parse(text)):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "input":
                        plain.append(f"{f.name}:{node.lineno}")
                    elif getattr(func, "id", getattr(func, "attr", "")) == "getpass":
                        hidden.append(f"{f.name}:{node.lineno}")
            continue
        for m in SH_READ.finditer(text):
            line, rest = m.group(0), m.group(1)
            if "<" in rest or "|" in line or re.search(r"\bwhile\b", line):
                continue
            where = f"{f.name}:{text.count(chr(10), 0, m.start()) + 1}"
            (hidden if re.search(r"(?:^|\s)-\w*s", rest) else plain).append(where)
    errors, notes = [], []
    if plain:
        errors.append(
            f"error: {', '.join(plain)} waits for typed input, which the console cannot answer and a scheduled run"
            " waits on forever: take the value as an argument"
        )
    if hidden:
        notes.append(
            f"note: {', '.join(hidden)} asks for a hidden value, which only a terminal can type, not the console"
        )
    return errors, notes


def _lint_task(task: Task) -> LintResult:
    content_hash = task.content_hash()
    if task.errors or task.meta is None:
        return LintResult(False, content_hash, "meta.json:\n  " + "\n  ".join(task.errors))
    if task.meta.kind == "plan":
        return lint_plan(task, content_hash)
    if task.meta.archived:
        return LintResult(False, content_hash, "archived: historical record kept byte-for-byte, not linted or runnable")
    links = task.links()
    if links:
        shown = ", ".join(str(p) for p in links)
        return LintResult(False, content_hash, f"symlinks are never task files (copy the file in instead): {shown}")
    files = task.files()
    py = [str(p) for p in files if p.suffix == ".py"]
    sh = [str(p) for p in files if p.suffix == ".sh"]
    other = [p for p in files if p.suffix not in (".py", ".sh", ".json", ".md", ".txt", ".sql", ".toml")]
    ok = True
    parts: list[str] = []
    if other:
        ok = False
        parts.append("unsupported files (no linter): " + ", ".join(str(p) for p in other))
    undeclared = undeclared_generals(task) if task.kind == "task" else []
    if undeclared:
        ok = False
        parts.append(
            f"error: the scripts name general scripts that no `uses` lists: {', '.join(undeclared)}; add them to "
            "`uses` in meta.json (the run refuses the task until then)"
        )
    if py:
        ruff, mypy = find_tool("ruff"), find_tool("mypy")
        if not ruff or not mypy:
            missing = "ruff and mypy must be installed next to opsquire (uv tool install --with ruff --with mypy ...)"
            return LintResult(False, content_hash, missing)
        uses = [str(home_dir() / "general" / u) for u in task.uses_all()]
        by_folder: dict[str, list[str]] = {}
        for f in py:
            by_folder.setdefault(str(Path(f).parent), []).append(f)
        mypy_cmd = [mypy, "--config-file", str(MYPY_CONFIG)]
        # mypy refuses a MYPYPATH under its own site-packages, which is where an installed opsquire keeps its helpers
        # when mypy shares its environment, so it reads a copy.
        with tempfile.TemporaryDirectory(prefix="opsquire-lint-") as scratch:
            helpers = Path(scratch) / "helpers"
            helpers.mkdir()
            shutil.copy(HELPERS_DIR / "opsquire_helpers.py", helpers)
            env = isolate.scrubbed_env(Path(scratch), MYPYPATH=os.pathsep.join([str(helpers), *uses]))
            for cmd in (
                [ruff, "check", "--no-cache", "--config", str(RUFF_CONFIG), *py],
                *([*mypy_cmd, *files] for files in by_folder.values()),
            ):
                passed, out = _run(cmd, env)
                ok &= passed
                parts.append(out)
    if sh:
        if not INTERPRETERS[".sh"][0]:
            return LintResult(False, content_hash, "bash tasks need bash 4 or later, not macOS's: brew install bash")
        shellcheck = find_tool("shellcheck")
        if not shellcheck:
            return LintResult(False, content_hash, "shellcheck must be installed")
        with tempfile.TemporaryDirectory(prefix="opsquire-lint-") as scratch:
            passed, out = _run([shellcheck, "-x", "-P", str(HELPERS_DIR), *sh], isolate.scrubbed_env(Path(scratch)))
        ok &= passed
        parts.append(out)
    tests, tests_ok, tests_out, isolation = run_tests(task)
    ok &= tests_ok
    if tests_out:
        parts.append(tests_out)
    typed, hidden = typed_input(task)
    ok &= not typed
    parts += [*typed, *hidden, *(n for n in (asking_note(task), host_names_note(task)) if n)]
    return LintResult(ok, content_hash, "\n\n".join(parts), tests, isolation=isolation)


def run_tests(task: Task) -> tuple[int, bool, str, str]:
    """unittest discovery in every folder holding test_*.py (the task's own and the general scripts it uses), on
    copies of those files, isolated as `isolate` describes: tests run offline, without the operator's home,
    environment or credentials, and cannot change the files they test. Also says which isolation level ran them.
    Below bwrap or sandbox-exec, without the operator's opt-in and with the credentials file readable, no test runs
    and the result fails."""
    assert task.meta
    files = task.files()
    folders = sorted({p.parent for p in files if p.name.startswith("test_") and p.suffix == ".py"})
    if not folders:
        return 0, True, "", ""
    python = task.meta.command(Path("main.py"))[0]
    chosen = isolate.level(python)
    if chosen not in isolate.STRONG and not isolate.weaker_ok_here():
        return 0, False, isolate.refusal(chosen), ""
    total, ok, report = 0, True, []
    with isolate.Sandbox(files, home_dir(), python) as box:
        uses = [str(box.copy_of(home_dir() / "general" / u)) for u in task.uses_all()]
        general = str(box.copy_of(home_dir() / "general"))
        for folder in folders:
            copy = box.copy_of(folder)
            path = os.pathsep.join([str(copy), str(box.helpers), *uses])
            env = box.env(PYTHONPATH=path, OPSQUIRE_TESTING="1", OPSQUIRE_GENERAL=general)
            cmd = [python, "-m", "unittest", "discover", "-s", str(copy), "-t", str(copy), "-p", "test_*.py"]
            try:
                r = subprocess.run(
                    box.command(cmd, copy), cwd=copy, capture_output=True, text=True, env=env, timeout=TEST_TIMEOUT
                )
                code, text = r.returncode, box.real_paths(r.stdout + r.stderr)
            except subprocess.TimeoutExpired:
                code, text = 1, f"timed out after {TEST_TIMEOUT}s"
            if code == -signal.SIGINT:
                raise Interrupted("unittest")
            m = re.search(r"Ran (\d+) tests?", text)
            ran = int(m.group(1)) if m else 0
            passed = code == 0 and ran > 0
            total += ran
            ok &= passed
            head = f"$ unittest {folder}: {'ok' if passed else 'FAILED'} ({ran} tests)"
            report.append(head if passed else head + "\n" + text.strip()[-3000:])
        isolation = box.level()
    refused = isolate.why_not_bwrap() if isolation not in isolate.STRONG else ""
    withheld = isolation not in isolate.STRONG and not isolate.weaker_allowed()
    note = "; the credentials file is withheld from this caller, so tests get no more than it has" if withheld else ""
    report.append(f"tests isolated: {isolation}" + (f" (bwrap refused: {refused}{note})" if refused else note))
    return total, ok, "\n".join(report), isolation


def lint_plan(plan: Task, content_hash: str) -> LintResult:
    assert plan.meta
    steps: list[LintResult] = []
    report: list[str] = []
    for step in plan.meta.steps or []:
        folder = task_folder(step["task"])
        sub = lint_task(load_task(folder)) if folder else LintResult(False, "", f"{step['task']} not found")
        steps.append(sub)
        report.append(f"== step {step['task']}: {'ok' if sub.ok else 'FAILED'}\n{sub.output}")
    return LintResult(
        all(s.ok for s in steps),
        content_hash,
        "\n\n".join(report),
        sum(s.tests for s in steps),
        interrupted=any(s.interrupted for s in steps),
        isolation=next((s.isolation for s in steps if s.isolation), ""),
    )


def save_lint(task: Task, result: LintResult) -> None:
    if result.interrupted:
        return
    share.adopt([task.folder, *(home_dir() / "general" / u for u in task.uses_all())])
    with contextlib.suppress(OSError):
        write_json(
            task.folder / LINT_FILE,
            {
                "ok": result.ok,
                "content_hash": result.content_hash,
                "checked": iso(now()),
                "tests": result.tests,
                "output": result.output,
                **({"isolation": result.isolation} if result.isolation else {}),
            },
        )


def lint_changed_scripts(task: Task) -> dict[str, LintResult]:
    """Lint, each on its own, the general scripts this task uses whose files changed since their last lint, and
    stamp them: a script edited for one task is checked then, not when the next task reuses it."""
    out: dict[str, LintResult] = {}
    for use in task.meta.uses if task.meta else []:
        script = load_task(home_dir() / "general" / use)
        if cached_lint_ok(script) is None:
            out[use] = lint_task(script)
            save_lint(script, out[use])
    return out


def last_lint(task: Task) -> dict[str, Any] | None:
    """The task's last lint stamp, whatever files it was taken on."""
    try:
        data = read_json(task.folder / LINT_FILE)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cached_lint_ok(task: Task) -> bool | None:
    data = last_lint(task)
    if data is None:
        return None
    try:
        current = task.content_hash()
    except OSError:
        return None
    if data.get("content_hash") != current:
        return None
    return bool(data.get("ok"))
