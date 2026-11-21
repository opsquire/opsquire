"""opsquire ask: the interview, parallel pieces, budgets and usage limits."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

from support import CLI, ROOT, OpsTestCase, free_port, run_in_pty


class AskTest(OpsTestCase):
    def test_ask_records_actions_and_resumes_by_task(self) -> None:
        self.agents_file()
        r = self.cli("ask", "restart", "web1", "workers")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RUN: opsquire run 20261030-1749_web1_restart-workers --apply", r.stdout)
        self.assertEqual(r.stdout.count("RUN: opsquire run 20261030-1749_web1_restart-workers --apply"), 1)
        self.assertIn("follow up: opsquire ask --resume ", r.stderr)
        self.assertIn("or: opsquire ask --task 20261030-1749_web1_restart-workers", r.stderr)
        self.assertIn("Bash: opsquire find web1 restart", r.stderr)
        self.assertIn(f"Edit(/{self.home}/tasks/**)", r.stdout)
        self.assertIn("author=opsquire-ask/fake", r.stdout)
        recs = [json.loads(p.read_text()) for p in sorted((self.home / "var/asks").glob("*.json"))]
        self.assertEqual(recs[0]["session"], "sess-new")
        self.assertEqual(recs[0]["task_ids"], ["20261030-1749_web1_restart-workers"])
        self.assertEqual([a["kind"] for a in recs[0]["actions"]], ["RUN", "RUN"])

        task = self.new("restart-workers", "print(1)\n")
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        rec["task_ids"] = [task.name]
        rec["session"] = "sess-42"
        (self.home / "var/asks" / "x-later.json").write_text(json.dumps(rec))
        r = self.cli("ask", "--task", "restart-workers", "how", "did", "it", "go")
        self.assertIn('"--resume", "sess-42"', r.stdout)
        r = self.cli("ask", "--task", "restart-workers", "--new", "again")
        self.assertNotIn("--resume", r.stdout)
        self.assertIn(f"This is about opsquire task {task.name}", r.stdout)

    def test_ask_refuses_disabled_and_reports_agent_failure(self) -> None:
        self.agents_file()
        r = self.cli("ask", "--agent", "off", "hi")
        self.assertEqual(r.returncode, 2)
        self.assertIn("disabled: not reviewed", r.stderr)
        self.agents_file(argv=[sys.executable, "-c", "import sys; sys.exit(4)"])
        r = self.cli("ask", "hi")
        self.assertEqual(r.returncode, 1)
        self.assertIn("agent failed (exit 4)", r.stderr)

    def test_an_agent_that_skips_or_fails_the_boundary_check_is_stopped_before_it_changes_anything(self) -> None:
        self.agents_file()
        cases = {
            "skip": "its first step was Read AGENTS.md",
            "echo": "its first step was Bash echo 'boundary kept: trust me'",
            "exposed": "its sandbox does not keep the boundary:\nEXPOSED read /creds",
        }
        for check, why in cases.items():
            self.env["FAKE_BOUNDARY"] = check
            r = self.cli("ask", "build", "it")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(f"stopped the agent before it changed anything: {why}", r.stderr)
            self.assertEqual(list(self.home.glob("tasks/*/*")), [], check)

    def test_the_boundary_check_may_follow_a_piece_briefs_exports(self) -> None:
        from opsquire.ask import boundary_call

        for ok in (
            "opsquire doctor --boundary",
            "export OPSQUIRE_HOME=/w OPSQUIRE_VAR=/v && opsquire doctor --boundary",
            "cd /w; OPSQUIRE_HOME=/w opsquire doctor --boundary",
        ):
            self.assertTrue(boundary_call(ok), ok)
        for bad in ("echo boundary kept", "opsquire doctor --boundary; rm -rf x", "true && opsquire doctor --boundary"):
            self.assertFalse(boundary_call(bad), bad)

    def test_ask_interrupt_then_resume(self) -> None:
        self.agents_file()
        self.env["FAKE_SLEEP"] = "30"
        p = self.start("ask", "do", "something")
        assert p.stderr
        p.stderr.readline()
        time.sleep(1.0)
        os.killpg(p.pid, signal.SIGINT)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        self.assertNotIn("Traceback", err)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual((rec["status"], rec["session"]), ("interrupted", "sess-new"))
        self.assertIn(f"--resume {rec['ask_id']}", err)
        del self.env["FAKE_SLEEP"]
        r = self.cli("ask", "--resume", rec["ask_id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"--resume", "sess-new"', r.stdout)
        r = self.cli("ask", "--resume", "sess-new", "more")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"--resume", "sess-new"', r.stdout)

    def test_grill_asks_one_question_at_a_time_and_enter_accepts(self) -> None:
        self.grill_env(2)
        r = self.cli("ask", "build", "something", "new", answers=[("[Enter =", ""), ("[Enter =", "my own answer")])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("RUN: opsquire run 20261030-1749_web1_restart-workers --apply", r.stdout)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual([x["answer"] for x in rec["rounds"]], ["rec 1", "my own answer"])
        self.assertEqual(rec["status"], "ok")
        seen = json.loads((self.tmp / "fake-state.json").read_text())
        self.assertEqual(seen[1:], ["Answer: rec 1", "Answer: my own answer"])

    def test_questions_are_pinned_below_the_progress_on_a_real_terminal(self) -> None:
        self.grill_env(2)
        self.env["TERM"] = "xterm"
        cmd = [*CLI, "ask", "build", "something", "new"]
        r = run_in_pty(cmd, self.env, [("[Enter = rec 1]", ""), ("[Enter = rec 2]", "mine")], size=(24, 80))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("\x1b[1;21r", r.stdout)
        self.assertEqual(r.stdout.count("\x1b[r"), 2)
        self.assertIn("-> rec 1", r.stdout)
        self.assertIn("RUN: opsquire run 20261030-1749_web1_restart-workers --apply", r.stdout)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual([x["answer"] for x in rec["rounds"]], ["rec 1", "mine"])

    def test_progress_while_a_question_is_pinned_scrolls_above_it(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import pinned

        out = io.StringIO()
        self.assertFalse(pinned.line("nothing pinned"))
        pinned.Pin.out, pinned.Pin.bottom, pinned.Pin.cols = out, 20, 12
        try:
            self.assertTrue(pinned.line("a long progress\nline"))
        finally:
            pinned.Pin.out = None
        self.assertEqual(out.getvalue(), "\x1b7\x1b[20;1H\na long prog\x1b8")
        block = pinned.layout("which host?", "web1 " * 30, 60)
        self.assertEqual(block[-1], "  [Enter = recommended] > ")
        self.assertTrue(all(len(x) < 60 for x in block))

    def test_grill_without_terminal_waits_and_resumes(self) -> None:
        self.grill_env(1)
        p = self.start("ask", "build", "it")
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("waiting on: question 1?", err)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual(rec["status"], "waiting")
        r = self.cli("ask", "--resume", rec["ask_id"], "go", "with", "rec")
        self.assertIn("RUN:", r.stdout)

    def test_grill_ctrl_c_at_question_keeps_session(self) -> None:
        self.grill_env(2)
        r = self.cli("ask", "build", "it", answers=[("[Enter =", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        self.assertNotIn("Traceback", r.stdout)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual((rec["status"], rec["session"]), ("interrupted", "sess-new"))

    def test_no_grill_skips_questions(self) -> None:
        self.grill_env(3)
        r = self.cli("ask", "--no-grill", "build", "it")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RUN:", r.stdout)
        self.assertIn("Do not ask questions", r.stdout)

    def test_piece_open_and_land_puts_a_subagents_task_into_the_home(self) -> None:
        self.git_home()
        self.env["OPSQUIRE_AUTHOR"] = "claude-code 26f643b0"
        env = self.piece_env(self.cli("piece", "open", "alpha", "--json", "--request", "print alpha"))
        self.assertEqual(json.loads(self.cli("piece", "ls", "--json").stdout)[0]["group"], "claude-code-26f643b0")
        self.assertIn("print alpha", self.cli("piece", "ls").stdout)
        tid = self.piece_task(env, "alpha", "print('alpha')\n")
        self.assertEqual(list(self.home.glob(f"tasks/*/{tid}")), [])
        r = self.cli("piece", "land", "alpha")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"{tid}: ready", r.stdout)
        self.assertEqual(json.loads(self.cli("show", tid, "--json").stdout)["status"], "ready")
        self.assertEqual(
            json.loads(self.cli("show", tid, "--json").stdout)["meta"]["author"], "claude-code 26f643b0 piece alpha"
        )
        self.assertFalse((self.home / ".worktrees").exists())
        self.assertEqual(self.git("branch", "--list", "piece/*"), "")
        self.assertEqual(self.git("status", "--short"), "")
        self.assertEqual(self.cli("piece", "ls").stdout.count("\n"), 1)
        r = self.cli("run", tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.last(tid)["status"], "ok")

    def test_piece_clash_is_reported_and_lands_after_the_markers_are_resolved(self) -> None:
        self.git_home()
        r = self.cli("new", "web1", "shared", "--general", "--purpose", "shared", "--lang", "py")
        self.assertEqual(r.returncode, 0, r.stderr)
        shared = Path(r.stdout.strip()) / "main.py"
        self.assertEqual(shared.parent, self.home / "general/web1_shared", r.stdout)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "shared")
        one = self.piece_env(self.cli("piece", "open", "one", "--group", "g", "--json"))
        two = self.piece_env(self.cli("piece", "open", "two", "--group", "g", "--json"))
        (Path(one["OPSQUIRE_HOME"]) / "general/web1_shared/main.py").write_text('"""one."""\n')
        (Path(two["OPSQUIRE_HOME"]) / "general/web1_shared/main.py").write_text('"""two."""\n')
        self.assertEqual(self.cli("piece", "land", "g/one").returncode, 0)
        r = self.cli("piece", "land", "two")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("conflicts in: general/web1_shared/main.py", r.stdout)
        self.assertEqual(shared.read_text(), '"""one."""\n')
        clashed = Path(two["OPSQUIRE_HOME"]) / "general/web1_shared/main.py"
        self.assertIn("<<<<<<<", clashed.read_text())
        r = self.cli("piece", "land", "two")
        self.assertEqual(r.returncode, 1)
        clashed.write_text('"""one and two."""\n')
        r = self.cli("piece", "land", "two")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(shared.read_text(), '"""one and two."""\n')
        self.assertFalse((self.home / ".worktrees").exists())
        self.assertEqual(self.git("status", "--short"), "")

    def test_piece_drop_parks_the_work_and_open_resumes_it(self) -> None:
        self.git_home()
        env = self.piece_env(self.cli("piece", "open", "slow", "--group", "g", "--json", "--request", "a slow piece"))
        tid = self.piece_task(env, "slow", "print('slow')\n")
        r = self.cli("piece", "drop", "slow")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("parked on piece/g/slow", r.stdout)
        self.assertFalse((self.home / ".worktrees").exists())
        self.assertIn("parked", self.cli("piece", "ls").stdout)
        self.assertIn("is parked", self.cli("piece", "land", "slow").stderr)
        env = self.piece_env(self.cli("piece", "open", "slow", "--group", "g", "--json"))
        self.assertEqual(json.loads(self.cli("piece", "ls", "--json").stdout)[0]["request"], "a slow piece")
        self.assertTrue((Path(env["OPSQUIRE_HOME"]) / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/main.py").is_file())
        self.assertEqual(self.cli("piece", "land", "slow").returncode, 0)
        self.assertEqual(json.loads(self.cli("show", tid, "--json").stdout)["status"], "ready")
        env = self.piece_env(self.cli("piece", "open", "gone", "--group", "g", "--json"))
        self.assertIn("discarded", self.cli("piece", "drop", "gone", "--delete").stdout)
        self.assertEqual(self.git("branch", "--list", "piece/*"), "")

    def test_pieces_do_not_nest(self) -> None:
        self.git_home()
        env = self.piece_env(self.cli("piece", "open", "outer", "--group", "g", "--json"))
        r = subprocess.run([*CLI, "piece", "open", "inner"], env=env, capture_output=True, text=True)
        self.assertIn("pieces do not nest", r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already open", self.cli("piece", "open", "outer", "--group", "g").stderr)

    def test_ask_records_a_handover_for_every_run_line(self) -> None:
        self.split("alpha task", "beta task")
        r = self.cli("ask", "two", "things")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        alpha = next(self.home.glob("tasks/*/*_web1_alpha-task"))
        meta = json.loads((alpha / "meta.json").read_text())
        self.assertEqual((meta["handover"]["mode"], meta["handover"]["why"]), ("read", "two things"))
        self.assertTrue(meta["handover"]["by"].startswith("ask "))
        top = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        plan = next(i for i in top if i["steps"])
        inbox = {i["task_id"]: i for i in top + plan["steps"]}
        self.assertEqual((inbox[alpha.name]["next"], inbox[alpha.name]["handed"]["why"]), ("read", "two things"))
        self.assertIn(next(self.home.glob("tasks/*/*_web1_beta-task")).name, inbox)
        self.assertEqual(len(plan["steps"]), 2)
        self.assertEqual([i["task_id"] for i in top], [plan["task_id"]])

    def test_parallel_ask_builds_each_piece_in_its_own_worktree_and_bundles_a_plan(self) -> None:
        log = self.split("alpha-build | alpha task", "beta task")
        r = self.cli("ask", "build", "alpha", "and", "beta")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("split into 2 pieces", r.stderr)
        self.assertIn("[1/2 alpha-build] ready", r.stdout)
        self.assertIn("[2/2 beta] ready", r.stdout)
        runs = re.findall(r"^\s*RUN: opsquire run (\S+)$", r.stdout, re.M)
        self.assertEqual(len(runs), 3, r.stdout)
        self.assertTrue(runs[-1].endswith("_web1_all-alpha-build-beta"), runs[-1])
        self.assertNotIn("--apply", r.stdout)
        plan = json.loads(next(self.home.glob(f"tasks/*/{runs[-1]}/meta.json")).read_text())
        self.assertEqual([s["task"] for s in plan["steps"]], runs[:2])
        for tid in runs:
            self.assertEqual(json.loads(self.cli("show", tid, "--json").stdout)["status"], "ready", tid)
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        cwds = {c["piece"]: c["cwd"] for c in calls if c["piece"]}
        self.assertEqual(set(cwds), {"alpha task", "beta task"})
        self.assertTrue(all("/.worktrees/" in c for c in cwds.values()))
        self.assertFalse((self.home / ".worktrees").exists())
        self.assertEqual(self.git("branch", "--list", "ask/*"), "")
        self.assertEqual(self.git("status", "--short"), "")
        rec = self.asks()[-1]
        self.assertEqual([p["status"] for p in rec["pieces"]], ["merged", "merged"])
        self.assertEqual((rec["status"], rec["plan"]), ("ok", runs[-1]))

    def test_parallel_questions_reach_the_operator_one_at_a_time_with_labels(self) -> None:
        self.split("alpha question", "beta question")
        r = self.cli("ask", "two", "things", answers=[("[Enter = tonight]", ""), ("[Enter = tonight]", "tomorrow")])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertRegex(r.stdout, r"\[\d/2 (alpha|beta)\] which window for")
        pieces = self.asks()[-1]["pieces"]
        self.assertEqual(sorted(p["rounds"][0]["answer"] for p in pieces), ["tomorrow", "tonight"])
        self.assertEqual([p["status"] for p in pieces], ["merged", "merged"])

    def test_parallel_piece_that_changes_only_code_no_task_runs_lands_and_is_linted_in_the_home(self) -> None:
        self.split("alpha task", "beta library")
        r = self.cli("ask", "two", "things")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[2/2 beta-library] ready", r.stdout)
        self.assertEqual([p["status"] for p in self.asks()[-1]["pieces"]], ["merged", "merged"])
        lib = next(self.home.glob("general/*beta-library"))
        self.assertTrue(json.loads((lib / ".lint.json").read_text())["ok"])

    def test_a_piece_that_ends_without_hand_over_lines_is_asked_for_them_once_and_lands(self) -> None:
        log = self.split("alpha task", "beta quiet")
        r = self.cli("ask", "two", "things")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[2/2 beta-quiet] ready", r.stdout)
        self.assertEqual([p["status"] for p in self.asks()[-1]["pieces"]], ["merged", "merged"])
        prompts = [json.loads(x)["prompt"] for x in log.read_text().splitlines() if "quiet" in json.loads(x)["piece"]]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(prompts[1].startswith("Your reply ended without hand-over lines"))

    def test_a_piece_that_drops_its_own_folder_while_resolving_a_clash_still_lands(self) -> None:
        self.split("alpha clash", "beta dropper")
        r = self.cli("ask", "both", "touch", "shared")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([p["status"] for p in self.asks()[-1]["pieces"]], ["merged", "merged"])
        self.assertFalse((self.home / "general/web1_dropped").exists(), r.stdout + r.stderr)

    def test_parallel_merge_clash_is_resolved_by_the_piece_agent(self) -> None:
        self.split("alpha clash", "beta clash")
        r = self.cli("ask", "both", "touch", "shared")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        notes = (self.home / "general/web1_shared/notes.txt").read_text()
        self.assertIn("written by alpha-clash", notes)
        self.assertIn("written by beta-clash", notes)
        self.assertNotIn("<<<<<<<", notes)
        self.assertEqual([p["status"] for p in self.asks()[-1]["pieces"]], ["merged", "merged"])

    def test_parallel_waiting_piece_is_parked_on_its_branch_and_resumes(self) -> None:
        self.split("alpha task", "beta need")
        r = self.cli("ask", "two", "things")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[2/2 beta-need] waiting: the bucket name", r.stdout)
        self.assertIn("continue with: opsquire ask --resume", r.stderr)
        rec = self.asks()[-1]
        self.assertEqual(self.git("branch", "--list", "ask/*").split(), [f"ask/{rec['ask_id']}/2"])
        self.assertFalse((self.home / ".worktrees").exists())
        r = self.cli("ask", "--resume", rec["ask_id"], "ldn1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[2/2 beta-need] ready", r.stdout)
        rec = self.asks()[-1]
        self.assertEqual([p["status"] for p in rec["pieces"]], ["merged", "merged"])
        self.assertEqual(rec["pieces"][1]["rounds"][-1]["answer"], "ldn1")
        self.assertTrue(rec["plan"])
        self.assertEqual(self.git("branch", "--list", "ask/*"), "")

    def test_parallel_ctrl_c_parks_unfinished_pieces_cleanly(self) -> None:
        log = self.split("alpha task", "beta slow")
        self.env["FAKE_SLEEP"] = "60"
        p = self.start("ask", "two", "things")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and "beta slow" not in (log.read_text() if log.exists() else ""):
            time.sleep(0.1)
        time.sleep(1)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130, err)
        self.assertNotIn("Traceback", out + err)
        rec = self.asks()[-1]
        self.assertEqual(rec["status"], "interrupted")
        self.assertEqual(rec["pieces"][1]["status"], "interrupted")
        self.assertIn(f"ask/{rec['ask_id']}/2", self.git("branch", "--list", "ask/*"))
        self.assertFalse((self.home / ".worktrees").exists())

    def test_no_split_and_strict_budget_reach_the_agent(self) -> None:
        self.split("alpha task", "beta task")
        r = self.cli("ask", "--no-split", "--strict-budget", "--budget", "3", "one", "thing")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Parallel work comes first", r.stdout)
        self.assertIn('"--max-budget-usd", "3.00"', r.stdout)
        self.assertNotIn("--max-budget-usd", self.cli("ask", "--no-split", "one", "thing").stdout)

    def test_budget_reminder_is_a_notice_by_default(self) -> None:
        self.split("alpha task", "beta task")
        self.env["FAKE_COST"] = "10"
        r = self.cli("ask", "two", "things")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr.count("past the $15.00 reminder"), 1)
        self.assertIn("$20.01 spent", r.stderr)

    def test_an_ask_records_the_tokens_its_agent_reported(self) -> None:
        self.agents_file()
        tokens = {"input_tokens": 12, "cache_read_input_tokens": 180000, "cache_creation_input_tokens": 6000}
        self.env["FAKE_USAGE"] = json.dumps({"usage": tokens | {"output_tokens": 3000}, "num_turns": 24})
        r = self.cli("ask", "--no-grill", "--no-split", "restart web1 workers")
        self.assertEqual(r.returncode, 0, r.stderr)
        want = {"input": 12, "cache_read": 180000, "cache_write": 6000, "output": 3000, "calls": 24}
        self.assertEqual(json.loads(next((self.home / "var/asks").glob("*.json")).read_text())["usage"], want)
        chat = [
            json.loads(line) for line in next((self.home / "var/asks").glob("*.chat.jsonl")).read_text().splitlines()
        ]
        self.assertEqual(next(c for c in chat if c["kind"] == "agent")["usage"], want)

    def test_the_bundled_agent_is_denied_the_credentials_file_and_var_whatever_its_config_dir(self) -> None:
        from opsquire.ask import build_argv
        from opsquire.model import DATA_DIR

        claude = json.loads((DATA_DIR / "retinue.json").read_text())["agents"]["claude"]
        values = {"prompt": "p", "system": "s", "home": "/h", "main_home": "/h", "creds": "/u/.creds", "var": "/h/var"}
        argv = build_argv(claude, values, None, None)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(settings["sandbox"]["filesystem"], {"denyRead": ["/u/.creds"], "denyWrite": ["/h/var"]})
        self.assertEqual(settings["permissions"]["deny"], ["Read(//u/.creds)", "Edit(//h/var/**)"])

    def test_a_usage_limit_reads_its_reset_as_the_next_such_wall_clock(self) -> None:
        from opsquire.ask import reset_time, usage_limit
        from opsquire.model import DATA_DIR

        at = dt.datetime(2026, 11, 13, 18, 47, tzinfo=dt.UTC)
        ny = "America/New_York"
        self.assertEqual(reset_time("2pm", ny, at), dt.datetime(2026, 11, 13, 19, 0, tzinfo=dt.UTC))
        self.assertEqual(reset_time("2:30 pm", ny, at), dt.datetime(2026, 11, 13, 19, 30, tzinfo=dt.UTC))
        self.assertEqual(reset_time("1pm", ny, at), dt.datetime(2026, 11, 14, 18, 0, tzinfo=dt.UTC), "passed: tomorrow")
        self.assertEqual(reset_time("14:00", "", at), dt.datetime(2026, 11, 14, 14, 0, tzinfo=dt.UTC))
        self.assertIsNone(reset_time("13pm", ny, at))
        self.assertIsNone(reset_time("2pm", "Mars/Olympus", at))
        claude = json.loads((DATA_DIR / "retinue.json").read_text())["agents"]["claude"]
        said = "You've hit your 5-hour limit · resets 2pm (America/New_York)"
        self.assertEqual(usage_limit(claude, said, at), (True, "2026-11-13T19:00:00.000Z"))
        self.assertEqual(usage_limit(claude, "You've hit your weekly limit", at), (True, None))
        self.assertEqual(usage_limit(claude, "API Error: 401 OAuth access token has expired", at), (False, None))
        self.assertEqual(usage_limit({}, said, at), (False, None), "an agent without limit_re never reports one")

    def test_an_ask_that_reaches_the_usage_limit_ends_limited(self) -> None:
        from opsquire.model import DATA_DIR

        self.agents_file(limit_re=json.loads((DATA_DIR / "retinue.json").read_text())["agents"]["claude"]["limit_re"])
        self.env["FAKE_ERROR"] = "You've hit your 5-hour limit · resets 2pm (America/New_York)"
        r = self.cli("ask", "--no-grill", "--no-split", "hello")
        self.assertEqual(r.returncode, 1)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual(rec["status"], "limited")
        self.assertRegex(rec["resets_at"], r"T\d\d:00:00\.000Z$")
        self.assertIn(f"usage limit is reached; it resets at {rec['resets_at']}", r.stderr)
        self.assertIn(f"opsquire ask --resume {rec['ask_id']}", r.stderr)
        chat = [
            json.loads(line) for line in next((self.home / "var/asks").glob("*.chat.jsonl")).read_text().splitlines()
        ]
        self.assertEqual(
            (chat[-1]["kind"], chat[-1]["text"], chat[-1]["resets_at"]), ("end", "limited", rec["resets_at"])
        )

    def test_the_console_starts_each_follow_up_once_and_keeps_only_recent_finished_jobs(self) -> None:
        from opsquire import followup
        from opsquire import jobs as jobs_module
        from opsquire.jobs import KEEP_DONE, Job, Jobs

        jobs = Jobs()
        started: list[str] = []
        wake = ("a1", "The operator ran what you handed over", False)
        with (
            mock.patch.object(followup, "due", return_value=[]),
            mock.patch.object(followup, "continuations", return_value=[wake]),
            mock.patch.object(jobs_module, "load_asks", return_value=[]),
            mock.patch.object(Jobs, "start_ask", side_effect=lambda m, a, fresh=False: started.append(a)),
        ):
            jobs.follow_up()
            jobs.follow_up()
        self.assertEqual(started, ["a1"], "a follow-up that ended without a new answer is not started again")
        for i in range(KEEP_DONE + 5):
            job = Job(f"2026111{i:05d}", "", "ask", "x", args=["q"])
            job.status = "done"
            jobs.add(job)
        self.assertEqual(len(jobs.jobs), KEEP_DONE)
        running = Job("20261119", "", "ask", "x", args=["q"])
        jobs.add(running)
        running.status = "done"
        jobs.prune()
        self.assertEqual(len(jobs.jobs), KEEP_DONE, "a job that finishes later still leaves only KEEP_DONE")

    def test_a_usage_limit_holds_follow_ups_and_continues_each_chat_once_after_the_reset(self) -> None:
        from opsquire import followup

        def ask(ask_id: str, start: str, **extra: Any) -> dict[str, Any]:
            rec = {"ask_id": ask_id, "agent": "claude", "status": "ok", "started": f"2026-11-13T{start}:00.000Z"}
            return rec | {"ended": f"2026-11-13T{start}:30.000Z", "session": f"s-{ask_id}"} | extra

        def run(rid: str, task: str, ended: str) -> dict[str, Any]:
            return {
                "run_id": rid,
                "task_id": task,
                "mode": "read",
                "status": "ok",
                "ended": f"2026-11-13T{ended}:00.000Z",
            }

        first = ask("a1", "18:00", prompt="read t1", actions=[{"kind": "RUN", "text": "opsquire run t1"}])
        stopped = ask("a2", "18:46", copies="a1", status="limited", resets_at="2026-11-13T19:00:00.000Z")
        stopped["prompt"] = "The operator ran what you handed over: t1"
        other = ask("b1", "18:10", actions=[{"kind": "RUN", "text": "opsquire run t2"}])
        runs = {"t1": [run("r1", "t1", "18:48")], "t2": [run("r2", "t2", "18:30")]}
        asks = [first, stopped, other]

        def at(clock: str) -> dt.datetime:
            return dt.datetime.fromisoformat(f"2026-11-13T{clock}+00:00")

        def wakes(clock: str, items: list[dict[str, Any]] = asks) -> tuple[list[Any], list[Any]]:
            held = followup.held_agents(items, at(clock))
            due = followup.due(items, lambda t: runs.get(t, []), set(), set(), at(clock), held)
            return due, followup.continuations(items, lambda t: runs.get(t, []), set(), held, at(clock))

        self.assertEqual(followup.held_agents(asks, at("18:50:00")), {"claude": "2026-11-13T19:00:00.000Z"})
        self.assertEqual(wakes("18:50:00"), ([], []), "the agent is held: b1's finished run waits too")
        self.assertEqual(wakes("19:01:59"), ([], []), "two minutes' margin after the reset")
        due, cont = wakes("19:02:00")
        self.assertEqual(due, [("b1", runs["t2"])])
        split = {**other, "pieces": [{"n": 1, "status": "merged"}]}
        self.assertEqual(wakes("19:02:00", [split])[0], [], "a split ask has no one agent to resume")
        self.assertEqual([(a, fresh) for a, _, fresh in cont], [("a1", False)], "resumes the session before the limit")
        message = cont[0][1]
        self.assertTrue(message.startswith(followup.LIMIT_NOTE), message)
        self.assertIn("The operator ran what you handed over: t1", message)
        self.assertIn("`opsquire run t1` ended ok (run r1)", message, "the runs that ended meanwhile, in the same turn")
        again = ask("a3", "19:03", copies="a1")
        self.assertEqual(wakes("19:10:00", [*asks, again])[1], [], "continued once: a new ask follows the limited one")
        alone = {**stopped, "copies": None, "ask_id": "c1"}
        self.assertEqual([(a, f) for a, _, f in wakes("19:02:00", [alone])[1]], [("c1", True)], "its first turn: fresh")
        unknown = {**stopped, "resets_at": None}
        self.assertEqual(followup.held_agents([first, unknown], at("23:00:00")), {"claude": None})
        self.assertEqual(wakes("23:00:00", [first, unknown]), ([], []), "an unread reset is the operator's to continue")
        repeat = {**stopped, "prompt": message, "started": "2026-11-13T19:03:00.000Z", "ask_id": "a4"}
        repeat |= {"ended": "2026-11-13T19:03:30.000Z", "resets_at": "2026-11-14T00:00:00.000Z"}
        cont = followup.continuations(
            [first, stopped, repeat], lambda t: [], set(), {}, dt.datetime(2026, 11, 14, 0, 5, tzinfo=dt.UTC)
        )
        self.assertEqual(
            cont[0][1].count(followup.LIMIT_NOTE), 1, "a turn that hits the limit again is not wrapped twice"
        )

    def test_the_console_continues_a_chat_stopped_at_the_limit_once_it_resets(self) -> None:
        self.agents_file()
        log = self.tmp / "agent.jsonl"
        self.env["FAKE_LOG"] = str(log)
        asks = self.home / "var/asks"
        asks.mkdir(parents=True)

        def stamp(minutes: float) -> str:
            t = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)
            return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        first: dict[str, Any] = {"ask_id": "a1", "agent": "fake", "status": "ok", "started": stamp(-60)}
        first["ended"] = stamp(-59)
        first |= {"session": "sess-a1", "prompt": "check web1", "actions": []}
        stopped = {"ask_id": "a2", "copies": "a1", "agent": "fake", "status": "limited", "session": "sess-a1"}
        stopped |= {"started": stamp(-20), "ended": stamp(-19), "resets_at": stamp(-10), "prompt": "and web2?"}
        for rec in (first, stopped):
            (asks / f"{rec['ask_id']}.json").write_text(json.dumps(rec))
        proc, call = self.console()
        try:
            deadline = time.time() + 60
            while time.time() < deadline:
                mine = sorted(
                    (a for a in call("/api/state")["asks"] if a["conversation"] == "a1"), key=lambda a: a["started"]
                )
                if len(mine) == 3 and mine[-1]["status"] == "ok":
                    break
                time.sleep(0.3)
            else:
                self.fail(f"the chat was not continued: {mine}")
            time.sleep(3)
            self.assertEqual(len([a for a in call("/api/state")["asks"] if a["conversation"] == "a1"]), 3, "only once")
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertIn("--resume", calls[0]["argv"])
        self.assertEqual(calls[0]["argv"][calls[0]["argv"].index("--resume") + 1], "sess-a1")
        self.assertIn("usage limit before you could answer", calls[0]["prompt"])
        self.assertIn("and web2?", calls[0]["prompt"])

    def test_a_fresh_session_is_briefed_with_the_operators_words_and_the_records(self) -> None:
        from opsquire import followup

        def ask(ask_id: str, start: str, prompt: str, **extra: Any) -> dict[str, Any]:
            return {"ask_id": ask_id, "started": f"2026-11-13T{start}:00.000Z", "prompt": prompt} | extra

        asks = [
            ask("a3", "18:20", "also look at web3", copies="a1", text="old answer"),
            ask("a1", "18:00", "check web1 and web2", actions=[{"kind": "RUN", "text": "opsquire run t1 -- web1"}]),
            ask("a2", "18:10", followup.REPORT_HEAD + "\n- `opsquire run t1` ended ok", copies="a1"),
            ask("a4", "18:30", followup.LIMIT_NOTE + "\n\nx", copies="a1", text="Done: web1 ok\n" + "y" * 7000),
        ]
        text = followup.brief(asks, "NEW MESSAGE")
        self.assertTrue(text.startswith(followup.FRESH_NOTE), text)
        self.assertIn("The operator's request: check web1 and web2", text)
        self.assertIn(
            "What the operator said since, in order:\n- also look at web3\n\n", text, "never the console's notes"
        )
        self.assertIn("handed over (read each: `opsquire show <task>`, its design.md):\n- t1\n", text)
        self.assertIn("Your last answer:\nDone: web1 ok", text)
        self.assertNotIn("old answer", text)
        self.assertLess(len(text), 6500, "a long last answer is cut")
        self.assertTrue(text.endswith("\n\nNow:\nNEW MESSAGE"), text[-60:])

    def test_a_follow_up_after_a_costly_turn_starts_a_fresh_briefed_session(self) -> None:
        self.agents_file()
        log = self.tmp / "agent.jsonl"
        self.env |= {"FAKE_LOG": str(log), "XDG_CONFIG_HOME": str(self.tmp / "cfg")}
        tid = self.new("check", "print(1)\n").name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        asks = self.home / "var/asks"
        asks.mkdir(parents=True)
        at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        first: dict[str, Any] = {
            "ask_id": "a1",
            "agent": "fake",
            "status": "ok",
            "started": at,
            "ended": at,
            "session": "sess-a1",
        }
        first |= {
            "prompt": "check web1",
            "cost_usd": 6.0,
            "text": "Built it.",
            "actions": [{"kind": "RUN", "text": f"opsquire run {tid}"}],
        }
        (asks / "a1.json").write_text(json.dumps(first))
        self.assertEqual(self.cli("run", tid).returncode, 0)
        proc, call = self.console()
        try:
            deadline = time.time() + 60
            while time.time() < deadline:
                mine = [a for a in call("/api/state")["asks"] if a["conversation"] == "a1" and a["ask_id"] != "a1"]
                if mine and mine[0]["status"] == "ok":
                    break
                time.sleep(0.3)
            else:
                self.fail("the chat was not followed up")
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--resume", calls[0]["argv"], "a new agent session")
        sent = calls[0]["prompt"]
        self.assertIn("The operator's request: check web1", sent)
        self.assertIn(f"- {tid}\n", sent)
        self.assertIn("Your last answer:\nBuilt it.", sent)
        self.assertIn(f"`opsquire run {tid}` ended ok", sent)
        self.assertTrue(
            mine[0]["prompt"].startswith("The operator ran what you handed over"), "the chat keeps the short message"
        )

    def test_the_fresh_session_threshold_is_a_setting(self) -> None:
        self.env["XDG_CONFIG_HOME"] = str(self.tmp / "cfg")
        config = self.tmp / "cfg/opsquire/config.json"
        proc, call = self.console()
        try:
            fresh = "follow_up_fresh_after_usd"
            off: dict[str, list[str]] = {"local": [], "dev": [], "staging": []}
            self.assertEqual(
                call("/api/settings"),
                {
                    fresh: 5.0,
                    "time_zone": "UTC",
                    "policy": {"tiers": off, "auto_run": off, "sealed": False},
                    "checkpoint_command": "",
                    "checkpoint_last": None,
                    "editor_url": "vscode://file{path}",
                    "editor_url_default": "vscode://file{path}",
                },
                "on at $5, times in UTC, no tiers, no auto-run, no checkpoint command",
            )
            changed = call("/api/settings", {"policy": {"tiers": {"dev": ["dev-*"]}, "auto_run": {"dev": ["read"]}}})
            self.assertEqual(
                (changed["policy"]["tiers"]["dev"], changed["policy"]["auto_run"]["dev"]), (["dev-*"], ["read"])
            )
            self.assertIn("not one of", call("/api/settings", {"policy": {"auto_run": {"prod": ["read"]}}})["error"])
            self.assertEqual(
                call("/api/settings")["policy"]["auto_run"]["dev"], ["read"], "a refused change changes nothing"
            )
            sent = call("/api/settings", {"checkpoint_command": "sh -c 'cat >> kept.txt'"})["checkpoint_command"]
            self.assertEqual(sent, "sh -c 'cat >> kept.txt'")
            self.assertEqual(json.loads(config.read_text())["checkpoint_command"], ["sh", "-c", "cat >> kept.txt"])
            self.assertEqual(call("/api/settings", {"checkpoint_command": ""})["checkpoint_command"], "")
            self.assertEqual(call("/api/settings", {fresh: 7.5})[fresh], 7.5)
            self.assertEqual(json.loads(config.read_text())[fresh], 7.5)
            self.assertIsNone(call("/api/settings", {fresh: None})[fresh])
            self.assertIs(json.loads(config.read_text())[fresh], False)
            self.assertIn("0 or more", call("/api/settings", {fresh: -1})["error"])
            self.assertEqual(call("/api/settings", {"time_zone": "America/New_York"})["time_zone"], "America/New_York")
            self.assertEqual(json.loads(config.read_text())["time_zone"], "America/New_York")
            self.assertIn("zone name", call("/api/settings", {"time_zone": "Mars/Olympus"})["error"])
            self.assertEqual(call("/api/settings")["time_zone"], "America/New_York", "a bad name changes nothing")
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)

    def test_an_ask_where_var_is_not_writable_says_so(self) -> None:
        self.agents_file()
        var = self.home / "var"
        var.mkdir()
        var.chmod(0o555)
        try:
            r = self.cli("ask", "--no-grill", "--no-split", "hello")
        finally:
            var.chmod(0o755)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn(f"cannot start an ask here: {var / 'asks'} is not writable", r.stderr)

    def test_a_failed_agent_says_so_and_how_to_continue(self) -> None:
        self.agents_file()
        self.env["FAKE_ERROR"] = "You've hit your limit"
        r = self.cli("ask", "--no-grill", "--no-split", "hello")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip(), "You've hit your limit")
        ask_id = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())["ask_id"]
        self.assertIn(
            f"the agent failed (exit 1); once what it says above is fixed, run opsquire ask --resume {ask_id}", r.stderr
        )
        self.assertNotIn("follow up:", r.stderr)

    def test_an_ask_from_the_console_is_a_chat_with_its_interview_and_its_runs(self) -> None:
        self.agents_file()
        self.env |= {"FAKE_GRILL": "1", "FAKE_STATE": str(self.tmp / "fake-state.json")}
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            def job_until(job: str, done: Any) -> dict[str, Any]:
                deadline = time.time() + 60
                while time.time() < deadline:
                    j: dict[str, Any] = call("GET", f"/api/job?id={job}&offset=0")[1]
                    if done(j):
                        return j
                    time.sleep(0.25)
                self.fail(f"job {job}: {j}")

            self.assertEqual(call("POST", "/api/ask", {"request": " "})[0], 400)
            status, started = call("POST", "/api/ask", {"request": "restart web1 workers"})
            self.assertEqual(status, 200, started)
            waiting = job_until(started["job_id"], lambda j: j["status"] == "waiting")
            self.assertTrue(waiting["ask_id"])
            self.assertEqual(call("POST", "/api/answer", {"job": started["job_id"], "text": ""})[0], 200)
            done = job_until(started["job_id"], lambda j: j["status"] == "done")
            self.assertEqual(done["exit_code"], 0, done["text"])
            chat = call("GET", f"/api/askchat?id={waiting['ask_id']}&offset=0")[1]["items"]
            kinds = [c["kind"] for c in chat]
            self.assertEqual(kinds[0], "you")
            self.assertEqual(chat[0]["text"], "restart web1 workers")
            question = next(c for c in chat if c["kind"] == "question")
            self.assertEqual((question["text"], question["recommend"]), ("question 1?", "rec 1"))
            self.assertIn({"kind": "you", "text": "rec 1"}, [{"kind": c["kind"], "text": c["text"]} for c in chat])
            reply = next(c for c in chat if c["kind"] == "agent")
            self.assertEqual([a["kind"] for a in reply["actions"]], ["RUN", "RUN"])
            self.assertEqual(kinds[-1], "end")
            self.assertEqual(chat[-1]["text"], "ok")
            status, again = call("POST", "/api/ask", {"request": "and the web2 workers?", "resume": waiting["ask_id"]})
            self.assertEqual(status, 200, again)
            job_until(again["job_id"], lambda j: j["status"] in ("waiting", "done"))
            follow = next(a for a in call("GET", "/api/state")[1]["asks"] if a["prompt"] == "and the web2 workers?")
            self.assertNotEqual(follow["ask_id"], waiting["ask_id"])
            self.assertEqual(follow["conversation"], waiting["ask_id"])
            whole = call("GET", f"/api/askchat?id={follow['ask_id']}&offset=0")[1]["items"]
            self.assertEqual(whole[0]["text"], "restart web1 workers")
            self.assertIn("and the web2 workers?", [c["text"] for c in whole if c["kind"] == "you"])
            self.assertEqual(call("GET", "/api/askchat?id=../x&offset=0")[0], 400)
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(20)

    def test_the_console_resumes_a_chat_once_the_run_it_handed_over_finishes(self) -> None:
        self.agents_file()
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'looked')\n"
        folder = self.new("restart-workers", body, *self.CHANGING)
        self.assertEqual(self.cli("lint", "restart-workers").returncode, 0)
        self.env["FAKE_TASK"] = folder.name
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            def until(what: Any, label: str) -> Any:
                deadline = time.time() + 60
                while time.time() < deadline:
                    found = what()
                    if found:
                        return found
                    time.sleep(0.25)
                self.fail(label)

            def asks() -> list[dict[str, Any]]:
                found: list[dict[str, Any]] = call("GET", "/api/state")[1]["asks"]
                return sorted(found, key=lambda a: str(a["started"]))

            def idle() -> bool:
                return all(j["status"] == "done" for j in call("GET", "/api/state")[1]["jobs"])

            status, started = call("POST", "/api/ask", {"request": "restart web1 workers"})
            self.assertEqual(status, 200, started)
            until(lambda: asks() and asks()[0]["status"] == "ok" and idle(), "the ask ends")
            first = asks()[0]
            self.assertEqual(call("POST", "/api/run", {"task": folder.name, "mode": "dry", "expect": "x"})[0], 400)
            status, refused = call("POST", "/api/run", {"task": folder.name, "mode": "dry", "expect": ["web2"]})
            self.assertEqual(status, 400, refused)
            self.assertIn(f"run it in a terminal: opsquire run {folder.name} -- web2", refused["error"])
            status, run = call("POST", "/api/run", {"task": folder.name, "mode": "dry", "expect": []})
            self.assertEqual(status, 200, run)
            follow = until(lambda: len(asks()) > 1 and asks()[1]["status"] == "ok" and idle() and asks()[1], "resumed")
            self.assertEqual(follow["conversation"], first["ask_id"])
            dry = self.last(folder.name)
            self.assertEqual((dry["mode"], dry["status"], dry["origin"]), ("dry", "ok", "console"))
            self.assertIn(f"`opsquire run {folder.name}` ended ok (run {dry['run_id']})", follow["prompt"])
            chat = call("GET", f"/api/askchat?id={first['ask_id']}&offset=0")[1]["items"]
            self.assertIn(follow["prompt"], [c["text"] for c in chat if c["kind"] == "you"])
            time.sleep(3)
            self.assertEqual(len(asks()), 2, "a run is reported to its chat once")
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(20)

    def test_a_chat_is_resumed_only_for_runs_it_has_not_seen(self) -> None:
        from opsquire import followup

        at = dt.datetime(2026, 11, 13, 16, 0, tzinfo=dt.UTC)
        run_line = [{"kind": "RUN", "text": "opsquire run t1 --apply -- build"}, {"kind": "PR", "text": "x"}]
        first: dict[str, Any] = {"ask_id": "a1", "status": "ok", "started": "2026-11-13T15:50:00.000Z"}
        first |= {"ended": "2026-11-13T15:54:00.000Z", "actions": run_line}
        run: dict[str, Any] = {"run_id": "r1", "task_id": "t1", "mode": "apply", "argv": ["build"], "status": "failed"}
        run |= {
            "started": "2026-11-13T15:55:00.000Z",
            "ended": "2026-11-13T15:56:00.000Z",
            "last_error": "ssh: timeout",
        }
        runs = {"t1": [run]}

        def due(asks: list[dict[str, Any]], convs: tuple[str, ...] = (), tasks: tuple[str, ...] = ()) -> list[Any]:
            return followup.due(asks, lambda t: runs.get(t, []), set(convs), set(tasks), at)

        self.assertEqual(due([first]), [("a1", [run])])
        text = followup.report([run])
        self.assertIn("`opsquire run t1 --apply -- build` ended failed (run r1): ssh: timeout", text)
        self.assertEqual(due([first], convs=("a1",)), [], "its agent is answering")
        self.assertEqual(due([first], tasks=("t1",)), [], "a job of the task still goes (a walk between stages)")
        self.assertEqual(due([{**first, "status": "waiting"}]), [], "it waits for the operator")
        self.assertEqual(due([{**first, "ended": "2026-11-09T15:54:00.000Z"}]), [], "too old to wake")
        self.assertEqual(due([{**first, "actions": []}]), [], "it handed nothing over")
        runs["t1"] = [{**run, "ended": "2026-11-13T15:53:00.000Z"}]
        self.assertEqual(due([first]), [], "the agent saw a run that ended before it answered")
        runs["t1"] = [run, {**run, "run_id": "r2", "status": "running", "ended": None}]
        self.assertEqual(due([first]), [], "wait for the run still going")
        runs["t1"] = [run]
        later: dict[str, Any] = {"ask_id": "a2", "copies": "a1", "status": "ok", "started": "2026-11-13T15:57:00.000Z"}
        later |= {"ended": "2026-11-13T15:58:00.000Z", "actions": []}
        self.assertEqual(due([later, first]), [], "the resumed agent read it")
        self.assertEqual(due([{**later, "ended": "2026-11-13T15:55:30.000Z"}, first]), [("a2", [run])])

    def test_an_ask_runs_on_the_chosen_model_and_says_how_it_was_billed(self) -> None:
        tiers = {t: {"id": f"model-{t}", "label": t.title()} for t in ("quick", "default", "deep")}
        self.agents_file(model_argv=["--model", "{model}"], models=tiers, billing={"key_env": "FAKE_PROVIDER_KEY"})
        log = self.tmp / "fake.log"
        self.env["FAKE_LOG"] = str(log)
        self.env.pop("FAKE_PROVIDER_KEY", None)
        self.assertEqual(self.cli("ask", "--model", "quick", "restart", "web1").returncode, 0)
        self.assertEqual(self.cli("ask", "restart", "web1", "again").returncode, 0)
        self.env["FAKE_PROVIDER_KEY"] = "test-key-not-real"
        self.assertEqual(self.cli("ask", "--model", "some-model-id", "one", "more").returncode, 0)
        argvs = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
        models = [a[a.index("--model") + 1] for a in argvs]
        self.assertEqual(models, ["model-quick", "model-default", "some-model-id"])
        recs = sorted(
            (json.loads(p.read_text()) for p in (self.home / "var/asks").glob("*_fake.json")),
            key=lambda r: r["started"],
        )
        self.assertEqual(
            [(r["model"], r["billing"]) for r in recs],
            [("model-quick", "plan"), ("model-default", "plan"), ("some-model-id", "api-key")],
        )
        chat = (self.home / "var/asks" / f"{recs[0]['ask_id']}.chat.jsonl").read_text().splitlines()
        reply = next(json.loads(c) for c in chat if json.loads(c)["kind"] == "agent")
        self.assertEqual((reply["model"], reply["billing"], reply["agent"]), ("model-quick", "plan", "fake"))

    def test_an_agent_without_tiers_or_billing_runs_on_its_own_default(self) -> None:
        self.agents_file()
        log = self.tmp / "fake.log"
        self.env["FAKE_LOG"] = str(log)
        self.assertEqual(self.cli("ask", "--model", "deep", "restart", "web1").returncode, 0)
        argv = json.loads(log.read_text().splitlines()[-1])["argv"]
        self.assertNotIn("--model", argv)
        rec = json.loads(next((self.home / "var/asks").glob("*_fake.json")).read_text())
        self.assertEqual((rec["model"], rec["billing"], rec["agent"]), ("", "", "fake"))

    def test_an_ask_job_waits_while_its_chat_ends_on_an_unanswered_approval(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import jobs

        with mock.patch.dict(os.environ, self.env):
            job = jobs.Job("j1", "", "ask", "2026-11-18T20:24:10.634Z", args=["library upkeep"], ask_id="a1")
            job.status = "running"
            chat = self.home / "var/asks/a1.chat.jsonl"
            chat.parent.mkdir(parents=True)

            def say(*items: dict[str, str]) -> dict[str, Any]:
                with chat.open("a") as f:
                    f.writelines(json.dumps(i) + "\n" for i in items)
                return job.view()

            self.assertEqual(say({"kind": "you", "text": "go"})["status"], "running")
            view = say({"kind": "approve", "text": "shell: ls"}, {"kind": "step", "text": "another piece works on"})
            self.assertEqual((view["status"], view["prompt"]), ("waiting", "Allow shell: ls?"), "steps after it")
            self.assertEqual(say({"kind": "you", "text": "allow"})["status"], "running")
            self.assertEqual(say({"kind": "question", "text": "Which window?"})["prompt"], "Which window?")
            job.status = "done"
            self.assertEqual(job.view()["status"], "done", "a finished job waits on nothing")
