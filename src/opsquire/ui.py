"""`opsquire ui`: the operator's console on localhost. One page (data/ui/) reads the state (threads, tasks, runs,
asks, scripts, locks) from /api/state and follows changes over /api/stream (server-sent events: one watcher thread
looks at every file of the home's tasks, general scripts, locks, asks and thread results, the runs folders and each
task's latest run.json), tails a run's output.log and shows a task's design.md or script. Its writes are closing or
abandoning a task (close.py, the same code as `opsquire close`), schedules linked from inbox items, runs, judges and
asks started from the page (jobs.py) and answers to their prompts, Settings (the config file), tool rules
(tools.json) and which chats the operator has read (chatread.py). A running server keeps the Python it started
with, so it also compares that code with what is on disk and installed now, and the page tells the operator to
restart it when they differ.

Nothing is served to a client that has not proven it is the operator's. Each start makes a random secret, kept in
memory only and printed in the operator's terminal as a login link (a script that starts the console itself may choose
the secret in OPSQUIRE_UI_TOKEN and send it in X-Opsquire-Token). Opening the link gives that browser a session
cookie (HttpOnly, SameSite=Strict); the link works until the console stops. Sessions live in the server's memory, so a
restart ends them. Without a session or the token, / answers a page that says where the link is, /ui/ answers 401, and
every /api/ request is refused. Every request must also name the console by its loopback Host (DNS rebinding), an
Origin, when sent, must be the console's own, and a POST must be application/json and carry X-Opsquire-Request, a
header no cross-site form can send."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shlex
import socketserver
import sys
import threading
import time
import traceback
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata, resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, chatread, codeview, ledger, policy, store, tend, tools
from .ask import configured_fresh_after, pick_agent
from .close import abandon_waiting, close_task
from .jobs import Jobs
from .model import (
    IGNORED_PARTS,
    PACKAGE_DIR,
    OpsquireError,
    configured_time_zone,
    home_dir,
    iso,
    now,
    resolve_task,
    runs_dir,
    task_folder,
    update_config,
    user_name,
    valid_time_zone,
    var_dir,
    write_json,
)
from .query import runs_for
from .snapshots import Snapshots
from .state import change_note, load_asks, state

DEFAULT_PORT = 7771
WATCH_INTERVAL_S = 1.0
HEALTH_EVERY_S = 5.0
HEALTH_STALE_S = 20.0
PING_INTERVAL_S = 15.0
MAX_LOG_BYTES = 512 * 1024
VIEWABLE = {"design.md", "main.py", "main.sh", "verify.py", "verify.sh", "meta.json", "test_main.py"}
UI_DIR = "data/ui"
PAGE = f"{UI_DIR}/index.html"
UI_FILES = ("index.html", "css/*.css", "js/*.js")
STATIC = {"css": "text/css; charset=utf-8", "js": "text/javascript; charset=utf-8"}
STATIC_PATH = re.compile(r"/ui/(css|js)/([a-z0-9][a-z0-9_-]*)\.(css|js)")
REQUEST_HEADER = "X-Opsquire-Request"
SESSION_MAX_AGE_S = 30 * 24 * 3600
TOKEN_ENV = "OPSQUIRE_UI_TOKEN"


def page() -> bytes:
    return resources.files("opsquire").joinpath(PAGE).read_bytes()


def static_file(path: str) -> tuple[bytes, str] | None:
    """One of the page's stylesheets or modules by its URL path, with its content type: only a file in data/ui/css
    ending .css or in data/ui/js ending .js, named without dots or slashes; None for any other path."""
    found = STATIC_PATH.fullmatch(path)
    if not found or found.group(1) != found.group(3):
        return None
    folder, name = found.group(1), f"{found.group(2)}.{found.group(3)}"
    file = resources.files("opsquire").joinpath(UI_DIR, folder, name)
    return (file.read_bytes(), STATIC[folder]) if file.is_file() else None


def small_page(title: str, body: str, refresh: str = "") -> bytes:
    """A page of a few lines (log in first, logged in), with no secret in it."""
    meta = f'<meta http-equiv="refresh" content="0; url={refresh}">' if refresh else ""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}<title>Opsquire</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#222}"
        "code{background:#eee;padding:0 .25rem;border-radius:3px}"
        "@media (prefers-color-scheme:dark){body{background:#111;color:#ddd}code{background:#333}}</style>"
        f"</head><body><h1>{html.escape(title)}</h1>{body}</body></html>"
    ).encode()


LOGIN_NEEDED = small_page(
    "Log in to this console",
    "<p>Open the login link that <code>opsquire ui</code> printed in its terminal. The link works until the "
    "console stops. A restarted console needs a new login.</p>",
)


def fingerprint() -> str:
    """Changes when anything the page shows may have moved: any file of a task or general script (its lint stamp
    and scripts included, since they decide draft or ready), run records, locks, asks and thread results. A new run is
    a new folder, which changes its task's runs folder; a finished run's record never changes again, so only each
    task's latest one is read."""
    home, var = home_dir(), var_dir()
    paths: list[str] = [str(chatread.path())]
    for root in (home / "tasks", home / "general", home / "threads", var / "asks", var / "locks"):
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in IGNORED_PARTS]
            paths += (os.path.join(folder, f) for f in files)
    for task_runs in runs_dir().glob("*/"):
        paths.append(str(task_runs))
        latest = max(task_runs.glob("*/run.json"), default=None, key=lambda p: p.parent.name)
        if latest:
            paths.append(str(latest))
    h = hashlib.sha256()
    for path in sorted(paths):
        try:
            st = os.stat(path)
        except OSError:
            continue
        h.update(f"{path}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return h.hexdigest()


def code_hashes() -> dict[str, str]:
    """The server's code as it is on disk now: the package's modules and the page's files, by content, so a touch or a
    checkout that lands back on the same code is no change. Helpers and templates are read fresh by every run, so they
    never go stale in a running server and are left out."""
    hashes: dict[str, str] = {}
    ui = PACKAGE_DIR / UI_DIR
    for path in [*PACKAGE_DIR.glob("*.py"), *(p for pattern in UI_FILES for p in ui.glob(pattern))]:
        try:
            hashes[path.relative_to(PACKAGE_DIR).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def installed_version() -> str:
    """The version installed now; __version__ is the one this process read when it started."""
    try:
        return metadata.version("opsquire")
    except metadata.PackageNotFoundError:
        return __version__


def page_hash(hashes: dict[str, str]) -> str | None:
    """The page a reload would get, as one hash of its shell, stylesheets and modules."""
    files = sorted((name, h) for name, h in hashes.items() if name.startswith(f"{UI_DIR}/"))
    return hashlib.sha256("".join(f"{n}\0{h}\n" for n, h in files).encode()).hexdigest() if files else None


def code_status(loaded: dict[str, str]) -> dict[str, Any]:
    """How the code the server started with (`loaded`) compares with what is there now. Only modules need a restart:
    the page's files are read per request, and `page`, the page a reload would get, lets a tab see it is older."""
    current, installed = code_hashes(), installed_version()
    names = (n for n in loaded.keys() | current.keys() if not n.startswith(f"{UI_DIR}/"))
    changed = sorted(n for n in names if loaded.get(n) != current.get(n))
    return {
        "stale": bool(changed) or installed != __version__,
        "changed": changed,
        "version": __version__,
        "installed": installed,
        "page": page_hash(current),
    }


def tools_spec() -> dict[str, Any]:
    spec: dict[str, Any] = pick_agent(None)[1].get("tools") or {}
    return spec


LISTED: dict[str, Any] = {"servers": [], "at": ""}


def tools_view(refresh: bool) -> dict[str, Any]:
    """Settings: the default agent's servers (listed by its CLI on refresh, which can take seconds) with the rules."""
    spec = tools_spec()
    if refresh:
        LISTED.update(servers=tools.listed_servers(spec), at=iso(now()))
    return {**tools.view(spec, LISTED["servers"]), "listed_at": LISTED["at"]}


def settings_view() -> dict[str, Any]:
    return {
        "follow_up_fresh_after_usd": configured_fresh_after(),
        "time_zone": configured_time_zone(),
        "policy": policy.settings(),
        "checkpoint_command": ledger.export_command(),
        "checkpoint_last": ledger.last_export(),
        "editor_url": codeview.editor_template(),
        "editor_url_default": codeview.default_editor_url(),
    }


def change_settings(body: dict[str, Any]) -> dict[str, Any]:
    """Settings the page changes in the config file: the cost of a chat's last turn after which a follow-up starts a
    fresh, briefed agent session (null turns it off), the zone times are shown in, the tiers and auto-run (`policy`,
    partial: see policy.change), the command each ledger checkpoint is sent to (empty turns it off) and the editor
    link template (`editor_url`)."""
    if "time_zone" in body:
        if not valid_time_zone(body["time_zone"]):
            raise OpsquireError("time_zone must be UTC, local, or a zone name such as America/New_York")
        update_config(time_zone=body["time_zone"])

    if "follow_up_fresh_after_usd" in body:
        value = body["follow_up_fresh_after_usd"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise OpsquireError("follow_up_fresh_after_usd must be a number of dollars, 0 or more, or null for off")
        update_config(follow_up_fresh_after_usd=False if value is None else float(value))
    if "policy" in body:
        policy.change(body["policy"])
    if "checkpoint_command" in body:
        ledger.set_export_command(body["checkpoint_command"])
    if "editor_url" in body:
        codeview.set_editor_template(body["editor_url"])
    return settings_view()


def task_code(task_id: str) -> dict[str, Any]:
    """The files a run of the task would execute, as they are now, against its last run, each with a link to open it
    in the operator's editor; and what its handover says changed."""
    task = resolve_task(task_id)
    runs = runs_for(task.id)
    before = codeview.last_run(runs)
    note = change_note(task, runs)
    return {
        "task": task.id,
        "since": (before or {}).get("run_id"),
        "files": codeview.listing(task, task.file_hashes(), home_dir(), before),
        "changed": note["text"] if note else "",
    }


def ask_chat(ask_id: str, offset: int) -> dict[str, Any]:
    """The conversation an ask belongs to, from line `offset` on, for the console's chat. A follow-up writes into
    its conversation's chat (the first ask's), so any ask of it reads the whole conversation."""
    if not re.fullmatch(r"[0-9A-Za-z_.-]+", ask_id):
        raise OpsquireError("bad ask id")
    folder = var_dir() / "asks"
    with contextlib.suppress(OSError, ValueError, AttributeError):
        conv = str(json.loads((folder / f"{ask_id}.json").read_text()).get("copies") or ask_id)
        if re.fullmatch(r"[0-9A-Za-z_.-]+", conv):
            ask_id = conv
    path = folder / f"{ask_id}.chat.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    items = []
    for line in lines[offset:]:
        with contextlib.suppress(ValueError):
            items.append(json.loads(line))
    return {"items": items, "offset": len(lines)}


def log_tail(run_id: str, offset: int | None, until: int | None = None) -> dict[str, Any]:
    """A slice of a run's output.log: the last MAX_LOG_BYTES when `offset` is None (first open), the bytes from
    `offset` on (tailing a running run), or the bytes in [offset, until) (loading earlier output). `start` says where
    the text begins so the page can ask for what precedes it."""
    root = runs_dir()
    found = [p for p in root.glob(f"*/{run_id}") if p.is_dir()] if root.is_dir() else []
    if not found:
        raise OpsquireError(f"no run {run_id}")
    status = None
    try:
        with (found[0] / "run.json").open() as f:
            status = json.load(f).get("status")
    except (OSError, ValueError):
        pass
    log = found[0] / "output.log"
    if log.is_file():
        size = log.stat().st_size
        start = max(0, size - MAX_LOG_BYTES) if offset is None else max(0, offset)
        stop = min(size, until) if until is not None else min(size, start + MAX_LOG_BYTES)
        with log.open("rb") as f:
            f.seek(start)
            data = f.read(max(0, stop - start))
    else:
        whole = store.read_log(found[0], "output.log")
        if not whole:
            return {"text": "", "start": 0, "offset": 0, "status": status}
        size = len(whole)
        start = max(0, size - MAX_LOG_BYTES) if offset is None else max(0, offset)
        stop = min(size, until) if until is not None else min(size, start + MAX_LOG_BYTES)
        data = whole[start:stop]
    return {"text": data.decode("utf-8", "replace"), "start": start, "offset": start + len(data), "status": status}


def task_file(task_id: str, name: str) -> dict[str, Any]:
    if name not in VIEWABLE or "/" in task_id or task_id.startswith("."):
        raise OpsquireError(f"{name!r} is not a viewable file")
    folder = task_folder(task_id) or (home_dir() / "general" / task_id)
    if not folder.is_dir():
        raise OpsquireError(f"no task {task_id}")
    path = folder / name
    if not path.is_file():
        return {"text": None}
    return {"text": path.read_text(errors="replace")}


def link_schedule(body: dict[str, Any]) -> dict[str, Any]:
    """Link inbox items, in the order given, into one schedule. Each step takes the arguments its inbox item shows,
    never any the page sends."""
    from .plan import create_schedule

    ids = body.get("tasks")
    if not (isinstance(ids, list) and all(isinstance(x, str) for x in ids)):
        raise OpsquireError("tasks must be a list of task ids, in run order")
    items = {str(i["task_id"]): i for i in state()["inbox"]}
    missing = [x for x in ids if x not in items]
    if missing:
        raise OpsquireError(f"not waiting in the inbox: {', '.join(missing)}")
    if any(items[x].get("steps") for x in ids):
        raise OpsquireError("a schedule does not take another schedule; link its steps' tasks instead")
    steps = [{"task": x, "args": shlex.split(str(items[x].get("args") or ""))} for x in ids]
    plan = create_schedule(steps, str(body.get("purpose") or ""), os.environ.get("OPSQUIRE_AUTHOR", "opsquire ui"))
    return {"id": plan.id}


class Handler(BaseHTTPRequestHandler):
    server_version = "opsquire-ui"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("OPSQUIRE_UI_LOG"):
            super().log_message(format, *args)

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK, close: bool = False) -> None:
        body = json.dumps(data).encode()
        zipped = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if zipped:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        if zipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def refused(self, path: str, post: bool = False) -> bool:
        """Answers 403 and closes the connection unless the request names the console by its own loopback Host,
        carries no foreign Origin, for /api/ comes from a logged-in browser or a script with the token, and for a POST
        carries X-Opsquire-Request. Returns whether it refused."""
        server: UIServer = self.server  # type: ignore[assignment]
        problem, origin = "", self.headers.get("Origin")
        if self.headers.get("Host") not in server.hosts:
            problem = "unexpected Host"
        elif origin is not None and origin not in server.origins:
            problem = "cross-origin request"
        elif path.startswith("/api/") and not self.authenticated():
            problem = "not logged in to this console: open the login link `opsquire ui` printed in its terminal"
        elif post and not self.headers.get(REQUEST_HEADER):
            problem = f"a POST must carry {REQUEST_HEADER}"
        if problem:
            self.send_json({"error": problem}, HTTPStatus.FORBIDDEN, close=True)
        return bool(problem)

    def authenticated(self) -> bool:
        server: UIServer = self.server  # type: ignore[assignment]
        token = self.headers.get("X-Opsquire-Token")
        if token is not None:
            return hmac.compare_digest(token.encode(), server.token.encode())
        return server.valid_session(self.session_cookie())

    def session_cookie(self) -> str:
        """This console's cookie, read by hand: other local apps' cookies for the same host may not parse."""
        server: UIServer = self.server  # type: ignore[assignment]
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == server.cookie_name:
                    return value
        return ""

    def send_page(
        self,
        body: bytes,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
        kind: str = "text/html; charset=utf-8",
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_in(self, code: str) -> None:
        """Swaps the login link's secret for a session cookie, then sends the browser on to the console. The hop is a
        page of its own rather than a redirect, so the browser counts it as a same-site navigation and sends the
        SameSite=Strict cookie with it."""
        server: UIServer = self.server  # type: ignore[assignment]
        session = server.new_session(code)
        if session is None:
            self.send_page(LOGIN_NEEDED, HTTPStatus.FORBIDDEN)
            return
        cookie = f"{server.cookie_name}={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_MAX_AGE_S}"
        body = small_page("Logged in", '<p>Opening <a href="/">the console</a>.</p>', refresh="/")
        self.send_page(body, headers={"Set-Cookie": cookie})

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if self.refused(url.path):
            return
        server: UIServer = self.server  # type: ignore[assignment]
        try:
            if url.path in ("/", "/index.html"):
                if self.authenticated():
                    self.send_page(page())
                else:
                    self.send_page(LOGIN_NEEDED, HTTPStatus.UNAUTHORIZED)
            elif url.path.startswith("/ui/"):
                if not self.authenticated():
                    self.send_json({"error": "not logged in to this console"}, HTTPStatus.UNAUTHORIZED)
                elif (found := static_file(url.path)) is None:
                    self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                else:
                    nosniff = {"X-Content-Type-Options": "nosniff"}
                    self.send_page(found[0], headers=nosniff, kind=found[1], cache="no-cache")
            elif url.path == "/login":
                self.log_in(q.get("code", ""))
            elif url.path == "/api/state":
                try:
                    answer = server.snapshots.answer(server.look(), q.get("since", ""))
                except Exception as e:
                    server.health["state_error"] = f"{type(e).__name__}: {e}"
                    raise
                extra = {
                    "jobs": self.jobs.views(),
                    "start_problems": self.jobs.problems,
                    "code": server.seen[1] if server.seen else code_status(server.code),
                }
                self.send_json({**answer, **extra})
            elif url.path == "/api/tools":
                self.send_json(tools_view(q.get("refresh") == "1"))
            elif url.path == "/api/settings":
                self.send_json(settings_view())
            elif url.path == "/api/approvals":
                self.send_json({"pending": self.jobs.approvals(q.get("job", ""))})
            elif url.path == "/api/code":
                self.send_json(task_code(q.get("task", "")))
            elif url.path == "/api/tend":
                self.send_json(tend.report())
            elif url.path == "/api/askchat":
                self.send_json(ask_chat(q.get("id", ""), int(q.get("offset") or 0)))
            elif url.path == "/api/job":
                self.send_json(self.jobs.read(q.get("id", ""), int(q.get("offset") or 0)))
            elif url.path == "/api/log":
                offset, until = q.get("offset") or None, q.get("until") or None
                self.send_json(
                    log_tail(q.get("run", ""), int(offset) if offset else None, int(until) if until else None)
                )
            elif url.path == "/api/file":
                self.send_json(task_file(q.get("task", ""), q.get("name", "")))
            elif url.path == "/api/stream":
                self.stream()
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except OpsquireError as e:
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.send_json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if self.refused(url.path, post=True):
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json({"error": "body must be application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE, close=True)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_json({"error": "body must be JSON"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(body, dict):
            self.send_json({"error": "body must be a JSON object"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            if url.path == "/api/run":
                before = set(self.jobs.jobs)
                expect = body.get("expect")
                if expect is not None and not (isinstance(expect, list) and all(isinstance(x, str) for x in expect)):
                    raise OpsquireError("expect must be a list of strings")
                job = self.jobs.start(str(body.get("task", "")), str(body.get("mode", "")), expect)
                self.send_json({"job_id": job.job_id, "joined": job.job_id in before})
            elif url.path == "/api/schedule":
                self.send_json(link_schedule(body))
            elif url.path == "/api/judge":
                job = self.jobs.start(str(body.get("task", "")), "judge")
                self.send_json({"job_id": job.job_id})
            elif url.path == "/api/ask":
                job = self.jobs.start_ask(
                    str(body.get("request", "")),
                    str(body.get("resume", "")),
                    str(body.get("model", "")),
                    str(body.get("agent", "")),
                )
                self.send_json({"job_id": job.job_id})
            elif url.path == "/api/approve":
                self.jobs.approve(str(body.get("job", "")), str(body.get("id", "")), body.get("allow") is True)
                self.send_json({"pending": self.jobs.approvals(str(body.get("job", "")))})
            elif url.path == "/api/tendfix":
                job = self.jobs.start_ask("", tend=True)
                self.send_json({"job_id": job.job_id})
            elif url.path == "/api/tools":
                tools.update(body, tools_spec())
                self.send_json(tools_view(False))
            elif url.path == "/api/answer":
                self.jobs.answer(str(body.get("job", "")), str(body.get("text", "")))
                self.send_json({"ok": True})
            elif url.path == "/api/interrupt":
                self.jobs.interrupt(str(body.get("job", "")))
                self.send_json({"ok": True})
            elif url.path == "/api/settings":
                self.send_json(change_settings(body))
            elif url.path == "/api/chatread":
                chatread.mark(str(body.get("conversation", "")), load_asks())
                self.send_json({"ok": True})
            elif url.path == "/api/close":
                run = body.get("run")
                closed = close_task(
                    str(body.get("task", "")),
                    str(body.get("outcome", "")),
                    str(body.get("note", "")),
                    str(run) if run else None,
                )
                self.send_json(closed)
            elif url.path == "/api/abandon":
                self.send_json(abandon_waiting(str(body.get("task", "")), str(body.get("note", ""))))
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except OpsquireError as e:
            busy = "job running" in str(e) or "too many jobs" in str(e)
            self.send_json({"error": str(e)}, HTTPStatus.CONFLICT if busy else HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.send_json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    @property
    def jobs(self) -> Jobs:
        server: UIServer = self.server  # type: ignore[assignment]
        return server.jobs

    def stream(self) -> None:
        """Server-sent events: `change` when the home moved, `ping` to keep the connection alive."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        server: UIServer = self.server  # type: ignore[assignment]
        seen = server.generation
        self.wfile.write(f"event: hello\ndata: {json.dumps({'generated': iso(now())})}\n\n".encode())
        self.wfile.flush()
        while not server.stopping.is_set():
            current = server.wait_change(seen, PING_INTERVAL_S)
            if current != seen:
                seen = current
                version = server.snapshots.version(current)
                self.wfile.write(f"event: change\ndata: {json.dumps({'version': version})}\n\n".encode())
            else:
                self.wfile.write(b"event: ping\ndata: {}\n\n")
            self.wfile.flush()


class UIServer(ThreadingHTTPServer):
    """Serves the page; one watcher thread notices when the home, a job or the code on disk moved and bumps
    `generation`, which every open stream waits on, so no stream misses a change another one saw."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, Handler)
        port = self.server_address[1]
        self.hosts = {f"{name}:{port}" for name in ("127.0.0.1", "localhost", "[::1]")}
        self.origins = {f"http://{host}" for host in self.hosts}
        self.token = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)
        self.cookie_name = f"opsquire-{port}"
        self.sessions: set[str] = set()
        self.login_lock = threading.Lock()
        self.stopping = threading.Event()
        self.jobs = Jobs()
        self.code = code_hashes()
        chatread.start()
        self.generation = 0
        self.followed = -1
        self.seen: tuple[str, dict[str, Any]] | None = None
        self.moved = threading.Condition()
        self.health: dict[str, Any] = {"state_s": None, "state_at": None, "state_error": ""}
        self.snapshots = Snapshots(self.timed_state)
        self.started = iso(now())
        self.beaten = 0.0
        threading.Thread(target=self.watch, daemon=True).start()

    def server_bind(self) -> None:
        """HTTPServer's own bind also looks up the machine's full DNS name, which a Mac can wait on for many seconds;
        the console names only the address it serves."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)

    def timed_state(self) -> dict[str, Any]:
        ledger.observe()
        t0 = time.monotonic()
        built = state()
        self.health.update(state_s=round(time.monotonic() - t0, 3), state_at=iso(now()), state_error="")
        self.beaten = 0.0
        if self.seen:
            self.beat(self.seen[1])
        return built

    def login_url(self) -> str:
        return f"{url(self.server_address[1], str(self.server_address[0]))}login?code={self.token}"

    def new_session(self, code: str) -> str | None:
        """A new session for a browser that brought this console's secret, None for any other code."""
        if not code or not hmac.compare_digest(code.encode(), self.token.encode()):
            return None
        session = secrets.token_urlsafe(32)
        with self.login_lock:
            self.sessions.add(session)
        return session

    def valid_session(self, cookie: str) -> bool:
        if not cookie:
            return False
        with self.login_lock:
            known = list(self.sessions)
        return any(hmac.compare_digest(cookie.encode(), s.encode()) for s in known)

    def health_file(self) -> Path:
        return var_dir() / "ui" / f"{self.server_address[1]}.json"

    def beat(self, code: dict[str, Any]) -> None:
        """What `opsquire doctor` reads to tell whether this console is up and current, from any shell: the agent
        sandboxes that cannot open its port read this file instead."""
        if time.monotonic() - self.beaten < HEALTH_EVERY_S:
            return
        self.beaten = time.monotonic()
        host, port = str(self.server_address[0]), self.server_address[1]
        record = {
            "pid": os.getpid(),
            "user": user_name(),
            "uid": os.geteuid(),
            "url": url(port, host),
            "started": self.started,
            "beat": iso(now()),
            "stale_code": code["changed"],
            "jobs": len([j for j in self.jobs.views() if j.get("status") != "done"]),
            **self.health,
        }
        with contextlib.suppress(OSError):
            self.health_file().parent.mkdir(parents=True, exist_ok=True)
            write_json(self.health_file(), record)

    def watch(self) -> None:
        while not self.stopping.is_set():
            job_moved = self.jobs.changed.wait(WATCH_INTERVAL_S)
            self.jobs.changed.clear()
            try:
                seen = self.look(force=job_moved)
                if seen != self.followed:
                    self.followed = seen
                    self.jobs.follow_up()
                    if any(policy.settings()["auto_run"].values()):
                        self.jobs.auto_run(self.snapshots.answer(seen, "")["inbox"])
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def look(self, force: bool = False) -> int:
        """Bump `generation` when the home, a job or the code moved since the last look, and return it. The watcher
        looks every second; a state request looks first too, so it never answers with a home older than itself."""
        current = (fingerprint(), code_status(self.code))
        self.beat(current[1])
        with self.moved:
            if force or current != self.seen:
                self.seen = current
                self.generation += 1
                self.moved.notify_all()
            return self.generation

    def wait_change(self, after: int, timeout: float) -> int:
        with self.moved:
            self.moved.wait_for(lambda: self.generation != after or self.stopping.is_set(), timeout)
            return self.generation

    def stop(self) -> None:
        self.stopping.set()
        with contextlib.suppress(OSError):
            self.health_file().unlink()
        with self.moved:
            self.moved.notify_all()


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1", on_ready: Callable[[str], None] | None = None) -> None:
    """Serve until interrupted. Binds localhost only: the page can close tasks, and the home is the operator's.
    `on_ready` gets the login link."""
    try:
        server = UIServer((host, port))
    except OSError as e:
        raise OpsquireError(f"cannot listen on {host}:{port}: {e.strerror or e}") from None
    if on_ready is not None:
        on_ready(server.login_url())
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.stop()
        server.jobs.stop_all()
        server.server_close()


def url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}/"
