"""Lint and the isolated unit tests it runs."""

from __future__ import annotations

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

from support import CLI, ROOT, OpsTestCase


class LintTest(OpsTestCase):
    def test_lint_failure_blocks_without_running(self) -> None:
        self.new("badtypes", "def f(x):\n    return x\n\nopen('/tmp/should-not-exist-opsquire-test', 'w')\n")
        r = self.cli("run", "badtypes")
        self.assertEqual(r.returncode, 2)
        rec = self.last("badtypes")
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("lint or tests failed", rec["last_error"])
        self.assertFalse(any(e["event"] == "spawn" for e in self.events(rec)))

    def test_a_part_target_warns_about_scripts_reading_targets_and_hosts_name_the_machine(self) -> None:
        body = "import opsquire_helpers as sq\n\nprint('hosts', sq.HOSTS, 'targets', sq.TARGETS)\n"
        task = self.new("parts", body, "--target", "web1/nginx", "--target", "web1/api")
        r = self.cli("lint", task.name)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("warning: web1/nginx, web1/api names part of a machine, and ", r.stdout)
        self.assertEqual(self.cli("run", task.name).returncode, 0)
        out = Path(self.last(task.name)["paths"]["output"]).read_text()
        self.assertIn("hosts ['web1'] targets ['web1/nginx', 'web1/api']", out)

    def test_a_changing_task_must_say_how_to_undo_it(self) -> None:
        r = self.cli("new", "web1", "noundo", "--purpose", "p", "--changes-prod", "--rollback", "read-only")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--rollback must say how to undo", r.stderr)
        folder = self.new("undo", "print(1)\n", "--changes-prod", "--rollback", "revert the file")
        meta = json.loads((folder / "meta.json").read_text())
        (folder / "meta.json").write_text(json.dumps(meta | {"rollback": "None."}))
        r = self.cli("lint", folder.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a changing task needs a rollback that says how to undo it", r.stdout + r.stderr)

    def test_a_script_never_waits_for_typed_input_and_a_hidden_secret_is_terminal_only(self) -> None:
        cases = [
            ("asks", 'name = input("name: ")\nprint(name)\n', "py", "main.py:1 waits for typed input"),
            ("reads", '#!/bin/bash\necho start\nread -r name\necho "$name"\n', "sh", "main.sh:3 waits for typed input"),
            ("secret", 'import getpass\n\nprint(len(getpass.getpass("key: ")))\n', "py", "main.py:3 asks for a hidden"),
            ("hidden", '#!/bin/bash\nread -rs pw\necho "${#pw}"\n', "sh", "main.sh:2 asks for a hidden value"),
            ("piped", "import sys\n\nprint(sys.stdin.read())\n", "py", ""),
            ("looped", '#!/bin/bash\nwhile read -r l; do echo "$l"; done < /etc/hostname\n', "sh", ""),
        ]
        for name, body, lang, said in cases:
            with self.subTest(name):
                r = self.cli("lint", self.new(name, body, lang=lang).name)
                self.assertEqual(r.returncode, 1 if "typed" in said else 0, r.stdout + r.stderr)
                if said:
                    self.assertIn(said, r.stdout)
                else:
                    self.assertNotIn("typed input", r.stdout)
                    self.assertNotIn("hidden value", r.stdout)

    def test_a_lint_tool_stopped_by_ctrl_c_while_starting_is_an_interruption(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import lint

        fatal = "import sys; sys.stderr.write('Fatal Python error\\nTraceback\\nKeyboardInterrupt\\n'); sys.exit(134)"
        with self.assertRaises(lint.Interrupted):
            lint._run([sys.executable, "-c", fatal])
        self.assertEqual(lint._run([sys.executable, "-c", "raise SystemExit(1)"])[0], False)

    def test_lint_refuses_symlinks_and_never_shows_what_they_point_at(self) -> None:
        folder = self.new("linked", "#!/bin/bash\necho valid\n", lang="sh")
        (folder / "input.sh").symlink_to(self.creds)
        (folder / "lib").symlink_to(self.tmp)
        self.env.pop("OPSQUIRE_TEST_ISOLATION")
        r = self.cli("lint", folder.name)
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlinks are never task files", r.stdout)
        self.assertIn("input.sh", r.stdout)
        self.assertIn(str(folder / "lib"), r.stdout)
        saved = (folder / ".lint.json").read_text()
        for text in (r.stdout, r.stderr, saved):
            self.assertNotIn("SECRET-TOKEN-123456", text)
        self.assertNotEqual(self.cli("run", folder.name).returncode, 0)
        (folder / "input.sh").unlink()
        (folder / "lib").unlink()
        self.assertEqual(self.cli("lint", folder.name).returncode, 0)

    def test_agent_sandbox_stubs_are_not_script_files(self) -> None:
        folder = self.new("stubbed", "print('ok')\n")
        before = self.cli("lint", "stubbed")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        stubs = folder / ".claude"
        stubs.mkdir()
        for name in ("agents", "settings.json", "loop.md"):
            (stubs / name).write_text("")
        after = self.cli("lint", "stubbed")
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertNotIn("unsupported files", after.stdout + after.stderr)

    def test_lint_ignores_active_venv_and_project_configs(self) -> None:
        self.new("pinned", "import os\n\nprint(os.getcwd())\n")
        hostile = self.tmp / "project"
        (hostile / "bin").mkdir(parents=True)
        (hostile / "mypy.ini").write_text("[mypy]\ninstall_types = True\nnon_interactive = True\n")
        (hostile / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["ALL"]\n')
        for tool in ("mypy", "ruff"):
            fake = hostile / "bin" / tool
            fake.write_text("#!/bin/sh\necho fake tool >&2\nexit 1\n")
            fake.chmod(0o755)
        env = {**self.env, "PATH": f"{hostile / 'bin'}:{self.env['PATH']}"}
        r = subprocess.run([*CLI, "lint", "pinned"], env=env, cwd=hostile, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("fake tool", r.stdout + r.stderr)
        self.assertIn("mypy.ini", r.stdout)

    def test_task_python_is_pinned_unless_meta_names_one(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "python3"
        fake.write_text("#!/bin/sh\necho WRONG PYTHON\nexit 1\n")
        fake.chmod(0o755)
        env = {**self.env, "PATH": f"{bin_dir}:{self.env['PATH']}"}
        self.new("pinned-py", "print('ran')\n")
        r = subprocess.run([*CLI, "run", "pinned-py"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("WRONG PYTHON", r.stdout)
        self.assertEqual(self.last("pinned-py")["interpreter"], sys.executable)

        venv_python = bin_dir / "venv-python"
        venv_python.write_text(f'#!/bin/sh\necho VENV PYTHON\nexec {sys.executable} "$@"\n')
        venv_python.chmod(0o755)
        self.new("venv-py", "print('ran')\n", "--interpreter", str(venv_python))
        r = subprocess.run([*CLI, "run", "venv-py"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VENV PYTHON", r.stdout)
        self.assertEqual(self.last("venv-py")["interpreter"], str(venv_python))

        folder = self.new("bad-interp", "print(1)\n", "--interpreter", str(self.tmp / "missing"))
        self.assertIn(
            "not an executable file",
            " ".join(json.loads(self.cli("show", folder.name, "--json").stdout)["meta_errors"]),
        )

    def test_ctrl_c_during_a_lint_tool_is_an_interruption_not_a_verdict(self) -> None:
        """Ctrl-C reaches the lint tools too (same process group); their death must not read as a lint failure, in
        the main thread or in an ask's worker thread, and no lint stamp may record it."""
        folder = self.new("tooled", "print(1)\n")
        fake = self.tmp / "ruff-sigint"
        fake.write_text("#!/bin/sh\nkill -INT $$\n")
        fake.chmod(0o755)
        self.env["OPSQUIRE_RUFF"] = str(fake)
        r = self.cli("lint", "tooled")
        self.assertEqual(r.returncode, 130, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("opsquire: interrupted", r.stderr)
        self.assertFalse((folder / ".lint.json").exists())

        sys.path.insert(0, str(ROOT / "src"))

        from opsquire.lint import lint_task, save_lint
        from opsquire.model import resolve_task

        results: list[Any] = []
        with mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home), "OPSQUIRE_RUFF": str(fake)}):
            task = resolve_task("tooled")
            t = threading.Thread(target=lambda: results.append(lint_task(task)))
            t.start()
            t.join(timeout=30)
            save_lint(task, results[0])
        self.assertEqual((results[0].ok, results[0].interrupted), (False, True))
        self.assertIn("interrupted: ruff-sigint was stopped by Ctrl-C", results[0].output)
        self.assertNotIn("Traceback", results[0].output)
        self.assertFalse((folder / ".lint.json").exists())

    def test_tests_run_isolated_from_credentials_home_network_and_the_task_folder(self) -> None:
        real_home = self.tmp / "operator-home"
        real_home.mkdir()
        marker = real_home / "marker.txt"
        marker.write_text("operator's file")
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        self.env.update(
            HOME=str(real_home),
            TMPDIR=str(scratch),
            GITHUB_TOKEN="ghp_dummy123456",
            AWS_SECRET_ACCESS_KEY="dummy-aws-secret",
            SSH_AUTH_SOCK=str(self.tmp / "agent.sock"),
            MY_PASSWORD="dummy-password",
        )
        folder = self.new("isolated", "print(1)\n")
        level = self.isolation_level()
        (folder / "test_main.py").write_text(
            self.ISOLATION_TEST.format(level=level, creds=str(self.creds), marker=str(marker), real=str(folder))
        )
        r = self.cli("lint", "isolated")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"tests isolated: {level}", r.stdout)
        self.assertIn("LINT OK (5 tests)", r.stdout)
        self.assertEqual(json.loads((folder / ".lint.json").read_text())["isolation"], level)
        self.assertFalse((folder / "written.txt").exists())
        self.assertEqual(list(folder.rglob("__pycache__")), [])
        self.assertEqual(list(scratch.iterdir()), [], "lint left its scratch directories behind")

    def test_ctrl_c_during_isolated_tests_leaves_no_scratch_behind(self) -> None:
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        self.env["TMPDIR"] = str(scratch)
        folder = self.new("slow", "print(1)\n")
        (folder / "test_main.py").write_text(
            "import time\nimport unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_slow(self) -> None:\n        time.sleep(60)\n"
        )
        p = subprocess.Popen(
            [*CLI, "lint", "slow"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not list(scratch.glob("opsquire-tests-*/src")):
            time.sleep(0.1)
        time.sleep(1)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130, out + err)
        self.assertIn("opsquire: interrupted", err)
        self.assertEqual(list(scratch.iterdir()), [])
        self.assertFalse((folder / ".lint.json").exists())

    def test_doctor_reports_how_tests_are_isolated(self) -> None:
        level = self.isolation_level()
        out = self.cli("doctor").stdout
        strong = level in ("bwrap", "sandbox-exec")
        self.assertIn(f"ok   tests isolated: {level}" if strong else f"WARN tests isolated: {level}", out)
        from opsquire import doctor

        self.enterContext(mock.patch.dict(os.environ, self.env))
        for weaker in ("netns", "env-only"):
            with mock.patch("opsquire.isolate.level", return_value=weaker):
                mark, text = doctor.isolation_check()
                self.assertEqual(mark, "WARN", text)
                self.assertIn("accepted by the operator", text)
                with mock.patch.dict(os.environ, {"OPSQUIRE_TEST_ISOLATION": ""}):
                    mark, text = doctor.isolation_check()
            self.assertEqual(mark, "FIX", text)
            self.assertIn("lint refuses to run tests", text)
            self.assertIn("OPSQUIRE_TEST_ISOLATION=weaker-ok in their own shell", text)
        with mock.patch("opsquire.isolate.level", return_value="bwrap"):
            self.assertEqual(doctor.isolation_check()[0], "ok")

    def test_tests_run_only_under_bwrap_unless_the_operator_accepts_less(self) -> None:
        from opsquire.lint import run_tests
        from opsquire.model import load_task

        stolen = self.tmp / "stolen"
        folder = self.new("reads-creds", "print(1)\n")
        (folder / "test_main.py").write_text(
            "import unittest\nfrom pathlib import Path\n\n\nclass T(unittest.TestCase):\n"
            "    def test_read(self) -> None:\n"
            f"        Path({str(stolen)!r}).write_text(Path({str(self.creds)!r}).read_text())\n"
        )
        self.enterContext(mock.patch.dict(os.environ, self.env))
        self.config.write_text(json.dumps({"test_isolation": "weaker-ok"}))
        os.environ["OPSQUIRE_TEST_ISOLATION"] = "bwrap"
        for weaker in ("netns", "env-only"):
            with mock.patch("opsquire.isolate.level", return_value=weaker):
                tests, ok, out, isolation = run_tests(load_task(folder))
            self.assertEqual((tests, ok, isolation), (0, False, ""), out)
            self.assertIn("tests NOT run", out)
            self.assertIn("OPSQUIRE_TEST_ISOLATION=weaker-ok", out)
            self.assertFalse(stolen.exists(), f"a test ran at {weaker} without the operator's opt-in")
        os.environ["OPSQUIRE_TEST_ISOLATION"] = "weaker-ok"
        with mock.patch("opsquire.isolate.level", return_value="env-only"):
            tests, ok, out, isolation = run_tests(load_task(folder))
        self.assertEqual((tests, ok, isolation), (1, True, "env-only"), out)
        if self.isolation_level() not in ("bwrap", "sandbox-exec"):
            self.env["OPSQUIRE_TEST_ISOLATION"] = ""
            r = self.cli("run", folder.name)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("tests NOT run", r.stdout + r.stderr)

        os.environ["OPSQUIRE_TEST_ISOLATION"] = ""
        self.creds.unlink()
        self.creds.symlink_to(os.devnull)
        with mock.patch("opsquire.isolate.level", return_value="env-only"):
            tests, ok, out, isolation = run_tests(load_task(folder))
        self.assertEqual((tests, isolation), (1, "env-only"), out)
        self.assertIn("withheld from this caller", out)
        self.assertEqual(stolen.read_text() if stolen.exists() else "", "", "a masked credentials file gives nothing")

    def test_isolation_mounts_every_hop_of_a_symlinked_interpreter(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import isolate

        store = self.tmp / "store"
        real = store / "cpython-3.13.18" / "bin"
        real.mkdir(parents=True)
        (real / "python3.13").symlink_to(sys.executable)
        (store / "cpython-3.13").symlink_to(store / "cpython-3.13.18")
        venv = self.tmp / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(store / "cpython-3.13" / "bin" / "python3.13")
        hops = [str(h) for h in isolate.link_chain(venv / "python")]
        self.assertIn(str(store / "cpython-3.13" / "bin" / "python3.13"), hops)
        self.assertIn(str(store / "cpython-3.13"), hops, "a symlinked version folder is a hop of its own")
        dirs = isolate.python_dirs.__wrapped__(str(venv / "python"))
        self.assertTrue(any(venv.is_relative_to(d) for d in dirs), dirs)
        self.assertTrue(
            any(str(store / "cpython-3.13") == d or (store / "cpython-3.13").is_relative_to(d) for d in dirs), dirs
        )
        self.assertFalse(
            any(a != b and Path(a).is_relative_to(b) for a in dirs for b in dirs), "no mount sits inside another"
        )

    def test_interpreter_folders_come_from_its_files_without_running_it(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import isolate

        ran = self.tmp / "ran"
        base = Path(sys.executable).resolve()
        venv = self.tmp / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text(f"home = {base.parent}\n")
        site = venv / "lib/python3.13/site-packages"
        site.mkdir(parents=True)
        (self.tmp / "editable").mkdir()
        (site / "_editable.pth").write_text(f"{self.tmp / 'editable'}\nimport os\n")
        wrapper = venv / "bin/python"
        wrapper.write_text(f'#!/bin/sh\ntouch {ran}\nexec {sys.executable} "$@"\n')
        wrapper.chmod(0o755)
        dirs = isolate.python_dirs.__wrapped__(str(wrapper))
        self.assertFalse(ran.exists(), "the task's interpreter never runs outside the sandbox")
        for wanted in (venv, self.tmp / "editable", base.parent.parent):
            self.assertTrue(any(wanted.resolve().is_relative_to(d) for d in dirs), (wanted, dirs))

    def test_unit_tests_gate_lint_and_runs(self) -> None:
        folder = self.new("tested", "from logic import double\n\nprint(double(21))\n")
        (folder / "logic.py").write_text("def double(x: int) -> int:\n    return x * 2\n")
        (folder / "test_logic.py").write_text(self.PASSING_TEST)
        r = self.cli("lint", "tested")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("LINT OK (2 tests)", r.stdout)
        self.assertEqual(self.cli("run", "tested").returncode, 0)
        self.assertEqual(self.last("tested")["tests"], 2)
        self.assertEqual(list(folder.rglob("__pycache__")), [])

        (folder / "logic.py").write_text("def double(x: int) -> int:\n    return x * 3\n")
        r = self.cli("run", "tested")
        self.assertEqual(r.returncode, 2, r.stderr)
        rec = self.last("tested")
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("lint or tests failed", rec["last_error"])
        self.assertIn("FAILED (2 tests)", r.stderr)

    def test_tests_of_used_general_scripts_run_too_and_empty_test_files_fail(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        (g / "logic.py").write_text("def double(x: int) -> int:\n    return x * 2\n")
        (g / "test_logic.py").write_text(self.PASSING_TEST)
        self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertIn("LINT OK (2 tests)", self.cli("lint", "user").stdout)
        empty = self.new("empty-tests", "print(1)\n")
        (empty / "test_nothing.py").write_text("import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n")
        r = self.cli("lint", "empty-tests")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED (0 tests)", r.stdout)

    def test_a_package_installed_next_to_mypy_can_lint_and_run(self) -> None:
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(venv)], check=True)
        python = str(venv / "bin/python")
        where = "import site; print(site.getsitepackages()[0])"
        site = Path(subprocess.run([python, "-c", where], capture_output=True, text=True, check=True).stdout.strip())
        shutil.copytree(ROOT / "src/opsquire", site / "opsquire", ignore=shutil.ignore_patterns("__pycache__"))
        mypy_home = subprocess.run(
            [sys.executable, "-c", "import mypy, os; print(os.path.dirname(os.path.dirname(mypy.__file__)))"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (site / "_lint_tools.pth").write_text(mypy_home + "\n")
        (venv / "bin/mypy").write_text(f"#!{python}\nfrom mypy.__main__ import console_entry\nconsole_entry()\n")
        (venv / "bin/mypy").chmod(0o755)
        ruff = shutil.which("ruff", path=f"{Path(sys.executable).parent}:{Path.home() / '.local/bin'}")
        assert ruff
        (venv / "bin/ruff").symlink_to(ruff)
        env = {k: v for k, v in self.env.items() if k != "PYTHONPATH"}
        env["TMPDIR"] = str(self.tmp)
        made = subprocess.run(
            [python, "-m", "opsquire", "new", "web1", "installed", "--purpose", "p", "--lang", "py"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        (Path(made.stdout.strip()) / "main.py").write_text("import opsquire_helpers as sq\n\nsq.result(sq.__file__)\n")
        for args in (["lint", "installed"], ["run", "installed"]):
            r = subprocess.run([python, "-m", "opsquire", *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.last("installed")["results"], [str(site / "opsquire/data/helpers/opsquire_helpers.py")])
        self.assertEqual(list(self.tmp.glob("opsquire-mypy-*")), [])

    def test_a_general_script_shows_its_lint_and_a_task_lint_checks_a_changed_one(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        good = '"""Doubling helpers."""\n\n\ndef double(x: int) -> int:\n    """Twice x."""\n    return x * 2\n'
        (g / "logic.py").write_text(good)
        (g / "test_logic.py").write_text(self.PASSING_TEST)

        def script() -> dict[str, Any]:
            rows: list[dict[str, Any]] = json.loads(self.cli("ls", "--kind", "general", "--json").stdout)
            return rows[0]

        self.assertEqual(script()["status"], "unchecked")
        self.assertEqual(self.cli("lint", "web1_shared").returncode, 0)
        self.assertEqual(script()["status"], "lint-ok")
        self.assertIn("(lint-ok; tests: 2; used by 0; never run)", (self.home / "docs/CATALOG.md").read_text())
        self.assertIn("- lint: lint-ok", (self.home / "docs/web1_shared.md").read_text())

        (g / "logic.py").write_text("import os\n" + good)
        self.assertEqual(script()["status"], "unchecked", "edited since its lint")
        self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertIn("(unchecked; tests: 2; used by 1; never run)", (self.home / "docs/CATALOG.md").read_text())
        failed = self.cli("lint", "user")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("web1_shared: LINT FAILED (general script, changed since its lint)", failed.stdout)
        self.assertEqual(script()["status"], "lint-failing")
        self.assertEqual(len(json.loads(self.cli("ls", "--status", "lint-failing", "--json").stdout)), 1)
        self.assertIn("(lint-failing; tests: 2; used by 1; never run)", (self.home / "docs/CATALOG.md").read_text())
        st = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(st["scripts"]["web1_shared"]["lint"], "lint-failing")
        self.assertEqual(st["inbox"], [])

        (g / "logic.py").write_text(good)
        fixed = self.cli("lint", "user", "--json")
        self.assertEqual(fixed.returncode, 0, fixed.stdout)
        self.assertEqual(json.loads(fixed.stdout)["scripts"], {"web1_shared": True})
        self.assertEqual(script()["status"], "lint-ok")
        self.assertEqual(json.loads(self.cli("lint", "user", "--json").stdout)["scripts"], {}, "unchanged: not again")
