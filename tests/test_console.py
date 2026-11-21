"""The operator's console: pages, state, inbox and jobs."""

from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import http.client
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from support import BASH_HEADER, CLI, ROOT, UI_TOKEN, Console, OpsTestCase, free_port, run_in_pty


class ConsoleTest(OpsTestCase):
    def test_state_infers_threads_from_sessions_links_and_mentions(self) -> None:
        self.env["OPSQUIRE_AUTHOR"] = "claude-code 26f643b0"
        a = self.new("alpha", "print('a')\n", "--link", "https://github.com/x/y/pull/1").name
        b = self.new("beta", "print('b')\n").name
        self.env["OPSQUIRE_AUTHOR"] = "claude-code (chat)"
        c = self.new("gamma", "print('c')\n", "--link", "https://github.com/x/y/pull/1/").name
        d = self.new("delta", "print('d')\n", "--purpose", f"follows {c} to verify").name
        e = self.new("eps", "print('e')\n").name
        f = self.new("zeta", "print('f')\n", "--thread", "named-story").name
        g = self.new("eta", "print('g')\n", "--thread", "named-story", "--link", "https://slack/1").name
        h = self.new("theta", "print('h')\n", "--thread", "other-story", "--link", "https://slack/1").name
        i = self.new("iota", "print('i')\n", "--thread", "named-story", "--link", "https://slack/4").name
        self.new("kappa", "print('k')\n", "--link", "https://slack/4", "--link", "https://slack/5")
        m = self.new("mu", "print('m')\n", "--thread", "other-story", "--link", "https://slack/5").name
        r = self.cli("threads", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads(r.stdout)
        of = {tid: t["thread"] for tid, t in state["tasks"].items()}
        self.assertEqual(len({of[a], of[b], of[c], of[d]}), 1, of)
        self.assertEqual(of[a], a)
        self.assertNotEqual(of[e], of[a])
        self.assertEqual((of[f], of[g], of[h]), ("named-story", "named-story", "other-story"))
        self.assertEqual((of[i], of[m]), ("named-story", "other-story"), "a task between them merges neither")
        self.assertTrue(state["tasks"][f]["thread_explicit"])
        titles = {th["id"]: th["title"] for th in state["threads"]}
        self.assertEqual(titles["named-story"], "named story")
        self.assertEqual(titles[a], "alpha (web1)")
        self.assertEqual({th["state"] for th in state["threads"]}, {"open"})
        self.assertEqual(self.cli("new", "web1", "bad", "--purpose", "p", "--thread", "Bad Name").returncode, 2)

    def test_state_suggestions_and_thread_state(self) -> None:
        tid = self.new(
            "deploy",
            "import opsquire_helpers as sq\n\nsq.target('web1', 'ok')\n",
            "--changes-prod",
            "--rollback",
            "undo",
        ).name
        for _ in range(3):
            self.assertEqual(self.cli("run", tid).returncode, 0)
        self.assertEqual(self.cli("run", tid, "--apply", answers=[("", "y")]).returncode, 0)
        state = json.loads(self.cli("threads", "--json").stdout)
        kinds = [s["kind"] for s in state["tasks"][tid]["suggestions"]]
        self.assertEqual(kinds, ["rerun-heavy"], state["tasks"][tid]["suggestions"])
        self.assertEqual(state["threads"][0]["state"], "done")
        meta_path = self.home / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/meta.json"
        meta = json.loads(meta_path.read_text())
        del meta["outcome"]
        meta_path.write_text(json.dumps(meta))
        (meta_path.parent / "design.md").unlink()
        state = json.loads(self.cli("threads", "--json").stdout)
        kinds = [s["kind"] for s in state["tasks"][tid]["suggestions"]]
        self.assertEqual(kinds, ["awaiting-close", "rerun-heavy"])
        self.assertEqual(state["threads"][0]["state"], "open")
        self.assertEqual(self.cli("close", tid, "--outcome", "done", "--note", "landed by hand").returncode, 0)
        state = json.loads(self.cli("threads", "--json").stdout)
        kinds = [s["kind"] for s in state["tasks"][tid]["suggestions"]]
        self.assertEqual(kinds, ["no-result", "rerun-heavy"])
        self.assertEqual(state["threads"][0]["state"], "done")
        old = self.new("old", "print('x')\n").name
        meta = json.loads((self.home / f"tasks/{old[:4]}-{old[4:6]}/{old}/meta.json").read_text())
        meta["created"] = "2026-01-01T00:00:00.000Z"
        (self.home / f"tasks/{old[:4]}-{old[4:6]}/{old}/meta.json").write_text(json.dumps(meta))
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual([s["kind"] for s in state["tasks"][old]["suggestions"]], ["stale"])
        self.assertEqual(state["runs"][state["tasks"][tid]["runs"][0]]["targets"], {"web1": "ok"})

    def test_ui_answers_only_its_own_page(self) -> None:
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            self.assertEqual(list((self.home / "var/ui").glob("token-*")), [], "the secret never goes on disk")
            token = UI_TOKEN

            def call(method: str, path: str, headers: dict[str, str], body: str | None = None) -> tuple[int, Any]:
                with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=10)) as conn:
                    conn.putrequest(method, path, skip_host="Host" in headers)
                    for name, value in headers.items():
                        conn.putheader(name, value)
                    conn.endheaders(body.encode() if body is not None else None)
                    resp = conn.getresponse()
                    raw = resp.read()
                    kind = resp.getheader("Content-Type", "")
                    return resp.status, json.loads(raw) if kind.startswith("application/json") else raw

            auth = {"X-Opsquire-Token": token, "X-Opsquire-Request": "1"}
            json_body = {"Content-Type": "application/json", "Content-Length": "2"}
            status, page = call("GET", "/", {})
            self.assertEqual(status, 401)
            self.assertNotIn(token.encode(), page, "the page a stranger gets holds no secret")
            self.assertIn(b"login link", page)
            self.assertEqual(call("GET", "/api/settings", {})[0], 403)
            self.assertEqual(call("GET", "/api/settings", {"X-Opsquire-Token": "x" + token[1:]})[0], 403)
            self.assertEqual(call("GET", "/api/settings", auth)[0], 200)
            self.assertEqual(call("GET", "/api/settings", {**auth, "Origin": "http://evil.example"})[0], 403)
            self.assertEqual(call("GET", "/api/settings", {**auth, "Origin": f"http://localhost:{port}"})[0], 200)
            self.assertEqual(call("GET", "/", {"Host": f"evil.example:{port}"})[0], 403)
            self.assertEqual(call("GET", "/api/settings", {**auth, "Host": f"evil.example:{port}"})[0], 403)
            self.assertEqual(call("GET", "/api/stream", {})[0], 403)
            plain = {**auth, "Content-Type": "text/plain", "Content-Length": "2"}
            self.assertEqual(call("POST", "/api/interrupt", plain, "{}")[0], 415)
            evil = {**auth, **json_body, "Origin": "http://evil.example"}
            self.assertEqual(call("POST", "/api/interrupt", evil, "{}")[0], 403)
            self.assertEqual(call("POST", "/api/interrupt", json_body, "{}")[0], 403)
            status, settings = call("POST", "/api/settings", {**auth, **json_body}, "{}")
            self.assertEqual((status, settings), (200, call("GET", "/api/settings", auth)[1]))
        finally:
            self.stop_ui(proc)

    def test_ui_logs_browsers_in_with_its_link_and_never_serves_its_secrets(self) -> None:
        port = free_port()

        def call(
            path: str, headers: dict[str, str] | None = None, body: str | None = None
        ) -> tuple[int, dict[str, str], bytes]:
            with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=10)) as conn:
                conn.request("POST" if body is not None else "GET", path, body, headers or {})
                resp = conn.getresponse()
                return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()

        def next_line(proc: subprocess.Popen[str]) -> str:
            got: list[str] = []
            reader = threading.Thread(target=lambda: got.append(proc.stdout.readline() if proc.stdout else ""))
            reader.start()
            reader.join(20)
            if not got:
                proc.send_signal(signal.SIGABRT)
                proc.wait(10)
                said = (self.tmp / f"ui-{port}.err").read_text()[-4000:]
                self.fail(f"the console printed no further line; its stderr:\n{said}")
            return got[0]

        def start() -> tuple[subprocess.Popen[str], str]:
            proc = self.ui_process(port)
            self.assertIn(f"http://127.0.0.1:{port}/", next_line(proc))
            found = re.search(r"log in with (http://\S+/login\?code=\S+)", next_line(proc))
            assert found, "the terminal shows a login link"
            return proc, found.group(1).removeprefix(f"http://127.0.0.1:{port}")

        proc, login = start()
        try:
            token = UI_TOKEN
            status, _, page = call("/")
            self.assertEqual(status, 401)
            code = login.split("=", 1)[1]
            self.assertNotIn(token.encode(), page)
            self.assertNotIn(code.encode(), page)
            self.assertEqual(call("/api/settings")[0], 403)
            self.assertEqual(call("/login?code=wrong")[0], 403)
            status, headers, _ = call(login)
            self.assertEqual(status, 200)
            cookie = headers["set-cookie"]
            for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
                self.assertIn(part, cookie)
            session = {"Cookie": "other=x y; " + cookie.split(";", 1)[0]}
            self.assertEqual(call(login)[0], 200, "the link works until the console stops")
            status, _, page = call("/", session)
            self.assertEqual(status, 200)
            self.assertIn(b"<title>Opsquire</title>", page)
            self.assertNotIn(token.encode(), page)
            self.assertEqual(call("/api/settings", session)[0], 200)
            self.assertEqual(call("/api/settings", {"Cookie": f"opsquire-{port}=forged"})[0], 403)
            json_body = {**session, "Content-Type": "application/json"}
            self.assertEqual(call("/api/settings", json_body, "{}")[0], 403, "a POST needs X-Opsquire-Request")
            self.assertEqual(call("/api/settings", {**json_body, "X-Opsquire-Request": "1"}, "{}")[0], 200)
        finally:
            self.stop_ui(proc)
        proc, _ = start()
        try:
            self.assertEqual(call("/api/settings", session)[0], 403, "a restart ends every login")
            self.assertEqual(call("/", session)[0], 401)
        finally:
            self.stop_ui(proc)

    def test_ui_serves_state_stream_log_file_and_close(self) -> None:
        tid = self.new("served", "import opsquire_helpers as sq\n\nsq.result('served fine')\n").name
        self.assertEqual(self.cli("run", tid).returncode, 0)
        (self.home / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/design.md").write_text("# d\n")
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn(f"http://127.0.0.1:{port}/", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def get(path: str) -> tuple[int, Any]:
                conn.request("GET", path)
                resp = conn.getresponse()
                body = resp.read()
                return resp.status, (
                    json.loads(body) if resp.getheader("Content-Type", "").startswith("application/json") else body
                )

            status, page = get("/")
            self.assertEqual(status, 200)
            self.assertIn(b"<title>Opsquire</title>", page)
            status, state = get("/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(state["tasks"][tid]["status"], "done")
            run_id = state["tasks"][tid]["runs"][0]
            status, log = get(f"/api/log?run={run_id}")
            self.assertEqual((status, log["status"], log["start"]), (200, "ok", 0))
            self.assertIn("served fine", log["text"])
            self.assertEqual(get(f"/api/log?run={run_id}&offset={log['offset']}")[1]["text"], "")
            head = get(f"/api/log?run={run_id}&offset=0&until=10")[1]
            self.assertEqual((len(head["text"]), head["offset"]), (10, 10))
            self.assertEqual(state["tasks"][tid]["entry"], "main.py")
            self.assertEqual(get("/api/log?run=nope")[0], 400)
            self.assertEqual(get(f"/api/file?task={tid}&name=design.md")[1]["text"], "# d\n")
            self.assertEqual(get(f"/api/file?task={tid}&name=../LOCAL.md")[0], 400)
            self.assertEqual(get(f"/api/file?task={tid}&name=nothing.md")[0], 400)
            body = json.dumps({"task": tid, "outcome": "partial", "note": "half done", "run": run_id})
            conn.request("POST", "/api/close", body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            closed = json.loads(resp.read())
            self.assertEqual(
                (resp.status, closed["outcome"]["state"], closed["outcome"]["run"]), (200, "partial", run_id)
            )
            self.assertTrue(any("Result" in n for n in closed["notes"]), closed)
            conn.request(
                "POST",
                "/api/close",
                json.dumps({"task": tid, "outcome": "nope", "note": "x"}),
                {"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
            resp.read()
            self.assertEqual(json.loads(self.cli("show", tid, "--json").stdout)["status"], "partial")
            conn.request(
                "POST", "/api/abandon", json.dumps({"task": tid, "note": "x"}), {"Content-Type": "application/json"}
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
            self.assertIn("nothing waits", json.loads(resp.read())["error"], "an ended task keeps how it ended")
            stream = self.http(port, 10)
            stream.request("GET", "/api/stream")
            sresp = stream.getresponse()
            self.assertIn(b"event: hello", sresp.readline() + sresp.readline())
            self.new("later", "print('later')\n")
            deadline = time.time() + 5
            seen = b""
            while b"event: change" not in seen and time.time() < deadline:
                seen += sresp.readline()
            self.assertIn(b"event: change", seen)
            stream.close()
            conn.close()
        finally:
            self.stop_ui(proc)

    def test_ui_streams_every_change_that_moves_the_inbox_to_every_page(self) -> None:
        folder = self.new("handed", "print('v1')\n")
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn(f"http://127.0.0.1:{port}/", proc.stdout.readline() if proc.stdout else "")
            streams = []
            for _ in range(2):
                conn = self.http(port, 10)
                conn.request("GET", "/api/stream")
                resp = conn.getresponse()
                self.assertIn(b"event: hello", resp.readline() + resp.readline())
                streams.append((conn, resp))

            def inbox() -> list[dict[str, Any]]:
                with contextlib.closing(Console(port, 10)) as conn:
                    conn.request("GET", "/api/state")
                    items: list[dict[str, Any]] = json.loads(conn.getresponse().read())["inbox"]
                return items

            def every_page_hears_a_change() -> None:
                for _, resp in streams:
                    seen = b""
                    while b"event: change" not in seen:
                        line = resp.readline()
                        self.assertTrue(line, "stream closed")
                        seen += line

            self.assertEqual(inbox(), [])
            self.assertEqual(self.cli("lint", folder.name).returncode, 0)
            every_page_hears_a_change()
            self.assertEqual(inbox(), [], "linted is not ready: its agent may still be writing it")
            self.hand_over(folder.name)
            every_page_hears_a_change()
            self.assertEqual([(i["task_id"], i["changed_since_lint"]) for i in inbox()], [(folder.name, False)])
            (folder / "main.py").write_text("print('v2')\n")
            every_page_hears_a_change()
            self.assertEqual(inbox(), [], "edited after its handover: back to being prepared")
            for conn, _ in streams:
                conn.close()
        finally:
            self.stop_ui(proc)

    def test_ui_inbox_and_jobs_run_tasks_with_prompts(self) -> None:
        body = (
            "import opsquire_helpers as sq\n\nif sq.APPLY:\n    sq.confirm('Really change web1?')\n"
            "sq.target('web1', 'ok')\nsq.result('changed' if sq.APPLY else 'would change')\n"
        )
        tid = self.new("job", body, "--changes-prod", "--rollback", "undo").name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        self.hand_over(tid)
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            def wait_job(job: str, status: str) -> dict[str, Any]:
                deadline = time.time() + 30
                while time.time() < deadline:
                    j: dict[str, Any] = call("GET", f"/api/job?id={job}&offset=0")[1]
                    if j["status"] == status:
                        return j
                    time.sleep(0.2)
                self.fail(f"job {job} never reached {status}: {j}")

            _, state = call("GET", "/api/state")
            self.assertEqual([(i["task_id"], i["next"]) for i in state["inbox"]], [(tid, "dry")])
            self.assertEqual(state["jobs"], [])
            self.assertEqual(self.cli("run", tid).returncode, 0)
            status, started = call("POST", "/api/run", {"task": tid, "mode": "apply"})
            self.assertEqual(status, 200, started)
            j = wait_job(started["job_id"], "waiting")
            self.assertIn("Really change web1? [y/N]", j["prompt"])
            self.assertIn("Really change web1?", j["text"])
            status, again = call("POST", "/api/run", {"task": tid, "mode": "apply"})
            self.assertEqual((status, again["job_id"], again["joined"]), (200, started["job_id"], True))
            status, other = call("POST", "/api/run", {"task": tid, "mode": "dry"})
            self.assertEqual(status, 409)
            self.assertIn("already has a apply job running", other["error"])
            self.assertEqual(call("POST", "/api/answer", {"job": started["job_id"], "text": "n"})[0], 200)
            j = wait_job(started["job_id"], "done")
            self.assertEqual(j["exit_code"], 3)
            self.assertEqual(self.last(tid)["status"], "aborted")
            self.assertEqual(j["run_id"], self.last(tid)["run_id"])
            _, state = call("GET", "/api/state")
            self.assertEqual([(i["task_id"], i["next"]) for i in state["inbox"]], [(tid, "dry")])
            status, started = call("POST", "/api/run", {"task": tid, "mode": "dry"})
            j = wait_job(started["job_id"], "done")
            self.assertEqual((j["exit_code"], self.last(tid)["status"]), (0, "ok"))
            _, state = call("GET", "/api/state")
            self.assertEqual([(i["task_id"], i["next"]) for i in state["inbox"]], [(tid, "apply")])
            status, started = call("POST", "/api/run", {"task": tid, "mode": "apply"})
            j = wait_job(started["job_id"], "waiting")
            self.assertEqual(call("POST", "/api/answer", {"job": started["job_id"], "text": "y"})[0], 200)
            j = wait_job(started["job_id"], "done")
            self.assertEqual(j["exit_code"], 0, j["text"])
            self.assertIn("closed done", j["text"])
            _, state = call("GET", "/api/state")
            self.assertEqual(state["inbox"], [])
            self.assertEqual(state["tasks"][tid]["status"], "done")
            self.assertEqual(state["tasks"][tid]["outcome"]["closed_by"], "opsquire run (auto)")
            self.assertEqual(len(state["jobs"]), 3)
            self.assertEqual(call("POST", "/api/run", {"task": tid, "mode": "read"})[0], 400)
            conn.close()
        finally:
            self.stop_ui(proc)

    def test_ui_job_runs_the_handed_over_args_the_inbox_shows(self) -> None:
        show = "import sys\n\nimport opsquire_helpers as sq\n\nsq.result(repr(sys.argv[1:]))\n"
        read = self.new("reused", show).name
        change = self.new(
            "flip",
            show.replace("sq.result", "sq.confirm('go?') if sq.APPLY else None\nsq.result"),
            "--changes-prod",
            "--rollback",
            "undo",
        ).name
        for tid in (read, change):
            self.assertEqual(self.cli("lint", tid).returncode, 0)
        self.assertEqual(self.cli("run", read).returncode, 0)
        with_space = ["focus", "two words"]
        self.assertEqual(self.cli("handover", read, "--", *with_space).returncode, 0)
        self.assertEqual(self.cli("run", change, "--", "web1").returncode, 0)
        self.assertEqual(self.cli("handover", change, "--apply", "--", "web1").returncode, 0)
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                return json.loads(conn.getresponse().read())

            def run(tid: str, mode: str, **extra: Any) -> dict[str, Any]:
                job = call("POST", "/api/run", {"task": tid, "mode": mode, **extra})["job_id"]
                deadline = time.time() + 30
                while time.time() < deadline:
                    j: dict[str, Any] = call("GET", f"/api/job?id={job}&offset=0")
                    if j["status"] == "waiting":
                        call("POST", "/api/answer", {"job": job, "text": "y"})
                    if j["status"] == "done":
                        return j
                    time.sleep(0.2)
                self.fail(f"job {job} never finished: {j}")

            inbox = {i["task_id"]: i for i in call("GET", "/api/state")["inbox"]}
            self.assertEqual(inbox[read]["command"], f"opsquire run {read} -- focus 'two words'")
            self.assertEqual(inbox[read]["args"], "focus 'two words'")

            j = run(read, "read", args=["from", "the", "page"])
            self.assertEqual(j["args"], "focus 'two words'")
            rec = self.last(read)
            self.assertEqual((rec["argv"], rec["results"]), (with_space, [repr(with_space)]))
            self.assertNotIn(read, {i["task_id"] for i in call("GET", "/api/state")["inbox"]})
            self.assertEqual(run(read, "read")["args"], "")
            self.assertEqual(self.last(read)["argv"], [])

            run(change, "dry")
            self.assertEqual(self.last(change)["argv"], [], "a dry run is not the handed-over apply")
            self.assertEqual(self.cli("handover", change, "--apply", "--", "web1").returncode, 0)
            folder = next(self.home.glob(f"tasks/*/{change}"))
            (folder / "main.py").write_text((folder / "main.py").read_text() + "\n")
            self.assertEqual(run(change, "apply")["exit_code"], 2, "edited after its dry run: refused")
            refused = self.last(change)
            self.assertEqual((refused["status"], refused["argv"]), ("blocked", ["web1"]))
            self.assertIn("the scripts changed since its last dry run", refused["last_error"])
            shown = call("GET", "/api/state")
            self.assertNotIn(change, {i["task_id"] for i in shown["inbox"]}, "edited after its handover")
            self.assertEqual([p["task_id"] for p in shown["preparing"]], [change])
            self.assertEqual(self.cli("run", change, "--", "web1").returncode, 0, "the operator's own run")
            waiting = next(i for i in call("GET", "/api/state")["inbox"] if i["task_id"] == change)
            self.assertEqual((waiting["next"], waiting["args"], waiting["handed"]), ("apply", "web1", None))
            self.assertEqual(run(change, "apply")["exit_code"], 0)
            runs = json.loads(self.cli("runs", change, "--json").stdout)
            applied = next(r for r in reversed(runs) if r["mode"] == "apply")
            self.assertEqual(
                (applied["status"], applied["argv"]), ("ok", ["web1"]), "the apply takes the arguments of its dry run"
            )

            conn.close()
        finally:
            self.stop_ui(proc)

    def test_ui_says_when_its_code_is_older_than_what_is_on_disk(self) -> None:
        code = self.tmp / "code"
        shutil.copytree(ROOT / "src/opsquire", code / "opsquire", ignore=shutil.ignore_patterns("__pycache__"))
        self.env["PYTHONPATH"] = str(code)
        jobs_py = code / "opsquire/jobs.py"
        original = jobs_py.read_text()
        port = free_port()

        def start() -> subprocess.Popen[str]:
            ui = self.ui_process(port)
            self.assertIn("opsquire ui", ui.stdout.readline() if ui.stdout else "")
            return ui

        def status() -> Any:
            with contextlib.closing(Console(port, 10)) as conn:
                conn.request("GET", "/api/state")
                return json.loads(conn.getresponse().read())["code"]

        proc = start()
        try:
            first = status()
            self.assertEqual((first["stale"], first["changed"]), (False, []))

            os.utime(jobs_py)
            (code / "opsquire/data/templates/main.py").write_text("# read fresh by every run\n")
            (code / "opsquire/data/helpers/opsquire_helpers.py").write_text("# read fresh by every run\n")
            self.assertFalse(status()["stale"], "same modules, and files the console does not keep")

            conn = self.http(port, 15)
            conn.request("GET", "/api/stream")
            stream = conn.getresponse()
            self.assertEqual(stream.readline(), b"event: hello\n")
            stream.readline(), stream.readline()
            jobs_py.write_text(original + "\n# newer\n")
            self.assertEqual(stream.readline(), b"event: change\n", "an open page hears about it by itself")
            conn.close()
            newer = status()
            self.assertEqual((newer["stale"], newer["changed"]), (True, ["jobs.py"]))

            module, sheet = code / "opsquire/data/ui/js/render.js", code / "opsquire/data/ui/css/base.css"
            for page in (module, sheet):
                page.write_text(page.read_text() + "\n")
            (code / "opsquire/added.py").write_text("")
            newer = status()
            self.assertEqual(newer["changed"], ["added.py", "jobs.py"], "page files need a reload, not a restart")
            self.assertNotEqual(newer["page"], first["page"], "a tab can tell a reload would give it a newer page")

            (code / "opsquire/added.py").unlink()
            for page in (module, sheet):
                page.write_text(page.read_text()[:-1])
            jobs_py.write_text(original)
            back = status()
            self.assertEqual((back["stale"], back["changed"]), (False, []), "back on the code it loaded")

            jobs_py.write_text(original + "\n# newer\n")
            self.assertTrue(status()["stale"])
        finally:
            self.stop_ui(proc)
        proc = start()
        try:
            self.assertEqual(status()["stale"], False, "a restart picks up the code on disk")
        finally:
            self.stop_ui(proc)

    def test_ui_code_status_sees_a_newer_install(self) -> None:
        from opsquire import __version__, ui

        loaded = ui.code_hashes()
        self.assertIn("jobs.py", loaded)
        for name in ("data/ui/index.html", "data/ui/css/tokens.css", "data/ui/js/boot.js"):
            self.assertIn(name, loaded)
        self.assertNotIn("data/templates/main.py", loaded)
        with mock.patch("opsquire.ui.installed_version", return_value=__version__):
            self.assertFalse(ui.code_status(loaded)["stale"])
        with mock.patch("opsquire.ui.installed_version", return_value="99.0.0"):
            newer = ui.code_status(loaded)
        self.assertEqual((newer["stale"], newer["changed"], newer["installed"]), (True, [], "99.0.0"))
        gone = ui.code_status({**loaded, "removed.py": "x"})
        self.assertEqual((gone["stale"], gone["changed"]), (True, ["removed.py"]))

    def test_ui_page_has_the_restart_banner(self) -> None:
        from opsquire import ui

        found = ui.static_file("/ui/js/render.js")
        assert found, "the page's render module is served"
        module = found[0].decode()
        self.assertIn("function renderStale()", module)
        self.assertIn("renderStale(), S.view === 'live' ? null : renderLive()", module)
        self.assertIn("opsquire ui --port ${location.port}", module)

    def test_ui_serves_its_stylesheets_and_modules_only_to_a_logged_in_browser(self) -> None:
        port = free_port()
        folder = ROOT / "src/opsquire/data/ui"

        def get(path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
            with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=10)) as conn:
                conn.request("GET", path, headers=headers or {})
                resp = conn.getresponse()
                return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()

        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            status, headers, _ = get(f"/login?code={UI_TOKEN}")
            self.assertEqual(status, 200)
            session = {"Cookie": headers["set-cookie"].split(";", 1)[0]}
            status, _, page = get("/", session)
            self.assertEqual(status, 200)
            referenced = re.findall(r'(?:href|src)="([^"]+)"', page.decode())
            self.assertTrue(any(r.endswith(".css") for r in referenced) and any(r.endswith(".js") for r in referenced))
            kinds = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
            for path in referenced:
                status, headers, body = get(path, session)
                self.assertEqual(status, 200, path)
                self.assertEqual(headers["content-type"], kinds[Path(path).suffix], path)
                self.assertEqual(headers["cache-control"], "no-cache", path)
                self.assertEqual(body, (folder / path.removeprefix("/ui/")).read_bytes(), path)

            modules = sorted((folder / "js").glob("*.js"))
            self.assertIn(folder / "js/boot.js", modules)
            for module in modules:
                text = module.read_text()
                for name in re.findall(r"^import\s[^;]*?\sfrom\s'([^']+)';", text, re.M | re.S):
                    self.assertRegex(name, r"^\./[a-z0-9_-]+\.js$", f"{module.name} imports {name}")
                    status, headers, _ = get(f"/ui/js/{name[2:]}", session)
                    self.assertEqual((status, headers["content-type"]), (200, kinds[".js"]), f"{module.name}: {name}")
                self.assertNotRegex(text, r"\bimport\s*\(", f"{module.name} imports at run time")

            status, _, body = get("/ui/js/boot.js")
            self.assertEqual(status, 401, "no session, no code")
            self.assertNotIn(UI_TOKEN.encode(), body)
            self.assertEqual(get("/ui/css/tokens.css", {"Cookie": f"opsquire-{port}=forged"})[0], 401)
            self.assertEqual(get("/ui/js/boot.js", {"Host": f"evil.example:{port}"})[0], 403)
            outside = [
                "/ui/js/../../ui.py",
                "/ui/js/..%2f..%2fui.py",
                "/ui/css/../js/boot.js",
                "/ui/js/boot.css",
                "/ui/css/boot.js",
                "/ui/js/missing.js",
                "/ui/js/.boot.js",
                "/ui/js/sub/boot.js",
                "/ui/js/boot.js/",
                "/ui/index.html",
                "/ui/ui.py",
                "/ui/js/",
            ]
            for path in outside:
                self.assertEqual(get(path, session)[0], 404, path)
        finally:
            self.stop_ui(proc)

    def test_run_origin_is_the_console_a_terminal_or_nobody(self) -> None:
        code = "from opsquire.model import run_origin; print(run_origin())"
        cmd = [sys.executable, "-c", code]
        for console, tty, origin in (
            (True, False, "console"),
            (True, True, "console"),
            (False, True, "terminal"),
            (False, False, "unattended"),
        ):
            env = {**self.env, "OPSQUIRE_ORIGIN": "console"} if console else self.env
            if tty:
                out = run_in_pty(cmd, env, []).stdout
            else:
                r = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
                out = r.stdout
            self.assertEqual(out.strip(), origin, (console, tty))

    def test_run_records_whether_the_console_a_terminal_or_nobody_started_it(self) -> None:
        tid = self.new("origin", "import opsquire_helpers as sq\n\nsq.result('ran')\n").name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        plan = self.cli("new", "web1", "both", "--plan", "--purpose", "p", "--step", tid)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        plan_id = next(p.name for p in self.home.glob("tasks/*/*") if p.name.endswith("_both"))

        self.assertEqual(self.cli("run", tid, answers=[]).returncode, 0)
        self.assertEqual(self.last(tid)["origin"], "terminal")
        self.assertEqual(self.cli("run", plan_id, answers=[]).returncode, 0)
        self.assertEqual(self.last(plan_id)["origin"], "terminal")

        self.env["OPSQUIRE_ORIGIN"] = "made up"
        self.assertEqual(self.cli("run", tid, answers=[]).returncode, 0)
        self.assertEqual(self.last(tid)["origin"], "terminal", "only the console's own word counts")
        del self.env["OPSQUIRE_ORIGIN"]

        self.assertEqual(
            self.cli("run", tid, unattended=True).returncode, 0, "stdin is not a terminal: nobody is there"
        )
        unattended = self.last(tid)
        self.assertEqual(unattended["origin"], "unattended")
        entries = [e for e in self.ledger_entries() if e["data"].get("run") == unattended["run_id"]]
        self.assertEqual(
            [(e["kind"], e["actor"]["origin"]) for e in entries], [(k, "unattended") for k in ("run_start", "run_end")]
        )

        self.assertEqual(self.env["OPSQUIRE_AUTHOR"], "test", "the author says nothing about the console here")
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                return json.loads(conn.getresponse().read())

            def run(task: str) -> None:
                job = call("POST", "/api/run", {"task": task, "mode": "read"})["job_id"]
                deadline = time.time() + 30
                while time.time() < deadline:
                    j = call("GET", f"/api/job?id={job}&offset=0")
                    if j["status"] == "done":
                        self.assertEqual(j["exit_code"], 0, j["text"])
                        return
                    time.sleep(0.2)
                self.fail(f"job {job} never finished: {j}")

            run(tid)
            rec = self.last(tid)
            self.assertEqual((rec["origin"], rec["cwd"]), ("console", os.getcwd()), "same cwd, different origin")
            run(plan_id)
            self.assertEqual(self.last(plan_id)["origin"], "console")
            self.assertEqual(self.last(tid)["origin"], "console", "a plan's step started where the plan did")

            runs = call("GET", "/api/state")["runs"]
            self.assertEqual(runs[self.last(tid)["run_id"]]["origin"], "console")
            self.assertEqual(runs[unattended["run_id"]]["origin"], "unattended")
            self.assertEqual({r["origin"] for r in runs.values()}, {"console", "terminal", "unattended"})
            conn.close()
        finally:
            self.stop_ui(proc)

        self.assertEqual(self.cli("run", tid, unattended=True).returncode, 0)
        table = self.cli("runs", tid).stdout
        self.assertIn("ORIGIN", table)
        self.assertIn("console", table)
        self.assertIn("terminal", table)
        self.assertIn("unattended", table)
        self.assertIn("origin=unattended", self.cli("log", tid).stdout)
        self.assertIn("origin=console", self.cli("log", plan_id).stdout, "a plan's record has no exit code of its own")

        folder = next(self.home.glob(f"var/runs/{tid}/*"))
        older = json.loads((folder / "run.json").read_text())
        del older["origin"]
        (folder / "run.json").write_text(json.dumps(older))
        self.assertEqual(self.cli("runs", tid).returncode, 0)

    def test_ui_judge_job_ticks_a_task_off(self) -> None:
        self.agents_file()
        tid = self.new(
            "tick",
            "import opsquire_helpers as sq\n\nsq.target('web1', 'failed')\n",
            "--changes-prod",
            "--rollback",
            "undo",
        ).name
        self.assertEqual(self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")]).returncode, 1)
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            _, state = call("GET", "/api/state")
            self.assertFalse(state["tasks"][tid]["settled"])
            self.assertEqual([i["task_id"] for i in state["inbox"]], [tid])
            status, started = call("POST", "/api/judge", {"task": tid})
            self.assertEqual(status, 200, started)
            deadline = time.time() + 30
            while time.time() < deadline:
                _, j = call("GET", f"/api/job?id={started['job_id']}&offset=0")
                if j["status"] == "done":
                    break
                time.sleep(0.2)
            self.assertEqual((j["mode"], j["exit_code"], j["run_id"]), ("judge", 0, None), j)
            self.assertIn("closed done", j["text"])
            _, state = call("GET", "/api/state")
            self.assertTrue(state["tasks"][tid]["settled"])
            self.assertEqual(state["tasks"][tid]["outcome"]["closed_by"], "opsquire judge (fake)")
            self.assertEqual(state["inbox"], [])
            self.assertEqual(state["jobs"][0]["mode"], "judge")
            conn.close()
        finally:
            self.stop_ui(proc)

    def test_a_nested_scripts_prompt_reaches_the_console_and_the_record(self) -> None:
        lines = "x" * 100
        children = {
            "py": "import opsquire_helpers as sq\n\n"
            f"for i in range(1, 41):\n    print(i, '{lines}')\nsq.confirm('Deploy web1?')\nsq.result('deployed')\n",
            "sh": BASH_HEADER + f'for i in $(seq 40); do echo "$i {lines}"; done\n'
            'sq_confirm "Deploy web1?"\nsq_result deployed\n',
        }
        for lang, child in children.items():
            with self.subTest(lang=lang):
                tid = self.nested_task(child, lang)
                self.assertEqual(self.cli("run", tid, answers=[("[y/N]", "y")]).returncode, 0)
                proc, call = self.console()
                try:
                    job = call("/api/run", {"task": tid, "mode": "apply"})["job_id"]
                    j = self.await_job(call, job, "waiting")
                    self.assertEqual(j["prompt"], ">>> Deploy web1? [y/N]")
                    self.assertTrue(j["text"].endswith("[y/N] "), j["text"][-200:])
                    self.assertIn(f"40 {lines}", j["text"])
                    rec = self.last(tid)
                    self.assertEqual((rec["status"], rec["waiting"]["question"]), ("running", "Deploy web1?"))
                    self.assertIn("waiting for the operator: Deploy web1?", Path(rec["paths"]["output"]).read_text())
                    self.assertIn('"event": "waiting"', Path(rec["paths"]["events"]).read_text())
                    table = self.cli("runs", tid).stdout.splitlines()[-1]
                    self.assertTrue(table.startswith("waiting"), table)
                    self.assertIn("asks the operator: Deploy web1?", table)
                    self.assertIn("waiting for the operator to answer", self.cli("log", tid).stdout)
                    view = call("/api/state")["runs"][rec["run_id"]]
                    self.assertEqual(view["waiting"]["question"], "Deploy web1?")
                    call("/api/answer", {"job": job, "text": "y"})
                    self.assertEqual(self.await_job(call, job, "done")["exit_code"], 0)
                    rec = self.last(tid)
                    self.assertEqual((rec["status"], rec["waiting"], rec["results"]), ("ok", None, ["deployed"]))
                finally:
                    self.stop_ui(proc)
                shutil.rmtree(next(self.home.glob(f"tasks/*/{tid}")))
                shutil.rmtree(self.home / "general/web1_child")
                shutil.rmtree(self.home / "var/runs" / tid)

    def test_inbox_lists_each_tasks_next_run_and_the_unread_chats(self) -> None:
        tid = self.new("restart", "print(1)\n", *self.CHANGING).name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        self.hand_over(tid)
        asks = self.home / "var/asks"
        asks.mkdir(parents=True)
        (self.home / "var/chats-read.json").write_text(json.dumps({"since": "2026-01-01T00:00:00.000Z", "read": {}}))
        record = {"ask_id": "a1", "status": "ok", "started": "2026-11-13T10:00:00.000Z", "prompt": "restart web1\nnow"}
        (asks / "a1.json").write_text(json.dumps(record | {"ended": "2026-11-13T10:01:00.000Z"}))
        r = self.cli("inbox")
        self.assertEqual(r.returncode, 0, r.stderr)
        row = next(line for line in r.stdout.splitlines() if tid in line)
        self.assertTrue(row.startswith("dry"), row)
        self.assertIn(f"opsquire run {tid}", row)
        self.assertIn("unread chats (1), open them in the console:\n  a1  restart web1", r.stdout)
        shown = json.loads(self.cli("inbox", "--json").stdout)
        self.assertEqual([(i["task_id"], i["next"]) for i in shown["inbox"]], [(tid, "dry")])
        self.assertEqual(
            shown["unread_chats"],
            [{"conversation": "a1", "prompt": record["prompt"], "ended": record["started"][:11] + "10:01:00.000Z"}],
        )

    def test_a_chat_with_a_reply_is_unread_for_the_whole_home_until_opened(self) -> None:
        asks = self.home / "var/asks"
        asks.mkdir(parents=True)

        def ended(ask_id: str, **extra: Any) -> None:
            at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            record = {"ask_id": ask_id, "status": "ok", "started": at, "ended": at, "prompt": ask_id, **extra}
            (asks / f"{ask_id}.json").write_text(json.dumps(record))

        ended("before", copies="before")
        time.sleep(0.01)
        proc, call = self.console()
        try:
            self.assertEqual(call("/api/state")["unread_chats"], [], "chats that ended before the console are read")
            time.sleep(0.01)
            ended("first")
            self.assertEqual(call("/api/state")["unread_chats"], ["first"])
            self.assertEqual(call("/api/chatread", {"conversation": "first"}), {"ok": True})
            self.assertEqual(call("/api/state")["unread_chats"], [])
            time.sleep(0.01)
            ended("follow", copies="first")
            self.assertEqual(call("/api/state")["unread_chats"], ["first"], "a follow-up's reply is news again")
            self.assertIn("no conversation", call("/api/chatread", {"conversation": "follow"})["error"])
            self.assertIn("bad conversation id", call("/api/chatread", {"conversation": "../x"})["error"])
        finally:
            self.stop_ui(proc)
        read = json.loads((self.home / f"var/chats-read/{pwd.getpwuid(os.geteuid()).pw_name}.json").read_text())
        self.assertEqual(sorted(read["read"]), ["first"])
        self.assertLess(read["since"], read["read"]["first"])

    def test_console_keeps_a_prompt_that_output_landed_behind(self) -> None:
        from opsquire.jobs import Job, Jobs

        jobs, master_slave = Jobs(), os.pipe()
        job = Job(
            "j1", "t", "apply", "now", output=bytearray(b"plan 1\r\n\r\n>>> Deploy?\r\nnow [y/N] plan 2\r\nplan 3\r\n")
        )
        job.fd = master_slave[1]
        jobs.jobs[job.job_id] = job
        try:
            jobs.detect_helper_prompt(job)
            self.assertEqual((job.status, job.prompt), ("waiting", ">>> Deploy?\nnow [y/N]"))
            job.output += b"more output\r\n"
            jobs.detect_helper_prompt(job)
            self.assertEqual(job.status, "waiting")
            job.output += b"\r\n>>> Second? [y/N] "
            jobs.answer("j1", "y")
            self.assertEqual(os.read(master_slave[0], 10), b"y\n")
            self.assertEqual((job.status, job.prompt), ("waiting", ">>> Second? [y/N]"))
            jobs.answer("j1", "n")
            self.assertEqual((job.status, job.prompt), ("running", None))
            jobs.detect_helper_prompt(job)
            self.assertEqual(job.status, "running")
        finally:
            os.close(master_slave[0])
            os.close(master_slave[1])

    def test_doctor_sees_a_running_console_through_its_health_file(self) -> None:
        self.env["OPSQUIRE_AUTHOR"] = "claude-code 1234abcd"
        r = self.cli("doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("console: not running", r.stdout)
        port = str(free_port())
        ui = subprocess.Popen(
            [*CLI, "ui", "--port", port], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            for _ in range(60):
                with contextlib.suppress(OSError):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/state",
                        headers={"X-Opsquire-Token": UI_TOKEN},
                    )
                    urllib.request.urlopen(req, timeout=5).read()
                    if (self.home / f"var/ui/{port}.json").is_file():
                        break
                time.sleep(0.25)
            out = self.cli("doctor").stdout
            self.assertIn(f"console: up at http://127.0.0.1:{port}/", out)
            self.assertIn("last state build", out)
        finally:
            ui.send_signal(signal.SIGINT)
            ui.wait(20)
        self.assertFalse((self.home / f"var/ui/{port}.json").exists(), "a console that stops takes its file with it")

    def test_every_state_field_the_page_reads_is_sent(self) -> None:
        js = "".join(p.read_text() for p in (ROOT / "src/opsquire/data/ui/js").glob("*.js"))
        read = set(re.findall(r"\b(?:S\.data|data)\.([a-z_]+)", js))
        proc, call = self.console()
        try:
            sent = call("/api/state")
        finally:
            self.stop_ui(proc)
        self.assertEqual(sent["kind"], "full")
        self.assertEqual(sorted(read - set(sent)), [])

    def test_the_console_sends_only_what_changed_since_the_pages_version(self) -> None:
        for name in ("one", "two"):
            self.new(name, "print(1)\n")
            self.assertEqual(self.cli("lint", name).returncode, 0)
        port = str(free_port())
        ui = subprocess.Popen(
            [*CLI, "ui", "--port", port], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        def get(since: str = "") -> tuple[dict[str, Any], str]:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/state" + (f"?since={since}" if since else ""),
                headers={"Accept-Encoding": "gzip", "X-Opsquire-Token": UI_TOKEN},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                raw, enc = r.read(), r.headers.get("Content-Encoding") or ""
            body: dict[str, Any] = json.loads(gzip.decompress(raw) if enc == "gzip" else raw)
            return body, enc

        try:
            for _ in range(80):
                with contextlib.suppress(OSError):
                    full, enc = get()
                    break
                time.sleep(0.25)
            self.assertEqual(full["kind"], "full")
            self.assertEqual(enc, "gzip")
            self.assertEqual(len(full["tasks"]), 2)
            self.assertEqual(get(full["version"])[0]["kind"], "same")
            self.assertEqual(self.cli("run", "one").returncode, 0)
            for _ in range(40):
                patch, _ = get(full["version"])
                if patch["kind"] != "same":
                    break
                time.sleep(0.25)
            self.assertEqual(patch["kind"], "patch")
            self.assertEqual(list(patch["changed"]["tasks"]), [next(t for t in full["tasks"] if "one" in t)])
            self.assertEqual(len(patch["changed"]["runs"]), 1)
            self.assertEqual(get("not-a-version")[0]["kind"], "full")
        finally:
            ui.send_signal(signal.SIGINT)
            ui.wait(20)

    def test_a_console_run_that_does_not_start_leaves_its_reason_on_the_task(self) -> None:
        folder = self.new("broken", "print(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify")
        self.assertEqual(self.cli("lint", "broken").returncode, 0)
        (folder / "main.py").write_text("def f(x):\n    return x\n")
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            def problems() -> dict[str, Any]:
                deadline = time.time() + 30
                while time.time() < deadline:
                    found: dict[str, Any] = call("GET", "/api/state")[1]["start_problems"]
                    if found.get(folder.name):
                        return found
                    time.sleep(0.2)
                return {}

            status, started = call("POST", "/api/run", {"task": folder.name, "mode": "dry"})
            self.assertEqual(status, 200, started)
            why = problems().get(folder.name) or {}
            self.assertEqual(why.get("mode"), "dry")
            self.assertIn("lint", why.get("why", ""))
            status, refused = call("POST", "/api/run", {"task": folder.name, "mode": "verify"})
            self.assertEqual(status, 400)
            self.assertIn("not verify", call("GET", "/api/state")[1]["start_problems"][folder.name]["why"])
            (folder / "main.py").write_text("print(1)\n")
            status, started = call("POST", "/api/run", {"task": folder.name, "mode": "dry"})
            self.assertEqual(status, 200)
            self.assertNotIn(folder.name, call("GET", "/api/state")[1]["start_problems"])
        finally:
            self.stop_ui(proc)

    def test_doctor_counts_only_this_accounts_consoles(self) -> None:
        from opsquire import doctor
        from opsquire.model import iso, now

        (self.home / "var/ui").mkdir(parents=True)
        for port, uid in (("7771", os.geteuid() + 1), ("7772", os.geteuid())):
            rec = {"pid": 1, "url": f"http://127.0.0.1:{port}/", "beat": iso(now()), "uid": uid}
            (self.home / f"var/ui/{port}.json").write_text(json.dumps(rec))
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual([c["url"] for c in doctor.consoles()], ["http://127.0.0.1:7772/"])

    def test_chat_read_state_is_each_operators_own_starting_from_the_homes_old_file(self) -> None:
        from opsquire import chatread

        legacy = self.home / "var/chats-read.json"
        legacy.parent.mkdir(parents=True)
        old = {"since": "2026-01-01T00:00:00.000Z", "read": {"a1": "2026-01-02T00:00:00.000Z"}}
        legacy.write_text(json.dumps(old))
        asks = [
            {"ask_id": "a1", "ended": "2026-01-03T00:00:00.000Z"},
            {"ask_id": "a2", "ended": "2026-01-03T00:00:00.000Z"},
        ]
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(chatread.unread(asks, chatread.load()), ["a1", "a2"])
            chatread.mark("a1", asks)
            mine = self.home / f"var/chats-read/{pwd.getpwuid(os.geteuid()).pw_name}.json"
            self.assertEqual(json.loads(mine.read_text())["since"], old["since"])
            self.assertEqual(chatread.unread(asks, chatread.load()), ["a2"])
            with mock.patch("opsquire.chatread.user_name", return_value="bob"):
                self.assertEqual(chatread.unread(asks, chatread.load()), ["a1", "a2"], "bob starts from the old file")
                chatread.mark("a2", asks)
                self.assertEqual(chatread.unread(asks, chatread.load()), ["a1"])
            self.assertEqual(chatread.unread(asks, chatread.load()), ["a2"], "bob's reads are not mine")
        self.assertEqual(json.loads(legacy.read_text()), old, "the old file is only read")
