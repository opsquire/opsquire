"""Locks, fencing tokens, nested targets and run liveness."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from support import BASH_HEADER, CLI, OpsTestCase, pid_alive


class LockTest(OpsTestCase):
    def test_lock_conflict_break_and_stale(self) -> None:
        self.new("locked", "print('ran')\n", "--changes-prod", "--rollback", "none needed")
        self.write_lock("web1", os.getpid())
        self.assertEqual(self.cli("run", "locked", "--no-wait").returncode, 2)
        self.assertIn("locked by other", self.last("locked")["last_error"])

        self.assertEqual(self.cli("run", "locked", "--break-lock", answers=[("Type BREAK", "nope")]).returncode, 2)
        r = self.cli("run", "locked", "--break-lock", answers=[("Type BREAK", "BREAK")])
        self.assertEqual(r.returncode, 0, r.stdout)
        rec = self.last("locked")
        self.assertEqual(rec["lock"]["broken"][0]["run_id"], "R1")
        self.assertEqual(json.loads(self.cli("locks", "--json").stdout), {})

        dead = subprocess.Popen(["true"])
        dead.wait()
        self.write_lock("web1", dead.pid)
        self.assertEqual(self.cli("run", "locked").returncode, 0)
        self.assertEqual(self.last("locked")["lock"]["stale_reclaimed"][0]["pid"], dead.pid)

    def held_on_web1(self) -> list[str]:
        held = json.loads(self.cli("locks", "--json").stdout)
        return [h["run_id"] for h in held.get("web1", [])]

    def test_unlock_removes_the_locks_when_the_target_name_is_typed(self) -> None:
        self.write_lock("web1", os.getpid())
        r = self.cli("unlock", "web1", answers=[("Type the target name to confirm: ", "web1")])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("removed 1 lock(s): R1", r.stdout)
        self.assertEqual(self.held_on_web1(), [])
        lines = (self.home / "var/ledger/entries.jsonl").read_text().splitlines()
        removes = [e["data"] for e in map(json.loads, lines) if e["kind"] == "lock" and e["data"]["event"] == "remove"]
        self.assertEqual([(d["target"], [h["run_id"] for h in d["holders"]]) for d in removes], [("web1", ["R1"])])

    def test_unlock_keeps_the_locks_on_any_other_answer(self) -> None:
        self.write_lock("web1", os.getpid())
        for answer in ("web", ""):
            r = self.cli("unlock", "web1", answers=[("Type the target name to confirm: ", answer)])
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("not removed", r.stdout)
            self.assertEqual(self.held_on_web1(), ["R1"], repr(answer))

    def test_unlock_without_a_terminal_keeps_the_locks(self) -> None:
        self.write_lock("web1", os.getpid())
        r = subprocess.run(
            [*CLI, "unlock", "web1"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            start_new_session=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not removed", r.stdout)
        self.assertEqual(self.held_on_web1(), ["R1"])

    def test_a_dead_runners_lock_holds_while_its_script_still_runs(self) -> None:
        from opsquire import locks
        from opsquire.model import host_name, iso, now, pid_namespace

        gate, started, changed = (self.tmp / n for n in ("gate", "started", "changed"))
        body = (
            "import os\nimport time\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            f"if sq.APPLY:\n    Path({str(started)!r}).write_text(str(os.getpid()))\n"
            f"    while not Path({str(gate)!r}).exists():\n        time.sleep(0.05)\n"
            f"    Path({str(changed)!r}).write_text(str(sq.lock_token('web1')))\n"
        )
        folder = self.new("orphaned", body, *self.CHANGING)
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        runner = subprocess.Popen(
            [*CLI, "run", folder.name, "--apply"],
            env=self.env,
            stdin=self.terminal(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def stop() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(runner.pid, signal.SIGKILL)

        self.addCleanup(stop)
        deadline = time.monotonic() + 60
        while not started.exists() and runner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(started.exists(), "the script started")
        script = int(started.read_text())
        os.kill(runner.pid, signal.SIGKILL)
        runner.wait(timeout=10)
        self.assertTrue(pid_alive(script))
        me = locks.Holder("R2", "other", os.getpid(), host_name(), "exclusive", iso(now()), pid_namespace())
        with mock.patch.dict(os.environ, self.env):
            conflicts, stale, _, _ = locks.acquire(["web1"], me, break_lock=False)
            self.assertEqual((len(conflicts), stale), (1, []), "the script can still change web1")
            self.assertIn(f"its runner is gone but its script still runs as pid {script}", conflicts[0].described())
            self.assertEqual(self.last(folder.name)["status"], "running")
            gate.touch()
            deadline = time.monotonic() + 30
            while pid_alive(script) and time.monotonic() < deadline:
                time.sleep(0.05)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(script, os.WNOHANG)
            conflicts, stale, _, _ = locks.acquire(["web1"], me, break_lock=False)
            self.assertEqual((conflicts, [h.task_id for h in stale]), ([], [folder.name]), "reclaimed once it ended")
            locks.release(["web1"], "R2")
        self.assertEqual(changed.read_text(), "2")
        self.assertEqual(self.last(folder.name)["status"], "interrupted")

    def test_shared_locks_coexist_but_block_an_apply(self) -> None:
        self.new("reader", "print('read')\n")
        self.write_lock("web1", os.getpid(), mode="shared")
        self.assertEqual(self.cli("run", "reader").returncode, 0)
        self.new("writer", "print('w')\n", "--changes-prod", "--rollback", "none needed")
        self.assertEqual(self.cli("run", "writer").returncode, 0, "a dry run shares")
        self.assertEqual(self.cli("run", "writer", "--apply", "--no-wait").returncode, 2)
        self.assertEqual(self.last("writer")["lock"]["mode"], "exclusive")

    def test_a_read_holds_its_targets_only_to_show_and_gets_no_token(self) -> None:
        self.new("look", "print('read')\n")
        self.write_lock("web1", os.getpid(), mode="shared")
        self.assertEqual(self.cli("run", "look", "--no-wait").returncode, 0)
        lock = self.last("look")["lock"]
        self.assertEqual((lock["mode"], lock["tokens"]), ("read", {}))

    def test_a_read_waits_for_a_change_and_nothing_waits_for_a_read(self) -> None:
        from opsquire import locks

        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(locks.acquire(["web1"], self.holder("R_read", locks.READ), break_lock=False)[0], [])
            self.assertEqual(locks.acquire(["web1"], self.holder("A_apply"), break_lock=False)[0], [], "no wait")
            for asking in (self.holder("R2_read", locks.READ), self.holder("D_dry", "shared")):
                conflicts = locks.acquire(["web1"], asking, break_lock=False)[0]
                self.assertEqual([h.run_id for h in conflicts], ["A_apply"], asking.run_id)

    def test_waiters_keep_their_place_in_line(self) -> None:
        from opsquire import locks
        from opsquire.model import locks_dir

        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(locks.acquire(["web1"], self.holder("X", "shared"), break_lock=False)[0], [])
            conflicts = locks.acquire(["web1"], self.holder("A"), break_lock=False, wait=True)[0]
            self.assertEqual([h.run_id for h in conflicts], ["X"])
            conflicts = locks.acquire(["web1/nginx"], self.holder("B", "shared"), break_lock=False, wait=True)[0]
            self.assertEqual([h.run_id for h in conflicts], ["A"], "no passing an exclusive run waiting ahead")
            self.assertEqual(locks.acquire(["web2"], self.holder("C"), break_lock=False)[0], [], "web2 is free")
            locks.release(["web1"], "X")
            self.assertEqual(locks.acquire(["web1"], self.holder("A"), break_lock=False, wait=True)[0], [])
            queue = locks_dir() / locks.QUEUE_FILE
            self.assertEqual([w["holder"]["run_id"] for w in json.loads(queue.read_text())], ["B"])
            locks.leave("B")
            self.assertEqual(json.loads(queue.read_text()), [])

    def test_a_read_that_a_change_overlapped_says_so(self) -> None:
        from opsquire import locks

        gate, started = self.tmp / "gate", self.tmp / "started"
        body = (
            f"import time\nfrom pathlib import Path\n\nPath({str(started)!r}).write_text('1')\n"
            f"while not Path({str(gate)!r}).exists():\n    time.sleep(0.05)\nprint('read')\n"
        )
        self.new("look", body)
        p = self.start("run", "look")
        for _ in range(400):
            if started.exists():
                break
            time.sleep(0.05)
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(locks.acquire(["web1/nginx"], self.holder("A_apply"), break_lock=False)[0], [])
            locks.release(["web1/nginx"], "A_apply")
        gate.write_text("go")
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("a change started on web1 while this read ran", err)
        self.assertEqual(self.last("look")["lock"]["changed_during"], ["web1"])

    def wait_in_line(self, name: str, *args: str) -> tuple[subprocess.Popen[str], str]:
        """Start a run of `name` behind a lock held by a live process, wait until it waits, then end the holder."""
        holder = subprocess.Popen(["sleep", "60"])
        try:
            self.write_lock("web1", holder.pid)
            p = self.start("run", name, *args)
            for _ in range(400):
                with contextlib.suppress(Exception):
                    if "waiting_for" in self.last(name)["lock"]:
                        break
                time.sleep(0.05)
            waiting = str(self.last(name)["lock"].get("waiting_for"))
        finally:
            holder.kill()
            holder.wait()
        _, err = p.communicate(timeout=30)
        return p, waiting + "\n" + err

    def test_a_blocked_run_waits_in_line_and_starts_when_the_lock_frees(self) -> None:
        self.new("writer", "print('w')\n", "--changes-prod", "--rollback", "none needed")
        p, said = self.wait_in_line("writer", "--apply", "--skip-dry", "test")
        self.assertEqual(p.returncode, 0, said)
        self.assertIn("other run R1 (exclusive", said)
        self.assertIn("waiting in line for other run R1", said)
        apply = next(r for r in json.loads(self.cli("runs", "writer", "--json").stdout) if r["mode"] == "apply")
        self.assertEqual((apply["lock"]["waited_for"], "waiting_for" in apply["lock"]), (["R1"], False))

    def test_an_apply_never_waits_for_another_apply(self) -> None:
        from opsquire import locks
        from opsquire.model import locks_dir

        self.new("writer", "print('w')\n", "--changes-prod", "--rollback", "none needed")
        self.assertEqual(self.cli("run", "writer").returncode, 0)
        self.write_lock("web1", os.getpid(), run_id="R1_apply")
        self.assertEqual(self.cli("run", "writer", "--apply").returncode, 2)
        self.assertIn("another apply is changing these targets", self.last("writer")["last_error"])
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual([h.run_id for h in locks.holders()["web1"]], ["R1_apply"])
            self.assertEqual(json.loads((locks_dir() / locks.QUEUE_FILE).read_text()), [])

    def test_fencing_tokens_only_go_up_per_target(self) -> None:
        from opsquire import locks

        dead = subprocess.Popen(["true"])
        dead.wait()
        self.write_lock("web1", os.getpid())
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual([h.run_id for h in locks.holders()["web1"]], ["R1"], "a file from before tokens reads")
            locks.release(["web1"], "R1")
            seen = [locks.acquire(["web1"], self.holder("A"), break_lock=False)[3]["web1"]]
            locks.release(["web1"], "A")
            seen.append(locks.acquire(["web1"], self.holder("B", pid=dead.pid), break_lock=False)[3]["web1"])
            _, stale, _, tokens = locks.acquire(["web1"], self.holder("C"), break_lock=False)
            self.assertEqual([h.run_id for h in stale], ["B"])
            seen.append(tokens["web1"])
            conflicts, _, _, none = locks.acquire(["web1"], self.holder("D"), break_lock=False)
            self.assertEqual(([h.run_id for h in conflicts], none), (["C"], {}))
            _, _, broken, tokens = locks.acquire(["web1"], self.holder("D"), break_lock=True)
            self.assertEqual([h.run_id for h in broken], ["C"])
            seen.append(tokens["web1"])
            self.assertEqual([h.token for h in locks.holders()["web1"]], [seen[-1]])
            locks.remove("web1")
            seen.append(locks.acquire(["web1"], self.holder("E"), break_lock=False)[3]["web1"])
            locks.release(["web1"], "E")
            self.assertEqual(seen, [1, 2, 3, 4, 5], "tokens survive release, reclaim, break and remove")
            part = locks.acquire(["web1/nginx"], self.holder("F"), break_lock=False)[3]["web1/nginx"]
            locks.release(["web1/nginx"], "F")
            whole = locks.acquire(["web1"], self.holder("G"), break_lock=False)[3]["web1"]
            locks.release(["web1"], "G")
            self.assertEqual((part, whole), (6, 7), "a part and its host share one order")
        state = json.loads((self.home / "var/locks/locks.json").read_text())
        self.assertEqual(state, {"holders": {}, "tokens": {"web1": 7, "web1/nginx": 6}})

    def test_nested_targets_lock_only_what_overlaps(self) -> None:
        from opsquire import locks
        from opsquire.state import same_target

        def blockers(targets: list[str], run_id: str, mode: str = "exclusive") -> list[str]:
            conflicts = locks.acquire(targets, self.holder(run_id, mode), break_lock=False)[0]
            return sorted({h.run_id for h in conflicts})

        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(blockers(["node1/worker:a"], "A"), [])
            self.assertEqual(blockers(["node1/worker:b"], "B"), [], "siblings run side by side")
            self.assertEqual(blockers(["node10"], "C"), [], "a longer name is another host")
            self.assertEqual(blockers(["node1"], "D"), ["A", "B"], "the host waits for its parts")
            self.assertEqual(blockers(["node1/worker:a/x"], "E", "shared"), ["A"])
            for run_id, targets in (("A", ["node1/worker:a"]), ("B", ["node1/worker:b"]), ("C", ["node10"])):
                locks.release(targets, run_id)
            self.assertEqual(blockers(["node1"], "H", "shared"), [])
            self.assertEqual(blockers(["node1/nginx"], "I", "shared"), [], "shared parent and shared part coexist")
            self.assertEqual(blockers(["node1/nginx"], "J"), ["H", "I"], "an exclusive part waits for a shared host")
            locks.release(["node1"], "H")
            self.assertEqual(blockers(["node1"], "K"), ["I"], "an exclusive host waits for a shared part")
            locks.release(["node1/nginx"], "I")
            self.assertEqual(blockers(["node1"], "L"), [])
            self.assertEqual(blockers(["node1/nginx"], "M", "shared"), ["L"], "a part waits for an exclusive host")
            locks.release(["node1"], "L")
        self.assertTrue(same_target("node_1/nginx", "node-1") and same_target("node1/worker:a", "node1/worker:b"))
        self.assertFalse(same_target("node10", "node1/nginx"))

    def test_targets_keep_old_names_and_accept_parts(self) -> None:
        from opsquire.model import TARGET_RE

        old = ["node1", "api-gateway", "queue_test", "eu1-197", "edge_cache", "db.internal", "claude-code-settings"]
        for name in [*old, "node1/nginx", "node1/worker:api", "db2/replica/wal.1"]:
            self.assertTrue(TARGET_RE.fullmatch(name), name)
        for name in ["Node1", "node1/", "/nginx", "node1//nginx", "job:a", "node1/:a", "node1/nginx\n", "a b", "../x"]:
            self.assertFalse(TARGET_RE.fullmatch(name), name)
        waiting = self.new("nginx", "print(1)\n", "--target", "node1/nginx", area="node1")
        self.assertEqual(self.cli("lint", waiting.name).returncode, 0)
        made = json.loads(self.cli("new", "node1", "host", "--purpose", "p", "--target", "node1", "--json").stdout)
        self.assertEqual([w["task_id"] for w in made["waiting_on_same_target"]], [waiting.name])
        self.new("bad", "print(1)\n", "--target", "node1/Nginx", area="node1")
        r = self.cli("lint", "bad")
        self.assertIn("a part of one after a /", r.stdout + r.stderr)

    def test_concurrent_processes_never_hold_overlapping_exclusive_locks(self) -> None:
        def overlaps(a: str, b: str) -> bool:
            return a == b or a.startswith(b + "/") or b.startswith(a + "/")

        journal = self.tmp / "journal"
        worker = textwrap.dedent(
            """
            import os, random, sys, time
            from opsquire import locks
            from opsquire.model import host_name, pid_namespace

            me, journal, held, deadline = sys.argv[1], sys.argv[2], 0, time.monotonic() + 150
            choices = ["node1", "node1/nginx", "node1/worker:a", "node1/worker:b"]
            while held < 12 and time.monotonic() < deadline:
                target = random.choice(choices)
                run_id = f"{me}-{held}"
                h = locks.Holder(run_id, "worker", os.getpid(), host_name(), "exclusive", "x", pid_namespace())
                conflicts, _, _, tokens = locks.acquire([target], h, break_lock=False)
                if conflicts:
                    time.sleep(random.random() / 200)
                    continue
                fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                os.write(fd, f"+ {target} {tokens[target]} {run_id}\\n".encode())
                time.sleep(random.random() / 200)
                os.write(fd, f"- {target} {tokens[target]} {run_id}\\n".encode())
                os.close(fd)
                locks.release([target], run_id)
                held += 1
            """
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", worker, f"w{i}", str(journal)], env=self.env) for i in range(6)
        ]
        for p in procs:
            self.addCleanup(p.kill)
        self.assertEqual([p.wait(timeout=180) for p in procs], [0] * 6)
        inside: dict[str, str] = {}
        highest: dict[str, int] = {}
        lines = journal.read_text().splitlines()
        for line in lines:
            sign, target, token, run_id = line.split()
            if sign == "-":
                del inside[run_id]
                continue
            self.assertEqual([t for t in inside.values() if overlaps(t, target)], [], line)
            self.assertGreater(int(token), max((n for t, n in highest.items() if overlaps(t, target)), default=0), line)
            inside[run_id], highest[target] = target, int(token)
        self.assertEqual(len(lines), 6 * 12 * 2, "every worker got its turns")

    def test_tokens_reach_the_scripts_and_the_records(self) -> None:
        body = (
            "import os\n\nimport opsquire_helpers as sq\n\n"
            'print("env", os.environ["OPSQUIRE_LOCK_TOKENS"])\n'
            "for name in sq.TARGETS:\n"
            '    print("token", name, sq.lock_token(name))\n'
            'print("elsewhere", sq.lock_token("elsewhere"))\n'
        )
        changing = ("--changes-prod", "--rollback", "none needed")
        task = self.new("tokens", body, "--target", "web1", "--target", "node1/worker:api", *changing)
        bash = (
            BASH_HEADER + 'echo "bash $(sq_lock_token web1)"\nif ! sq_lock_token nowhere; then echo "bash none"; fi\n'
        )
        bash_task = self.new("bashtokens", bash, *changing, lang="sh")
        for expected in (1, 2):
            r = self.cli("run", task.name)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            rec = self.last(task.name)
            self.assertEqual(rec["lock"]["tokens"], {"web1": expected, "node1/worker:api": expected})
            out = Path(rec["paths"]["output"]).read_text()
            self.assertIn(f"env web1={expected} node1/worker:api={expected}", out)
            self.assertIn(f"token node1/worker:api {expected}", out)
            self.assertIn("elsewhere None", out)
        self.assertEqual(self.cli("run", bash_task.name).returncode, 0)
        out = Path(self.last(bash_task.name)["paths"]["output"]).read_text()
        self.assertIn("bash 3", out)
        self.assertIn("bash none", out)
        plan = self.make_plan(task, bash_task)
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        tokens = self.plan_rec(plan)["lock"]["tokens"]
        self.assertEqual(tokens, {"node1/worker:api": 3, "web1": 4})
        self.assertEqual(self.last(task.name)["lock"]["tokens"], tokens, "a plan's steps carry the plan's tokens")
        self.assertEqual(self.last(bash_task.name)["lock"]["tokens"], {"web1": 4})
        self.assertIn("bash 4", Path(self.last(bash_task.name)["paths"]["output"]).read_text())

    def test_liveness_from_another_pid_namespace_uses_the_heartbeat(self) -> None:
        self.assertEqual(self.fake_running("pid:[1]", 2), "running")

    def test_liveness_stale_heartbeat_means_dead(self) -> None:
        self.assertEqual(self.fake_running("pid:[1]", 120), "interrupted")

    def test_liveness_same_namespace_trusts_the_kernel(self) -> None:
        self.assertEqual(self.fake_running(self.own_ns(), 2), "interrupted")

    def test_live_run_beats_and_locks_follow_it(self) -> None:
        body = "import time\n\nimport opsquire_helpers as sq\n\n\ndef main() -> None:\n"
        body += "    print('start', flush=True)\n    time.sleep(120)\n\n\nsq.run(main)\n"
        self.new("beating", body)
        p = self.start("run", "beating")
        assert p.stdout
        p.stdout.readline()
        beat = next((self.home / "var/runs").rglob("heartbeat"))
        first = beat.stat().st_mtime
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and beat.stat().st_mtime == first:
            time.sleep(0.2)
        self.assertGreater(beat.stat().st_mtime, first)
        held = json.loads(self.cli("locks", "--json").stdout)
        self.assertTrue(held["web1"][0]["alive"])
        self.assertEqual(held["web1"][0]["pid_ns"], self.own_ns())
        os.killpg(p.pid, signal.SIGINT)
        p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130)
        self.assertEqual(json.loads(self.cli("locks", "--json").stdout), {})

    def test_another_operators_live_run_holds_its_lock_and_a_dead_one_is_reclaimed(self) -> None:
        import dataclasses

        from opsquire import locks
        from opsquire.model import host_name, pid_namespace, run_alive, user_name

        host, other = host_name(), os.geteuid() + 1
        alice = locks.Holder("RA", "other", 4242, host, "exclusive", "x", pid_namespace(), user="alice", uid=other)

        def mine(run_id: str) -> Any:
            return dataclasses.replace(self.holder(run_id), user=user_name(), uid=os.geteuid())

        with mock.patch.dict(os.environ, self.env):
            with mock.patch("os.kill", side_effect=PermissionError):
                self.assertTrue(run_alive(4242, host, self.tmp, pid_namespace()))
                locks.acquire(["web1"], alice, break_lock=False)
                conflicts, stale, _, _ = locks.acquire(["web1"], mine("B1"), break_lock=False)
                self.assertEqual(([h.user for h in conflicts], stale), (["alice"], []))
                _, _, broken, _ = locks.acquire(["web1"], mine("B2"), break_lock=True)
                self.assertEqual([h.run_id for h in broken], ["RA"])
                locks.release(["web1"], "B2")
                locks.acquire(["web1"], alice, break_lock=False)
            with mock.patch("os.kill", side_effect=ProcessLookupError):
                self.assertFalse(run_alive(4242, host, self.tmp, pid_namespace()))
                conflicts, stale, _, _ = locks.acquire(["web1"], mine("B3"), break_lock=False)
                self.assertEqual((conflicts, [h.user for h in stale]), ([], ["alice"]))
                locks.release(["web1"], "B3")
                beat = self.tmp / "heartbeat"
                beat.write_text("now\n")
                self.assertTrue(run_alive(4242, host, self.tmp, "pid:[1]"), "another user's heartbeat counts")
                os.utime(beat, (time.time() - 120, time.time() - 120))
                self.assertFalse(run_alive(4242, host, self.tmp, "pid:[1]"))
        lines = (self.home / "var/ledger/entries.jsonl").read_text().splitlines()
        events = {e["data"]["event"]: e["data"] for e in map(json.loads, lines) if e["kind"] == "lock"}
        for kind in ("break", "reclaim"):
            self.assertEqual(events[kind]["holder"]["user"], "alice", kind)
            self.assertEqual((events[kind]["by"]["user"], events[kind]["by"]["uid"]), (user_name(), os.geteuid()))

    @unittest.skipIf(sys.platform == "darwin", "process states come from /proc")
    def test_a_zombie_script_is_not_still_running(self) -> None:
        from opsquire.model import host_name, left_running, pid_namespace, proc_start, run_alive

        def state(pid: int) -> str:
            return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]

        gone = subprocess.Popen(["true"])
        gone.wait()
        zombie = subprocess.Popen(["true"])
        self.addCleanup(zombie.wait)
        live = subprocess.Popen(["sleep", "30"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        deadline = time.monotonic() + 10
        while state(zombie.pid) != "Z" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(state(zombie.pid), "Z")
        for child, running in ((zombie, []), (live, [live.pid])):
            (self.tmp / "script.json").write_text(json.dumps({"pid": child.pid, "start": proc_start(child.pid)}))
            self.assertEqual(left_running(self.tmp), running)
            self.assertEqual(run_alive(gone.pid, host_name(), self.tmp, pid_namespace()), bool(running))

    def test_a_lock_names_the_operator_whose_run_holds_it(self) -> None:
        self.new("locked", "print('ran')\n", "--changes-prod", "--rollback", "none needed")
        self.write_lock("web1", os.getpid())
        state = json.loads((self.home / "var/locks/locks.json").read_text())
        state["web1"][0]["user"] = "alice"
        (self.home / "var/locks/locks.json").write_text(json.dumps(state))
        self.assertEqual(self.cli("run", "locked", "--no-wait").returncode, 2)
        self.assertIn("targets locked by other run R1 (exclusive, alice, since x)", self.last("locked")["last_error"])
        table = self.cli("locks").stdout.splitlines()
        self.assertEqual(table[0].split(), ["TARGET", "MODE", "TOKEN", "RUN", "TASK", "USER", "SINCE", "ALIVE"])
        self.assertEqual(table[1].split()[:6], ["web1", "exclusive", "-", "R1", "other", "alice"])
        with mock.patch.dict(os.environ, self.env):
            from opsquire.state import state as home_state

            self.assertEqual([h["user"] for h in home_state()["locks"]], ["alice"])
