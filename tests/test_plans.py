"""Plans, schedules, staged tasks and dry run, then apply."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

from support import CLI, OpsTestCase, free_port


class PlanTest(OpsTestCase):
    def test_plan_dry_then_apply_runs_steps_in_order_under_one_lock(self) -> None:
        check = self.new("check", "print('checked')\n")
        change = self.new("change", self.CONFIRM_BODY, "--changes-prod", "--rollback", "undo")
        verify = self.new("verify", "print('verified')\n")
        plan = self.make_plan(check, change, verify, extra={"2": {"verify": True}})
        r = self.cli("run", plan.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual((rec["status"], rec["mode"]), ("ok", "dry"))
        self.assertEqual(
            [(s["status"], s["mode"]) for s in rec["steps"]], [("ok", "read"), ("ok", "dry"), ("ok", "read")]
        )
        self.assertIn("verify step", rec["warnings"][0])
        step_rec = self.last("change")
        self.assertEqual((step_rec["plan_run"], step_rec["lock"]["held_by_plan_run"]), (rec["run_id"], rec["run_id"]))

        r = self.cli("run", plan.name, "--apply", answers=[("[y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout)
        rec = self.plan_rec(plan)
        self.assertEqual([s["mode"] for s in rec["steps"]], ["read", "apply", "read"])
        self.assertEqual(rec["warnings"], [])
        self.assertEqual(json.loads(self.cli("show", plan.name, "--json").stdout)["status"], "done")
        self.assertEqual(json.loads(self.cli("ls", "--kind", "plan", "--json").stdout)[0]["id"], plan.name)

    def test_plan_stops_at_failure_and_runs_rollback_only_in_apply(self) -> None:
        marker = self.tmp / "rolled-back"
        ok = self.new("prep", "print(1)\n")
        bad = self.new("bad", "import sys\n\nsys.exit(1)\n", "--changes-prod", "--rollback", "undo")
        after = self.new("after", "print(1)\n")
        undo = self.new("undo", f"from pathlib import Path\n\nPath({str(marker)!r}).touch()\n")
        plan = self.make_plan(ok, bad, after, extra={"1": {"on_failure": undo.name}})
        self.assertEqual(self.cli("run", plan.name).returncode, 1)
        self.assertFalse(marker.exists())
        r = self.cli("run", plan.name, "--apply")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("no pre-run covers this apply", self.plan_rec(plan)["last_error"])
        self.assertFalse(marker.exists(), "a refused plan runs no step and no rollback")
        r = self.cli("run", plan.name, "--apply", "--skip-dry", "its dry run fails the same way")
        self.assertEqual(r.returncode, 1, r.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual(rec["status"], "failed")
        self.assertEqual([s["status"] for s in rec["steps"]], ["ok", "failed", "skipped"])
        self.assertEqual(rec["steps"][1]["rollback"]["status"], "ok")
        self.assertTrue(marker.exists())
        self.assertIn("step 2", rec["last_error"])

    def test_a_plans_lock_covers_its_rollback_targets(self) -> None:
        from opsquire import locks
        from opsquire.model import host_name, iso, now, pid_namespace

        marker = self.tmp / "rolled-back"
        bad = self.new("bad", "import sys\n\nsys.exit(1)\n", *self.CHANGING)
        undo = self.new(
            "undo", f"from pathlib import Path\n\nPath({str(marker)!r}).touch()\n", *self.CHANGING, "--target", "db9"
        )
        plan = self.make_plan(bad, extra={"0": {"on_failure": undo.name}})
        holder = locks.Holder("R1", "other", os.getpid(), host_name(), "exclusive", iso(now()), pid_namespace())
        with mock.patch.dict(os.environ, self.env):
            locks.acquire(["db9"], holder, break_lock=False)
            try:
                r = self.cli("run", plan.name, "--apply", "--no-wait", *self.NO_DRY)
            finally:
                locks.release(["db9"], holder.run_id)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("targets locked by other run R1", self.plan_rec(plan)["last_error"])
        self.assertFalse(marker.exists(), "the rollback never runs past another run's lock")
        self.assertEqual(self.cli("runs", bad.name, "--json").stdout.strip(), "[]", "the plan refused before step 1")
        r = self.cli("run", plan.name, "--apply", *self.NO_DRY)
        self.assertEqual(r.returncode, 1, r.stderr)
        rec, undone = self.plan_rec(plan), self.last(undo.name)
        self.assertTrue(marker.exists())
        self.assertEqual((rec["lock"]["mode"], rec["lock"]["targets"]), ("exclusive", ["db9", "web1"]))
        self.assertEqual((undone["lock"]["mode"], undone["lock"]["held_by_plan_run"]), ("exclusive", rec["run_id"]))

    def test_plan_abort_skips_the_rest(self) -> None:
        change = self.new("change", self.CONFIRM_BODY, "--changes-prod", "--rollback", "undo")
        verify = self.new("verify", "print(1)\n")
        plan = self.make_plan(change, verify)
        r = self.cli("run", plan.name, "--apply", *self.NO_DRY, answers=[("[y/N]", "n")])
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual([s["status"] for s in self.plan_rec(plan)["steps"]], ["aborted", "skipped"])

    def test_plan_blocks_before_any_step_on_lock_or_lint(self) -> None:
        first = self.new("first", "print(1)\n", *self.CHANGING)
        broken = self.new("broken", "def f(x):\n    return x\n", *self.CHANGING)
        plan = self.make_plan(first, broken)
        self.assertEqual(self.cli("run", plan.name).returncode, 2)
        rec = self.plan_rec(plan)
        self.assertIn("fails lint", rec["last_error"])
        self.assertEqual(self.cli("runs", "first", "--json").stdout.strip(), "[]")

        (broken / "main.py").write_text("print(1)\n")
        self.write_lock("web1", os.getpid())
        self.assertEqual(self.cli("run", plan.name, "--no-wait").returncode, 2)
        self.assertIn("locked by other", self.plan_rec(plan)["last_error"])
        self.assertEqual(self.cli("runs", "first", "--json").stdout.strip(), "[]")

    def test_plan_ctrl_c_mid_step_releases_locks(self) -> None:
        body = "import time\n\nimport opsquire_helpers as sq\n\n\ndef main() -> None:\n"
        body += "    print('start', flush=True)\n    time.sleep(30)\n\n\nsq.run(main)\n"
        slow = self.new("slow", body)
        later = self.new("later", "print(1)\n")
        plan = self.make_plan(slow, later)
        p = self.start("run", plan.name)
        assert p.stdout
        p.stdout.readline()
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGINT)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual([s["status"] for s in self.plan_rec(plan)["steps"]], ["interrupted", "skipped"])

    def test_plan_validation(self) -> None:
        a = self.new("a", "print(1)\n")
        plan = self.make_plan(a)
        meta = json.loads((plan / "meta.json").read_text())
        meta["targets"] = ["elsewhere"]
        meta["steps"].append({"task": plan.name})
        (plan / "meta.json").write_text(json.dumps(meta))
        errors = " ".join(json.loads(self.cli("show", plan.name, "--json").stdout)["meta_errors"])
        self.assertIn("plans do not nest", errors)
        self.assertEqual(self.cli("new", "web1", "empty", "--plan", "--purpose", "p").returncode, 2)
        self.assertIn("no arguments", self.cli("run", plan.name, "--", "x").stderr)

    def test_staged_apply_stays_open_until_its_verify_check_passes(self) -> None:
        body = (
            "import os\nimport sys\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            "stage = sys.argv[1] if len(sys.argv) > 1 else 'build'\n"
            "if sq.APPLY and stage == 'deploy':\n    Path(os.environ['STAGED_LIVE']).write_text('v2')\n"
            "sq.target('web1', 'ok', stage)\n"
        )
        self.env["STAGED_LIVE"] = str(self.tmp / "staged-live")
        folder = self.new("staged", body, "--changes-prod", "--rollback", "undo")
        tid = folder.name
        (folder / "verify.py").write_text(
            "import os\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            "live = Path(os.environ['STAGED_LIVE']).is_file()\n"
            "sq.target('web1', 'ok' if live else 'failed', 'v2 live' if live else 'still v1')\n"
        )
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        for stage in ("build", "deploy"):
            self.assertEqual(self.cli("run", tid, "--", stage).returncode, 0)
        r = self.cli("run", tid, "--apply", "--", "build", answers=[("", "y")])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("running the verify check", r.stdout)
        self.assertEqual((self.last(tid)["mode"], self.last(tid)["targets"]["web1"]["status"]), ("verify", "failed"))
        self.assertIsNone(json.loads((folder / "meta.json").read_text()).get("outcome"))
        r = self.cli("run", tid, "--apply", "--", "deploy", answers=[("", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("closed done", r.stdout)
        outcome = json.loads((folder / "meta.json").read_text())["outcome"]
        self.assertIn("verify run", outcome["note"])

    def test_inbox_nests_the_waiting_steps_under_their_plan(self) -> None:
        a = self.new("stop-workers", self.CONFIRM_BODY, "--changes-prod", "--rollback", "start them", "--no-verify")
        b = self.new("start-workers", self.CONFIRM_BODY, "--changes-prod", "--rollback", "stop them", "--no-verify")
        plan = self.make_plan(a, b, name="cutover")
        for name in ("stop-workers", "start-workers", "cutover"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.hand_over("cutover")
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([i["task_id"] for i in inbox], [plan.name])
        self.assertEqual(inbox[0]["next"], "dry", "the group owes its pre-run")
        steps = [(s["task_id"], s["state"], s["locked_in"], s["next"]) for s in inbox[0]["steps"]]
        self.assertEqual(steps, [(a.name, "ready", plan.name, None), (b.name, "ready", plan.name, None)])
        self.assertEqual(self.cli("run", "cutover").returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([(i["task_id"], i["next"]) for i in inbox], [(plan.name, "apply")])
        self.assertEqual([s["state"] for s in inbox[0]["steps"]], ["pre-ran", "pre-ran"])
        self.assertEqual(inbox[0]["schedule"]["prerun"]["run_id"], self.plan_rec(plan)["run_id"])
        self.assertEqual(self.cli("run", a.name).returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([i["task_id"] for i in inbox], [plan.name], "a step run alone stays in its schedule")
        tasks = json.loads(self.cli("threads", "--json").stdout)["tasks"]
        self.assertEqual((tasks[a.name]["in_schedule"], tasks[plan.name]["in_schedule"]), (plan.name, None))
        self.assertEqual(self.cli("close", "cutover", "--outcome", "abandoned", "--note", "by hand").returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual(sorted((i["task_id"], i["steps"]) for i in inbox), sorted([(a.name, []), (b.name, [])]))

    def test_schedule_resumes_after_the_steps_a_stopped_apply_finished(self) -> None:
        log, flag = self.tmp / "log", self.tmp / "flag"
        a = self.new("one", self.append_body(log, "a"), "--changes-prod", "--rollback", "x", "--no-verify")
        b = self.new("two", self.append_body(log, "b", flag), "--changes-prod", "--rollback", "x", "--no-verify")
        c = self.new("three", self.append_body(log, "c"), "--changes-prod", "--rollback", "x", "--no-verify")
        plan = self.make_plan(a, b, c, name="chain")
        self.assertEqual(self.cli("lint", plan.name).returncode, 0)
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        flag.touch()
        self.assertEqual(self.cli("run", plan.name, "--apply").returncode, 1)
        self.assertEqual(log.read_text(), "a ")
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([i["task_id"] for i in item], [plan.name], "the stopped schedule keeps its steps")
        sched = item[0]["schedule"]
        self.assertEqual(
            (item[0]["next"], sched["resume_at"], sched["done"], sched["stopped"]["at"]), ("dry", 2, [1], 2)
        )
        self.assertEqual([s["state"] for s in item[0]["steps"]], ["done", "stopped", "ready"])
        self.assertIn("flag set", sched["stopped"]["reason"], "the stop names the step's own error")
        table = self.cli("inbox")
        self.assertEqual(table.returncode, 0, table.stderr)
        self.assertIn("runs in the schedule", table.stdout)
        r = self.cli("run", plan.name, "--apply")
        self.assertEqual(r.returncode, 2, "a stopped apply used its pre-run up")
        flag.unlink()
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        pre = self.plan_rec(plan)
        self.assertEqual(pre["resumed_after"], [1])
        self.assertEqual([s.get("done_in") is not None for s in pre["steps"]], [True, False, False])
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual((item["next"], item["schedule"]["resume_at"]), ("apply", 2))
        self.assertEqual(self.cli("run", plan.name, "--apply").returncode, 0)
        self.assertEqual(log.read_text(), "a b c ", "step 1 did not run twice")
        statuses = json.loads(self.cli("threads", "--json").stdout)["tasks"]
        closed = [(statuses[t.name]["outcome"] or {}).get("state") for t in (a, b, c)]
        self.assertEqual(closed, ["done", "done", "done"], "a step done before the stop closes with the schedule too")
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        self.assertEqual(self.cli("run", plan.name, "--from-start").returncode, 0)
        self.assertEqual(self.plan_rec(plan)["resumed_after"], None)

    def test_schedule_apply_runs_the_code_its_pre_run_saw(self) -> None:
        log = self.tmp / "log"
        a = self.new("deploy", self.append_body(log, "v1"), "--changes-prod", "--rollback", "x", "--no-verify")
        b = self.new("start", self.append_body(log, "s"), "--changes-prod", "--rollback", "x", "--no-verify")
        plan = self.make_plan(a, b, name="frozen")
        lint = self.cli("lint", plan.name)
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        pre = self.plan_rec(plan)["run_id"]
        (a / "main.py").write_text(self.append_body(log, "v2"))
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual(item["next"], "apply")
        self.assertEqual(item["schedule"]["changed_since_prerun"], [str((a / "main.py").relative_to(self.home))])
        self.assertEqual(self.cli("run", plan.name, "--apply").returncode, 0)
        self.assertEqual(log.read_text(), "v1 s ", "the edit after the pre-run did not reach the apply")
        rec = self.plan_rec(plan)
        self.assertEqual((rec["prerun"], [s.get("frozen") is not None for s in rec["steps"]]), (pre, [True, True]))
        self.assertIn("changed since and wait for the next pre-run", rec["warnings"][0])
        self.assertEqual(self.last(a.name)["frozen"]["run_id"], pre and self.last(a.name)["frozen"]["run_id"])

    def test_a_frozen_plan_locks_what_its_pre_run_declared(self) -> None:
        from opsquire import locks
        from opsquire.model import host_name, iso, now, pid_namespace

        marker = self.tmp / "applied"
        body = (
            "from pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            f"if sq.APPLY:\n    Path({str(marker)!r}).touch()\n"
        )
        folder = self.new("locked-step", body, *self.CHANGING)
        plan = self.make_plan(folder, name="frozen-locks")
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        meta = json.loads((folder / "meta.json").read_text())
        (folder / "meta.json").write_text(json.dumps(meta | {"lock": "none", "lock_reason": "a later edit"}))
        holder = locks.Holder("R1", "other", os.getpid(), host_name(), "exclusive", iso(now()), pid_namespace())
        with mock.patch.dict(os.environ, self.env):
            locks.acquire(["web1"], holder, break_lock=False)
            try:
                r = self.cli("run", plan.name, "--apply", "--no-wait")
            finally:
                locks.release(["web1"], holder.run_id)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("targets locked by other run R1", r.stderr)
        self.assertFalse(marker.exists(), "the pre-run's exclusive step never runs past another lock")
        rec = self.plan_rec(plan)
        self.assertEqual((rec["lock"]["mode"], rec["lock"]["targets"]), ("exclusive", ["web1"]))
        self.assertEqual(self.cli("run", plan.name, "--apply").returncode, 0)
        rec, step = self.plan_rec(plan), self.last(folder.name)
        self.assertTrue(marker.exists())
        self.assertEqual((rec["lock"]["mode"], step["lock"]["mode"]), ("exclusive", "exclusive"))
        self.assertEqual(step["lock"]["held_by_plan_run"], rec["run_id"])

    def test_a_killed_schedule_keeps_the_steps_it_finished_and_names_the_one_it_was_in(self) -> None:
        from opsquire import schedule
        from opsquire.model import load_task
        from opsquire.query import runs_for

        count, entered = self.tmp / "count", self.tmp / "entered"
        first_body = (
            f"from pathlib import Path\n\nimport opsquire_helpers as sq\n\ncount = Path({str(count)!r})\n"
            "if sq.APPLY:\n    count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')\n"
        )
        second_body = (
            f"import time\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            f"if sq.APPLY:\n    Path({str(entered)!r}).touch()\n    time.sleep(60)\n"
        )
        first = self.new("first", first_body, *self.CHANGING)
        second = self.new("second", second_body, *self.CHANGING)
        plan = self.make_plan(first, second, name="crash-resume")
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        proc = subprocess.Popen(
            [*CLI, "run", plan.name, "--apply"],
            env=self.env,
            stdin=self.terminal(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 60
            while not entered.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(entered.exists(), "step 2 never started")
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        killed = self.plan_rec(plan)
        self.assertEqual([(s["task"], s["status"]) for s in killed["steps"]], [(first.name, "ok")])
        with mock.patch.dict(os.environ, self.env):
            task = load_task(plan)
            unwritten = [dict(killed, steps=[], current=1)]
            self.assertEqual(list(schedule.done_steps(task, unwritten)), [0], "step 1's own run ended ok")
            self.assertIsNone(schedule.recovered(task, unwritten)[1])
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual((item["schedule"]["done"], item["schedule"]["stopped"]["at"]), ([1], 2))
        self.assertIn("step 2", item["schedule"]["stopped"]["reason"])
        self.assertIn("interrupted mid-run last time", item["schedule"]["stopped"]["reason"])
        self.assertEqual([s["state"] for s in item["steps"]], ["done", "stopped"])
        self.assertEqual(self.cli("run", plan.name, "--apply").returncode, 2, "the kill used the pre-run up")
        (second / "main.py").write_text("print('retry second')\n")
        pre = self.cli("run", plan.name)
        self.assertEqual(pre.returncode, 0, pre.stderr)
        self.assertIn(f"step 2 ({second.name}) was interrupted mid-run last time", pre.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual((rec["resumed_after"], rec["interrupted_step"]["i"]), ([1], 2))
        applied = self.cli("run", plan.name, "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("was interrupted mid-run last time", applied.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual((rec["status"], rec["resumed_after"], rec["interrupted_step"]["i"]), ("ok", [1], 2))
        self.assertEqual(count.read_text(), "1", "step 1 did not run twice")
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(schedule.done_steps(load_task(plan), runs_for(plan.name)), {})

    def test_schedule_runs_each_steps_verify_check_inside_the_apply(self) -> None:
        log = self.tmp / "log"
        a = self.new("deploy", self.append_body(log, "a"), "--changes-prod", "--rollback", "x")
        (a / "verify.py").write_text(
            f"from pathlib import Path\n\nassert Path({str(log)!r}).read_text().startswith('a ')\n"
        )
        b = self.new("start", self.append_body(log, "b"), "--changes-prod", "--rollback", "x", "--no-verify")
        plan = self.make_plan(a, b, name="checked")
        self.assertEqual(self.cli("lint", plan.name).returncode, 0)
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        self.assertEqual(self.cli("run", plan.name, "--apply").returncode, 0)
        rec = self.plan_rec(plan)
        self.assertEqual(rec["steps"][0]["verify"]["status"], "ok")
        self.assertEqual(
            [r["mode"] for r in json.loads(self.cli("runs", a.name, "--json").stdout)], ["dry", "apply", "verify"]
        )
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [], "nothing left for the group")
        self.assertEqual(self.cli("run", plan.name, "--verify").returncode, 0)
        self.assertEqual(self.plan_rec(plan)["mode"], "verify")

    def test_a_running_schedule_carries_what_its_bar_shows(self) -> None:
        gate = self.tmp / "gate"
        quick = self.new("quick", "print(1)\n", "--changes-prod", "--rollback", "x", "--no-verify")
        slow = self.new(
            "slow",
            "import time\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            "sq.progress('hosts', 3, 8)\n"
            f"while sq.APPLY and not Path({str(gate)!r}).exists():\n    time.sleep(0.1)\n",
            "--changes-prod",
            "--rollback",
            "x",
            "--no-verify",
        )
        plan = self.make_plan(quick, slow, name="bar")
        self.assertEqual(self.cli("lint", plan.name).returncode, 0)
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        p = self.start("run", plan.name, "--apply")
        try:
            deadline, view = time.time() + 60, None
            while time.time() < deadline:
                state = json.loads(self.cli("threads", "--json").stdout)
                running = [state["runs"][r] for r in state["running"] if state["runs"][r]["task_id"] == plan.name]
                if running and running[0].get("live", {}).get("now", {}) and running[0]["live"]["now"].get("progress"):
                    view = running[0]
                    break
                time.sleep(0.3)
            self.assertIsNotNone(view, "the running plan never showed its step's progress")
            assert view
            self.assertEqual((view["total"], view["current"]), (2, 2))
            self.assertEqual([s["mode"] for s in view["live"]["steps"]], ["apply", "apply"])
            self.assertIsNone(view["live"]["steps"][0]["expect_s"], "no ok apply of it before this run")
            self.assertEqual(view["live"]["now"]["progress"]["done"], 3)
            self.assertEqual(view["live"]["now"]["progress"]["total"], 8)
        finally:
            gate.touch()
            p.communicate(timeout=60)
        self.assertEqual(self.plan_rec(plan)["status"], "ok")

    def test_ui_links_inbox_items_into_one_schedule(self) -> None:
        a = self.new("stop", "print(1)\n", "--changes-prod", "--rollback", "x", "--no-verify", "--thread", "move").name
        b = self.new("start", "print(1)\n", "--changes-prod", "--rollback", "x", "--no-verify", "--thread", "move").name
        for tid in (a, b):
            self.assertEqual(self.cli("lint", tid).returncode, 0)
            self.hand_over(tid)
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 120)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                return json.loads(conn.getresponse().read())

            self.assertIn("error", call("POST", "/api/schedule", {"tasks": [a]}))
            made = call("POST", "/api/schedule", {"tasks": [b, a], "purpose": "swap"})
            inbox = call("GET", "/api/state")["inbox"]
            self.assertEqual([(i["task_id"], i["next"]) for i in inbox], [(made["id"], "dry")])
            self.assertEqual([s["task_id"] for s in inbox[0]["steps"]], [b, a], "in the order linked")
            meta = json.loads(next(self.home.glob(f"tasks/*/{made['id']}/meta.json")).read_text())
            self.assertEqual((meta["kind"], meta["thread"], meta["purpose"]), ("plan", "move", "swap"))
            self.assertIn("error", call("POST", "/api/schedule", {"tasks": [made["id"], a]}))
            conn.close()
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)

    def test_a_plan_is_refused_whole_when_a_step_has_no_dry_run(self) -> None:
        marker = self.tmp / "applied"
        body = (
            "from pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            f"if sq.APPLY:\n    Path({str(marker)!r}).touch()\n"
        )
        first = self.new("first", body, "--changes-prod", "--rollback", "undo", "--no-verify")
        second = self.new("second", "print(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify")
        plan = self.make_plan(first, second, name="both")
        self.hand_over(plan.name)
        self.assertEqual(self.cli("run", first.name).returncode, 0)
        r = self.cli("run", plan.name, "--apply")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("no pre-run covers this apply: no dry run of the plan since its last apply", rec["last_error"])
        self.assertIn(f"(step {first.name} onward)", rec["last_error"])
        self.assertFalse(marker.exists(), "a step's own dry run does not cover it: only the plan's pre-run does")
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual((item["task_id"], item["schedule"]["blocked"]["mode"]), (plan.name, "apply"))
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        self.assertEqual(self.cli("run", plan.name, "--apply").returncode, 0)
        self.assertTrue(marker.exists())

    def test_a_staged_task_walks_its_stages_from_the_inbox_and_checks_only_after_the_last(self) -> None:
        head = "import sys\n\nimport opsquire_helpers as sq\n\n"
        folder = self.new(
            "staged",
            head + "stage = sys.argv[1] if len(sys.argv) > 1 else 'build'\nsq.target('web1', 'ok', stage)\n",
            "--changes-prod",
            "--rollback",
            "undo",
        )
        (folder / "verify.py").write_text("import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'live')\n")
        meta = json.loads((folder / "meta.json").read_text())
        meta["stages"] = ["build", "build"]
        (folder / "meta.json").write_text(json.dumps(meta))
        self.assertNotEqual(self.cli("lint", "staged").returncode, 0, "stages must be distinct")
        meta["stages"] = ["build", "deploy"]
        (folder / "meta.json").write_text(json.dumps(meta))
        r = self.cli("lint", "staged")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.hand_over("staged", "--apply")

        def offered() -> tuple[str, str]:
            item = self.inbox_item(folder.name)
            assert item is not None
            return str(item["next"]), str(item["args"])

        self.assertEqual(offered(), ("apply", ""), "one item runs every stage")
        r = self.cli("run", "staged", "--apply", "--", "build")
        self.assertEqual(r.returncode, 2, "one stage alone still needs its own dry run")
        self.assertEqual(self.cli("run", "staged", "--", "build").returncode, 0)
        r = self.cli("run", "staged", "--apply", "--", "build")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("stage build applied; `opsquire run", r.stdout)
        self.assertIsNone(json.loads((folder / "meta.json").read_text()).get("outcome"))
        self.assertEqual(offered(), ("apply", ""))
        r = self.cli("run", "staged", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("stage 2/2 deploy: dry run", r.stdout)
        self.assertNotIn("stage 1/2", r.stdout, "it carries on from the stage left")
        modes = [(x["mode"], x["argv"]) for x in json.loads(self.cli("runs", "staged", "--json").stdout)]
        self.assertEqual(
            modes[-5:],
            [("dry", ["build"]), ("apply", ["build"]), ("dry", ["deploy"]), ("apply", ["deploy"]), ("verify", [])],
        )
        self.assertEqual(json.loads((folder / "meta.json").read_text())["outcome"]["state"], "done")
        self.assertIsNone(self.inbox_item(folder.name))

    def test_a_staged_task_with_stages_left_keeps_its_walk_in_the_inbox_after_a_verify(self) -> None:
        from opsquire.model import load_task
        from opsquire.state import next_mode

        body = "import sys\n\nimport opsquire_helpers as sq\n\nsq.target('web1', 'ok', sys.argv[1])\n"
        stages = ("--stage", "install", "--stage", "reader", "--stage", "service")
        folder = self.new("walk", body, "--changes-prod", "--rollback", "undo", *stages)
        (folder / "verify.py").write_text("import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'live')\n")
        self.assertEqual(self.cli("lint", folder.name).returncode, 0)
        self.enterContext(mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home)}))
        task = load_task(folder)
        files = task.file_hashes()

        def run(mode: str, status: str, *argv: str, changed: bool = False) -> dict[str, Any]:
            return {"mode": mode, "status": status, "argv": list(argv), "files": {} if changed else files}

        installed = [run("dry", "ok", "install"), run("apply", "ok", "install"), run("dry", "ok", "reader")]
        interrupted = [*installed, run("apply", "interrupted", "reader")]
        self.assertEqual(next_mode(task, interrupted, "ready"), "dry", "an interrupted stage walks again")
        early = [*interrupted, run("verify", "failed")]
        self.assertEqual(next_mode(task, early, "ready"), "dry", "a verify with stages left hides nothing")
        self.assertEqual(next_mode(task, [*early, run("verify", "failed")], "ready"), "dry")
        failed = [*installed, run("apply", "failed", "reader")]
        self.assertIsNone(next_mode(task, failed, "ready"), "a failed stage waits for its scripts to change")
        self.assertIsNone(next_mode(task, [*failed, run("verify", "failed")], "ready"))
        self.assertEqual(next_mode(task, [*installed, run("apply", "failed", "reader", changed=True)], "ready"), "dry")
        walked = [*installed, run("apply", "ok", "reader"), run("dry", "ok", "service"), run("apply", "ok", "service")]
        self.assertEqual(next_mode(task, walked, "ready"), "verify", "the check runs after the last stage")
        self.assertIsNone(next_mode(task, [*walked, run("verify", "failed")], "ready"), "a failed check needs a call")
        last_failed = [*walked[:-1], run("apply", "failed", "service", changed=True)]
        self.assertEqual(next_mode(task, last_failed, "ready"), "dry", "a failed last stage is no finished walk")
        self.assertIsNone(next_mode(task, [*installed, run("apply", "running", "reader")], "ready"))

    def test_a_run_in_progress_owes_nothing(self) -> None:
        from opsquire.model import load_task
        from opsquire.state import next_mode

        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'done')\n"
        folder = self.new("busy", body, "--changes-prod", "--rollback", "undo")
        (folder / "verify.py").write_text("import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'live')\n")
        self.assertEqual(self.cli("lint", folder.name).returncode, 0)
        self.enterContext(mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home)}))
        task = load_task(folder)
        files = task.file_hashes()

        def run(mode: str, status: str) -> dict[str, Any]:
            return {"mode": mode, "status": status, "argv": [], "files": files}

        dried = [run("dry", "ok")]
        self.assertIsNone(next_mode(task, [run("dry", "running")], "ready"), "a dry run in progress is not owed again")
        self.assertIsNone(next_mode(task, [*dried, run("apply", "running")], "ready"), "the apply runs its own check")
        self.assertEqual(next_mode(task, [*dried, run("apply", "interrupted")], "ready"), "verify")
        self.assertEqual(next_mode(task, [*dried, run("apply", "failed")], "ready"), "verify")

    def test_dry_first_runs_the_dry_run_then_the_apply_only_when_it_ended_ok(self) -> None:
        body = "import os\n\nimport opsquire_helpers as sq\n\nsq.target('web1', 'ok', os.environ['OPSQUIRE_MODE'])\n"
        folder = self.new("chained", body, "--changes-prod", "--rollback", "undo")
        self.assertEqual(self.cli("lint", folder.name).returncode, 0)
        refused = self.cli("run", folder.name, "--dry-first")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--dry-first goes with --apply alone", refused.stderr)

        r = self.cli("run", folder.name, "--apply", "--dry-first")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        runs = json.loads(self.cli("runs", folder.name, "--json").stdout)
        modes = sorted((x["mode"], x["status"]) for x in runs)
        self.assertEqual(modes, [("apply", "ok"), ("dry", "ok"), ("verify", "ok")], "and its verify check after")
        dry = next(x for x in runs if x["mode"] == "dry")
        apply = next(x for x in runs if x["mode"] == "apply")
        rec = json.loads((Path(apply["paths"]["dir"]) / "run.json").read_text())
        self.assertEqual(rec["dry"], {"run_id": dry["run_id"]}, "the apply is covered by that dry run")

        failing = (
            "import os\nimport sys\n\nif os.environ['OPSQUIRE_MODE'] == 'dry':\n    sys.exit('plan does not hold')\n"
        )
        bad = self.new("chained-bad", failing, "--changes-prod", "--rollback", "undo")
        self.assertEqual(self.cli("lint", bad.name).returncode, 0)
        r = self.cli("run", bad.name, "--apply", "--dry-first")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the apply did not run", r.stdout)
        self.assertEqual([x["mode"] for x in json.loads(self.cli("runs", bad.name, "--json").stdout)], ["dry"])

    def test_the_console_runs_a_dry_run_then_the_apply_as_one_job(self) -> None:
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'done')\n"
        folder = self.new("one-job", body, "--changes-prod", "--rollback", "undo")
        read = self.new("only-reads", "print(1)\n").name
        for tid in (folder.name, read):
            self.assertEqual(self.cli("lint", tid).returncode, 0)
        code = (
            "import json\nfrom opsquire.jobs import Jobs\nfrom opsquire.model import OpsquireError\n"
            f"job = Jobs().start({folder.name!r}, 'dryapply')\nassert job.proc\ncode = job.proc.wait(timeout=100)\n"
            f"try:\n    Jobs().start({read!r}, 'dryapply')\n    refused = ''\n"
            "except OpsquireError as e:\n    refused = str(e)\n"
            "print(json.dumps([code, refused]))"
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=150)
        self.assertEqual(r.returncode, 0, r.stderr)
        exit_code, refused = json.loads(r.stdout)
        self.assertEqual(exit_code, 0)
        self.assertIn("has no apply", refused)
        runs = json.loads(self.cli("runs", folder.name, "--json").stdout)
        modes = sorted((x["mode"], x["origin"]) for x in runs)
        self.assertEqual(modes, [("apply", "console"), ("dry", "console"), ("verify", "console")])

    def test_the_console_runs_the_staged_apply_its_inbox_shows(self) -> None:
        body = "import sys\n\nimport opsquire_helpers as sq\n\nsq.target('web1', 'ok', sys.argv[1])\n"
        folder = self.new(
            "consoled", body, "--changes-prod", "--rollback", "undo", "--stage", "build", "--stage", "deploy"
        )
        self.assertEqual(self.cli("lint", "consoled").returncode, 0)
        self.hand_over("consoled", "--apply")
        item = self.inbox_item(folder.name)
        assert item is not None
        self.assertEqual((item["next"], item["args"]), ("apply", ""), "a staged task never run walks every stage")
        self.assertEqual(self.console_apply(folder.name), ([], 0))
        modes = [(x["mode"], x["argv"]) for x in json.loads(self.cli("runs", "consoled", "--json").stdout)]
        self.assertEqual(
            modes,
            [("dry", ["build"]), ("apply", ["build"]), ("dry", ["deploy"]), ("apply", ["deploy"]), ("verify", [])],
        )

        folder = self.new(
            "handed", body, "--changes-prod", "--rollback", "undo", "--stage", "build", "--stage", "deploy"
        )
        self.assertEqual(self.cli("lint", "handed").returncode, 0)
        self.assertEqual(self.cli("run", "handed", "--", "build").returncode, 0)
        r = self.cli("handover", "handed", "--apply", "--why", "build only", "--", "build")
        self.assertEqual(r.returncode, 0, r.stderr)
        item = self.inbox_item(folder.name)
        assert item is not None
        self.assertEqual((item["next"], item["args"]), ("apply", "build"), "a handed-over stage keeps its arguments")
        self.assertEqual(self.console_apply(folder.name), (["build"], 0))
        modes = [(x["mode"], x["argv"]) for x in json.loads(self.cli("runs", "handed", "--json").stdout)]
        self.assertEqual(modes, [("dry", ["build"]), ("apply", ["build"])])

    def test_one_apply_walks_every_stage_and_stops_at_the_first_failure(self) -> None:
        body = (
            "import sys\n\nimport opsquire_helpers as sq\n\nstage = sys.argv[1]\n"
            "if sq.APPLY and stage == 'deploy':\n    raise SystemExit(1)\nsq.target('web1', 'ok', stage)\n"
        )
        folder = self.new(
            "piped", body, "--changes-prod", "--rollback", "undo", "--stage", "build", "--stage", "deploy"
        )
        (folder / "verify.py").write_text("import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'live')\n")
        self.assertEqual(self.cli("lint", "piped").returncode, 0)
        r = self.cli("handover", "piped", "--apply", "--why", "roll it out")
        self.assertEqual(r.returncode, 0, "a staged apply makes its own dry runs: " + r.stderr)
        r = self.cli("run", "piped", "--apply")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("stopped at stage deploy (apply); later stages did not run", r.stdout)
        modes = [(x["mode"], x["argv"]) for x in json.loads(self.cli("runs", "piped", "--json").stdout)]
        self.assertEqual(modes, [("dry", ["build"]), ("apply", ["build"]), ("dry", ["deploy"]), ("apply", ["deploy"])])
        self.assertIsNone(json.loads((folder / "meta.json").read_text()).get("outcome"))
