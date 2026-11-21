"""A task's life after its runs: verify, close, handover, hold, expiry and offboarding."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

from support import ASKING_BODY, CLI, ROOT, OpsTestCase, pid_alive


class LifecycleTest(OpsTestCase):
    def test_wait_returns_the_next_finished_run(self) -> None:
        self.new("waited", "import time\n\ntime.sleep(1)\nprint('done')\n")
        self.assertEqual(self.cli("run", "waited").returncode, 0)
        first = self.last("waited")["run_id"]
        w = self.start("wait", "waited", "--timeout", "60", "--json")
        time.sleep(1.5)
        self.assertEqual(self.cli("run", "waited").returncode, 0)
        out, err = w.communicate(timeout=90)
        self.assertEqual(w.returncode, 0, err)
        rec = json.loads(out)
        self.assertNotEqual(rec["run_id"], first)
        self.assertEqual(rec["status"], "ok")
        self.assertIn("started, waiting for it to finish", err)
        r = self.cli("wait", "waited", "--timeout", "1")
        self.assertEqual(r.returncode, 124)

    def test_wait_does_not_take_a_clock_jump_for_a_vanished_run(self) -> None:
        self.assertEqual(self.fake_running("pid:[1]", 6819), "interrupted", "the heartbeat looks two hours old")
        run_dir = Path(self.last("ghost")["paths"]["dir"])

        def status(to: str) -> None:
            record = run_dir / "run.json"
            record.write_text(json.dumps(json.loads(record.read_text()) | {"status": to}))

        w = self.start("wait", "ghost", "--since", "2000-01-01T00:00:00.000Z", "--timeout", "60", "--json")
        time.sleep(2)
        (run_dir / "heartbeat").touch()
        time.sleep(2)
        status("ok")
        out, err = w.communicate(timeout=90)
        self.assertEqual((w.returncode, json.loads(out)["status"]), (0, "ok"), err)

        status("running")
        os.utime(run_dir / "heartbeat", (time.time() - 120, time.time() - 120))
        started = time.monotonic()
        r = self.cli("wait", "ghost", "--since", "2000-01-01T00:00:00.000Z", "--timeout", "60", "--json")
        self.assertEqual(json.loads(r.stdout)["status"], "interrupted", "a heartbeat that stays stale is a dead run")
        self.assertGreater(time.monotonic() - started, 10, "only after two heartbeats it should have written")

    def test_ok_apply_closes_the_task_itself_and_close_can_amend(self) -> None:
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'restarted')\nsq.result('web1 on v2')\n"
        tid = self.new("auto", body, "--changes-prod", "--rollback", "undo").name
        self.assertEqual(self.cli("run", tid).returncode, 0)
        meta_path = self.home / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/meta.json"
        self.assertIsNone(json.loads(meta_path.read_text())["outcome"])
        r = self.cli("run", tid, "--apply", answers=[("", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("closed done", r.stdout)
        outcome = json.loads(meta_path.read_text())["outcome"]
        self.assertEqual((outcome["state"], outcome["closed_by"]), ("done", "opsquire run (auto)"))
        self.assertIn("ok on web1", outcome["note"])
        self.assertIn("web1 on v2", outcome["note"])
        runs = json.loads(self.cli("runs", tid, "--json").stdout)
        self.assertEqual([r["mode"] for r in runs][-2:], ["apply", "verify"])
        self.assertEqual(outcome["run"], runs[-2]["run_id"])
        design = (meta_path.parent / "design.md").read_text()
        self.assertIn("## Result", design)
        self.assertIn("- web1 on v2", design)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["tasks"][tid]["suggestions"], [])
        self.assertEqual(state["threads"][0]["state"], "done")
        self.assertEqual(
            self.cli("close", tid, "--outcome", "partial", "--note", "web1 rolled back later").returncode, 0
        )
        self.assertEqual(json.loads(meta_path.read_text())["outcome"]["state"], "partial")
        (meta_path.parent / "design.md").write_text(design + "\n## Later\n\nkept\n")
        self.assertEqual(self.cli("run", tid).returncode, 0)
        r = self.cli("run", tid, "--apply", answers=[("", "y")])
        self.assertIn("closed done", r.stdout, r.stdout + r.stderr)
        again = json.loads(meta_path.read_text())["outcome"]
        self.assertEqual((again["state"], again["previous"]["state"]), ("done", "partial"))
        self.assertNotIn("previous", again["previous"])
        self.assertGreater(again["closed"], outcome["closed"])
        design = (meta_path.parent / "design.md").read_text()
        self.assertLess(design.index("Closed again automatically"), design.index("## Later"))
        self.assertTrue(design.rstrip().endswith("kept"))

    def test_judge_closes_from_the_record_or_keeps_it_open(self) -> None:
        self.agents_file()
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'refused')\n"
        tid = self.new("judged", body, "--changes-prod", "--rollback", "undo").name
        (self.home / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/design.md").write_text("# why\n\nbecause\n")
        self.assertEqual(self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")]).returncode, 1)
        self.env["FAKE_JUDGE"] = "KEEP: no evidence that web1 came back"
        r = self.cli("judge", tid)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("kept open: no evidence", r.stdout)
        self.assertIsNone(json.loads(self.cli("show", tid, "--json").stdout)["meta"]["outcome"])
        judged = json.loads(self.cli("threads", "--json").stdout)["tasks"][tid]["judged"]
        self.assertEqual((judged["decision"], judged["note"]), ("keep", "no evidence that web1 came back"))
        self.env["FAKE_JUDGE"] = "CLOSE: partial | web1 refused but web2 was done by hand; see run above"
        r = self.cli("judge", tid, "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["decision"], out["cost_usd"]), ("partial", 0.02))
        meta = json.loads(self.cli("show", tid, "--json").stdout)["meta"]
        self.assertEqual((meta["outcome"]["state"], meta["outcome"]["closed_by"]), ("partial", "opsquire judge (fake)"))
        self.assertIn("web2 was done by hand", meta["outcome"]["note"])
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertTrue(state["tasks"][tid]["settled"])
        self.assertEqual(self.cli("judge", "nothing-like-this").returncode, 2)

    def test_needs_decision_holds_every_condition_of_the_rule(self) -> None:
        from opsquire.state import needs_decision

        base: dict[str, Any] = {
            "id": "t",
            "settled": False,
            "kind": "task",
            "archived": False,
            "in_schedule": None,
            "suggestions": [],
        }
        failed = {"status": "failed"}
        self.assertTrue(needs_decision(base, failed, set()))
        self.assertTrue(needs_decision(base, {}, set()), "a run with no status did not end ok")
        self.assertTrue(needs_decision(base | {"suggestions": [{"kind": "stale"}]}, None, set()))
        self.assertFalse(needs_decision(base, None, set()), "never run and nothing suggested")
        for status in ("ok", "running"):
            self.assertFalse(needs_decision(base, {"status": status}, set()), status)
        suggested = base | {"suggestions": [{"kind": "stale"}]}
        for out in ({"settled": True}, {"kind": "general"}, {"archived": True}, {"in_schedule": "plan"}):
            self.assertFalse(needs_decision(suggested | out, failed, set()), out)
        self.assertFalse(needs_decision(suggested, failed, {"t"}), "waiting in the inbox, or running")

    def test_judge_due_judges_every_task_that_needs_a_decision_and_survives_a_failure(self) -> None:
        self.agents_file(model_argv=["--model", "{model}"], models={"quick": {"id": "m-quick"}})
        failing = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'refused')\n"
        first = self.new("due-a", failing).name
        second = self.new("due-b", failing).name
        fine = self.new("due-ok", "print(1)\n").name
        waiting = self.new("due-handed", failing).name
        for tid in (first, second, fine, waiting):
            self.cli("run", tid)
        self.hand_over(waiting, "--why", "run it again")
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(
            sorted(t for t, v in state["tasks"].items() if v["decide"]), [first, second], "not ok, not waiting"
        )
        self.env["FAKE_JUDGE_FAIL"] = second
        r = self.cli("judge", "--due", "--jobs", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{first}: closed done: the apply run landed it on web1  ($0.02)", r.stdout)
        self.assertIn(f"{second}: failed: the judge agent failed (exit 1)", r.stdout)
        self.assertTrue(r.stdout.rstrip().endswith("2 judged, 1 failed, $0.02 in all"), r.stdout)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertTrue(state["tasks"][first]["settled"])
        self.assertTrue(state["tasks"][second]["decide"])
        del self.env["FAKE_JUDGE_FAIL"]
        log = self.tmp / "agent.jsonl"
        self.env["FAKE_LOG"] = str(log)
        r = self.cli("judge", "--due", "--max", "1", "--tier", "quick", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(o["id"], o["decision"]) for o in json.loads(r.stdout)], [(second, "done")])
        argv = json.loads(log.read_text())["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "m-quick")
        r = self.cli("judge", "--due")
        self.assertEqual((r.returncode, r.stdout), (0, "no task needs a decision\n"))
        self.assertEqual(self.cli("judge").returncode, 2)
        self.assertEqual(self.cli("judge", first, "--jobs", "2").returncode, 2)
        self.assertEqual(self.cli("judge", "--due", "--jobs", "0").returncode, 2)

    def test_judge_all_runs_jobs_at_once_and_one_failure_stops_no_other(self) -> None:
        from opsquire import judge as judging
        from opsquire.model import OpsquireError

        together = threading.Barrier(2, timeout=30)
        lock = threading.Lock()
        live, peak = [0], [0]

        def one(tid: str, _agent: str | None, tier: str) -> dict[str, Any]:
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                if tid in ("t1", "t2"):
                    together.wait()
                if tid == "t3":
                    raise OpsquireError("the agent broke")
                return {"id": tid, "decision": "done", "note": tier, "cost_usd": 0.5}
            finally:
                with lock:
                    live[0] -= 1

        seen: list[dict[str, Any]] = []
        with mock.patch.object(judging, "judge", one):
            out = judging.judge_all(["t1", "t2", "t3", "t4"], 2, seen.append, tier="quick")
        self.assertEqual(seen, out, "each result is reported as it comes")
        self.assertEqual(peak[0], 2)
        by_id = {o["id"]: (o["decision"], o["note"]) for o in out}
        self.assertEqual(
            by_id,
            {
                "t1": ("done", "quick"),
                "t2": ("done", "quick"),
                "t3": ("failed", "the agent broke"),
                "t4": ("done", "quick"),
            },
        )

    def test_judge_due_interrupted_stops_every_agent(self) -> None:
        self.agents_file()
        failing = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'refused')\n"
        tids = [self.new(f"slow-{n}", failing).name for n in ("a", "b")]
        for tid in tids:
            self.cli("run", tid)
        log = self.tmp / "agent.jsonl"
        self.env |= {"FAKE_SLEEP": "60", "FAKE_LOG": str(log)}
        p = self.start("judge", "--due", "--jobs", "2")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and len(log.read_text().splitlines() if log.exists() else []) < 2:
            time.sleep(0.1)
        pids = [json.loads(line)["pid"] for line in log.read_text().splitlines()]
        self.assertEqual(len(pids), 2)
        os.kill(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual([pid for pid in pids if pid_alive(pid)], [], "the agents outlived opsquire")
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual([state["tasks"][t]["decide"] for t in tids], [True, True])

    def test_verify_check_is_scaffolded_runs_read_only_and_closes_when_all_ok(self) -> None:
        folder = self.new("deploy-v", "print('deploy')\n", "--changes-prod", "--rollback", "undo")
        tid = folder.name
        self.assertTrue((folder / "verify.py").is_file())
        self.assertEqual(json.loads((folder / "meta.json").read_text())["verify"], "verify.py")
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        r = self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.last(tid)["mode"], "verify")
        self.assertIn("closed done", r.stdout)
        self.assertIn("apply run", json.loads((folder / "meta.json").read_text())["outcome"]["note"])
        meta = json.loads((folder / "meta.json").read_text())
        meta["outcome"] = None
        (folder / "meta.json").write_text(json.dumps(meta))
        r = self.cli("run", tid, "--verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.last(tid)
        self.assertEqual(
            (rec["mode"], rec["status"], rec["entry"], rec["targets"]["web1"]["status"]),
            ("verify", "ok", "verify.py", "skipped"),
        )
        self.assertEqual(rec["lock"]["mode"], "shared")
        self.assertIsNone(json.loads((folder / "meta.json").read_text())["outcome"])
        self.assertNotIn("closed done", r.stdout)
        (folder / "verify.py").write_text(
            "import opsquire_helpers as sq\n\nfor name in sq.TARGETS:\n    sq.target(name, 'ok', 'v2 is live')\n"
        )
        r = self.cli("run", tid, "--verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("closed done", r.stdout)
        outcome = json.loads((folder / "meta.json").read_text())["outcome"]
        self.assertIn("verify run", outcome["note"])
        self.assertEqual(self.cli("run", tid, "--verify", "--apply").returncode, 0)
        plain = self.new("plain", "print(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify")
        self.assertFalse((plain / "verify.py").exists())
        self.assertEqual(self.cli("run", plain.name, "--verify").returncode, 2)
        read = self.new("ro", "print(1)\n")
        self.assertFalse((read / "verify.py").exists())

    def test_a_failed_automatic_verify_fails_the_apply_command_and_what_waits_for_it(self) -> None:
        folder = self.new(
            "verify-fails",
            "import opsquire_helpers as sq\n\nsq.target('web1', 'ok')\n",
            "--changes-prod",
            "--rollback",
            "undo",
        )
        tid = folder.name
        failing = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'service is unhealthy')\n"
        (folder / "verify.py").write_text(failing)
        self.assertEqual(self.cli("run", tid).returncode, 0)
        r = self.cli("run", tid, "--apply", answers=[("", "y")])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        runs = json.loads(self.cli("runs", tid, "--json").stdout)
        self.assertEqual([(x["mode"], x["status"]) for x in runs[-2:]], [("apply", "ok"), ("verify", "failed")])
        self.assertIsNone(json.loads((folder / "meta.json").read_text()).get("outcome"), "the task stays open")
        (folder / "verify.py").write_text(
            "import sys\n\nimport opsquire_helpers as sq\n\nsq.step('stops')\nsys.exit(130)\n"
        )
        r = self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")])
        self.assertEqual(r.returncode, 130, r.stdout + r.stderr)
        self.assertEqual((self.last(tid)["mode"], self.last(tid)["status"]), ("verify", "interrupted"))

        meta_path = folder / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta_path.write_text(json.dumps({**meta, "verify": None}))
        self.assertEqual(self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")]).returncode, 0)
        applied = self.last(tid)
        meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "verify": "verify.py"}))
        (folder / "verify.py").write_text(failing)
        since = ("--since", str(runs[-1]["started"]))
        r = self.cli("wait", tid, *since, "--timeout", "3")
        self.assertEqual(r.returncode, 124, "an ok apply alone does not say how a task with a verify check ended")
        self.assertIn("its verify check did not finish", r.stderr)
        waiting = subprocess.Popen(
            [*CLI, "wait", tid, *since, "--json", "--timeout", "120"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(1)
        self.assertEqual(self.cli("run", tid, "--verify").returncode, 1)
        out, err = waiting.communicate(timeout=120)
        self.assertEqual(waiting.returncode, 1, out + err)
        rec = json.loads(out)
        self.assertEqual((rec["mode"], rec["status"]), ("verify", "failed"))
        self.assertIn(f"run {applied['run_id']} applied ok; waiting for the verify check", err)

    def test_inbox_asks_for_the_verify_check_after_an_apply_and_when_the_judge_says_so(self) -> None:
        self.agents_file()
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'refused')\n"
        folder = self.new("needs-check", body, "--changes-prod", "--rollback", "undo")
        tid = folder.name
        self.assertEqual(self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")]).returncode, 1)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(
            [(i["next"], i["command"]) for i in state["inbox"]], [("verify", f"opsquire run {tid} --verify")]
        )
        self.assertEqual(self.cli("run", tid, "--verify").returncode, 0)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["inbox"], [])
        self.env["FAKE_JUDGE"] = "VERIFY: that web1 serves v2 on port 80"
        r = self.cli("judge", tid)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("needs the verify check: that web1 serves v2", r.stdout)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["tasks"][tid]["judged"]["decision"], "verify")
        self.assertEqual([i["next"] for i in state["inbox"]], ["verify"])
        self.assertEqual(state["tasks"][tid]["verify"], "verify.py")
        other = self.new("no-check", body, "--changes-prod", "--rollback", "undo", "--no-verify").name
        r = self.cli("judge", other)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no verify check yet; an agent must add one", r.stdout)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"][0]["task_id"], tid)

    def test_milestones_report_closed_changing_tasks_failed_applies_and_thread_results(self) -> None:
        self.new("peek", "print(1)\n")
        good = self.new(
            "flip", self.CONFIRM_BODY, "--changes-prod", "--rollback", "flip back", "--no-verify", "--thread", "story"
        )
        bad = self.new(
            "boom",
            "import sys\n\nsys.exit(1)\n",
            "--changes-prod",
            "--rollback",
            "undo",
            "--no-verify",
            "--thread",
            "story",
        )
        for name in ("peek", "flip", "boom"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertEqual(self.cli("run", "peek").returncode, 0)
        self.assertEqual(self.cli("run", "flip").returncode, 0)
        self.assertEqual(self.cli("run", "flip", "--apply", answers=[("[y/N]", "y")]).returncode, 0)
        self.assertEqual(self.cli("run", "boom").returncode, 1)
        self.assertNotEqual(self.cli("run", "boom", "--apply", *self.NO_DRY, answers=[("[y/N]", "y")]).returncode, 0)
        (self.home / "threads").mkdir(exist_ok=True)
        (self.home / "threads/story.md").write_text("# story\n\nFirst paragraph.\n\nFlip is live, boom is not.\n")
        items = self.milestones()
        self.assertEqual(
            [(m["kind"], m["task_id"], m["thread"]) for m in items],
            [("done", good.name, "story"), ("failed", bad.name, "story"), ("result", None, "story")],
        )
        done, failed, result = items
        self.assertEqual((done["state"], done["by"]), ("done", "opsquire run (auto)"))
        self.assertEqual(done["text"], "done")
        self.assertEqual((failed["mode"], failed["state"], failed["failed_targets"]), ("apply", "failed", ["web1"]))
        self.assertEqual(result["text"], "Flip is live, boom is not.")
        self.assertEqual([m["kind"] for m in self.milestones("--kind", "failed,result")], ["failed", "result"])
        self.assertEqual(self.milestones("--thread", "other"), [])
        self.assertEqual(len(self.milestones("--thread", "story")), 3)
        self.assertEqual(self.cli("milestones", "--since", "2999-01-01").stdout.strip(), "no milestones in that window")
        text = self.cli("milestones", "--since", "2020-01-01").stdout
        self.assertIn("story\n", text)
        self.assertIn("apply failed   boom [web1]", text)
        self.assertIn("done           flip [web1]: done", text)
        self.assertIn("result         thread result [web1]: Flip is live, boom is not.", text)
        self.assertEqual(self.cli("milestones", "--kind", "nope").returncode, 2)

    def test_milestones_mark_what_an_unattended_apply_led_to(self) -> None:
        flip = self.new("flip", "import opsquire_helpers as sq\n\nsq.target('web1', 'ok')\n", *self.CHANGING)
        boom = self.new("boom", "import sys\n\nsys.exit(1)\n", *self.CHANGING)
        for name in ("flip", "boom"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertEqual(self.cli("policy", "--tier", "local", "web1").returncode, 0)
        self.assertEqual(self.cli("policy", "--auto", "local", "apply").returncode, 0)
        self.assertEqual(self.cli("run", "flip", "--apply", *self.NO_DRY, unattended=True).returncode, 0)
        self.assertNotEqual(self.cli("run", "boom", "--apply", *self.NO_DRY, unattended=True).returncode, 0)
        self.assertNotEqual(self.cli("run", "boom", "--apply", *self.NO_DRY, answers=[]).returncode, 0)
        self.assertEqual(
            [(m["kind"], m["task_id"], m["unattended"]) for m in self.milestones()],
            [("done", flip.name, True), ("failed", boom.name, True), ("failed", boom.name, False)],
        )
        text = self.cli("milestones", "--since", "2020-01-01").stdout
        self.assertIn("done (unattended) flip [web1]", text)
        self.assertIn("apply failed (unattended) boom [web1]", text)
        self.assertIn("apply failed   boom [web1]", text, "a run typed in a terminal is not marked")

    def test_milestones_report_a_plan_once_and_never_a_dry_or_blocked_run(self) -> None:
        a = self.new("stop", self.CONFIRM_BODY, "--changes-prod", "--rollback", "start", "--no-verify")
        b = self.new("start", self.CONFIRM_BODY, "--changes-prod", "--rollback", "stop", "--no-verify")
        plan = self.make_plan(a, b, name="cutover")
        for name in ("stop", "start", "cutover"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertEqual(self.cli("run", "cutover").returncode, 0)
        self.assertEqual(self.milestones(), [])
        r = self.cli("run", "cutover", "--apply", answers=[("[y/N]", "y"), ("[y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        items = self.milestones()
        self.assertEqual([(m["kind"], m["task_id"]) for m in items], [("done", plan.name)])
        for step in (a, b):
            self.assertTrue(json.loads(self.cli("show", step.name, "--json").stdout)["meta"]["outcome"])

    def test_milestones_since_accepts_durations_clock_times_and_dates(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))

        from opsquire.milestones import since_arg
        from opsquire.model import OpsquireError

        at = dt.datetime(2026, 11, 5, 18, 30, tzinfo=dt.UTC)
        self.assertEqual(since_arg(None, at), "2026-11-05T00:00:00.000Z")
        self.assertEqual(since_arg("2h", at), "2026-11-05T16:30:00.000Z")
        self.assertEqual(since_arg("30m", at), "2026-11-05T18:00:00.000Z")
        self.assertEqual(since_arg("1d", at), "2026-11-04T18:30:00.000Z")
        self.assertEqual(since_arg("9:05", at), "2026-11-05T09:05:00.000Z")
        self.assertEqual(since_arg("2026-11-01", at), "2026-11-01T00:00:00.000Z")
        self.assertEqual(since_arg("2026-11-01T04:00", at), "2026-11-01T04:00")
        with self.assertRaises(OpsquireError):
            since_arg("yesterday", at)
        ny = "America/New_York"
        self.assertEqual(since_arg(None, at, ny), "2026-11-05T05:00:00.000Z", "midnight in New York")
        self.assertEqual(since_arg("9:05", at, ny), "2026-11-05T14:05:00.000Z")
        self.assertEqual(since_arg("9:05Z", at, ny), "2026-11-05T09:05:00.000Z", "a trailing Z is UTC")
        self.assertEqual(since_arg("2026-07-01", at, ny), "2026-07-01T04:00:00.000Z", "summer time")
        late = dt.datetime(2026, 11, 6, 2, 0, tzinfo=dt.UTC)
        self.assertEqual(since_arg(None, late, ny), "2026-11-05T05:00:00.000Z", "still the 5th in New York")

    def test_handover_puts_a_reused_task_in_the_inbox_until_it_runs(self) -> None:
        folder = self.new("count", "print(1)\n")
        self.assertEqual(self.cli("lint", "count").returncode, 0)
        self.assertEqual(self.cli("run", "count").returncode, 0)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        r = self.cli("handover", "count", "--why", "the host changed since", "--", "web1", "--fast")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"handed over: opsquire run {folder.name} -- web1 --fast", r.stdout)
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual(
            (item["task_id"], item["next"], item["command"]),
            (folder.name, "read", f"opsquire run {folder.name} -- web1 --fast"),
        )
        self.assertEqual((item["handed"]["by"], item["handed"]["why"]), ("test", "the host changed since"))
        meta = json.loads((folder / "meta.json").read_text())
        self.assertEqual(meta["handover"]["args"], ["web1", "--fast"])
        self.assertEqual(json.loads(self.cli("show", "count", "--json").stdout)["status"], "done")
        self.assertEqual(self.cli("run", "count", "--", "web1", "--fast").returncode, 0)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        self.assertIn("handover", json.loads((folder / "meta.json").read_text()))

        bad = self.cli("handover", "count", "--apply")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("read-only; it has no apply run", bad.stderr)
        changing = self.new("flip", "print(1)\n", "--changes-prod", "--rollback", "flip back")
        self.assertEqual(self.cli("lint", "flip").returncode, 0)
        self.assertEqual(self.cli("run", "flip").returncode, 0)
        self.assertEqual([i["next"] for i in json.loads(self.cli("threads", "--json").stdout)["inbox"]], ["apply"])
        self.assertEqual(self.cli("handover", "flip", "--why", "dry again first").returncode, 0)
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual(
            (item["task_id"], item["next"], item["command"]), (changing.name, "dry", f"opsquire run {changing.name}")
        )
        self.assertEqual(self.cli("handover", "flip", "--verify").returncode, 0)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"][0]["next"], "verify")
        general = self.cli("new", "web1", "lib", "--general", "--purpose", "g")
        self.assertEqual(general.returncode, 0)
        self.assertIn("general script", self.cli("handover", "web1_lib").stderr)

    def test_close_records_outcome_and_shows_in_status_and_catalog(self) -> None:
        folder = self.new(
            "finish-by-hand",
            ASKING_BODY,
            "--changes-prod",
            "--rollback",
            "undo",
        )
        (folder / "design.md").unlink()
        self.assertEqual(self.cli("run", "finish-by-hand").returncode, 0)
        dry = self.last("finish-by-hand")["run_id"]
        r = self.cli("close", "finish-by-hand", "--outcome", "done", "--note", "restore finished by hand", "--run", dry)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no design.md", r.stderr)
        shown = json.loads(self.cli("show", "finish-by-hand", "--json").stdout)
        self.assertEqual(shown["status"], "done")
        outcome = shown["meta"]["outcome"]
        self.assertEqual((outcome["state"], outcome["closed_by"], outcome["run"]), ("done", "test", dry))
        self.assertEqual(self.cli("docs").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("Closed done by test: restore finished by hand", catalog)
        (folder / "design.md").write_text("# d\n\n## Result\n\nlive\n")
        again = self.cli("close", "finish-by-hand", "--outcome", "partial", "--note", "one worker still down")
        self.assertNotIn("design.md", again.stderr)
        self.assertEqual(json.loads(self.cli("show", "finish-by-hand", "--json").stdout)["status"], "partial")

    def test_close_abandoned_sets_reason_and_rejects_bad_input(self) -> None:
        self.new("gave-up", "print('x')\n")
        self.assertNotEqual(self.cli("close", "gave-up", "--outcome", "done", "--note", "  ").returncode, 0)
        self.assertNotEqual(self.cli("close", "gave-up", "--outcome", "finished", "--note", "x").returncode, 0)
        bad_run = self.cli("close", "gave-up", "--outcome", "done", "--note", "x", "--run", "nope")
        self.assertNotEqual(bad_run.returncode, 0)
        self.assertIn("not a run of", bad_run.stderr)
        self.assertEqual(
            json.loads(self.cli("show", "gave-up", "--json").stdout)["meta"].get("outcome"),
            None,
            "failed closes write nothing",
        )
        self.assertEqual(
            self.cli("close", "gave-up", "--outcome", "abandoned", "--note", "superseded by hand").returncode, 0
        )
        shown = json.loads(self.cli("show", "gave-up", "--json").stdout)
        self.assertEqual(shown["status"], "abandoned")
        self.assertEqual(shown["meta"]["abandoned_reason"], "superseded by hand")

    def test_abandon_takes_an_item_out_of_the_inbox_and_keeps_how_finished_tasks_ended(self) -> None:
        from opsquire.close import abandon_waiting
        from opsquire.model import OpsquireError

        self.enterContext(mock.patch.dict(os.environ, self.env))
        open_one = self.new("give-up", "print('x')\n").name
        ran = self.new("ran", "print('x')\n").name
        self.assertEqual(self.cli("run", ran).returncode, 0)
        self.cli("runs", ran)
        self.assertEqual(self.cli("handover", ran, "--why", "read it again").returncode, 0)
        step = self.new("step", "print('x')\n").name
        plan = Path(self.cli("new", "web1", "bundle", "--plan", "--step", step, "--purpose", "p").stdout.strip()).name
        for t in (open_one, plan):
            self.assertEqual(self.cli("lint", t).returncode, 0)
            self.hand_over(t)

        def waiting() -> list[str]:
            return [i["task_id"] for i in json.loads(self.cli("inbox", "--json").stdout)["inbox"]]

        self.assertTrue({open_one, ran, plan} <= set(waiting()), waiting())
        with self.assertRaises(OpsquireError):
            abandon_waiting(open_one, " ")
        self.assertNotIn("withdrawn", abandon_waiting(open_one, "not needed"))
        self.assertTrue(abandon_waiting(ran, "read enough")["withdrawn"])
        abandon_waiting(plan, "dropped")
        self.assertEqual(waiting(), [])

        def shown(t: str) -> Any:
            return json.loads(self.cli("show", t, "--json").stdout)

        self.assertEqual(shown(open_one)["meta"]["abandoned_reason"], "not needed")
        self.assertEqual(shown(ran)["status"], "done", "a task that ran ok keeps how it ended")
        self.assertEqual(shown(step)["meta"]["abandoned_reason"], f"abandoned with plan {plan}: dropped")
        with self.assertRaises(OpsquireError):
            abandon_waiting(ran, "again")

    def test_close_refuses_general_scripts(self) -> None:
        self.assertEqual(self.cli("new", "web1", "shared-bit", "--general", "--purpose", "g").returncode, 0)
        general = self.cli("close", "web1_shared-bit", "--outcome", "done", "--note", "x")
        self.assertNotEqual(general.returncode, 0)
        self.assertIn("general scripts are not closed", general.stderr)

    def test_handover_refuses_an_apply_that_already_ran_the_same_way(self) -> None:
        folder = self.new(
            "flip",
            "import sys\n\nimport opsquire_helpers as sq\n\nsq.result(' '.join(sys.argv[1:]))\n",
            "--changes-prod",
            "--rollback",
            "flip back",
            "--no-verify",
        )
        (folder / "design.md").write_text("# flip\n")
        self.assertEqual(self.cli("lint", "flip").returncode, 0)
        self.assertEqual(self.cli("run", "flip").returncode, 0)
        self.assertEqual(self.cli("handover", "flip", "--apply", "--why", "dry ok").returncode, 0)
        self.assertEqual(self.cli("run", "flip", "--apply").returncode, 0)
        applied = self.last("flip")
        self.assertIn("## Result", (folder / "design.md").read_text(), "closing changes the task's content hash")

        refused = self.cli("handover", "flip", "--apply", "--why", "dry ok")
        self.assertEqual(refused.returncode, 2)
        self.assertIn(applied["run_id"], refused.stderr)
        self.assertIn("--again", refused.stderr)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        self.assertEqual(self.cli("handover", "flip", "--apply", "--again").returncode, 2, "--again needs a reason")

        again = self.cli("handover", "flip", "--apply", "--again", "--why", "rolled back at 09:00")
        self.assertEqual(again.returncode, 0, again.stderr)
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertIn(applied["run_id"], item["applied_already"])
        self.assertEqual(
            self.cli("handover", "flip", "--withdraw").stdout.strip(), f"{folder.name}: handover withdrawn"
        )
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        self.assertIn("no handover", self.cli("handover", "flip", "--withdraw").stdout)

        uncovered = self.cli("handover", "flip", "--apply", "--", "other")
        self.assertEqual(uncovered.returncode, 2, "other arguments need their own dry run")
        self.assertIn(f"opsquire run {folder.name} -- other", uncovered.stderr)
        self.assertEqual(self.cli("run", "flip", "--", "other").returncode, 0)
        self.assertEqual(self.cli("handover", "flip", "--apply", "--", "other").returncode, 0, "other arguments")
        repeated = self.cli("run", "flip", "--apply")
        self.assertEqual(repeated.returncode, 0)
        self.assertIn("this apply runs it again", repeated.stderr, "a typed command still says so")
        (folder / "main.py").write_text((folder / "main.py").read_text() + "\n")
        self.assertEqual(self.cli("handover", "flip", "--apply").returncode, 2, "other scripts need a dry run")
        self.assertEqual(self.cli("run", "flip").returncode, 0)
        self.assertEqual(self.cli("handover", "flip", "--apply").returncode, 0, "other scripts")

    def test_a_failed_apply_does_not_count_as_already_applied(self) -> None:
        self.new(
            "boom",
            "import sys\n\nimport opsquire_helpers as sq\n\nsys.exit(1 if sq.APPLY else 0)\n",
            "--changes-prod",
            "--rollback",
            "undo",
            "--no-verify",
            "--target",
            "web1",
        )
        self.assertEqual(self.cli("lint", "boom").returncode, 0)
        self.assertEqual(self.cli("run", "boom").returncode, 0)
        self.assertEqual(self.cli("run", "boom", "--apply").returncode, 1)
        self.assertEqual(self.cli("handover", "boom", "--apply", "--why", "the host is back").returncode, 0)

    def test_an_ask_keeps_a_repeated_apply_out_of_the_inbox(self) -> None:
        self.new("flip", "print(1)\n", "--changes-prod", "--rollback", "flip back", "--no-verify")
        self.assertEqual(self.cli("lint", "flip").returncode, 0)
        self.assertEqual(self.cli("run", "flip", "--apply", *self.NO_DRY).returncode, 0)
        from opsquire import handover

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home)}),
            mock.patch("sys.stdout", out),
            mock.patch("sys.stderr", err),
        ):
            handed = handover.record_run_lines([("RUN", "opsquire run flip --apply")], "again", "ask 1")
        self.assertEqual(handed, [])
        self.assertIn("not put in the inbox", err.getvalue())

    def test_a_plan_that_applied_ok_is_not_handed_over_again(self) -> None:
        self.new("step", "print(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify")
        step = next(self.home.glob("tasks/*/*_web1_step")).name
        made = self.cli("new", "web1", "bundle", "--plan", "--step", step, "--purpose", "p", "--changes-prod")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.cli("lint", "bundle").returncode, 0)
        self.assertEqual(self.cli("run", "bundle", "--apply", *self.NO_DRY).returncode, 0)
        refused = self.cli("handover", "bundle", "--apply", "--why", "again")
        self.assertEqual(refused.returncode, 2, refused.stdout)

    def test_new_and_handover_say_what_else_waits_on_the_target(self) -> None:
        waiting = self.new("replay", "print(1)\n", "--target", "db", "--target", "db_2", area="data")
        self.assertEqual(self.cli("lint", "replay").returncode, 0)
        self.hand_over("replay")
        new = self.cli("new", "db-2", "canary", "--purpose", "p", "--changes-prod", "--rollback", "undo")
        self.assertEqual(new.returncode, 0)
        self.assertEqual(len(new.stdout.strip().splitlines()), 1, "stdout stays one path for scripts")
        self.assertIn(f"also waiting on db_2: {waiting.name}, read run", new.stderr)
        self.assertIn("one plan or task", new.stderr)
        canary = Path(new.stdout.strip())
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        handed = self.cli("handover", "canary", "--why", "go")
        self.assertIn(waiting.name, handed.stderr)
        as_json = json.loads(self.cli("handover", "canary", "--why", "go", "--json").stdout)
        self.assertEqual(as_json["waiting_on_same_target"][0]["task_id"], waiting.name)
        self.assertEqual(self.cli("new", "other-host", "x", "--purpose", "p").stderr, "", "nothing else waits there")
        inbox = {i["task_id"]: i for i in json.loads(self.cli("threads", "--json").stdout)["inbox"]}
        self.assertEqual(inbox[canary.name]["same_target"], [waiting.name])
        self.assertEqual(inbox[waiting.name]["same_target"], [canary.name])

    def test_a_failed_changing_task_leaves_the_inbox_until_it_changes(self) -> None:
        folder = self.new(
            "canary", "import sys\n\nsys.exit(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify"
        )
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(self.cli("run", "canary").returncode, 1)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        (folder / "main.py").write_text("print(1)\n")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [], "fixed, not handed over yet")
        self.assertEqual(self.cli("handover", "canary").returncode, 2, "an edited task says what changed")
        self.hand_over("canary", "--changed", "no longer exits 1")
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([(i["next"], i["changed_note"]["text"]) for i in inbox], [("dry", "no longer exits 1")])
        (folder / "main.py").write_text("import sys\n\nsys.exit(1)\n")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(self.cli("run", "canary").returncode, 1)
        self.assertEqual(self.cli("handover", "canary", "--why", "the host is back").returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([i["next"] for i in inbox], ["dry"], "a handover brings it back")

    def test_a_dry_run_of_older_scripts_does_not_offer_the_apply(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        folder = self.new(
            "canary", "print(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify", "--uses", "web1_shared"
        )

        def owed() -> list[str]:
            return [i["next"] for i in json.loads(self.cli("threads", "--json").stdout)["inbox"]]

        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.hand_over("canary")
        self.assertEqual(owed(), ["dry"])
        self.assertEqual(self.cli("run", "canary").returncode, 0)
        self.assertEqual(owed(), ["apply"])
        (folder / "design.md").write_text("# canary\n\nnotes do not change what runs\n")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(owed(), ["apply"])
        (g / "notes.txt").write_text("the general script it uses changed after the dry run\n")
        self.assertEqual(owed(), [], "a shared script changed since its dry run: being prepared")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.hand_over("canary", "--changed", "its shared script has notes")
        self.assertEqual(owed(), ["dry"], "the dry run was of other files")
        self.assertEqual(self.cli("run", "canary").returncode, 0)
        self.assertEqual(owed(), ["apply"])
        (folder / "main.py").write_text("print(2)\n")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(owed(), [], "its own code changed: being prepared until handed over again")
        self.hand_over("canary", "--changed", "prints 2")
        self.assertEqual(owed(), ["dry"])

    def test_settled_tasks_leave_after_the_retention_and_come_back(self) -> None:
        self.offboard_env()
        self.git_home()
        seen = self.new("seen", "print('looked')\n", "--thread", "cleanup")
        self.assertEqual(self.cli("lint", "seen").returncode, 0)
        self.assertEqual(self.cli("run", "seen").returncode, 0)
        open_task = self.new("open", "print(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify")
        self.assertEqual(self.cli("lint", "open").returncode, 0)
        stale = self.new("stale", "print(1)\n", "--thread", "cleanup")
        fresh = self.new("fresh", "print(1)\n", "--thread", "cleanup")
        for name in ("stale", "fresh"):
            r = self.cli("close", name, "--outcome", "abandoned", "--note", "not needed after all")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.backdate(stale, 8)
        self.assertEqual(self.due_in(0), [stale.name])
        self.assertEqual(self.due_in(6), [stale.name], "a read task that ran ok and a close both start the clock")
        self.assertEqual(self.due_in(8), sorted([seen.name, stale.name, fresh.name]), "the open task never leaves")

        dry = json.loads(self.cli("offboard", "--due", "--dry", "--json").stdout)
        self.assertEqual((dry["would_offboard"], dry["offboarded"], dry["days"]), ([stale.name], [], 7.0))
        self.assertTrue(stale.is_dir())

        stamp = self.home / "var/offboard.json"
        self.assertTrue(stamp.is_file(), "the commands above already swept today and found nothing")
        stamp.unlink()
        r = self.cli("lint", "open")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("offboarded 1 settled tasks idle 7+ days", r.stderr)
        gone = self.home / "offboarded" / stale.parent.name / stale.name
        self.assertTrue((gone / "main.py").is_file())
        self.assertFalse(stale.exists())
        log = subprocess.run(
            ["git", "-C", str(self.home), "log", "--format=%s", "--name-status", "-M", "-3"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("offboarded 1 tasks idle 7+ days", log)
        self.assertRegex(log, rf"R100\ttasks/[\d-]+/{stale.name}/main.py\toffboarded/[\d-]+/{stale.name}/main.py")

        self.backdate(fresh, 8)
        r = self.cli("lint", "open")
        self.assertNotIn("offboarded", r.stderr, "the sweep rides at most once a day")
        self.assertTrue(fresh.is_dir())
        r = self.cli("offboard", "--due")
        self.assertEqual((r.returncode, r.stdout), (0, f"offboarded 1 idle 7+ days:\n  {fresh.name}\n"))
        self.assertEqual(self.cli("offboard", "--due").stdout, "offboarded 0 idle 7+ days\n")

        listed = {t["id"] for t in json.loads(self.cli("ls", "--json").stdout)}
        self.assertEqual(listed, {seen.name, open_task.name})
        hits = {h["id"]: h for h in json.loads(self.cli("find", "stale", "seen", "--json").stdout)["history"]}
        self.assertEqual((hits[stale.name]["offboarded"], hits[stale.name]["status"]), (True, "abandoned"))
        self.assertFalse(hits[seen.name]["offboarded"])
        self.assertIn("abandoned, offboarded", self.cli("find", "stale").stdout)
        shown = json.loads(self.cli("show", "stale", "--json").stdout)
        self.assertEqual((shown["offboarded"], shown["path"]), (True, str(gone)))
        self.assertEqual(self.cli("runs", "stale").returncode, 0)
        for cmd in (
            ["run", "stale"],
            ["lint", "stale"],
            ["handover", "stale"],
            ["close", "stale", "--outcome", "done"],
        ):
            r = self.cli(*cmd, "--note", "x") if cmd[0] == "close" else self.cli(*cmd)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn(f"`opsquire restore {stale.name}` brings it back", r.stderr)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertNotIn(stale.name, catalog)
        self.assertIn("## Offboarded\n\n2 settled tasks idle past the retention are in offboarded/", catalog)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual((state["offboarded"], sorted(state["tasks"])), (2, sorted([seen.name, open_task.name])))
        thread = next(th for th in state["threads"] if th["id"] == "cleanup")
        self.assertEqual((thread["tasks"], thread["offboarded"]), ([seen.name], 2))
        self.assertIn("(+2 offboarded)", self.cli("threads").stdout)
        ended = [m["task_id"] for m in json.loads(self.cli("milestones", "--since", "30d", "--json").stdout)]
        self.assertEqual(ended, [], "abandoned read tasks are no milestone, and reading them back does not fail")

        r = self.cli("restore", "stale")
        self.assertEqual((r.returncode, r.stdout), (0, f"restored: {stale.name}\n"))
        self.assertTrue((stale / "main.py").is_file())
        self.assertFalse(gone.exists())
        self.assertEqual(self.due_in(0), [], "restoring starts the idle clock again")
        self.assertIn(stale.name, (self.home / "docs/CATALOG.md").read_text())
        self.assertIn("is not offboarded", self.cli("restore", "stale").stderr)
        self.assertEqual(self.cli("restore", "fresh").returncode, 0)
        self.assertEqual(list((self.home / "offboarded").iterdir()), [])
        status = subprocess.run(
            ["git", "-C", str(self.home), "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(status, "", "every move is committed")

    def test_offboarding_by_hand_refuses_what_is_still_alive(self) -> None:
        self.offboard_env(offboard_after_days=0)
        script = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        user = self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertEqual(self.cli("lint", "user").returncode, 0)
        r = self.cli("offboard", "user")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"nothing offboarded:\n  {user.name}: it is not settled (status ready)", r.stderr)
        self.assertEqual(self.cli("run", "user").returncode, 0)
        self.assertEqual(self.cli("handover", "user", "--why", "look again").returncode, 0)
        self.assertIn("a read run of it is waiting in the inbox", self.cli("offboard", "user").stderr)
        self.assertEqual(self.cli("handover", "user", "--withdraw").returncode, 0)
        r = self.cli("offboard", "web1_shared")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"web1_shared: active task {user.name} uses it", r.stderr)
        self.assertEqual(self.cli("offboard").returncode, 2)
        self.assertEqual(self.cli("offboard", "user", "--due").returncode, 2)
        self.assertIn("retention 0", self.cli("offboard", "--due").stderr)
        self.assertEqual(self.due_in(30), [user.name], "the home's retention of 0 only stops the sweep")
        self.backdate(user, 30)
        self.assertNotIn("offboarded", self.cli("lint", "web1_shared").stderr)
        self.assertTrue(user.is_dir(), "retention 0: nothing leaves on its own")

        self.assertEqual(self.cli("offboard", "user").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("used by 0, 1 offboarded; runs 1, 1 ok read)", catalog)
        self.assertIn(f"- used by offboarded tasks: {user.name}", (self.home / "docs/web1_shared.md").read_text())
        scripts = json.loads(self.cli("threads", "--json").stdout)["scripts"]
        self.assertEqual((scripts["web1_shared"]["used_by"], scripts["web1_shared"]["used_by_offboarded"]), ([], 1))
        self.assertTrue(script.is_dir(), "a general script never leaves on its own")
        self.assertEqual(self.cli("offboard", "--due", "--days", "0").stdout, "offboarded 0 idle 0+ days\n")

        self.assertEqual(self.cli("offboard", "web1_shared").returncode, 0)
        self.assertTrue((self.home / "offboarded/general/web1_shared/main.py").is_file())
        self.assertTrue((self.home / "general").is_dir())
        self.assertFalse((self.home / "docs/web1_shared.md").exists())
        r = self.cli("new", "web1", "shared", "--general", "--purpose", "again")
        self.assertEqual(r.returncode, 2)
        self.assertIn("web1_shared exists and is offboarded: `opsquire restore web1_shared`", r.stderr)
        other = self.new("other", "print(1)\n")
        meta = json.loads((other / "meta.json").read_text())
        (other / "meta.json").write_text(json.dumps(meta | {"uses": ["web1_shared"]}))
        errors = json.loads(self.cli("show", "other", "--json").stdout)["meta_errors"]
        self.assertEqual(
            errors,
            [
                "uses 'web1_shared' is not a general script (it is offboarded: `opsquire restore web1_shared` "
                "brings it back)"
            ],
        )

        r = self.cli("restore", "user", "--json")
        self.assertEqual(json.loads(r.stdout)["restored"], ["web1_shared", user.name])
        shown = json.loads(self.cli("show", "user", "--json").stdout)
        self.assertEqual((shown["status"], shown["meta_errors"], shown["offboarded"]), ("done", [], False))
        self.assertEqual(self.cli("lint", "other").returncode, 0)

    def test_a_plan_and_its_steps_leave_together(self) -> None:
        self.offboard_env()
        first = self.new("first", "print(1)\n")
        second = self.new("second", "print(2)\n")
        for step in (first, second):
            self.assertEqual(self.cli("lint", step.name).returncode, 0)
        plan = self.make_plan(first, second)
        self.assertEqual(self.cli("lint", plan.name).returncode, 0)
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        everything = [plan.name, first.name, second.name]
        self.assertEqual(
            self.due_in(8), everything, "the plan first: an interrupted sweep leaves no plan without steps"
        )
        self.assertEqual(self.cli("handover", "second", "--why", "once more").returncode, 0)
        self.assertEqual(self.due_in(8), [], "one step has a run waiting, so the plan and the other step stay too")
        r = self.cli("offboard", "first")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{first.name}: it is a step of plan {plan.name}, which stays", r.stderr)
        r = self.cli("offboard", plan.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{second.name}: a read run of it is waiting in the inbox", r.stderr)
        self.assertTrue(first.is_dir() and plan.is_dir())
        self.assertEqual(self.cli("handover", "second", "--withdraw").returncode, 0)
        r = self.cli("offboard", plan.name, "--json")
        self.assertEqual(json.loads(r.stdout)["offboarded"], everything)
        self.assertEqual(json.loads(self.cli("ls", "--json").stdout), [])
        self.assertFalse(plan.parent.exists(), "an emptied month folder goes too")
        self.assertEqual(self.cli("restore", plan.name).stdout, f"restored: {second.name}, {first.name}, {plan.name}\n")
        self.assertEqual(self.cli("run", plan.name).returncode, 0)

    def test_an_apply_needs_an_ok_dry_run_of_the_same_scripts_and_arguments(self) -> None:
        body = "import sys\n\nimport opsquire_helpers as sq\n\nsq.target('web1', 'ok', ' '.join(sys.argv[1:]))\n"
        folder = self.new("guarded", body, "--changes-prod", "--rollback", "undo", "--no-verify")
        tid = folder.name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        self.hand_over(tid)

        def refused(*args: str) -> str:
            r = self.cli("run", tid, "--apply", *args)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            rec = self.last(tid)
            self.assertEqual(
                (rec["status"], rec["mode"], rec["targets"]["web1"]["status"]), ("blocked", "apply", "skipped")
            )
            return str(rec["last_error"])

        never = refused()
        self.assertIn("it never had a dry run", never)
        self.assertIn(f"`opsquire run {tid}`", never)
        self.assertIn("--skip-dry", never)
        self.assertEqual(self.cli("handover", tid, "--apply").returncode, 2, "nor is it handed over")
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(
            [(i["task_id"], i["next"]) for i in state["inbox"]], [(tid, "dry")], "a refused run ran nothing"
        )

        self.assertEqual(self.cli("run", tid, "--", "one").returncode, 0)
        dry = self.last(tid)
        self.assertIn("had other arguments", refused())
        self.assertIn(f"`opsquire run {tid} -- two`", refused("--", "two"))
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual((item["next"], item["command"]), ("apply", f"opsquire run {tid} --apply -- one"))
        self.assertEqual(self.cli("run", tid, "--apply", "--", "one").returncode, 0)
        self.assertEqual(self.last(tid)["dry"], {"run_id": dry["run_id"]})

        (folder / "main.py").write_text(body + "sq.step('new')\n")
        changed = refused("--", "one")
        self.assertIn("the scripts changed since its last dry run", changed)
        self.assertIn(dry["run_id"], changed)

        self.assertEqual(self.cli("run", tid, "--skip-dry", "why").returncode, 2, "only with --apply")
        self.assertEqual(self.cli("run", tid, "--apply", "--skip-dry", " ").returncode, 2, "the reason is required")
        r = self.cli("run", tid, "--apply", "--skip-dry", "hotfix, the host is down", "--", "one")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.last(tid)
        self.assertEqual((rec["status"], rec["dry"]), ("ok", {"skipped": "hotfix, the host is down"}))
        warned = [e["msg"] for e in self.events(rec) if e.get("level") == "warn"]
        self.assertTrue(any("applying without a dry run" in m and "hotfix" in m for m in warned), warned)

    def test_a_dry_run_that_failed_does_not_cover_an_apply(self) -> None:
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'cannot reach it')\n"
        tid = self.new("shaky", body, "--changes-prod", "--rollback", "undo", "--no-verify").name
        self.assertEqual(self.cli("run", tid).returncode, 1)
        dry = self.last(tid)
        r = self.cli("run", tid, "--apply")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"its dry run {dry['run_id']} of these scripts ended failed", self.last(tid)["last_error"])

    def test_a_waiting_task_can_be_superseded_and_reopened(self) -> None:
        old = self.new("old", "print('old')\n")
        newer = self.new("newer", "print('newer')\n")
        for name in ("old", "newer"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertEqual(self.inbox_ids(), [], "linted, not handed over: still being prepared")
        for name in ("old", "newer"):
            self.hand_over(name)
        self.assertIn(old.name, self.inbox_ids())
        self.assertNotEqual(self.cli("supersede", "old", "--by", "old").returncode, 0)
        r = self.cli("supersede", "old", "--by", "newer", "--note", "newer reads both hosts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(old.name, self.inbox_ids())
        self.assertIn(newer.name, self.inbox_ids())
        r = self.cli("run", "old")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{old.name} was superseded by {newer.name} (test, ", r.stderr)
        self.assertIn("newer reads both hosts", r.stderr)
        self.assertEqual(self.cli("runs", "old", "--json").stdout.strip(), "[]")
        self.assertNotEqual(self.cli("reopen", "old", "--note", " ").returncode, 0)
        r = self.cli("reopen", "old", "--note", "the newer one cannot reach web2")
        self.assertEqual(r.returncode, 0, r.stderr)
        meta = json.loads((old / "meta.json").read_text())
        self.assertNotIn("superseded_by", meta)
        self.assertEqual(meta["reopened"]["was"]["superseded_by"], newer.name)
        self.assertEqual(self.cli("run", "old").returncode, 0)

    def test_a_task_that_never_ran_expires_and_one_that_failed_waits_for_a_person(self) -> None:
        unrun = self.new("unrun", "print(1)\n")
        failed = self.new("failed", "raise SystemExit(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify")
        recent = self.new("recent", "print(1)\n")
        for name in ("unrun", "failed", "recent"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertEqual(self.cli("run", "failed", "--apply", *self.NO_DRY, answers=[("[y/N]", "y")]).returncode, 1)
        self.backdate(unrun, 15)
        self.backdate(failed, 15)
        self.backdate(recent, 4)
        self.hand_over("recent")
        self.age_handover(recent, 4)
        (self.home / "var/offboard.json").unlink()
        r = self.cli("lint", "recent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"abandoned 1 tasks that never ran in 14 days: {unrun.name}", r.stderr)
        self.assertIn(f"(oldest {recent.name}, 4 days)", r.stderr)
        outcome = json.loads((unrun / "meta.json").read_text())["outcome"]
        self.assertEqual((outcome["state"], outcome["closed_by"]), ("abandoned", "opsquire (expiry)"))
        self.assertIsNone(json.loads((failed / "meta.json").read_text()).get("outcome"))
        self.assertIsNone(json.loads((recent / "meta.json").read_text()).get("outcome"))
        self.assertNotIn("need a decision", self.cli("lint", "recent").stderr, "once a day, not on every command")

    def test_the_author_hears_what_became_of_its_tasks(self) -> None:
        self.env["OPSQUIRE_AUTHOR"] = "agent-x (chat)"
        waiting = self.new("waiting", "print(1)\n")
        self.assertEqual(self.cli("lint", "waiting").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = "operator-y"
        r = self.cli("close", "waiting", "--outcome", "abandoned", "--note", "done by hand")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("new", "web1", "unrelated", "--purpose", "by someone else")
        self.assertNotIn("was abandoned", r.stderr, "only the author hears it")
        self.env["OPSQUIRE_AUTHOR"] = "agent-x (chat)"
        r = self.cli("new", "web1", "next", "--purpose", "the next one")
        self.assertIn(f"{waiting.name} was abandoned (operator-y, ", r.stderr)
        self.assertIn("done by hand", r.stderr)
        r = self.cli("wait", "waiting", "--timeout", "30")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("no run is coming", r.stderr)

    def test_a_read_that_failed_comes_back_to_the_inbox_once_it_is_fixed(self) -> None:
        folder = self.new("probe", "raise SystemExit(1)\n")
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        self.assertEqual(self.cli("run", "probe").returncode, 1)
        self.assertIsNone(self.inbox_item(folder.name), "not again with the same scripts")
        (folder / "main.py").write_text("print('fixed')\n")
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        self.assertIsNone(self.inbox_item(folder.name), "fixed, but its agent has not said it is ready")
        self.hand_over("probe", "--changed", "prints fixed instead of failing")
        item = self.inbox_item(folder.name)
        self.assertEqual(item and item["next"], "read")
        self.assertEqual(self.cli("run", "probe").returncode, 0)
        self.assertIsNone(self.inbox_item(folder.name), "a read that ended ok is done")

    def test_a_run_stamped_in_the_future_by_a_clock_jump_does_not_count_as_the_latest(self) -> None:
        folder = self.new("move", "print(1)\n", "--changes-prod", "--rollback", "undo", "--no-verify")
        self.assertEqual(self.cli("lint", "move").returncode, 0)
        self.assertEqual(self.cli("run", "move").returncode, 0)
        first = self.last("move")
        jumped = Path(first["paths"]["dir"]).with_name("20991231T000000000Z_dry")
        shutil.copytree(first["paths"]["dir"], jumped)
        rec = json.loads((jumped / "run.json").read_text())
        rec.update(
            run_id=jumped.name, status="failed", started="2099-12-31T00:00:00.000Z", ended="2000-01-01T00:00:00.000Z"
        )
        (jumped / "run.json").write_text(json.dumps(rec))
        self.assertEqual(self.cli("run", "move").returncode, 0)
        recs = json.loads(self.cli("runs", "move", "--json").stdout)
        self.assertEqual(recs[0]["run_id"], jumped.name, "ordered by when it really ran")
        self.assertIn("system clock moved", recs[0]["clock_note"])
        item = self.inbox_item(folder.name)
        self.assertEqual(item and item["next"], "apply")

    def test_a_held_task_and_its_plans_steps_stay_out_of_the_inbox_until_handed_over(self) -> None:
        one = self.new("one", "print(1)\n", *self.CHANGING)
        two = self.new("two", "print(2)\n", *self.CHANGING)
        for name in ("one", "two"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        plan = self.make_plan(one, two, name="golive")
        self.assertEqual(self.cli("lint", plan.name).returncode, 0)
        self.hand_over(plan.name)
        self.assertIsNotNone(self.inbox_item(plan.name))
        self.assertNotEqual(self.cli("hold", plan.name).returncode, 0, "a hold says why")
        r = self.cli("hold", plan.name, "--why", "go live at 15:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        for t in (plan, one, two):
            self.assertIsNone(self.inbox_item(t.name), t.name)
        self.assertEqual(self.cli("run", plan.name).returncode, 0, "a hold keeps it out of the inbox, not unrunnable")
        self.assertEqual(json.loads(self.cli("show", plan.name, "--json").stdout)["status"], "ready")
        r = self.cli("handover", plan.name, "--apply", "--why", "15:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        item = self.inbox_item(plan.name)
        self.assertEqual(item and item["next"], "apply", "the dry run still covers it: a hold is not a change")
        self.assertEqual(self.cli("hold", one.name, "--why", "later", "--until", "2000-01-01T00:00:00Z").returncode, 0)
        self.assertEqual(self.cli("hold", two.name, "--why", "later").returncode, 0)
        self.assertEqual(self.cli("hold", two.name, "--release").returncode, 0)
        self.assertNotIn("hold", json.loads((two / "meta.json").read_text()))

    def test_a_handover_held_until_later_comes_back_when_the_hold_ends(self) -> None:
        folder = self.new("check", "print(1)\n", *self.CHANGING)
        self.assertEqual(self.cli("lint", "check").returncode, 0)
        self.assertEqual(self.cli("run", "check").returncode, 0)
        self.assertEqual(self.cli("run", "check", "--apply").returncode, 0)
        self.assertIsNone(self.inbox_item(folder.name), "a done task owes no run")
        r = self.cli("handover", "check", "--why", "after the nightly report")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("hold", "check", "--why", "report at 00:55", "--until", "2099-01-01T00:00:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.inbox_item(folder.name))
        self.assertEqual(json.loads((folder / "meta.json").read_text())["handover"]["mode"], "dry")
        r = self.cli("hold", "check", "--why", "report at 00:55", "--until", "2000-01-01T00:00:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        item = self.inbox_item(folder.name)
        self.assertEqual(item and item["next"], "dry", "the handover waits out the hold instead of being dropped")

    def test_new_can_create_a_task_held(self) -> None:
        r = self.cli("new", "web1", "later", "--purpose", "x", "--hold", "after the merge")
        folder = Path(r.stdout.strip())
        self.assertEqual(self.cli("lint", folder.name).returncode, 0)
        self.assertIsNone(self.inbox_item(folder.name))
        self.assertEqual(json.loads((folder / "meta.json").read_text())["hold"]["why"], "after the merge")

    def test_the_stop_hook_reminds_a_session_once_per_edit_of_a_task_it_left_unhanded(self) -> None:
        def hook(session: str = "7c1f2a3b-1111-2222") -> str:
            event = {"session_id": session, "stop_hook_active": False}
            r = subprocess.run(
                [*CLI, "unread", "--hook"], env=self.env, input=json.dumps(event), capture_output=True, text=True
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            return str(json.loads(r.stdout)["reason"]) if r.stdout.strip() else ""

        self.env["OPSQUIRE_AUTHOR"] = "claude-code 7c1f2a3b"
        folder = self.new("draft", "print('one')\n")
        self.assertIn(f"{folder.name} (never handed over)", hook())
        self.assertEqual(hook(), "", "once per edit: an agent waiting on the operator is not stopped every turn")
        self.assertEqual(hook("9d8e7f6a-0000"), "", "another session's task is not this one's")
        (folder / "main.py").write_text("print('two')\n")
        self.assertIn("never handed over", hook(), "an edit is news again")
        self.assertEqual(self.cli("lint", "draft").returncode, 0)
        self.hand_over("draft")
        self.assertEqual(hook(), "")
        self.env["OPSQUIRE_AUTHOR"] = "operator-z"
        self.assertEqual(self.cli("run", "draft").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = "claude-code 7c1f2a3b"
        self.assertEqual(self.cli("log", "draft").returncode, 0)
        (folder / "main.py").write_text("print('three')\n")
        self.assertIn("main.py changed since its last handover or run", hook())
        (folder / "main.py").write_text("print('four')\n")
        self.assertEqual(self.cli("hold", "draft", "--why", "after the merge").returncode, 0)
        self.assertEqual(hook(), "", "a held task waits on purpose")
        later = self.new("dropped", "print('x')\n")
        r = self.cli("close", later.name, "--outcome", "abandoned", "--note", "not needed")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(hook(), "", "a closed task is finished")

    def test_an_agent_reads_the_tasks_last_run_and_its_own_results_before_more_work(self) -> None:
        agent, operator = "claude-code 6d4a8e7e", "operator-z"
        self.env["OPSQUIRE_AUTHOR"] = agent
        folder = self.new("probe", "print('found 3 stale locks')\n")
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        self.assertEqual(self.cli("handover", "probe", "--why", "first look").returncode, 0, "nothing ran yet")
        self.env["OPSQUIRE_AUTHOR"] = operator
        self.assertEqual(self.cli("run", "probe").returncode, 0)
        self.assertEqual(self.last("probe")["handed_by"], agent)
        self.env["OPSQUIRE_AUTHOR"] = agent
        r = self.cli("new", "web1", "next", "--purpose", "more")
        self.assertEqual(r.returncode, 2)
        self.assertIn("runs you handed over finished and you have not read them", r.stderr)
        self.assertEqual(self.cli("unread").returncode, 1)
        hook = subprocess.run(
            [*CLI, "unread", "--hook"],
            env=self.env,
            input=json.dumps({"session_id": "6d4a8e7e-1111-2222", "stop_hook_active": False}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(hook.stdout)["decision"], "block", hook.stderr)
        again = subprocess.run(
            [*CLI, "unread", "--hook"],
            env=self.env,
            input=json.dumps({"session_id": "6d4a8e7e-1111-2222", "stop_hook_active": True}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(again.stdout, "", "once per stop attempt, never a loop")
        self.assertEqual(self.cli("log", "probe").returncode, 0)
        self.assertEqual(self.cli("unread").returncode, 0)
        self.assertEqual(self.cli("new", "web1", "next", "--purpose", "more").returncode, 0)

        self.env["OPSQUIRE_AUTHOR"] = operator
        self.assertEqual(self.cli("run", "probe").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = agent
        r = self.cli("handover", "probe", "--why", "again")
        self.assertEqual(r.returncode, 2)
        self.assertIn("read the task's latest run first", r.stderr)
        self.assertEqual(self.cli("runs", "probe", "--json").returncode, 0)
        self.assertEqual(self.cli("handover", folder.name, "--why", "again").returncode, 0)

    def test_an_agent_that_cannot_write_var_still_records_its_reads(self) -> None:
        """Agent sandboxes deny writes under var/; reading a run must still count, and a read that cannot be recorded
        says so instead of leaving the agent refused for no visible reason."""
        if os.geteuid() == 0:
            self.skipTest("root writes through read-only modes")
        agent, operator = "claude-code 2d39fd02", "operator-z"
        self.env["OPSQUIRE_AUTHOR"] = agent
        self.new("probe", "print('found 3 stale locks')\n")
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = operator
        self.assertEqual(self.cli("run", "probe").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = agent
        var_dirs = [self.home / "var", *(p for p in (self.home / "var").rglob("*") if p.is_dir())]
        try:
            for d in var_dirs:
                d.chmod(0o555)
            r = self.cli("runs", "probe", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.cli("log", self.last("probe")["run_id"]).returncode, 0)
            r = self.cli("handover", "probe", "--why", "again")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((self.home / "var/reads").exists())

            other = "claude-code 7c1e9a40"
            self.env["OPSQUIRE_AUTHOR"] = other
            ledgers = self.home / ".reads"
            ledgers.chmod(0o555)
            try:
                r = self.cli("log", "probe")
            finally:
                ledgers.chmod(0o755)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"could not record that {other} read", r.stderr)
        finally:
            for d in var_dirs:
                d.chmod(0o755)
