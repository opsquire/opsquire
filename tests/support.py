"""Shared helpers and the base class every test module builds on."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import grp
import http.client
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
LINUX_ONLY = unittest.skipIf(sys.platform == "darwin", "shared homes need Linux: a Mac drops setgid on folders")


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


UI_TOKEN = "test-console-secret"


class Console(http.client.HTTPConnection):
    """A connection to `opsquire ui` on `port` that sends the secret the test environment gave it."""

    def __init__(self, port: int, timeout: float) -> None:
        super().__init__("127.0.0.1", port, timeout=timeout)
        self.token = UI_TOKEN

    def putrequest(self, method: str, url: str, skip_host: bool = False, skip_accept_encoding: bool = False) -> None:
        super().putrequest(method, url, skip_host, skip_accept_encoding)
        self.putheader("X-Opsquire-Token", self.token)
        self.putheader("X-Opsquire-Request", "1")


CLI = [sys.executable, "-m", "opsquire"]


BASH_HEADER = (
    '#!/usr/bin/env bash\nset -euo pipefail\n# shellcheck source=opsquire.sh\nsource "$OPSQUIRE_HELPERS/opsquire.sh"\n'
)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


ASKING_BODY = """import opsquire_helpers as sq


def main() -> None:
    sq.step("Pre-checks (read-only)")

    if sq.APPLY:
        sq.confirm("Apply the change?")
        sq.step("Apply")

    for name in sq.TARGETS:
        sq.target(name, "ok")
    sq.result("done")


if __name__ == "__main__":
    sq.run(main)
"""


def run_in_pty(
    cmd: list[str], env: dict[str, str], answers: list[tuple[str, str]], size: tuple[int, int] | None = None
) -> subprocess.CompletedProcess[str]:
    fd, slave = pty.openpty()
    if size:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
    try:
        proc = subprocess.Popen(
            cmd, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True, preexec_fn=tty_of(slave)
        )
    finally:
        os.close(slave)
    out = b""
    pending = list(answers)
    seen = 0
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        at = out.find(pending[0][0].encode(), seen) if pending else -1
        if at >= 0:
            seen = at + len(pending[0][0].encode())
            answer = pending.pop(0)[1]
            os.write(fd, (answer if answer == "\x03" else answer + "\n").encode())
    if time.monotonic() >= deadline:
        out += b"\n[run_in_pty: no exit in 120 s; the session's processes, then SIGABRT for every thread's stack]\n"
        out += session_report(proc.pid).encode()
        proc.send_signal(signal.SIGABRT)
        out += drain(fd, 5.0)
        if proc.poll() is None:
            proc.kill()
    code = proc.wait()
    os.close(fd)
    return subprocess.CompletedProcess(cmd, code, out.decode(errors="replace"), "")


def session_report(sid: int) -> str:
    """Every process in a session: command, state, what it waits in and its signal masks (blocked, ignored, caught)."""
    lines = []
    for stat in sorted(Path("/proc").glob("[0-9]*/stat")):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            if int(fields[3]) != sid:
                continue
            folder = stat.parent
            status = dict(
                line.split(":\t", 1) for line in (folder / "status").read_text().splitlines() if ":\t" in line
            )
            cmd = (folder / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            wchan = (folder / "wchan").read_text().strip()
        except (OSError, ValueError, IndexError):
            continue
        masks = " ".join(f"{k}={status.get(k, '?').strip()}" for k in ("SigBlk", "SigIgn", "SigCgt"))
        lines.append(f"{folder.name} {fields[0]} wchan={wchan} {masks} {cmd[:160]}")
    return "\n".join(lines) + "\n"


def drain(fd: int, seconds: float) -> bytes:
    got, until = b"", time.monotonic() + seconds
    while time.monotonic() < until and select.select([fd], [], [], 0.2)[0]:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        got += chunk
    return got


def tty_of(slave: int) -> Any:
    """Make the pty the child's controlling terminal (as the console's jobs do): never pty.fork() from a process with
    threads, which can deadlock the child."""

    def hook() -> None:
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    return hook


class OpsTestCase(unittest.TestCase):
    """A scratch home per test, with the helpers every area uses."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))).resolve()
        self.home = self.tmp / "home"
        self.ui_errs: dict[int, Path] = {}
        (self.home / "tasks").mkdir(parents=True)
        (self.home / "general").mkdir()
        self.creds = self.tmp / "creds.json"
        self.creds.write_text(json.dumps({"metrics": {"prod": {"token": "SECRET-TOKEN-123456"}}}))
        self.env = {
            **{k: v for k, v in os.environ.items() if k != "WSL_DISTRO_NAME"},
            "OPSQUIRE_HOME": str(self.home),
            "OPSQUIRE_CREDS": str(self.creds),
            "OPSQUIRE_AUTHOR": "test",
            "OPSQUIRE_UI_TOKEN": UI_TOKEN,
            "OPSQUIRE_TEST_ISOLATION": "weaker-ok",
            "PYTHONFAULTHANDLER": "1",
            "PATH": os.environ["PATH"] + ":" + str(Path.home() / ".local/bin"),
            "PYTHONPATH": str(ROOT / "src"),
            "XDG_CONFIG_HOME": str(self.tmp / "cfg"),
        }
        self.config = self.tmp / "cfg/opsquire/config.json"
        self.config.parent.mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"]}))

    def tearDown(self) -> None:
        litter = [str(p) for p in self.home.rglob("*.tmp")] if self.home.exists() else []
        self.assertEqual(litter, [], "temp files left behind")
        locks_file = self.home / "var/locks/locks.json"
        if locks_file.is_file():
            state = json.loads(locks_file.read_text())
            held = state.get("holders", state)
            alive = [h for hs in held.values() for h in hs if h["task_id"] != "other" and pid_alive(h["pid"])]
            self.assertEqual(alive, [], "locks left held by live processes")
        shutil.rmtree(self.tmp, ignore_errors=True)

    CHANGING = ("--changes-prod", "--rollback", "undo", "--no-verify")

    NO_DRY = ("--skip-dry", "the dry run is not what this test is about")

    def cli(
        self, *args: str, answers: list[tuple[str, str]] | None = None, unattended: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """The command line. `run` gets a terminal on stdin, as the operator typing it, unless the test says nobody is
        there (cron, a script); every other command gets none. With `answers`, a whole terminal answers its prompts."""
        if answers is not None:
            return run_in_pty([*CLI, *args], self.env, answers)
        if args[:1] != ("run",) or unattended:
            return subprocess.run(
                [*CLI, *args], env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120
            )
        return subprocess.run(
            [*CLI, *args], env=self.env, stdin=self.terminal(), capture_output=True, text=True, timeout=120
        )

    def terminal(self) -> int:
        """A terminal for a command's stdin, as the operator typing it has; both ends close once the test ends."""
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        return slave

    def ui_process(self, port: int) -> subprocess.Popen[str]:
        """`opsquire ui` on this home; the test stops it, and its stdout pipe is closed once the test ends. Its stderr
        goes to ui-<port>.err, with a traceback of every thread when it is aborted (faulthandler)."""
        err = (self.tmp / f"ui-{port}.err").open("w")
        self.addCleanup(err.close)
        env = {**self.env, "PYTHONFAULTHANDLER": "1"}
        cmd = [*CLI, "ui", "--port", str(port)]
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=err, text=True)
        self.ui_errs[proc.pid] = Path(err.name)
        if proc.stdout:
            self.addCleanup(proc.stdout.close)
        return proc

    def stop_ui(self, proc: subprocess.Popen[str]) -> None:
        """Ctrl-C the console and expect a clean exit; one still up after 20 s is aborted and fails with its threads'
        tracebacks."""
        proc.send_signal(signal.SIGINT)
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGABRT)
            proc.wait(10)
            self.fail(f"the console did not stop; its stderr:\n{self.ui_errs[proc.pid].read_text()[-4000:]}")
        self.assertEqual(code, 0)

    def http(self, port: int, timeout: float) -> Console:
        """A connection to the console on `port`, closed once the test ends."""
        conn = Console(port, timeout)
        self.addCleanup(conn.close)
        return conn

    def new(self, name: str, body: str, *extra: str, lang: str = "py", area: str = "web1") -> Path:
        r = self.cli("new", area, name, "--purpose", f"test {name}", "--lang", lang, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = Path(r.stdout.strip())
        (folder / f"main.{lang}").write_text(body)
        (folder / "test_main.py").unlink(missing_ok=True)
        return folder

    def hand_over(self, task: str, *args: str) -> None:
        """Say the task is ready, as its agent does once it stops editing: only then does it reach the inbox."""
        r = self.cli("handover", task, *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def last(self, task: str) -> dict[str, Any]:
        r = self.cli("runs", task, "--json")
        recs: list[dict[str, Any]] = json.loads(r.stdout)
        return recs[-1]

    def events(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        return [json.loads(line) for line in Path(rec["paths"]["events"]).read_text().splitlines()]

    def fixture(
        self, task_id: str, purpose: str, *uses: str, created: str = "2026-11-01T10:00:00.000Z", body: str = ""
    ) -> Path:
        """A task (or, for an id without a date, a general script) written straight into the home, as `new` would
        leave it, without spawning the CLI."""
        general = not task_id[:1].isdigit()
        folder = self.home / ("general" if general else f"tasks/{created[:7]}") / task_id
        folder.mkdir(parents=True)
        area = task_id.split("_")[0 if general else 1]
        meta = {
            "schema": 1,
            "id": task_id,
            "kind": "general" if general else "task",
            "created": created,
            "author": "test",
            "purpose": purpose,
            "area": area,
            "entry": "main.py",
            "changes_prod": False,
            "targets": [area],
            "lock": "shared",
            "lock_reason": "",
            "credentials": {},
            "uses": list(uses),
            "rollback": "read-only",
            "links": [],
        }
        (folder / "meta.json").write_text(json.dumps(meta))
        (folder / "main.py").write_text(body or "print(1)\n")
        return folder

    def fixture_run(
        self, task_id: str, started: str, status: str, duration: float, *argv: str, error: str = ""
    ) -> None:
        run_id = started.replace("-", "").replace(":", "").replace(".", "") + "_read"
        folder = self.home / "var/runs" / task_id / run_id
        folder.mkdir(parents=True)
        rec: dict[str, Any] = {"run_id": run_id, "task_id": task_id, "mode": "read", "status": status}
        rec |= {"started": started, "ended": started, "duration_s": duration, "argv": list(argv), "last_error": error}
        (folder / "run.json").write_text(json.dumps(rec | {"pid": 0, "host": "nowhere", "paths": {"dir": str(folder)}}))

    def eval_home(self) -> None:
        """General scripts whose users say more than they do, a helper nearly every task uses, and one task that
        nothing in its purpose ties to its script."""
        for general, purpose in (
            ("web_ssh", "run a command on a host over ssh"),
            ("web_rotate-keys", "rotate the vendor api keys"),
            ("db_vacuum", "vacuum a postgres database and report bloat"),
            ("web_purge-cdn", "purge the cdn cache for a site"),
        ):
            self.fixture(general, purpose)
        for task_id, purpose, *uses in (
            ("20261101-1000_web1_rotate-zeppelin-keys", "rotate the zeppelin keys on web1", "web_rotate-keys"),
            ("20261102-1000_web2_zeppelin-leak", "after the zeppelin leak: new keys on web2", "web_rotate-keys"),
            ("20261103-1000_db1_vacuum-invoices", "vacuum the invoices database on db1 overnight", "db_vacuum"),
            ("20261104-1000_db2_ledger-bloat", "report bloat of the ledger database on db2", "db_vacuum"),
            ("20261105-1000_db5_invoices-upkeep", "nightly invoices upkeep on db5", "db_vacuum"),
            ("20261106-1000_shop_release-cache", "shop release: clear the cdn cache", "web_purge-cdn"),
            ("20261106-1000_web4_cdn-keys", "rotate the cdn keys on web4", "web_purge-cdn"),
            ("20261107-1000_web3_restart-nginx", "restart nginx on web3 after the zeppelin leak"),
            ("20261108-1000_db4_check-disk", "check the disk of db4"),
            ("20261108-1000_db6_invoices-export", "export the nightly invoices from db6"),
            ("20261108-1000_db7_invoices-count", "count the nightly invoices on db7"),
            ("20261109-1000_db8_fsck-volume", "fsck the ext4 volume of db8", "db_vacuum"),
        ):
            self.fixture(task_id, purpose, *uses, "web_ssh")
        self.fixture("20261105-1000_shop_purge-cache", "purge the shop site cache after the release", "web_purge-cdn")

    def write_lock(self, target: str, pid: int, mode: str = "exclusive", run_id: str = "R1") -> None:
        from opsquire.model import host_name

        d = self.home / "var/locks"
        d.mkdir(parents=True, exist_ok=True)
        holder = {
            "run_id": run_id,
            "task_id": "other",
            "pid": pid,
            "host": host_name(),
            "mode": mode,
            "since": "x",
        }
        (d / "locks.json").write_text(json.dumps({target: [holder]}))

    def holder(self, run_id: str, mode: str = "exclusive", pid: int = 0) -> Any:
        from opsquire import locks
        from opsquire.model import host_name, pid_namespace

        return locks.Holder(run_id, "other", pid or os.getpid(), host_name(), mode, "x", pid_namespace())

    def ledger_entries(self) -> list[dict[str, Any]]:
        """The ledger's entries, without the lock entries locks.py adds."""
        lines = (self.home / "var/ledger/entries.jsonl").read_text().splitlines()
        return [e for e in map(json.loads, lines) if e["kind"] != "lock"]

    def audit(self, *args: str) -> tuple[int, dict[str, Any]]:
        r = self.cli("audit", "--json", *args)
        self.assertNotIn("Traceback", r.stderr)
        out: dict[str, Any] = json.loads(r.stdout)
        return r.returncode, out

    def reseal(self, *folders: Path) -> None:
        script = (
            "import sys\nfrom pathlib import Path\nfrom opsquire import ledger\n"
            "for d in sys.argv[1:]:\n    ledger.run_started(Path(d), 'x')\n    ledger.run_ended(Path(d))\n"
        )
        r = subprocess.run([sys.executable, "-c", script, *map(str, folders)], env=self.env, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def agents_file(self, **override: Any) -> None:
        fake = str(Path(__file__).parent / "fake_agent.py")
        agent = {"enabled": True, "argv": [sys.executable, fake, "{prompt}", "--sys", "{system}", "{allowed_tools*}"]}
        agent |= {"resume_argv": ["--resume", "{session}"], "output": "claude-stream-json"}
        agent |= {"budget_argv": ["--max-budget-usd", "{budget}"]}
        agent |= {"allowed_tools": ["Edit(/{home}/tasks/**)"]} | override
        cfg = {"default": "fake", "agents": {"fake": agent, "off": {"enabled": False, "reason": "not reviewed"}}}
        path = self.tmp / "retinue.json"
        path.write_text(json.dumps(cfg))
        self.env["OPSQUIRE_RETINUE"] = str(path)

    TOOLS_SPEC: dict[str, Any] = {
        "approval_argv": ["--permission-prompt-tool", "mcp__opsquire__approve", "--mcp-config", "{mcp_config}"],
        "deny_argv": ["--disallowedTools", "{server_tools}"],
        "server_tools": "mcp__{server}",
        "name_re": "^mcp__(?P<server>.+?)__(?P<tool>.+)$",
        "shell": {"tool": "Bash", "input": "command"},
    }

    SLACK_READ = {"name": "mcp__chat__read_thread", "input": {"channel_id": "C1"}}

    SLACK_SEND = {"name": "mcp__chat__send_message", "input": {"channel_id": "C1", "text": "hello"}}

    def tool_rules(self) -> None:
        self.agents_file(tools=self.TOOLS_SPEC)
        rules = {"servers": {"chat": {"enabled": True, "tools": {"read_*": "allow"}}, "mail": {"enabled": False}}}
        (self.home / "tools.json").write_text(json.dumps(rules))

    def start(self, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [*CLI, *args],
            env=self.env,
            stdin=self.terminal(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    CONFIRM_BODY = ASKING_BODY

    def make_plan(self, *steps: Path, name: str = "flow", extra: dict[str, Any] | None = None) -> Path:
        args = [a for s in steps for a in ("--step", s.name)]
        r = self.cli("new", "web1", name, "--plan", "--purpose", "check, change, verify", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = Path(r.stdout.strip())
        if extra:
            meta = json.loads((folder / "meta.json").read_text())
            for i, step_extra in extra.items():
                meta["steps"][int(i)].update(step_extra)
            (folder / "meta.json").write_text(json.dumps(meta))
        return folder

    def plan_rec(self, plan: Path) -> dict[str, Any]:
        return self.last(plan.name)

    def grill_env(self, questions: int) -> None:
        self.agents_file()
        self.env["FAKE_GRILL"] = str(questions)
        self.env["FAKE_STATE"] = str(self.tmp / "fake-state.json")

    def fake_running(self, pid_ns: str, beat_age: float | None) -> str:
        self.new("ghost", "print(1)\n")
        self.assertEqual(self.cli("run", "ghost").returncode, 0)
        rec = self.last("ghost")
        run_dir = Path(rec["paths"]["dir"])
        dead = subprocess.Popen(["true"])
        dead.wait()
        rec |= {"status": "running", "pid": dead.pid, "pid_ns": pid_ns}
        (run_dir / "run.json").write_text(json.dumps(rec))
        beat = run_dir / "heartbeat"
        if beat_age is None:
            beat.unlink()
        else:
            os.utime(beat, (time.time() - beat_age, time.time() - beat_age))
        return str(self.last("ghost")["status"])

    def own_ns(self) -> str:
        from opsquire.model import pid_namespace

        return pid_namespace()

    PASSING_TEST = (
        "import unittest\n\nfrom logic import double\n\n\nclass T(unittest.TestCase):\n"
        "    def test_double(self) -> None:\n        self.assertEqual(double(2), 4)\n\n"
        "    def test_zero(self) -> None:\n        self.assertEqual(double(0), 0)\n"
    )

    ISOLATION_TEST = """import os
import socket
import unittest
from pathlib import Path

LEVEL = {level!r}
CREDS = Path({creds!r})  # noqa: E501
MARKER = Path({marker!r})  # noqa: E501
REAL = Path({real!r})  # noqa: E501


class Isolation(unittest.TestCase):
    def test_no_credentials_or_secrets_in_the_environment(self) -> None:
        self.assertNotIn("OPSQUIRE_CREDS", os.environ)
        for name in os.environ:
            for word in ("TOKEN", "SECRET", "KEY", "PASS", "AWS", "GITHUB", "GH_", "SSH"):
                self.assertNotIn(word, name)
        self.assertNotIn("SECRET-TOKEN-123456", repr(dict(os.environ)))

    def test_home_is_fresh_and_empty(self) -> None:
        self.assertNotEqual(Path.home(), MARKER.parent)
        self.assertEqual(list(Path.home().iterdir()), [])
        self.assertFalse((Path.home() / MARKER.name).exists())

    def test_writes_land_in_a_copy(self) -> None:
        (Path(__file__).parent / "written.txt").write_text("x")
        self.assertNotEqual(Path(__file__).parent, REAL)

    def test_files_outside_the_copy_are_out_of_reach(self) -> None:
        if LEVEL not in ("bwrap", "sandbox-exec"):
            self.skipTest(LEVEL)
        for path in (CREDS, MARKER):
            with self.assertRaises(OSError):
                path.read_text()
        with self.assertRaises(OSError):
            (REAL / "written.txt").write_text("x")

    def test_no_network(self) -> None:
        if LEVEL == "env-only":
            self.skipTest(LEVEL)
        with self.assertRaises(OSError), socket.create_connection(("1.1.1.1", 53), timeout=3):
            pass
"""

    def isolation_level(self) -> str:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire.isolate import level

        return level(sys.executable)

    PROGRESS_BODY = (
        "import json\n\n"
        "for i in range(51):\n"
        "    ev = {'event': 'progress', 'msg': 'download', 'done': i * 2 << 20, 'total': 100 << 20, 'unit': 'B'}\n"
        "    print('@@OPSQUIRE ' + json.dumps(ev), flush=True)\n"
        "    if i == 25:\n"
        "        print('halfway note', flush=True)\n"
    )

    def git_home(self) -> None:
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        self.env |= {"HOME": str(self.tmp / "user"), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}
        self.env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        r = self.cli("integrate")
        self.assertIn("home repo: initialized a local git repository", r.stdout, r.stderr)

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(self.home), *args], env=self.env, capture_output=True, text=True)
        return r.stdout

    def asks(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text()) for p in sorted((self.home / "var/asks").glob("*.json"))]

    def split(self, *pieces: str) -> Path:
        self.git_home()
        self.agents_file()
        log = self.tmp / "agent-log.jsonl"
        self.env |= {"FAKE_SPLIT": json.dumps(list(pieces)), "FAKE_LOG": str(log)}
        return log

    def piece_env(self, opened: subprocess.CompletedProcess[str]) -> dict[str, str]:
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        info = json.loads(opened.stdout)
        self.assertTrue(info["worktree"].startswith(str(self.home / ".worktrees/")), info)
        self.assertEqual(info["env"]["OPSQUIRE_VAR"], str(self.home / "var"))
        self.assertIn(info["worktree"], info["brief"])
        env: dict[str, str] = {**self.env, **info["env"]}
        return env

    def piece_task(self, env: dict[str, str], name: str, body: str) -> str:
        r = subprocess.run(
            [*CLI, "new", "web1", name, "--purpose", f"piece {name}"], env=env, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = Path(r.stdout.strip())
        self.assertTrue(str(folder).startswith(env["OPSQUIRE_HOME"]), folder)
        (folder / "main.py").write_text(body)
        return folder.name

    def append_body(self, log: Path, word: str, fail_if: Path | None = None) -> str:
        """A changing script that appends `word` to `log` when it applies; it fails while `fail_if` exists."""
        if fail_if:
            head = "import sys\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            head += f"if Path({str(fail_if)!r}).exists():\n    sq.error('flag set')\n    sys.exit(1)\n"
        else:
            head = "import opsquire_helpers as sq\n\n"
        return head + f"if sq.APPLY:\n    with open({str(log)!r}, 'a') as f:\n        f.write({word!r} + ' ')\n"

    def milestones(self, *args: str) -> list[dict[str, Any]]:
        r = self.cli("milestones", "--since", "2020-01-01", "--json", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        items: list[dict[str, Any]] = json.loads(r.stdout)
        return items

    def code_repo(self) -> Path:
        """A registered repo whose origin/main has three files, and a fake gh that records what reaches GitHub."""
        repo = self.tmp / "shop"
        repo.mkdir()
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        files = {"README.md": "hello\n", "src/app.py": "x = 1\n", "old.txt": "gone soon\n", "c/__pycache__/k.pyc": "o"}
        (repo / "link").symlink_to("missing-target")
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(repo), *cmd], env=self.env, check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"], env=self.env, check=True
        )
        cfg = {"branch_prefix": "dev/", "repos": {"shop": {"path": str(repo), "github": "acme/shop", "base": "main"}}}
        (self.home / "repos.json").write_text(json.dumps(cfg))
        bin_dir = self.tmp / "gh-bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(f'#!/bin/sh\nexec {sys.executable} {ROOT / "tests/fake_gh.py"} "$@"\n')
        (bin_dir / "gh").chmod(0o755)
        self.env |= {"PATH": f"{bin_dir}:{self.env['PATH']}", "FAKE_GH_STATE": str(self.tmp / "gh.json")}
        self.env["FAKE_GH_LOG"] = str(self.tmp / "gh.log")
        return repo

    def gh_state(self) -> dict[str, Any]:
        state: dict[str, Any] = json.loads((self.tmp / "gh.json").read_text())
        return state

    def checkout_copy(self) -> Path:
        r = self.cli("repo", "checkout", "shop")
        self.assertEqual(r.returncode, 0, r.stderr)
        return Path(r.stdout.strip())

    def console(self) -> tuple[subprocess.Popen[str], Any]:
        """A real `opsquire ui` on this home and a function that calls its API."""
        port = free_port()
        proc = self.ui_process(port)
        self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")

        def call(path: str, body: dict[str, Any] | None = None) -> Any:
            with contextlib.closing(Console(port, 20)) as conn:
                conn.request(
                    "POST" if body else "GET",
                    path,
                    json.dumps(body) if body else None,
                    {"Content-Type": "application/json"},
                )
                return json.loads(conn.getresponse().read())

        return proc, call

    def await_job(self, call: Any, job: str, status: str) -> dict[str, Any]:
        deadline = time.time() + 60
        while time.time() < deadline:
            j: dict[str, Any] = call(f"/api/job?id={job}&offset=0")
            if j["status"] == status:
                return j
            time.sleep(0.1)
        self.fail(f"job {job} never reached {status}: {j}")

    def nested_task(self, child: str, lang: str) -> str:
        """A changing task that runs a general script as a plain child, knowing nothing about the prompt handshake."""
        made = self.cli("new", "web1", "child", "--general", "--purpose", "asks", "--lang", lang)
        self.assertEqual(made.returncode, 0, made.stderr)
        (Path(made.stdout.strip()) / f"main.{lang}").write_text(child)
        interpreter = "sys.executable" if lang == "py" else '"bash"'
        folder = self.new(
            "parent",
            "import os\nimport subprocess\nimport sys\n\n"
            f'child = os.environ["OPSQUIRE_GENERAL"] + "/web1_child/main.{lang}"\n'
            f"sys.exit(subprocess.run([{interpreter}, child], check=False).returncode)\n",
            "--changes-prod",
            "--rollback",
            "undo",
            "--no-verify",
            "--uses",
            "web1_child",
        )
        lint = self.cli("lint", "parent")
        self.assertEqual(lint.returncode, 0, lint.stdout)
        return folder.name

    def backdate(self, folder: Path, days: float) -> None:
        """Make a task look last touched `days` ago: its creation stamp and the time meta.json was written."""
        meta = json.loads((folder / "meta.json").read_text())
        then = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        meta["created"] = then.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        (folder / "meta.json").write_text(json.dumps(meta))
        os.utime(folder / "meta.json", (then.timestamp(), then.timestamp()))

    def age_handover(self, folder: Path, days: float) -> None:
        """Make the task's handover `days` old; an annotation, so the task's code stays the same."""
        meta = json.loads((folder / "meta.json").read_text())
        then = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        meta["handover"]["at"] = then.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        (folder / "meta.json").write_text(json.dumps(meta))
        os.utime(folder / "meta.json", (then.timestamp(), then.timestamp()))

    def due_in(self, days: float) -> list[str]:
        code = (
            "import datetime as dt, json\nfrom opsquire.model import now\nfrom opsquire.offboard import due\n"
            f"print(json.dumps([t.id for t in due(now() + dt.timedelta(days={days}))]))"
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        ids: list[str] = json.loads(r.stdout)
        return ids

    def offboard_env(self, **config: Any) -> None:
        (self.tmp / "cfg/opsquire/config.json").write_text(json.dumps(config))
        self.env["XDG_CONFIG_HOME"] = str(self.tmp / "cfg")

    def inbox_ids(self) -> list[str]:
        code = "import json\nfrom opsquire.state import state\nprint(json.dumps(state()['inbox']))"
        r2 = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        items: list[dict[str, Any]] = json.loads(r2.stdout)
        return [str(it["task_id"]) for it in items]

    def task_view(self, task_id: str) -> dict[str, Any]:
        code = f"import json\nfrom opsquire.state import state\nprint(json.dumps(state()['tasks'][{task_id!r}]))"
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        view: dict[str, Any] = json.loads(r.stdout)
        return view

    def inbox_item(self, task_id: str) -> dict[str, Any] | None:
        code = "import json\nfrom opsquire.state import state\nprint(json.dumps(state()['inbox']))"
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        items: list[dict[str, Any]] = json.loads(r.stdout)
        return next((it for it in items if it["task_id"] == task_id), None)

    def console_apply(self, task_id: str) -> tuple[list[str], int]:
        code = (
            "import json, sys\nfrom opsquire.jobs import Jobs\n"
            f"job = Jobs().start({task_id!r}, 'apply')\nassert job.proc\n"
            "print(json.dumps([job.args, job.proc.wait(timeout=100)]))"
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        args, exit_code = json.loads(r.stdout)
        return args, exit_code

    def own_group(self) -> str:
        return grp.getgrgid(os.getegid()).gr_name

    def umask_022(self) -> None:
        """Every CLI the test starts begins with umask 022, as a typical operator's shell does."""
        self.addCleanup(os.umask, os.umask(0o022))

    def tree(self) -> dict[Path, tuple[int, int]]:
        return {p: (p.lstat().st_mode, p.lstat().st_gid) for p in [self.home, *self.home.rglob("*")]}
