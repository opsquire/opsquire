"""Ctrl-C and kills: every interruption ends cleanly."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from support import BASH_HEADER, CLI, ROOT, OpsTestCase


class InterruptTest(OpsTestCase):
    def test_ctrl_c_and_kill_9(self) -> None:
        body = "import time\n\nimport opsquire_helpers as sq\n\nsq.step('copying files')\nprint('start', flush=True)\n"
        self.new("slow", body + "time.sleep(30)\n")
        p = subprocess.Popen(
            [*CLI, "run", "slow"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        assert p.stdout
        p.stdout.readline()
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGINT)
        p.communicate(timeout=20)
        self.assertEqual(p.returncode, 130)
        self.assertEqual(self.last("slow")["status"], "interrupted")
        self.assertEqual(
            self.last("slow")["last_error"],
            "interrupted by the operator (Ctrl-C); it had reached: copying files",
            "the record says who stopped it and how far it got",
        )
        self.assertFalse((Path(self.last("slow")["paths"]["dir"]) / ".work").exists(), "Ctrl-C removes the copy")

        own_tmp = Path(tempfile.mkdtemp(prefix="kill9-tmp-", dir=self.tmp))
        p = subprocess.Popen(
            [*CLI, "run", "slow"],
            env=self.env | {"TMPDIR": str(own_tmp)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert p.stdout
        p.stdout.readline()
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate(timeout=20)
        rec = self.last("slow")
        self.assertEqual(rec["status"], "interrupted")
        self.assertIn("vanished", rec["last_error"])
        self.assertEqual(list(own_tmp.iterdir()), [], "a killed run leaves nothing in its temp dir")
        code, out = self.audit()
        self.assertEqual((code, out["runs"]["sealed"], out["runs"]["unsealed"]), (0, 1, 1), out["problems"])

    def test_ctrl_c_before_script_records_interrupted_and_cleans_up(self) -> None:
        self.new("early", "print(1)\n", "--changes-prod", "--rollback", "x")
        p = self.start("run", "early")
        deadline = time.monotonic() + 20
        while not list((self.home / "var/runs").rglob("run.json")) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        self.assertNotIn("Traceback", out + err)
        rec = self.last("early")
        self.assertEqual(rec["status"], "interrupted")
        self.assertIn("before the script started", rec["last_error"])
        self.assertEqual(rec["targets"]["web1"]["status"], "skipped")

    def test_ctrl_c_at_python_prompt_runs_cleanup_without_traceback(self) -> None:
        body = (
            "from pathlib import Path\n\nimport opsquire_helpers as sq\n\nSCRATCH = Path('scratch.txt')\n\n\n"
            "def main() -> None:\n    SCRATCH.write_text('x')\n    sq.step('half way')\n    sq.confirm('Go?')\n\n\n"
            "sq.run(main, cleanup=lambda: SCRATCH.unlink(missing_ok=True))\n"
        )
        folder = self.new("prompt", body, "--changes-prod", "--rollback", "x")
        r = self.cli("run", "prompt", "--apply", *self.NO_DRY, answers=[("[y/N]", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        self.assertNotIn("Traceback", r.stdout)
        rec = self.last("prompt")
        self.assertEqual(rec["status"], "interrupted")
        log = Path(rec["paths"]["output"]).read_text()
        self.assertIn("half way", log)
        self.assertIn("interrupted by the operator", log)
        self.assertFalse((folder / "scratch.txt").exists())

    def test_ctrl_c_at_python_prompt_ends_it_even_when_the_read_would_not_wake(self) -> None:
        # A restarting read stays blocked after its signal is handled, as any read does when the Ctrl-C lands just
        # before it starts (on a loaded machine): every Ctrl-C hits that race here.
        body = (
            "import signal\n\nimport opsquire_helpers as sq\n\nsignal.siginterrupt(signal.SIGINT, False)\n"
            "sq.run(lambda: sq.confirm('Go?'))\n"
        )
        self.new("restarting", body, "--changes-prod", "--rollback", "x")
        r = self.cli("run", "restarting", "--apply", *self.NO_DRY, answers=[("[y/N]", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        self.assertEqual(self.last("restarting")["status"], "interrupted")

    def test_ctrl_c_at_bash_prompt_runs_sq_cleanup(self) -> None:
        body = BASH_HEADER + 'sq_cleanup() { rm -f scratch.txt; }\ntouch scratch.txt\nsq_confirm "Go?"\n'
        folder = self.new("bashprompt", body, "--changes-prod", "--rollback", "x", lang="sh")
        r = self.cli("run", "bashprompt", "--apply", *self.NO_DRY, answers=[("[y/N]", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        self.assertEqual(self.last("bashprompt")["status"], "interrupted")
        self.assertFalse((folder / "scratch.txt").exists())

    def test_new_is_all_or_nothing(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import cli

        with (
            mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home)}),
            mock.patch("shutil.copyfile", side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(cli.main(["new", "web1", "half", "--purpose", "p"]), 130)
        self.assertEqual(list((self.home / "tasks").iterdir()), [])

    def test_a_run_stopped_at_its_prompt_says_so_and_leaves_nothing(self) -> None:
        tmpdir = self.tmp / "scratch"
        tmpdir.mkdir()
        self.env["TMPDIR"] = str(tmpdir)
        tid = self.nested_task("import opsquire_helpers as sq\n\nsq.run(lambda: sq.confirm('Deploy web1?'))\n", "py")
        r = self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("[y/N]", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        rec = self.last(tid)
        self.assertEqual((rec["status"], rec["waiting"]["question"]), ("interrupted", "Deploy web1?"))
        self.assertIn("waiting for the operator to answer: Deploy web1?", rec["last_error"])
        self.assertEqual([p.name for p in tmpdir.glob("opsquire-sync-*")], [])
