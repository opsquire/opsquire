"""The runner: modes, records, credentials, guards and errors."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import ASKING_BODY, BASH_HEADER, ROOT, OpsTestCase


class RunnerTest(OpsTestCase):
    def test_read_only_ok_with_redaction(self) -> None:
        self.new(
            "read-ok",
            "import os\n\nimport opsquire_helpers as sq\n\n"
            "print('token', os.environ['TOKEN'])\nsq.target('web1', 'ok', 'fine')\n",
            "--cred",
            "TOKEN=metrics.prod.token",
        )
        r = self.cli("run", "read-ok")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("SECRET-TOKEN-123456", r.stdout + r.stderr)
        rec = self.last("read-ok")
        self.assertEqual((rec["status"], rec["mode"], rec["targets"]["web1"]["status"]), ("ok", "read", "ok"))
        self.assertEqual(rec["redactions"], 1)
        self.assertEqual(rec["credentials_used"], ["TOKEN"])
        log = Path(rec["paths"]["output"]).read_text()
        self.assertIn("[REDACTED:metrics.prod.token]", log)
        self.assertNotIn("SECRET-TOKEN-123456", log + Path(rec["paths"]["events"]).read_text())
        self.assertTrue(Path(rec["paths"]["snapshot"]).is_dir())
        self.assertEqual(json.loads(self.cli("ls", "--json").stdout)[0]["status"], "done")

    def test_prod_task_dry_apply_and_abort(self) -> None:
        self.new(
            "restart",
            ASKING_BODY,
            "--changes-prod",
            "--rollback",
            "restart again",
        )
        self.assertEqual(self.cli("run", "restart").returncode, 0)
        self.assertEqual(self.last("restart")["mode"], "dry")
        self.assertEqual(json.loads(self.cli("show", "restart", "--json").stdout)["status"], "ready")

        r = self.cli("run", "restart", "--apply", answers=[("[y/N]", "n")])
        self.assertEqual(r.returncode, 3, r.stdout)
        rec = self.last("restart")
        self.assertEqual((rec["status"], rec["targets"]["web1"]["status"]), ("aborted", "skipped"))

        r = self.cli("run", "restart", "--apply", answers=[("[y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.last("restart")["status"], "ok")
        self.assertEqual(json.loads(self.cli("show", "restart", "--json").stdout)["status"], "done")

    def test_partial_multi_host(self) -> None:
        body = (
            "import sys\n\nimport opsquire_helpers as sq\n\n"
            "sq.target('db1', 'ok')\nsq.target('db2', 'failed', 'supervisor refused')\nsys.exit(1)\n"
        )
        self.new("multi", body, "--target", "db1", "--target", "db2", "--target", "db3", area="db")
        r = self.cli("run", "multi")
        self.assertEqual(r.returncode, 1)
        rec = self.last("multi")
        self.assertEqual(rec["status"], "partial")
        self.assertEqual(
            {k: v["status"] for k, v in rec["targets"].items()}, {"db1": "ok", "db2": "failed", "db3": "skipped"}
        )
        self.assertEqual(rec["last_error"], "supervisor refused")

    def test_crash_is_failed(self) -> None:
        self.new("crash", "raise RuntimeError('boom')\n")
        self.assertEqual(self.cli("run", "crash").returncode, 1)
        rec = self.last("crash")
        self.assertEqual((rec["status"], rec["targets"]["web1"]["status"]), ("failed", "failed"))
        self.assertIn("RuntimeError: boom", Path(rec["paths"]["output"]).read_text())

    def test_missing_credential_blocks(self) -> None:
        self.new("nocred", "print('hi')\n", "--cred", "X=metrics.prod.missing")
        self.assertEqual(self.cli("run", "nocred").returncode, 2)
        self.assertIn("metrics.prod.missing", self.last("nocred")["last_error"])

    def test_bash_task_and_remote_payload_pattern(self) -> None:
        body = (
            BASH_HEADER + 'sq_step "local driver"\n'
            "bash -c 'python3 -u -' <<'PY'\n"
            "import json\n"
            "print('remote says hi')\n"
            "event = {'event': 'target', 'target': 'web1', 'status': 'unchanged', 'msg': 'already running'}\n"
            "print('@@OPSQUIRE ' + json.dumps(event))\n"
            "PY\n"
        )
        self.new("bashremote", body, lang="sh")
        r = self.cli("run", "bashremote")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.last("bashremote")
        self.assertEqual((rec["status"], rec["targets"]["web1"]["status"]), ("ok", "unchanged"))
        self.assertIn("remote says hi", Path(rec["paths"]["output"]).read_text())

    def test_guards(self) -> None:
        self.new("ro", "print(1)\n")
        self.assertIn("read-only", self.cli("run", "ro", "--apply").stderr)
        r = self.cli("new", "Bad_Area", "x", "--purpose", "p")
        self.assertEqual(r.returncode, 2)
        self.assertIn("kebab-case", r.stderr)
        g = self.cli("new", "supervisor", "restart-stack", "--general", "--purpose", "reusable")
        self.assertEqual(g.returncode, 0, g.stderr)
        self.assertIn("not run directly", self.cli("run", "supervisor_restart-stack").stderr)
        folder = Path(
            self.cli(
                "new", "web1", "uses-general", "--purpose", "p", "--uses", "supervisor_restart-stack"
            ).stdout.strip()
        )
        self.assertEqual(self.cli("run", "uses-general").returncode, 0)
        files = self.last("uses-general")["files"]
        self.assertTrue(any(f.startswith("general/supervisor_restart-stack/") for f in files), files)
        meta = json.loads((folder / "meta.json").read_text())
        meta["id"] = "renamed"
        (folder / "meta.json").write_text(json.dumps(meta))
        shown = json.loads(self.cli("show", "uses-general", "--json").stdout)
        self.assertEqual(shown["status"], "done", "it ran ok; a later meta error does not undo that")
        self.assertIn("differs from folder name", " ".join(shown["meta_errors"]))
        self.assertEqual(self.cli("run", "uses-general").returncode, 2)
        never_ran = self.new("never-ran", "print(1)\n")
        self.assertEqual(self.cli("lint", "never-ran").returncode, 0)
        (never_ran / "meta.json").write_text(
            json.dumps(json.loads((never_ran / "meta.json").read_text()) | {"id": "x"})
        )
        self.assertEqual(json.loads(self.cli("show", "never-ran", "--json").stdout)["status"], "draft")

    def test_a_run_executes_its_own_copy_of_the_checked_files(self) -> None:
        body = "from pathlib import Path\n\nimport opsquire_helpers as sq\n\nsq.result(str(Path(__file__).resolve()))\n"
        folder = self.new("copy", body)
        (folder / "test_main.py").write_text(
            "from pathlib import Path\n\n\ndef test_edits_the_script_it_checks() -> None:\n"
            "    main = Path(__file__).with_name('main.py')\n"
            "    main.write_text(main.read_text() + \"print('EDITED')\\n\")\n"
        )
        r = self.cli("run", folder.name)
        rec = self.last(folder.name)
        self.assertNotIn("EDITED", Path(rec["paths"]["output"]).read_text(), "an edit during lint never runs")
        if rec["status"] == "ok":
            ran = rec["results"][0]
            self.assertTrue(ran.startswith(rec["paths"]["dir"]) and not ran.startswith(rec["paths"]["snapshot"]), ran)
            self.assertFalse(Path(ran).exists(), "the private copy goes when the run ends")
        else:
            self.assertEqual((r.returncode, rec["status"]), (2, "blocked"), r.stderr)

    def test_a_run_refuses_general_scripts_its_scripts_name_without_uses(self) -> None:
        lib = Path(self.cli("new", "web1", "lib", "--general", "--purpose", "shared").stdout.strip())
        (lib / "helper.py").write_text("WORD = 'from-lib'\n")
        body = (
            "import os\nimport sys\n\nimport opsquire_helpers as sq\n\n"
            f"sys.path.insert(0, os.path.join(os.environ['OPSQUIRE_GENERAL'], {lib.name!r}))\n"
            "import helper  # noqa: E402\n\nsq.result(helper.WORD)\n"
        )
        folder = self.new("undeclared", body)
        (folder / "design.md").write_text(f"Prose may name {lib.name} freely.\n")
        lint = self.cli("lint", folder.name)
        self.assertEqual(lint.returncode, 1, lint.stdout)
        self.assertIn(f"general scripts that no `uses` lists: {lib.name}", lint.stdout)
        r = self.cli("run", folder.name)
        rec = self.last(folder.name)
        self.assertEqual((r.returncode, rec["status"], rec["results"]), (2, "blocked", []), r.stdout + r.stderr)
        self.assertIn(lib.name, rec["last_error"])
        self.assertIn("Add them to `uses` in meta.json", rec["last_error"])
        wrapper = Path(
            self.cli("new", "web1", "wrapper", "--general", "--purpose", "calls lib", "--uses", lib.name).stdout.strip()
        )
        self.assertEqual(json.loads((wrapper / "meta.json").read_text())["uses"], [lib.name])
        meta = json.loads((folder / "meta.json").read_text())
        (folder / "meta.json").write_text(json.dumps(meta | {"uses": [wrapper.name]}))
        r = self.cli("run", folder.name)
        rec = self.last(folder.name)
        self.assertEqual((r.returncode, rec["results"]), (0, ["from-lib"]), r.stdout + r.stderr)
        self.assertIn(f"general/{lib.name}/helper.py", rec["files"], "a used script's own uses are hashed and run")

    def test_sq_use_loads_a_used_general_once_with_its_own_imports_in_runs_and_tests(self) -> None:
        lib = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared").stdout.strip())
        (lib / "words.py").write_text("WORD = 'from-sibling'\n")
        lib_main = "import words\n\n\ndef word() -> str:\n    return words.WORD\n"
        (lib / "main.py").write_text(lib_main)
        (lib / "test_main.py").unlink(missing_ok=True)
        body = (
            "import opsquire_helpers as sq\n\n\ndef main() -> None:\n"
            f"    shared = sq.use({lib.name!r})\n"
            f"    assert sq.use({lib.name!r}) is shared\n"
            f"    words = sq.use({lib.name!r}, 'words')\n"
            "    sq.result(f'{shared.word()} {words.WORD}')\n\n\n"
            "if __name__ == '__main__':\n    sq.run(main)\n"
        )
        folder = self.new("user", body, "--uses", lib.name)
        (folder / "test_main.py").write_text(
            "import unittest\n\nimport opsquire_helpers as sq\n\n\nclass T(unittest.TestCase):\n"
            f"    def test_use(self) -> None:\n        self.assertEqual(sq.use({lib.name!r}).word(), 'from-sibling')\n"
        )
        r = self.cli("run", folder.name)
        rec = self.last(folder.name)
        self.assertEqual((r.returncode, rec["results"]), (0, ["from-sibling from-sibling"]), r.stdout + r.stderr)
        self.assertEqual(rec["tests"], 1, "the task's own test reaches the general through sq.use too")

        other = self.new("other", f"import opsquire_helpers as sq\n\nsq.use({'web1_missing'!r})\n")
        r = self.cli("run", other.name)
        self.assertEqual(r.returncode, 1, r.stdout)
        log = Path(self.last(other.name)["paths"]["output"]).read_text()
        self.assertIn("web1_missing/main.py is not in this run: list web1_missing in the task's uses", log)

    def test_a_run_keeps_its_files_modes_even_from_a_pre_run(self) -> None:
        body = "import subprocess\n\nsubprocess.run(['./helper.sh'], check=True)\n"
        folder = self.new("exec-helper", body, *self.CHANGING)
        helper = folder / "helper.sh"
        helper.write_text("#!/usr/bin/env bash\necho HELPER_EXECUTED\n")
        helper.chmod(0o755)
        name = str(helper.relative_to(self.home))
        r = self.cli("run", folder.name)
        rec = self.last(folder.name)
        self.assertEqual((r.returncode, rec["modes"][name]), (0, "0755"), r.stdout + r.stderr)
        self.assertIn("HELPER_EXECUTED", r.stdout)
        plan = self.make_plan(folder, name="modes")
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        helper.chmod(0o644)
        r = self.cli("run", plan.name, "--apply")
        rec = self.last(folder.name)
        self.assertEqual((r.returncode, rec["mode"], rec["modes"][name]), (0, "apply", "0755"), r.stdout + r.stderr)
        self.assertIsNotNone(rec["frozen"])
        self.assertIn("HELPER_EXECUTED", Path(rec["paths"]["output"]).read_text())

    def test_archived_is_listed_but_never_runs_or_lints(self) -> None:
        body = "import os\nos.system('touch should-not-run')\n"
        folder = self.new("legacy", body, "--changes-prod", "--rollback", "x")
        meta = json.loads((folder / "meta.json").read_text())
        meta["archived"] = {"note": "ran before ops", "history": [{"when": "2026-10-23", "by": "codex"}]}
        (folder / "meta.json").write_text(json.dumps(meta))
        self.assertEqual(json.loads(self.cli("ls", "--status", "archived", "--json").stdout)[0]["id"], folder.name)
        r = self.cli("run", "legacy", "--apply")
        self.assertEqual(r.returncode, 2)
        self.assertIn("archived", r.stderr)
        self.assertFalse((folder / "should-not-run").exists())
        self.assertEqual(self.cli("lint", "legacy").returncode, 1)
        meta["archived"] = {"note": ""}
        (folder / "meta.json").write_text(json.dumps(meta))
        errors = json.loads(self.cli("show", "legacy", "--json").stdout)["meta_errors"]
        self.assertIn("archived must be", " ".join(errors))

    def test_unreadable_creds_file_blocks_only_tasks_that_declare_credentials(self) -> None:
        self.creds.chmod(0o000)
        self.new("no-creds", "import opsquire_helpers as sq\n\nsq.result('fine')\n")
        r = self.cli("run", "no-creds")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.last("no-creds")
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["counts"]["warn"], 1)
        self.assertIn("not readable", r.stdout + r.stderr)
        self.new("with-creds", "print('x')\n", "--cred", "TOKEN=metrics.prod.token")
        r = self.cli("run", "with-creds")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.last("with-creds")["status"], "blocked")
        self.assertIn("cannot read", self.last("with-creds")["last_error"])

    def test_run_records_usage_and_state_shows_agent_work(self) -> None:
        tid = self.new("usage", "import opsquire_helpers as sq\n\nsq.result('x' * 10)\n").name
        self.assertEqual(self.cli("run", tid).returncode, 0)
        rec = self.last(tid)
        self.assertGreater(rec["usage"]["max_rss_mb"], 1)
        self.assertGreaterEqual(rec["usage"]["cpu_s"], 0)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["runs"][rec["run_id"]]["usage"], rec["usage"])
        self.assertTrue(state["tasks"][tid]["settled"])
        self.assertEqual(state["tasks"][tid]["agents"]["asks"], 0)
        self.assertGreater(state["tasks"][tid]["agents"]["operator_s"], 0)
        self.agents_file()
        r = self.cli("ask", "--no-grill", "restart", "web1")
        self.assertEqual(r.returncode, 0, r.stderr)
        ask = self.asks()[-1]
        made = self.new("after-ask", "print(1)\n").name
        ask["task_ids"] = [made]
        ask["ended"] = "2999-01-01T00:00:00.000Z"
        ask["cost_usd"] = 1.5
        ask["pieces"] = [{"name": "a", "status": "merged"}, {"name": "b", "status": "merged"}]
        Path(self.home / "var/asks" / "patched.json").write_text(json.dumps(ask))
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["tasks"][made]["agents"], {"asks": 1, "pieces": 2, "cost_usd": 1.5, "operator_s": 0.0})

    def test_threads_writing_one_file_never_share_a_temporary_file(self) -> None:
        from opsquire.model import write_json

        target, errors, start = self.tmp / "claude.mcp.json", [], threading.Barrier(8)

        def piece() -> None:
            start.wait()
            for _ in range(100):
                try:
                    write_json(target, {"mcpServers": {}})
                except OSError as e:
                    errors.append(repr(e))

        threads = [threading.Thread(target=piece) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], "the pieces of a split ask write one approvals config at once")
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])

    def test_a_recorded_end_survives_a_renamed_general_script(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        made = {}
        for name in ("replaced", "dropped", "ran", "waiting"):
            made[name] = self.new(name, "print(1)\n", "--uses", "web1_shared")
            self.assertEqual(self.cli("lint", name).returncode, 0)
        meta = json.loads((made["replaced"] / "meta.json").read_text())
        (made["replaced"] / "meta.json").write_text(json.dumps(meta | {"superseded_by": made["ran"].name}))
        self.assertEqual(self.cli("close", "dropped", "--outcome", "abandoned", "--note", "not needed").returncode, 0)
        self.assertEqual(self.cli("run", "ran").returncode, 0)
        renamed = g.with_name("web1_common")
        g.rename(renamed)
        (renamed / "meta.json").write_text(
            json.dumps(json.loads((renamed / "meta.json").read_text()) | {"id": "web1_common"})
        )
        listed = {t["id"].split("_", 2)[-1]: t for t in json.loads(self.cli("ls", "--json").stdout)}
        self.assertIn("uses 'web1_shared' is not a general script", listed["replaced"]["meta_errors"])
        self.assertEqual(
            {name: listed[name]["status"] for name in made},
            {"replaced": "superseded", "dropped": "abandoned", "ran": "done", "waiting": "draft"},
        )
        st = json.loads(self.cli("threads", "--json").stdout)
        views = {name: st["tasks"][folder.name] for name, folder in made.items()}
        self.assertEqual(
            {name: v["settled"] for name, v in views.items()}, dict.fromkeys(made, True) | {"waiting": False}
        )
        self.assertEqual(views["dropped"]["outcome"]["state"], "abandoned")
        self.assertEqual(views["replaced"]["superseded_by"], made["ran"].name)
        self.assertEqual([s["kind"] for s in views["waiting"]["suggestions"]], ["broken"])
        self.assertIn("web1_shared", views["waiting"]["suggestions"][0]["text"])
        self.assertEqual(views["ran"]["suggestions"], [])
        self.assertEqual(self.cli("docs").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertRegex(catalog, r"web1_replaced \[superseded, task; never run\]")
        self.assertRegex(catalog, r"web1_dropped \[abandoned, task; never run\].* Closed abandoned by test: not needed")

    def test_last_error_names_the_cause_not_where_to_look(self) -> None:
        body = (
            "import traceback\n\nimport opsquire_helpers as sq\n\n"
            "try:\n    raise RuntimeError('no finished build of abc123')\n"
            "except RuntimeError:\n    sq.error(traceback.format_exc())\n"
            "sq.target('web1', 'failed', 'deploy failed, see the error')\n"
        )
        tid = self.new("cause", body).name
        self.assertEqual(self.cli("run", tid).returncode, 1)
        self.assertEqual(
            self.last(tid)["last_error"], "deploy failed, see the error: RuntimeError: no finished build of abc123"
        )
        two = (
            "import opsquire_helpers as sq\n\nsq.error('web1 answered slowly')\nsq.target('web1', 'ok', 'fine')\n"
            "sq.target('web2', 'failed', 'refused the config')\n"
        )
        tid = self.new("unrelated", two, "--target", "web1", "--target", "web2").name
        self.assertEqual(self.cli("run", tid).returncode, 1)
        self.assertEqual(self.last(tid)["last_error"], "refused the config", "an error before an ok target is not it")

    def test_a_changing_task_should_ask_once_and_the_record_says_when_it_asked_twice(self) -> None:
        head = "import opsquire_helpers as sq\n\n"
        twice = self.new(
            "twice",
            head + "if sq.APPLY:\n    sq.confirm('web1?')\n    sq.confirm('web2?')\n",
            "--changes-prod",
            "--rollback",
            "undo",
            "--no-verify",
        )
        silent = self.new(
            "silent", head + "sq.step('changing')\n", "--changes-prod", "--rollback", "undo", "--no-verify"
        )
        self.new("reader", head + "sq.step('looking')\n")
        r = self.cli("lint", "silent")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("never ask before changing production", r.stdout)
        for name in ("twice", "reader"):
            r = self.cli("lint", name)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn("never ask", r.stdout)
        self.assertTrue(self.task_view(twice.name)["asks_first"])
        self.assertFalse(self.task_view(silent.name)["asks_first"])
        r = self.cli("run", "twice", "--apply", *self.NO_DRY, answers=[("web1? [y/N]", "y"), ("web2? [y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout)
        warns = [e["msg"] for e in self.events(self.last("twice")) if e["event"] == "warn"]
        self.assertEqual(len([w for w in warns if "second yes in one run" in w]), 1)

    def test_the_scaffold_lints_runs_and_asks_once_as_it_comes(self) -> None:
        r = self.cli("new", "web1", "look", "--purpose", "look around", "--target", "web1", "--target", "web2")
        read = Path(r.stdout.strip())
        r = self.cli("new", "web1", "change", "--purpose", "change it", "--changes-prod", "--rollback", "undo")
        change = Path(r.stdout.strip())
        self.assertEqual(sorted(p.name for p in read.iterdir()), ["design.md", "main.py", "meta.json"])
        self.assertTrue((change / "test_main.py").is_file())
        self.assertIn("## Rollback", (change / "design.md").read_text())
        self.assertIn("change it", (change / "design.md").read_text())
        for folder in (read, change):
            r = self.cli("lint", folder.name)
            self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("(1 tests)", self.cli("lint", change.name).stdout)
        self.assertEqual(self.cli("run", read.name).returncode, 0)
        kinds = {e["event"] for e in self.events(self.last(read.name))}
        self.assertTrue({"data", "progress", "target", "result"} <= kinds, kinds)
        self.assertEqual(self.cli("run", change.name).returncode, 0)
        targets = self.last(change.name)["targets"]
        self.assertEqual(targets["web1"]["status"], "unchanged", "the starter plan changes nothing")
        r = self.cli("new", "web1", "bad", "--purpose", "x", "--changes-prod", "--rollback", "undo", "--stage", "a")
        self.assertEqual(r.returncode, 2)
        r = self.cli(
            "new", "web1", "two", "--purpose", "x", "--changes-prod", "--rollback", "n", "--stage", "a", "--stage", "b"
        )
        self.assertEqual(json.loads((Path(r.stdout.strip()) / "meta.json").read_text())["stages"], ["a", "b"])

    def test_an_exception_in_a_script_is_the_runs_last_error(self) -> None:
        self.new(
            "boom",
            "import opsquire_helpers as sq\n\n\ndef main() -> None:\n    raise RuntimeError('disk full on web1')\n\n\n"
            "sq.run(main, cleanup=lambda: sq.step('cleaned up'))\n",
        )
        r = self.cli("run", "boom")
        self.assertEqual(r.returncode, 1, r.stdout)
        rec = self.last("boom")
        self.assertIn("RuntimeError: disk full on web1", rec["last_error"])
        self.assertIn("cleaned up", [e["msg"] for e in self.events(rec)])

    def test_a_dry_run_hands_its_saved_inputs_to_the_apply_it_covers_and_the_records_say_so(self) -> None:
        body = (
            "import opsquire_helpers as sq\n\n\ndef main() -> None:\n"
            "    history = sq.load('history', max_age=AGE)\n"
            "    sq.result('loaded' if history else 'read fresh')\n"
            "    sq.save('history', history or {'rows': [1, 2, 3]})\n"
            "    if sq.APPLY:\n        sq.confirm('Apply?')\n\n\nsq.run(main)\n"
        )
        folder = self.new("cached", body.replace("AGE", "'1h'"), *self.CHANGING)
        self.assertEqual(self.cli("run", "cached").returncode, 0)
        dry = self.last("cached")
        saved = Path(dry["paths"]["dir"]) / "inputs/history.json.gz"
        self.assertEqual((dry["results"], [i["name"] for i in dry["inputs"]["saved"]]), (["read fresh"], ["history"]))
        self.assertTrue(saved.is_file())

        r = self.cli("run", "cached", "--apply", answers=[("[y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout)
        applied = self.last("cached")
        self.assertEqual(applied["results"], ["loaded"])
        used = applied["inputs"]["used"][0]
        self.assertEqual((used["name"], used["from_run"], used["used"]), ("history", dry["run_id"], True))
        self.assertEqual(applied["inputs"]["saved"], [], "an apply keeps nothing")

        self.assertEqual(self.cli("run", "cached").returncode, 0)
        dry = self.last("cached")
        Path(dry["paths"]["dir"], "inputs/history.json.gz").write_bytes(gzip.compress(b'{"rows": [9]}'))
        r = self.cli("run", "cached", "--apply", answers=[("[y/N]", "y")])
        tampered = self.last("cached")
        self.assertEqual(
            (tampered["results"], tampered["inputs"]["used"][0]["why"]),
            (["read fresh"], "not what its dry run recorded"),
        )

        (folder / "main.py").write_text(body.replace("AGE", "'0s'"))
        self.assertEqual(self.cli("run", "cached").returncode, 0)
        r = self.cli("run", "cached", "--apply", answers=[("[y/N]", "y")])
        stale = self.last("cached")
        self.assertEqual((stale["results"], stale["inputs"]["used"][0]["why"]), (["read fresh"], "older than 0s"))

        (folder / "main.py").write_text(body.replace("AGE", "'2h'"))
        r = self.cli("run", "cached", "--apply", "--skip-dry", "the host is down", answers=[("[y/N]", "y")])
        unchecked = self.last("cached")
        self.assertEqual((unchecked["results"], unchecked["inputs"]), (["read fresh"], None), "no dry run, no inputs")

        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import store

        later = dt.datetime.now(dt.UTC) + dt.timedelta(days=3)
        store.compact(self.home / "var/runs", later)
        self.assertEqual(list((self.home / "var/runs").rglob("inputs")), [], "compaction drops kept inputs")

    def test_the_verify_check_gets_the_arguments_of_the_apply_it_checks(self) -> None:
        body = "import sys\n\nimport opsquire_helpers as sq\n\nsq.target('web1', 'ok', ' '.join(sys.argv[1:]))\n"
        folder = self.new("argued", body, *self.CHANGING)
        checked = "sq.target('web1', 'ok', 'checked ' + ' '.join(sys.argv[1:]))\n"
        (folder / "verify.py").write_text("import sys\n\nimport opsquire_helpers as sq\n\n" + checked)
        meta = json.loads((folder / "meta.json").read_text())
        (folder / "meta.json").write_text(json.dumps(meta | {"verify": "verify.py"}))
        self.assertEqual(self.cli("run", "argued", "--", "--from", "2026-11-11").returncode, 0)
        r = self.cli("run", "argued", "--apply", "--", "--from", "2026-11-11")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        check = self.last("argued")
        self.assertEqual((check["mode"], check["argv"]), ("verify", ["--from", "2026-11-11"]))
        self.assertEqual(check["targets"]["web1"]["msg"], "checked --from 2026-11-11")

        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import handover
        from opsquire.model import resolve_task
        from opsquire.query import runs_for

        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(
                handover.args_for(resolve_task("argued"), runs_for(folder.name), "verify"), ["--from", "2026-11-11"]
            )


class Bundle(unittest.TestCase):
    def test_payload_imports_its_modules_through_stdin(self) -> None:
        from opsquire.model import HELPERS_DIR

        sys.path.insert(0, str(HELPERS_DIR))
        try:
            import opsquire_helpers as sq
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as d:
            lib, payload = Path(d) / "shared_lib.py", Path(d) / "payload.py"
            lib.write_text("from __future__ import annotations\n\n\ndef twice(x: int) -> int:\n    return 2 * x\n")
            payload.write_text(
                "import sys\n\nimport shared_lib\n\nprint(shared_lib.twice(int(sys.argv[1])), __name__)\n"
                "raise ValueError('boom')\n"
            )
            r = subprocess.run(
                [sys.executable, "-I", "-u", "-", "21"],
                input=sq.bundle(payload, lib),
                capture_output=True,
                text=True,
                cwd=d,
                check=False,
            )
        self.assertEqual(r.stdout, "42 __main__\n")
        self.assertIn('File "payload.py", line 6, in <module>', r.stderr)
        self.assertIn("raise ValueError('boom')", r.stderr)
