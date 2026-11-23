"""The home: init, integrate, git, sharing, doctor and the contract."""

from __future__ import annotations

import grp
import importlib.util
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from support import CLI, LINUX_ONLY, ROOT, OpsTestCase

from opsquire import contract


class HomeTest(OpsTestCase):
    def test_doctor_boundary_probe_reports_what_an_agent_sandbox_exposes(self) -> None:
        (self.home / "var" / "runs").mkdir(parents=True, exist_ok=True)
        r = self.cli("doctor", "--boundary", "--json")
        out = json.loads(r.stdout)
        self.assertEqual((r.returncode, out["ok"]), (1, False), "an unsandboxed process can write var/ and read creds")
        self.assertTrue(any(c.startswith("EXPOSED") and "read" in c for c in out["checks"]), out["checks"])
        self.assertNotIn("SECRET-TOKEN", r.stdout, "the probe never prints what it read")
        self.creds.write_text("")
        (self.home / "var").chmod(0o500)
        (self.home / "var" / "runs").chmod(0o500)
        try:
            kept = self.cli("doctor", "--boundary")
        finally:
            (self.home / "var" / "runs").chmod(0o700)
            (self.home / "var").chmod(0o700)
        self.assertNotIn("EXPOSED read", kept.stdout, "an empty mask over the creds file keeps it")
        self.assertEqual(list((self.home / "var" / "runs").iterdir()), [], "the probe leaves nothing behind")
        piece = self.tmp / "worktree"
        piece.mkdir()
        self.env |= {"OPSQUIRE_HOME": str(piece), "OPSQUIRE_VAR": str(self.home / "var")}
        out = json.loads(self.cli("doctor", "--boundary", "--json").stdout)
        self.assertIn(f"EXPOSED write {self.home / 'var' / 'runs'}", out["checks"], "a piece's records are the home's")

    def test_repo_contains_nothing_employer_specific(self) -> None:
        spec = importlib.util.spec_from_file_location("clean_text", ROOT / "scripts/clean_text.py")
        assert spec and spec.loader
        clean_text = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(clean_text)
        if not clean_text.words():
            self.skipTest("no word list here (.private/banned-words.txt or OPSQUIRE_BANNED_WORDS)")
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        hits = []
        for name in listed:
            if not (ROOT / name).is_file():
                continue
            hits += [f"{name}: {m}" for m in clean_text.hits((ROOT / name).read_text(errors="ignore"))]
        self.assertEqual(hits, [])

    def test_a_scratch_home_never_writes_the_operators_config(self) -> None:
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        operator = {"home": str(self.home), "time_zone": "local", "policy": {"tiers": {"local": ["workstation"]}}}
        self.config.write_text(json.dumps(operator))
        demo = scratch / "demo/home"
        self.env |= {"TMPDIR": str(scratch), "OPSQUIRE_HOME": str(demo)}
        for args in (("init", str(demo)), ("init", "--time-zone", "UTC"), ("home", str(demo))):
            r = self.cli(*args)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(f"not writing {self.config} for the temporary home {demo}", r.stderr)
            self.assertEqual(json.loads(self.config.read_text()), operator, args)
        self.assertFalse(demo.exists())
        self.env["XDG_CONFIG_HOME"] = str(scratch / "cfg")
        r = self.cli("init", str(demo))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads((scratch / "cfg/opsquire/config.json").read_text())["home"], str(demo))
        self.assertEqual(json.loads(self.config.read_text()), operator)

    def test_init_builds_a_working_home_from_nothing(self) -> None:
        user = self.tmp / "user"
        self.env |= {
            "HOME": str(user),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
            "XDG_CONFIG_HOME": str(self.tmp / "cfg"),
        }
        self.env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        del self.env["OPSQUIRE_HOME"]
        self.env["OPSQUIRE_CREDS"] = str(self.tmp / "fresh-creds.json")
        home = self.tmp / "fresh-home"
        dry = self.cli("init", str(home), "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(home.exists() or (self.tmp / "fresh-creds.json").exists())
        r = self.cli("init", str(home), "--budget", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        for line in (
            f"home: set to {home}",
            "tasks/: created",
            "general/: created",
            "local rules: written from the template",
            "ask budget: $5.00 per ask as a reminder;",
            "credentials: created empty",
            "tool ruff: found",
            "claude skill: installed",
            "home repo: initialized a local git repository",
            "contract: written",
            "first task: created and ready (",
            "offboarding: settled tasks leave tasks/ 7 days after their last activity",
        ):
            self.assertIn(line, out)
        config = json.loads((self.tmp / "cfg/opsquire/config.json").read_text())
        self.assertEqual(config, {"home": str(home), "ask_budget_usd": 5.0})
        creds = self.tmp / "fresh-creds.json"
        self.assertEqual((json.loads(creds.read_text()), creds.stat().st_mode & 0o777), ({}, 0o600))
        self.assertIn("### What is script-shaped here", (home / "LOCAL.md").read_text())
        self.assertIn("## Local rules for this home", (home / "AGENTS.md").read_text())
        demo = re.search(r"first task: created and ready \((\S+)\)", out)
        assert demo
        run = self.cli("run", demo.group(1))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("hello from demo", run.stdout)
        self.assertEqual(json.loads(self.cli("last", "--json").stdout)["status"], "ok")
        (home / "LOCAL.md").write_text("mine\n")
        self.assertEqual(self.cli("init", "--offboard-days", "-1").returncode, 2)
        kept = self.cli("init", "--offboard-days", "30", "--dry-run")
        self.assertIn("offboarding: settled tasks leave tasks/ 30 days after", kept.stdout)
        self.assertNotIn("offboard_after_days", (self.tmp / "cfg/opsquire/config.json").read_text())
        never = self.cli("init", "--offboard-days", "0")
        self.assertIn("offboarding: off; settled tasks stay until `opsquire offboard <task>`", never.stdout)
        self.assertEqual(json.loads((self.tmp / "cfg/opsquire/config.json").read_text())["offboard_after_days"], 0.0)
        again = self.cli("init")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("offboarding: off;", again.stdout)
        for line in (
            "tasks/: present",
            "local rules: present",
            "credentials: present (",
            "claude skill: unchanged",
            f"first task: present ({demo.group(1)})",
            "ask budget: $5.00",
        ):
            self.assertIn(line, again.stdout)
        self.assertEqual((home / "LOCAL.md").read_text(), "mine\n")
        self.agents_file()
        ask = self.cli("ask", "--no-split", "--strict-budget", "one", "thing")
        self.assertEqual(ask.returncode, 0, ask.stdout + ask.stderr)
        self.assertIn('"--max-budget-usd", "5.00"', ask.stdout)

    def test_init_on_a_machine_with_no_git_identity_or_lint_tools(self) -> None:
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        cli = [str(venv / "bin/python"), "-m", "opsquire"]
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "git").symlink_to(shutil.which("git") or "git")
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tuseConfigOnly = true\n")
        env = {
            "PATH": str(tools),
            "HOME": str(self.tmp / "user"),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
            "XDG_CONFIG_HOME": str(self.tmp / "cfg"),
            "PYTHONUSERBASE": str(self.tmp / "no-user-packages"),
            "PYTHONPATH": str(ROOT / "src"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "OPSQUIRE_CREDS": str(self.tmp / "fresh-creds.json"),
            "TMPDIR": str(self.tmp),
        }

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([*cli, *args], env=env, capture_output=True, text=True, timeout=120)

        home = self.tmp / "fresh-home"
        r = run("init", str(home))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tool ruff: MISSING", r.stdout)
        self.assertIn("home repo: initialized", r.stdout)
        self.assertIn("next: install what is MISSING above", r.stdout)
        self.assertNotIn("next: run the first task", r.stdout)
        authors = subprocess.run(
            ["git", "-C", str(home), "log", "--format=%an <%ae>"], capture_output=True, text=True, check=True
        )
        self.assertEqual(set(authors.stdout.split("\n")) - {""}, {"opsquire <opsquire@localhost>"})
        demo = re.search(r"first task: created \((\S+)\) but its lint did not pass", r.stdout)
        assert demo
        again = run("init")
        self.assertIn(f"first task: present ({demo.group(1)}) but its lint did not pass", again.stdout)
        self.assertNotIn("next: run the first task", again.stdout)
        blocked = run("run", demo.group(1))
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn("ruff and mypy must be installed", blocked.stdout + blocked.stderr)

        for tool in ("ruff", "mypy"):
            found = shutil.which(tool, path=f"{Path(sys.executable).parent}:{Path.home() / '.local/bin'}")
            assert found, tool
            (tools / tool).symlink_to(found)
        env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        ready = run("init")
        self.assertIn(f"first task: present ({demo.group(1)}); run it", ready.stdout)
        self.assertIn("next: run the first task", ready.stdout)
        ran = run("run", demo.group(1))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("hello from demo", ran.stdout)

        (tools / "git").unlink()
        other = self.tmp / "home-without-git"
        no_git = run("init", str(other))
        self.assertEqual(no_git.returncode, 0, no_git.stdout + no_git.stderr)
        self.assertIn("tool git: MISSING", no_git.stdout)
        self.assertIn("home repo: skipped, git is not installed", no_git.stdout)
        self.assertIn("contract: written", no_git.stdout)

    def test_integrate_installs_the_skill_and_makes_the_home_a_local_git_repo(self) -> None:
        self.env |= {"HOME": str(self.tmp / "user"), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}
        self.env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        skills = [self.tmp / "claude/skills/opsquire/SKILL.md", self.tmp / "user/.claude/skills/opsquire/SKILL.md"]
        r = self.cli("integrate", "--dry-run")
        self.assertEqual(r.stdout.count("claude skill: would be installed"), 2)
        self.assertIn("home repo: would initialize", r.stdout)
        self.assertFalse(any(s.exists() for s in skills) or (self.home / ".git").exists())
        r = self.cli("integrate")
        self.assertEqual(r.stdout.count("claude skill: installed"), 2)
        want = (ROOT / "src/opsquire/data/skills/opsquire/SKILL.md").read_text().split("If your shell")[0]
        self.assertTrue(all(s.read_text().startswith(want) and "{{" not in s.read_text() for s in skills))
        self.assertIn("var/", (self.home / ".gitignore").read_text())
        self.assertEqual(len(self.git("log", "--oneline").splitlines()), 1)
        r = self.cli("integrate")
        self.assertEqual(r.stdout.count("claude skill: unchanged"), 2)
        self.assertIn("home repo: local-only git repository", r.stdout)
        self.git("remote", "add", "origin", "https://example.com/x.git")
        self.assertIn("refusing: it has remotes (origin)", self.cli("integrate").stdout)

    def test_lint_pass_commits_the_ready_version(self) -> None:
        self.git_home()
        folder = self.new("tidy", "print(1)\n")
        self.assertEqual(self.cli("lint", "tidy").returncode, 0)
        self.assertIn(f"ready: {folder.name}", self.git("log", "--format=%s"))
        self.assertEqual(self.git("status", "--short"), "")

    def test_lint_pass_commits_the_general_scripts_it_checked(self) -> None:
        self.git_home()
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        folder = self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertEqual(self.cli("lint", "user").returncode, 0)
        (g / "main.py").write_text('"""Shared."""\n\nVALUE = 2\n')
        self.assertEqual(self.cli("lint", "user").returncode, 0)
        self.assertEqual(self.git("status", "--short"), "")
        self.assertIn("VALUE = 2", self.git("show", "HEAD:general/web1_shared/main.py"))
        self.assertIn(f"ready: {folder.name}", self.git("log", "-1", "--format=%s"))

    def test_home_git_runs_no_hook_and_refuses_config_or_attributes_that_run_commands(self) -> None:
        self.git_home()
        marker = self.tmp / "ran"
        planted = self.tmp / "planted"
        planted.write_text(f'#!/bin/sh\necho "$0" >> {marker}\ncat\n')
        planted.chmod(0o755)
        for hook in ("pre-commit", "commit-msg", "post-commit", "reference-transaction"):
            shutil.copy(planted, self.home / ".git/hooks" / hook)
        folder = self.new("hooked", "print('valid')\n")
        (folder / "main.py").write_text("this is not valid python !!!\n")
        r = self.cli("run", folder.name)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"before running {folder.name}", self.git("log", "-1", "--format=%s"))
        self.assertFalse(marker.exists(), "opsquire's commit ran a git hook")

        config = self.home / ".git/config"
        clean = config.read_text()
        for key, value in (
            ("core.hooksPath", str(self.tmp)),
            ("core.fsmonitor", str(planted)),
            ("alias.add", str(planted)),
            ("include.path", str(self.tmp / "more.gitconfig")),
            ("filter.x.clean", str(planted)),
            ("diff.x.textconv", str(planted)),
            ("core.sshCommand", str(planted)),
        ):
            config.write_text(clean + f"[{key.rsplit('.', 1)[0]}]\n\t{key.rsplit('.', 1)[1]} = {value}\n")
            (folder / "main.py").write_text(f"print({key!r})\n")
            r = self.cli("run", folder.name)
            self.assertNotEqual(r.returncode, 0, key)
            self.assertIn(f"sets {key.lower()}", r.stderr.lower(), r.stderr)
            self.assertIn("git config --file", r.stderr)
            self.assertFalse(marker.exists(), key)
        config.write_text(clean)
        (self.home / "tasks/.gitattributes").write_text("# fine\n*.py merge=union\n* filter=x\n")
        r = self.cli("run", folder.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("line 3: filter=x", r.stderr)
        (self.home / "tasks/.gitattributes").write_text("*.py merge=union -diff\n")
        r = self.cli("run", folder.name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(marker.exists())

    def test_home_commits_are_not_signed_even_when_the_operator_signs_everything(self) -> None:
        gpg = self.tmp / "gpg-that-fails"
        gpg.write_text("#!/bin/sh\necho 'gpg: signing failed: no passphrase' >&2\nexit 2\n")
        gpg.chmod(0o755)
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Test\n\temail = test@example.com\n\tsigningkey = ABC\n"
            f"[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = {gpg}\n"
        )
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        self.env |= {"HOME": str(self.tmp / "user"), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}
        self.env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        self.assertIn("home repo: initialized", self.cli("integrate").stdout)
        folder = self.new("tidy", "print(1)\n")
        r = self.cli("lint", "tidy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("home not committed", r.stdout + r.stderr)
        self.assertIn(f"ready: {folder.name}", self.git("log", "--format=%s"))
        self.assertEqual(set(self.git("log", "--format=%G?").split()), {"N"})
        self.assertEqual(self.git("config", "--global", "commit.gpgsign").strip(), "true")

    def test_unwritable_var_gives_clear_error(self) -> None:
        self.new("rw", "print(1)\n")
        self.assertEqual(self.cli("run", "rw").returncode, 0)
        self.write_lock("other-target", os.getpid())
        var = self.home / "var"
        dirs = sorted((d for d in var.rglob("*") if d.is_dir()), reverse=True) + [var]
        for d in dirs:
            d.chmod(0o500)
        try:
            r = self.cli("run", "rw")
            self.assertEqual(r.returncode, 2)
            self.assertIn("written only by opsquire run", r.stderr)
            queries = [["ls", "--json"], ["show", "rw", "--json"], ["runs", "--json"], ["last", "--json"]]
            queries += [["log", "rw"], ["log", "rw", "--events"], ["locks", "--json"]]
            for query in queries:
                q = self.cli(*query)
                self.assertEqual(q.returncode, 0, f"{query}: {q.stderr}")
            self.assertIn("other-target", self.cli("locks", "--json").stdout)
        finally:
            for d in dirs:
                d.chmod(0o700)

    def test_home_prints_only_the_path_on_stdout(self) -> None:
        r = self.cli("home")
        self.assertEqual(r.stdout, f"{self.home.resolve()}\n")
        self.assertIn("(from OPSQUIRE_HOME)", r.stderr)

    def test_naming_a_task_without_a_home_says_where_opsquire_looked_and_how_to_make_one(self) -> None:
        missing = self.tmp / "no-home"
        self.env["OPSQUIRE_HOME"] = str(missing)
        r = self.cli("run", "nosuch")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        for words in (str(missing), "OPSQUIRE_HOME", "opsquire init", "opsquire home"):
            self.assertIn(words, r.stderr)
        self.assertFalse(missing.exists(), "nothing creates the home")
        del self.env["OPSQUIRE_HOME"]
        self.config.write_text(json.dumps({"home": str(missing)}))
        r = self.cli("run", "nosuch")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"no home at {missing} (from {self.config})", r.stderr)

    def test_naming_no_task_in_an_existing_home_says_only_that(self) -> None:
        r = self.cli("run", "nosuch")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        errors = [line for line in r.stderr.splitlines() if line.startswith("opsquire: ")]
        self.assertEqual(errors, ["opsquire: no task matches 'nosuch'"])

    def test_new_takes_json_and_explains_a_bad_area(self) -> None:
        made = self.cli("new", "web1", "thing", "--purpose", "p", "--json")
        self.assertEqual(made.returncode, 0, made.stderr)
        data = json.loads(made.stdout)
        self.assertTrue((Path(data["path"]) / "meta.json").is_file())
        self.assertEqual((data["id"], data["waiting_on_same_target"]), (Path(data["path"]).name, []))
        closed = json.loads(self.cli("close", "thing", "--outcome", "abandoned", "--note", "n", "--json").stdout)
        self.assertEqual(closed["outcome"], "abandoned")

        bad = self.cli("new", "db_2", "Other_Name", "--purpose", "p")
        self.assertEqual((bad.returncode, bad.stdout), (2, ""))
        self.assertIn("nothing created", bad.stderr)
        self.assertIn("area 'db_2', use 'db-2'", bad.stderr)
        self.assertIn("name 'Other_Name', use 'other-name'", bad.stderr)
        self.assertIn("--target", bad.stderr)
        self.assertEqual(len(list(self.home.glob("tasks/*/*"))), 1)

    def test_contract_and_skill_say_how_to_call_opsquire_off_path(self) -> None:
        self.env["PATH"] = "/usr/bin:/bin"
        contract = self.cli("contract").stdout
        self.assertNotIn("{{", contract.split("## Local rules")[0])
        self.assertIn("-m opsquire", contract.split("## Layout")[0])
        self.env["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        self.env["HOME"] = str(self.tmp)
        self.assertEqual(self.cli("integrate").returncode, 0)
        skill = (self.tmp / "claude/skills/opsquire/SKILL.md").read_text()
        self.assertNotIn("{{", skill)
        self.assertIn("-m opsquire", skill)

    def test_the_homes_contract_follows_local_rules_and_keeps_a_hand_edit(self) -> None:
        agents, local = self.home / "AGENTS.md", self.home / "LOCAL.md"
        local.write_text("### Rule: first\n")
        self.assertEqual(self.cli("contract", "--write").returncode, 0)
        self.assertIn("### Rule: first", agents.read_text())
        self.assertTrue(agents.read_text().rstrip().endswith("-->"))
        self.new("one", "print(1)\n")
        local.write_text("### Rule: first\n\n### Rule: second\n")
        r = self.cli("lint", "one")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("AGENTS.md rewritten", r.stderr)
        self.assertIn("### Rule: second", agents.read_text())
        self.assertNotIn("AGENTS.md", self.cli("lint", "one").stderr, "quiet when current")

        agents.write_text(agents.read_text().replace("### Rule: second", "### Rule: second\n\nonly here"))
        local.write_text("### Rule: first\n\n### Rule: third\n")
        r = self.cli("lint", "one")
        self.assertIn("edited by hand since opsquire wrote it: move the edit into LOCAL.md", r.stderr)
        self.assertIn("only here", agents.read_text())
        r = self.cli("contract", "--write")
        self.assertEqual(r.returncode, 2)
        self.assertIn("only here", agents.read_text())
        self.assertEqual(self.cli("contract", "--write", "--force").returncode, 0)
        self.assertNotIn("only here", agents.read_text())
        self.assertIn("### Rule: third", agents.read_text())

    def test_integrate_installs_the_stop_hook_once_and_keeps_other_settings(self) -> None:
        claude = self.tmp / "claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps({"model": "x", "hooks": {"Stop": []}}))
        self.env["CLAUDE_CONFIG_DIR"] = str(claude)
        self.env["HOME"] = str(self.tmp)
        for _ in range(2):
            r = self.cli("integrate")
            self.assertEqual(r.returncode, 0, r.stderr)
        settings = json.loads((claude / "settings.json").read_text())
        self.assertEqual(settings["model"], "x")
        commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].endswith("unread --hook"))

    def test_doctor_names_libraries_the_homes_scripts_need_that_are_not_installed(self) -> None:
        self.assertIn("ok   libraries: none declared", self.cli("doctor").stdout)
        (self.home / "requirements.txt").write_text("# for the scripts\nmypy>=1\nno-such-lib-for-opsquire==2\n")
        out = self.cli("doctor").stdout
        self.assertIn("FIX  libraries: no-such-lib-for-opsquire from", out)
        self.assertIn("--with-requirements", out)
        (self.home / "requirements.txt").write_text("mypy\n")
        self.assertIn("ok   libraries: all 1 in requirements.txt installed", self.cli("doctor").stdout)

    def test_doctor_names_the_fix_for_each_contract_state(self) -> None:
        agents = self.home / "AGENTS.md"
        self.assertEqual(self.cli("contract", "--write").returncode, 0)
        self.assertIn("ok   contract: AGENTS.md current", self.cli("doctor").stdout.splitlines())
        with (self.home / "LOCAL.md").open("a") as f:
            f.write("### Rule: more\n")
        outdated = "FIX  contract: AGENTS.md outdated (`opsquire contract --write` rewrites it)"
        self.assertIn(outdated, self.cli("doctor").stdout.splitlines())
        with agents.open("a") as f:
            f.write("only here\n")
        self.assertIn(f"FIX  contract: {contract.EDITED}", self.cli("doctor").stdout.splitlines())
        agents.unlink()
        missing = "FIX  contract: AGENTS.md missing (`opsquire contract --write` writes it)"
        self.assertIn(missing, self.cli("doctor").stdout.splitlines())

    @LINUX_ONLY
    def test_share_gives_the_group_the_whole_home_with_setgid_folders_and_is_idempotent(self) -> None:
        self.umask_022()
        folder = self.new("together", "print('hi')\n")
        self.assertEqual(self.cli("run", "together").returncode, 0)
        (folder / "notes.txt").write_text("mine\n")
        (folder / "notes.txt").chmod(0o600)
        before = self.tree()
        group = self.own_group()
        dry = self.cli("share", group, "--dry")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertRegex(dry.stdout, rf"for group {group}: would change [1-9]\d* paths, \d+ already right")
        self.assertEqual(self.tree(), before, "--dry changes nothing")
        done = self.cli("share", group)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout, r": changed [1-9]\d* paths")
        for path, (mode, gid) in self.tree().items():
            self.assertEqual(gid, os.getegid(), path)
            if stat.S_ISDIR(mode):
                self.assertEqual(stat.S_IMODE(mode) & 0o2070, 0o2070, path)
                self.assertFalse(mode & stat.S_ISVTX, path)
            else:
                self.assertFalse((mode & 0o700) >> 3 & ~mode, f"{path} {oct(mode)}")
        self.assertEqual(stat.S_IMODE((folder / "notes.txt").stat().st_mode), 0o660)
        blobs = [p for p in (self.home / "var/blobs").rglob("*") if p.is_file()]
        self.assertTrue(blobs)
        self.assertEqual({stat.S_IMODE(p.stat().st_mode) for p in blobs}, {0o444}, "blobs stay read-only")
        shared = self.tree()
        again = self.cli("share", group)
        self.assertIn(f": changed 0 paths, {len(shared)} already right", again.stdout)
        self.assertEqual(self.tree(), shared)

    @LINUX_ONLY
    def test_share_leaves_a_file_hard_linked_from_outside_the_home_alone(self) -> None:
        self.umask_022()
        outside = self.tmp / "private"
        outside.write_text("not the group's\n")
        outside.chmod(0o600)
        os.link(outside, self.home / "linked")
        folder = self.new("together", "print('hi')\n")
        self.assertEqual(self.cli("run", "together").returncode, 0)
        r = self.cli("share", self.own_group())
        self.assertEqual(r.returncode, 1)
        self.assertIn("left 1 files hard-linked from outside the home (linked)", r.stderr)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((folder / "main.py").stat().st_mode), 0o664, "the rest is shared")

    @LINUX_ONLY
    def test_share_refuses_a_group_that_does_not_exist_or_you_are_not_in(self) -> None:
        r = self.cli("share", "no-such-group-for-opsquire")
        self.assertEqual(r.returncode, 2)
        self.assertIn("there is no group 'no-such-group-for-opsquire'", r.stderr)
        if os.getegid() == 0 or 0 in os.getgroups():
            self.skipTest("this account is in the root group")
        before = self.tree()
        r = self.cli("share", grp.getgrgid(0).gr_name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("you are not in group", r.stderr)
        self.assertEqual(self.tree(), before)

    @LINUX_ONLY
    def test_share_without_a_home_says_where_opsquire_looked_and_how_to_make_one(self) -> None:
        missing = self.tmp / "no-home"
        self.env["OPSQUIRE_HOME"] = str(missing)
        r = self.cli("share", self.own_group())
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"no home at {missing} (from OPSQUIRE_HOME)", r.stderr)
        self.assertIn("opsquire init", r.stderr)

    @LINUX_ONLY
    def test_a_shared_home_creates_everything_group_writable_and_an_unshared_one_as_before(self) -> None:
        self.umask_022()
        plain = self.new("plain", "print('plain')\n")
        self.assertEqual(self.cli("run", "plain").returncode, 0)
        run = Path(self.last("plain")["paths"]["dir"])
        for path in (
            plain / "main.py",
            plain / "meta.json",
            plain / ".lint.json",
            run / "run.json",
            run / "output.log",
            self.home / "var/locks/locks.json",
            self.home / "var/ledger/entries.jsonl",
            self.home / "var/ledger/checkpoints.jsonl",
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644, path)
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o755)

        shutil.rmtree(self.home / "var")
        shutil.rmtree(self.home / "tasks")
        (self.home / "tasks").mkdir()
        self.assertEqual(self.cli("share", self.own_group()).returncode, 0)
        folder = self.new("together", "print('together')\n", *self.CHANGING)
        self.assertEqual(self.cli("lint", "together").returncode, 0)
        self.assertEqual(self.cli("run", "together").returncode, 0)
        run = Path(self.last("together")["paths"]["dir"])
        for path in (
            folder / "main.py",
            folder / "meta.json",
            folder / ".lint.json",
            run / "run.json",
            run / "output.log",
            run / "heartbeat",
            self.home / "var/locks/locks.json",
            self.home / "var/locks/.mutex",
            self.home / "var/ledger/entries.jsonl",
            self.home / "var/ledger/checkpoints.jsonl",
            self.home / "var/ledger/state.json",
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o664, path)
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o2775)
        with mock.patch.dict(os.environ, self.env):
            from opsquire import share

            self.assertEqual(share.drift(self.home, os.getegid()), [], "everything a run made is the group's")

    @LINUX_ONLY
    def test_a_shared_home_copies_another_operators_blob_instead_of_linking_it(self) -> None:
        from opsquire import store

        self.assertEqual(self.cli("share", self.own_group()).returncode, 0)
        src = self.tmp / "main.py"
        src.write_text("print(1)\n")
        src.chmod(0o644)
        mine, theirs = self.home / "var/runs/a/snapshot/main.py", self.home / "var/runs/b/snapshot/main.py"
        with mock.patch.dict(os.environ, self.env):
            store.place(src, mine)
            with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
                store.place(src, theirs)
        self.assertEqual((mine.stat().st_nlink, stat.S_IMODE(mine.stat().st_mode)), (2, 0o444))
        self.assertEqual((theirs.stat().st_nlink, stat.S_IMODE(theirs.stat().st_mode)), (1, 0o664))

    def test_records_name_the_account_of_the_process_whatever_the_environment_says(self) -> None:
        me = pwd.getpwuid(os.geteuid()).pw_name
        self.env |= {"USER": "mallory", "LOGNAME": "mallory"}
        self.new("who", "print(1)\n")
        self.assertEqual(self.cli("run", "who").returncode, 0)
        rec = self.last("who")
        self.assertEqual((rec["user"], rec["uid"]), (me, os.geteuid()))
        start = next(e for e in self.events(rec) if e["event"] == "run_start")
        self.assertEqual((start["user"], start["uid"]), (me, os.geteuid()))
        lines = (self.home / "var/ledger/entries.jsonl").read_text().splitlines()
        entries = [json.loads(line) for line in lines]
        self.assertEqual({(e["actor"]["user"], e["actor"]["uid"]) for e in entries}, {(me, os.geteuid())})
        acquire = next(e["data"] for e in entries if e["kind"] == "lock" and e["data"]["event"] == "acquire")
        self.assertEqual((acquire["holder"]["user"], acquire["holder"]["uid"]), (me, os.geteuid()))
        with mock.patch.dict(os.environ, self.env):
            from opsquire.model import user_name

            self.assertEqual(user_name(), me)

    def test_home_git_trusts_only_the_repository_it_runs_in_and_still_refuses_unsafe_config(self) -> None:
        from opsquire import homegit

        self.git_home()
        env = self.env | {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
        foreign = subprocess.run(["git", "-C", str(self.home), "status"], env=env, capture_output=True, text=True)
        if "dubious ownership" not in foreign.stderr:
            self.skipTest("this git cannot act as if another user owned the repository")
        calls: list[list[str]] = []
        real = subprocess.run

        def spy(argv: list[str], *args: Any, **kwargs: Any) -> Any:
            calls.append(list(argv))
            return real(argv, *args, **kwargs)

        with mock.patch.dict(os.environ, env), mock.patch("subprocess.run", spy):
            (self.home / "tasks/note.md").write_text("x\n")
            self.assertTrue(homegit.commit(self.home, None, "another operator's commit"))
            config = self.home / ".git/config"
            clean = config.read_text()
            config.write_text(clean + "[alias]\n\tst = status\n")
            with self.assertRaises(homegit.UnsafeGit):
                homegit.git(self.home, "status")
            config.write_text(clean)
        located = [argv for argv in calls if "-C" in argv]
        self.assertTrue(located)
        for argv in located:
            self.assertIn(f"safe.directory={Path(argv[argv.index('-C') + 1]).resolve()}", argv, argv)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "another operator's commit")

    @LINUX_ONLY
    def test_doctor_names_the_group_of_a_shared_home_and_what_it_cannot_write(self) -> None:
        self.umask_022()
        self.assertNotIn("shared home", self.cli("doctor").stdout)
        group = self.own_group()
        self.assertEqual(self.cli("share", group).returncode, 0)
        self.assertIn(f"ok   shared home: group {group}; everything in it is the group's", self.cli("doctor").stdout)
        (self.home / "tasks/stray.txt").write_text("written with umask 022\n")
        self.assertIn(
            f"FIX  shared home: group {group}; the group cannot read and write 1 of its paths (tasks/stray.txt): "
            f"`opsquire share {group}`",
            self.cli("doctor").stdout,
        )

    @LINUX_ONLY
    def test_lint_in_a_shared_home_gives_the_group_the_callers_own_files_and_leaves_others(self) -> None:
        from opsquire import share

        self.umask_022()
        self.assertEqual(self.cli("share", self.own_group()).returncode, 0)
        folder = self.new("edited", "print('edited')\n")
        (folder / "main.py").chmod(0o644)
        (folder / "data").mkdir()
        (folder / "data/hosts.txt").write_text("web1\n")
        self.assertNotEqual(stat.S_IMODE((folder / "data").stat().st_mode) & 0o070, 0o070)
        with mock.patch.dict(os.environ, self.env):
            with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
                share.adopt([folder])
            self.assertEqual(stat.S_IMODE((folder / "main.py").stat().st_mode), 0o644, "another operator's: untouched")
            self.assertEqual(self.cli("lint", "edited").returncode, 0)
            self.assertEqual(share.drift(folder, os.getegid()), [])
        self.assertEqual(stat.S_IMODE((folder / "main.py").stat().st_mode), 0o664)
        self.assertEqual(stat.S_IMODE((folder / "data").stat().st_mode), 0o2775)
        self.assertEqual(stat.S_IMODE((folder / "data/hosts.txt").stat().st_mode), 0o664)

    def test_a_run_keeps_its_home_when_the_configs_home_moves_meanwhile(self) -> None:
        flag = self.tmp / "go"
        body = (
            "import time\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\nprint('start', flush=True)\n"
            f"while not Path({str(flag)!r}).exists():\n    time.sleep(0.05)\nsq.result('finished')\n"
        )
        folder = self.new("long", body)
        other = self.tmp / "other-home"
        (other / "tasks").mkdir(parents=True)
        self.config.write_text(json.dumps({"home": str(self.home)}))
        env = {k: v for k, v in self.env.items() if k != "OPSQUIRE_HOME"}
        p = subprocess.Popen(
            [*CLI, "run", folder.name], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert p.stdout
        while "start" not in (line := p.stdout.readline()):
            self.assertTrue(line, "the run ended before its script started")
        self.config.write_text(json.dumps({"home": str(other)}))
        flag.touch()
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out + err)
        rec = self.last(folder.name)
        self.assertEqual((rec["status"], rec["results"]), ("ok", ["finished"]), "it finished in the home it began in")
        self.assertFalse((other / "var").exists())
