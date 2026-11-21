"""Repo copies and GitHub-signed PRs."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from unittest import mock

from support import LINUX_ONLY, OpsTestCase


class RepoTest(OpsTestCase):
    def test_state_gives_the_console_each_repos_github_slug_for_links(self) -> None:
        self.code_repo()
        code = "import json; from opsquire.state import repo_slugs; print(json.dumps(repo_slugs()))"
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(json.loads(r.stdout), {"shop": "acme/shop"}, r.stderr)

    def test_repo_pr_pushes_a_copy_as_signed_commits_and_updates_the_pr(self) -> None:
        repo = self.code_repo()
        base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        self.env["OPSQUIRE_ASK_ID"] = "ask-1"
        r = self.cli("repo", "checkout", "shop")
        self.assertEqual(r.returncode, 0, r.stderr)
        copy = Path(r.stdout.strip())
        self.assertEqual(copy, self.home / ".repos/ask-1/shop")
        self.assertEqual(self.cli("repo", "checkout", "shop").stdout.strip(), str(copy))
        (copy / "src/app.py").write_text("x = 2\n")
        (copy / "src/new.py").write_text("y = 1\n")
        (copy / "old.txt").unlink()
        (copy / "src/__pycache__").mkdir()
        (copy / "src/__pycache__/app.cpython-310.pyc").write_bytes(b"junk")
        body = self.tmp / "body.md"
        body.write_text("Why: because\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Fix the app", "--body-file", str(body))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PR: https://github.com/acme/shop/pull/1", r.stdout)
        state = self.gh_state()
        self.assertEqual(state["prs"][0]["head"], "dev/fix-the-app")
        first = state["commits"][0]
        self.assertEqual(first["added"], {"src/app.py": "x = 2\n", "src/new.py": "y = 1\n"})
        self.assertEqual(first["deleted"], ["old.txt"])
        calls = [json.loads(x) for x in (self.tmp / "gh.log").read_text().splitlines()]
        refs = [c for c in calls if c[:3] == ["api", "-X", "POST"]]
        self.assertIn(f"sha={base.strip()}", refs[0])
        (copy / "README.md").write_text("better\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Fix the app")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.gh_state()
        self.assertEqual(state["commits"][1]["added"], {"README.md": "better\n"})
        self.assertEqual(state["commits"][1]["deleted"], [])
        self.assertEqual(len(state["prs"]), 1)
        self.assertEqual((repo / "src/app.py").read_text(), "x = 1\n")
        self.assertEqual(self.cli("repo", "pr", str(copy), "--title", "Fix the app").returncode, 0)
        self.assertEqual(len(self.gh_state()["commits"]), 2)
        (copy / "src/new.py").unlink()
        r = self.cli("repo", "pr", str(copy), "--title", "Fix the app")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.gh_state()["commits"][2]["deleted"], ["src/new.py"], "a pushed new file can go again")

    def test_repo_checkout_of_another_branch_commits_onto_it_only_while_it_is_unmoved(self) -> None:
        repo = self.code_repo()

        def git(*args: str) -> str:
            out = subprocess.run(["git", "-C", str(repo), *args], env=self.env, check=True, capture_output=True)
            return out.stdout.decode().strip()

        git("checkout", "-q", "-b", "peer/review")
        (repo / "src/app.py").write_text("x = 1\npeer = True\n")
        git("commit", "-qam", "peer")
        head = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/peer/review", head)
        state_path = self.tmp / "gh.json"
        state_path.write_text(
            json.dumps(
                {
                    "refs": {"peer/review": head},
                    "commits": [],
                    "prs": [{"url": "https://github.com/acme/shop/pull/7", "head": "peer/review", "base": "main"}],
                }
            )
        )
        self.env["OPSQUIRE_ASK_ID"] = "ask-1"
        copy = Path(self.cli("repo", "checkout", "shop", "--base", "peer/review").stdout.strip())
        self.assertEqual(copy, self.home / ".repos/ask-1/shop-peer-review")
        self.assertEqual((copy / "src/app.py").read_text(), "x = 1\npeer = True\n")
        (copy / "src/app.py").write_text("x = 1\npeer = True\nmine = True\n")
        body = self.tmp / "body.md"
        body.write_text("No PR is opened, so this explains the commit.\n")
        r = self.cli(
            "repo", "pr", str(copy), "--title", "Add mine", "--branch", "peer/review", "--body-file", str(body)
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PR: https://github.com/acme/shop/pull/7", r.stdout)
        self.assertEqual(self.gh_state()["commits"][0]["body"], "No PR is opened, so this explains the commit.")
        gh = self.gh_state()
        self.assertEqual(gh["commits"][0]["branch"], "peer/review")
        self.assertEqual(gh["commits"][0]["added"], {"src/app.py": "x = 1\npeer = True\nmine = True\n"})
        self.assertEqual(len(gh["prs"]), 1)
        gh["refs"]["peer/review"] = "f" * 40
        state_path.write_text(json.dumps(gh))
        (copy / "README.md").write_text("later\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Add mine", "--branch", "peer/review")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("expectedHeadOid mismatch", r.stderr)
        other = Path(self.cli("repo", "checkout", "shop", "--base", "peer/review").stdout.strip())
        self.assertEqual(other, copy)
        main_copy = Path(self.cli("repo", "checkout", "shop").stdout.strip())
        (main_copy / "README.md").write_text("direct\n")
        r = self.cli("repo", "pr", str(main_copy), "--title", "Direct", "--branch", "main")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to commit straight onto main", r.stderr)

    def test_repo_checkout_refuses_a_base_that_is_not_a_branch_name(self) -> None:
        self.code_repo()
        for bad in ("--upload-pack=touch pwned", "a..b", "x y"):
            r = self.cli("repo", "checkout", "shop", f"--base={bad}")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("not a branch name", r.stderr)
        self.assertFalse((self.home / ".repos").exists())

    def test_repo_pr_onto_a_branch_without_a_pr_does_not_repeat_the_commit(self) -> None:
        repo = self.code_repo()
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/peer/review", head.strip()],
            env=self.env,
            check=True,
        )
        (self.tmp / "gh.json").write_text(json.dumps({"refs": {"peer/review": head.strip()}, "commits": [], "prs": []}))
        copy = Path(self.cli("repo", "checkout", "shop", "--base", "peer/review").stdout.strip())
        (copy / "README.md").write_text("on top\n")
        for _ in range(2):
            r = self.cli("repo", "pr", str(copy), "--title", "On top", "--branch", "peer/review")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Pushed to peer/review (no open PR)", r.stdout)
        self.assertEqual(len(self.gh_state()["commits"]), 1)
        self.assertEqual(self.gh_state()["prs"], [])

    def test_repo_pr_into_a_branch_the_home_marks_auto_deploy_warns(self) -> None:
        self.code_repo()
        cfg = json.loads((self.home / "repos.json").read_text())
        cfg["repos"]["shop"]["auto_deploy"] = ["main"]
        (self.home / "repos.json").write_text(json.dumps(cfg))
        copy = Path(self.cli("repo", "checkout", "shop").stdout.strip())
        (copy / "README.md").write_text("ship it\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Ship it")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: main auto-deploys", r.stderr)

    def test_repo_pr_from_another_base_opens_the_pr_against_that_base(self) -> None:
        repo = self.code_repo()
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/peer/review", head.strip()],
            env=self.env,
            check=True,
        )
        copy = Path(self.cli("repo", "checkout", "shop", "--base", "peer/review").stdout.strip())
        (copy / "README.md").write_text("on top\n")
        r = self.cli("repo", "pr", str(copy), "--title", "On top")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dev/on-top -> peer/review", r.stdout)
        self.assertEqual(self.gh_state()["prs"][0]["base"], "peer/review")

    def test_chat_checkouts_never_share_a_copy(self) -> None:
        self.code_repo()
        first = self.cli("repo", "checkout", "shop")
        second = self.cli("repo", "checkout", "shop")
        self.assertEqual(first.returncode, 0, first.stderr)
        a, b = Path(first.stdout.strip()), Path(second.stdout.strip())
        self.assertNotEqual(a, b)
        self.assertEqual({a.parent, b.parent}, {self.home / ".repos/chat"})
        (a / "README.md").write_text("from session a\n")
        self.assertEqual((b / "README.md").read_text(), "hello\n")

    def test_prune_removes_idle_chat_copies_only_when_every_edit_is_on_github(self) -> None:
        self.code_repo()
        untouched = self.checkout_copy()
        pushed = self.checkout_copy()
        (pushed / "README.md").write_text("pushed\n")
        (pushed / "old.txt").unlink()
        self.assertEqual(self.cli("repo", "pr", str(pushed), "--title", "Pushed").returncode, 0)
        (pushed / "c/__pycache__/k.pyc").write_text("rebuilt")
        later = self.checkout_copy()
        (later / "README.md").write_text("pushed\n")
        self.assertEqual(self.cli("repo", "pr", str(later), "--title", "Later").returncode, 0)
        (later / "src/app.py").write_text("x = 2\n")
        touched_back = self.checkout_copy()
        mtime = (touched_back / "src/app.py").stat().st_mtime
        (touched_back / "src/app.py").write_text("x = 9\n")
        os.utime(touched_back / "src/app.py", (mtime, mtime))
        deleted = self.checkout_copy()
        (deleted / "old.txt").unlink()
        legacy = self.checkout_copy()
        state = json.loads((legacy / ".opsquire-pr.json").read_text())
        del state["extracted"]
        (legacy / ".opsquire-pr.json").write_text(json.dumps(state))
        (legacy / "README.md").write_text("legacy edit\n")
        leftover = self.home / ".repos/chat/.shop.123.tmp"
        leftover.mkdir()
        os.utime(leftover, (time.time() - 2 * 86400,) * 2)

        r = self.cli("repo", "prune", "--days", "0", "--dry", "--json")
        out = json.loads(r.stdout)
        self.assertEqual(sorted(c["path"] for c in out["removed"]), sorted(map(str, [untouched, pushed, leftover])))
        self.assertTrue(untouched.is_dir() and leftover.is_dir())
        r = self.cli("repo", "prune", "--days", "1")
        self.assertEqual((r.returncode, r.stdout), (0, "removed 0 chat repo copies idle 1+ days (0 B)\n"), r.stderr)
        self.assertFalse(leftover.exists(), "an interrupted checkout's leftover goes after a day, whatever --days says")
        kept = {c["path"]: c["unpushed"] for c in out["kept"]}
        self.assertEqual(
            kept,
            {
                str(later): ["src/app.py"],
                str(touched_back): ["src/app.py"],
                str(deleted): ["old.txt"],
                str(legacy): ["README.md"],
            },
        )
        r = self.cli("repo", "prune", "--days", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("removed 2 chat repo copies idle 0+ days", r.stdout)
        self.assertIn(f"kept {later} (unpushed: src/app.py)", r.stdout)
        self.assertEqual(
            sorted(p.name for p in (self.home / ".repos/chat").iterdir()),
            sorted(p.name for p in (later, touched_back, deleted, legacy)),
        )

    def test_the_daily_sweep_prunes_idle_chat_copies(self) -> None:
        self.offboard_env(offboard_after_days=1 / 86400)
        self.code_repo()
        copy = self.checkout_copy()
        edited = self.checkout_copy()
        (edited / "README.md").write_text("unpushed\n")
        time.sleep(1.5)
        r = self.cli("new", "web1", "tidy", "--purpose", "rides the daily sweep")
        self.assertIn("opsquire: removed 1 chat repo copies", r.stderr)
        self.assertIn(f"kept {edited} (unpushed: README.md)", r.stderr)
        self.assertFalse(copy.exists())

    def test_repo_pr_refuses_a_new_title_on_a_copy_with_an_earlier_branch(self) -> None:
        self.code_repo()
        copy = Path(self.cli("repo", "checkout", "shop").stdout.strip())
        (copy / "README.md").write_text("first\n")
        self.assertEqual(self.cli("repo", "pr", str(copy), "--title", "First change").returncode, 0)
        (copy / "src/app.py").write_text("x = 3\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Unrelated change")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already pushed to branch dev/first-change", r.stderr)
        self.assertIn("--branch dev/first-change", r.stderr)
        self.assertEqual(len(self.gh_state()["commits"]), 1)
        state = json.loads((copy / ".opsquire-pr.json").read_text())
        del state["title"]
        (copy / ".opsquire-pr.json").write_text(json.dumps(state))
        r = self.cli("repo", "pr", str(copy), "--title", "First change")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("an earlier PR", r.stderr)
        r = self.cli("repo", "pr", str(copy), "--title", "Unrelated change", "--branch", "dev/first-change")
        self.assertEqual(r.returncode, 0, r.stderr)
        gh = self.gh_state()
        self.assertEqual([c["branch"] for c in gh["commits"]], ["dev/first-change"] * 2)
        self.assertEqual(gh["commits"][1]["added"], {"src/app.py": "x = 3\n"})
        r = self.cli("repo", "pr", str(copy), "--title", "Split out", "--branch", "dev/split-out")
        self.assertEqual(r.returncode, 0, r.stderr)
        gh = self.gh_state()
        self.assertEqual(gh["commits"][2]["branch"], "dev/split-out")
        self.assertEqual(gh["commits"][2]["added"], {"README.md": "first\n", "src/app.py": "x = 3\n"})
        self.assertEqual(len(gh["prs"]), 2)

    def test_ask_hands_over_its_pr_and_drops_its_repo_copy(self) -> None:
        self.code_repo()
        self.agents_file()
        self.env |= {"FAKE_REPO": "shop", "FAKE_STATE": str(self.tmp / "fake-state.json")}
        r = self.cli("ask", "--no-split", "change", "the", "readme")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PR: https://github.com/acme/shop/pull/1", r.stdout)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual([a["kind"] for a in rec["actions"]], ["RUN", "RUN", "PR"])
        self.assertEqual(self.gh_state()["commits"][0]["added"], {"README.md": "changed by the agent\n"})
        self.assertFalse((self.home / ".repos").exists())

    def test_home_commits_never_include_repo_copies(self) -> None:
        self.git_home()
        self.code_repo()
        self.assertEqual(self.cli("repo", "checkout", "shop").returncode, 0)
        self.new("tidy", "print(1)\n")
        self.assertEqual(self.cli("lint", "tidy").returncode, 0)
        r = self.cli("run", "tidy")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([f for f in self.git("ls-files").splitlines() if f.startswith(".repos")], [])

    def test_repo_warns_about_a_stale_copy_and_open_prs_on_the_same_files(self) -> None:
        repo = self.code_repo()
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], env=self.env, check=True)
        for cmd in (["remote", "add", "origin", str(origin)], ["push", "-q", "origin", "HEAD:refs/heads/main"]):
            subprocess.run(["git", "-C", str(repo), *cmd], env=self.env, check=True, capture_output=True)
        listed = json.loads(self.cli("repo", "list", "--json").stdout)[0]
        self.assertIn("up to date", listed["local"])
        fresh = self.cli("repo", "checkout", "shop")
        self.assertEqual(fresh.stderr, "", "origin matches")
        copy = Path(fresh.stdout.strip())

        other = self.tmp / "other"
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(other)], cwd=repo, env=self.env, check=True)
        (other / "moved.txt").write_text("x\n")
        for cmd in (
            ["add", "-A"],
            ["commit", "-q", "-m", "moved"],
            ["push", "-q", "origin", "HEAD:refs/heads/main"],
        ):
            subprocess.run(["git", "-C", str(other), *cmd], env=self.env, check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD~0"], env=self.env, check=True
        )
        listed = json.loads(self.cli("repo", "list", "--json").stdout)[0]
        self.assertRegex(listed["local"], r"1 behind origin/main")
        with mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home)}):
            from opsquire import repos

            note = repos.stale_copy_note(copy)
        self.assertIn("could not fetch", note)
        self.assertIn(f"git -C {repo} fetch origin main", note)

        (copy / "src/app.py").write_text("x = 3\n")
        (self.tmp / "gh.json").write_text(
            json.dumps(
                {
                    "refs": {},
                    "commits": [],
                    "prs": [],
                    "open_prs": [
                        {
                            "url": "https://github.com/acme/shop/pull/9",
                            "author": {"login": "colleague"},
                            "headRefName": "their-fix",
                            "files": [{"path": "src/app.py"}, {"path": "unrelated.py"}],
                        },
                        {
                            "url": "https://github.com/acme/shop/pull/10",
                            "author": {"login": "other"},
                            "headRefName": "elsewhere",
                            "files": [{"path": "unrelated.py"}],
                        },
                    ],
                }
            )
        )
        found = json.loads(self.cli("repo", "overlap", str(copy), "--json").stdout)
        self.assertEqual(
            found,
            [
                {
                    "url": "https://github.com/acme/shop/pull/9",
                    "author": "colleague",
                    "branch": "their-fix",
                    "files": ["src/app.py"],
                }
            ],
        )
        body = self.tmp / "body.md"
        body.write_text("why\n")
        opened = self.cli("repo", "pr", str(copy), "--title", "fix app", "--body-file", str(body))
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIn("pull/9 (colleague) also changes src/app.py", opened.stderr)

    @LINUX_ONLY
    def test_a_shared_homes_repo_copies_keep_the_groups_access(self) -> None:
        from opsquire import repos

        self.assertEqual(self.cli("share", self.own_group()).returncode, 0)
        archive = self.tmp / "repo.tar"
        with tarfile.open(archive, "w") as tar:
            folder = tarfile.TarInfo("pkg")
            folder.type, folder.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(folder)
            script = tarfile.TarInfo("pkg/run.sh")
            script.mode, script.size = 0o755, 5
            tar.addfile(script, io.BytesIO(b"echo\n"))
        dest = self.home / "copy"
        dest.mkdir()
        with mock.patch.dict(os.environ, self.env), tarfile.open(archive) as tar:
            tar.extractall(dest, filter=repos.group_filter)
        self.assertEqual(stat.S_IMODE((dest / "pkg").stat().st_mode), 0o2775)
        self.assertEqual(stat.S_IMODE((dest / "pkg/run.sh").stat().st_mode), 0o775)
