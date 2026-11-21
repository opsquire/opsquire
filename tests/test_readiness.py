"""The readiness checkpoint (only a handed-over task waits on the operator) and the code a run shows first."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

from support import ROOT, OpsTestCase, free_port


class ReadinessTest(OpsTestCase):
    def preparing(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = json.loads(self.cli("inbox", "--json").stdout)["preparing"]
        return out

    def test_a_task_waits_on_the_operator_only_once_handed_over_and_leaves_when_edited(self) -> None:
        folder = self.new("probe", "print('v1')\n")
        main = str((folder / "main.py").relative_to(self.home))
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        self.assertEqual(self.inbox_ids(), [], "a passing lint is not the agent saying it is done")
        self.assertEqual([(p["task_id"], p["since"]) for p in self.preparing()], [(folder.name, None)])
        self.assertIn("being prepared (1)", self.cli("inbox").stdout)

        broken = self.new("broken", "def f(x):\n    return x\n")
        r = self.cli("handover", broken.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("its lint fails", r.stderr)

        self.hand_over("probe")
        self.assertEqual(self.inbox_ids(), [folder.name])
        self.assertNotIn(folder.name, [p["task_id"] for p in self.preparing()])

        (folder / "main.py").write_text("print('v2')\n")
        self.assertEqual(self.inbox_ids(), [], "edited after its handover")
        prep = next(p for p in self.preparing() if p["task_id"] == folder.name)
        self.assertEqual((prep["since"], prep["own_changed"]), ("handover", [main]))
        self.hand_over("probe")
        self.assertEqual(self.inbox_ids(), [folder.name], "never ran, so no words are owed for the change")

        self.assertEqual(self.cli("run", "probe").returncode, 0)
        (folder / "main.py").write_text("print('v3')\n")
        r = self.cli("handover", "probe")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"its code changed since its last run ({main})", r.stderr)
        self.hand_over("probe", "--changed", "prints v3")
        item = self.inbox_item(folder.name)
        assert item is not None
        self.assertEqual(item["changed_note"], {"text": "prints v3", "files": [main]})

    def test_the_operators_own_run_counts_as_ready(self) -> None:
        folder = self.new("change", "print(1)\n", *self.CHANGING)
        self.assertEqual(self.cli("lint", "change").returncode, 0)
        self.assertEqual(self.inbox_ids(), [])
        self.assertEqual(self.cli("run", "change").returncode, 0)
        item = self.inbox_item(folder.name)
        self.assertEqual(item and item["next"], "apply", "the operator ran these files: the apply waits on them")

    def test_a_shared_script_edited_after_the_handover_is_being_prepared_then_waits_without_running_by_itself(
        self,
    ) -> None:
        lib = Path(self.cli("new", "web1", "lib", "--general", "--purpose", "shared").stdout.strip())
        folder = self.new("user", "print(1)\n", "--uses", lib.name)
        self.hand_over("user")
        (lib / "helper.py").write_text("X = 1\n")
        self.assertIsNone(self.inbox_item(folder.name), "someone is still working on what it runs")
        shared = f"general/{lib.name}/helper.py"
        self.assertEqual([p["own_changed"] for p in self.preparing() if p["task_id"] == folder.name], [[shared]])
        day_old = time.time() - 25 * 3600
        os.utime(lib / "helper.py", (day_old, day_old))
        item = self.inbox_item(folder.name)
        assert item is not None
        self.assertEqual(item["shared_changed"], [shared])
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire.state import with_tiers

        rules = {
            "tiers": {"local": ["web1"], "dev": [], "staging": []},
            "auto_run": {"local": ["read"], "dev": [], "staging": []},
            "sealed": True,
        }
        clean = {**item, "shared_changed": []}
        self.assertEqual([i["auto"] for i in with_tiers([item, clean], rules)], [False, True])

    def test_an_asks_changed_line_reaches_the_handover(self) -> None:
        self.new("again", "print(1)\n")
        self.assertEqual(self.cli("run", "again").returncode, 0)
        folder = next(self.home.glob("tasks/*/*_web1_again"))
        (folder / "main.py").write_text("print(2)\n")
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import handover

        with mock.patch.dict(os.environ, self.env):
            handed = handover.record_run_lines(
                [("RUN", f"opsquire run {folder.name}"), ("CHANGED", "prints 2 now")], "the request", "ask x"
            )
        self.assertEqual(handed, [folder.name])
        self.assertEqual(json.loads((folder / "meta.json").read_text())["handover"]["changed"], "prints 2 now")

    def test_a_run_lists_the_code_it_executes_linked_to_its_exact_copy(self) -> None:
        lib = Path(self.cli("new", "web1", "lib", "--general", "--purpose", "shared").stdout.strip())
        folder = self.new("listed", "print(1)\n", "--uses", lib.name)
        first = self.cli("run", "listed")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("code this run executes (its first run):", first.stderr)
        self.assertIn("  new  main.py", first.stderr)
        self.assertIn(f"  new  general/{lib.name}/main.py", first.stderr)
        self.assertNotIn("test_main.py", first.stderr, "tests do not run in a run")
        snap = self.last("listed")["paths"]["snapshot"]
        self.assertIn(f"  exact copies: {snap}", first.stderr)

        (folder / "main.py").write_text("print(1)\nprint(2)\n")
        self.hand_over("listed", "--changed", "prints 2 too")
        self.env["TERM"] = "xterm-256color"
        r = self.cli("run", "listed", answers=[])
        self.assertEqual(r.returncode, 0, r.stdout)
        before = json.loads(self.cli("runs", "listed", "--json").stdout)[0]["run_id"]
        shown = re.sub(r"\x1b\]8;;[^\x1b]*\x1b\\", "", r.stdout)
        self.assertIn(f"code this run executes (since its last run, {before}):", shown)
        self.assertIn("  changed +1 -0  main.py", shown)
        self.assertIn(f"  same           general/{lib.name}/main.py", shown)
        snap = Path(self.last("listed")["paths"]["snapshot"])
        url = f"vscode://file{snap / folder.relative_to(self.home) / 'main.py'}"
        self.assertIn(f"\033]8;;{url}\033\\main.py\033]8;;\033\\", r.stdout, "a terminal gets a link to the copy")
        log = Path(self.last("listed")["paths"]["output"]).read_text()
        self.assertIn("changed +1 -0  main.py", log)
        self.assertNotIn("\033]8", log, "the log keeps plain text")

    def test_the_console_lists_a_tasks_code_and_its_editor_link_is_a_setting(self) -> None:
        folder = self.new("shown", "print(1)\n")
        self.assertEqual(self.cli("run", "shown").returncode, 0)
        (folder / "main.py").write_text("print(2)\n")
        self.hand_over("shown", "--changed", "prints 2")
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                conn = self.http(port, 20)
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                return json.loads(conn.getresponse().read())

            code = call("GET", f"/api/code?task={folder.name}")
            files = {f["short"]: f for f in code["files"]}
            self.assertEqual((code["changed"], files["main.py"]["state"]), ("prints 2", "changed"))
            self.assertEqual(files["main.py"]["url"], f"vscode://file{folder / 'main.py'}")
            settings = call("POST", "/api/settings", {"editor_url": "myeditor://open?file={path}"})
            self.assertEqual(settings["editor_url"], "myeditor://open?file={path}")
            again = {f["short"]: f for f in call("GET", f"/api/code?task={folder.name}")["files"]}
            self.assertEqual(again["main.py"]["url"], f"myeditor://open?file={folder / 'main.py'}")
            self.assertIn("error", call("POST", "/api/settings", {"editor_url": "no placeholder"}))
            back = call("POST", "/api/settings", {"editor_url": ""})
            self.assertEqual(back["editor_url"], back["editor_url_default"])
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(20)

    def test_the_default_editor_link_reaches_into_this_wsl_distribution(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import codeview

        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu-22.04"}):
            self.assertEqual(codeview.default_editor_url(), "vscode://vscode-remote/wsl+Ubuntu-22.04{path}")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(codeview.default_editor_url(), "vscode://file{path}")
