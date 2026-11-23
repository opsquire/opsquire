"""The release and signed-commit scripts."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from support import ROOT


class ReleaseScripts(unittest.TestCase):
    """scripts/release.py and scripts/signed_commit.py: what the release workflow and every push to main rely on."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.env = {**os.environ}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def load(name: str) -> Any:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_release_bumps_and_rewrites_the_version(self) -> None:
        release = self.load("release")
        self.assertEqual(release.bump("0.1.9", "patch"), "0.1.10")
        self.assertEqual(release.bump("0.1.9", "minor"), "0.2.0")
        self.assertEqual(release.bump("0.1.9", "major"), "1.0.0")
        text = 'name = "opsquire"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
        self.assertEqual(release.current_version(text), "0.1.0")
        self.assertEqual(release.set_version("0.2.0", text), text.replace("0.1.0", "0.2.0"))
        with self.assertRaises(release.ReleaseError):
            release.set_version("0.2", text)
        with self.assertRaises(release.ReleaseError):
            release.current_version("name = x\n")
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertEqual(release.current_version(), pyproject.split('version = "')[1].split('"')[0])

    def test_release_notes_group_commits_by_area_and_skip_release_commits(self) -> None:
        release = self.load("release")
        commits = [
            ("release: v0.2.0", ""),
            ("ui: run any task from its pane", "Why it matters.\n\nSecond paragraph is left out."),
            ("close: record how a task ended", ""),
            ("Merge branch fix", ""),
            ("ui: simple mode first", "One line."),
        ]
        text = release.notes("v0.2.0", "v0.1.0", commits, "acme/opsquire")
        self.assertEqual(
            text,
            "### close\n- record how a task ended\n\n"
            "### other\n- Merge branch fix\n\n"
            "### ui\n- run any task from its pane\n  Why it matters.\n- simple mode first\n  One line.\n\n"
            "**Full changelog**: https://github.com/acme/opsquire/compare/v0.1.0...v0.2.0\n",
        )
        first = release.notes("v0.1.0", None, [("release: v0.1.0", "")], "acme/opsquire")
        self.assertIn("No changes since the previous release.", first)
        self.assertIn("https://github.com/acme/opsquire/commits/v0.1.0", first)

    def test_release_has_no_notes_subcommand_and_still_prints_the_version(self) -> None:
        release = self.load("release")
        for argv in (["notes", "v1.0.0"], ["notes", "v1.0.0", "--since", "v0.9.0"]):
            with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
                release.main(argv)
            self.assertEqual(raised.exception.code, 2, argv)
        self.assertFalse(hasattr(release, "previous_tag"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(release.main(["version"]), 0)
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertEqual(out.getvalue(), pyproject.split('version = "')[1].split('"')[0] + "\n")

    def signed_checkout(self) -> tuple[Path, Path]:
        """A bare 'GitHub' repository with one commit on main, a clone of it, and a fake gh that commits into the bare
        repository the way createCommitOnBranch would."""
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        bare, clone = self.tmp / "github" / "acme" / "opsquire.git", self.tmp / "clone"
        bare.parent.mkdir(parents=True)
        self.git(bare.parent, "init", "-q", "--bare", "-b", "main", str(bare))
        seed = self.tmp / "seed"
        seed.mkdir()
        self.git(seed, "init", "-q", "-b", "main")
        (seed / "pyproject.toml").write_text('version = "0.1.0"\n')
        (seed / "old.txt").write_text("old\n")
        self.git(seed, "add", "-A")
        self.git(seed, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")
        self.git(seed, "push", "-q", str(bare), "main")
        self.git(self.tmp, "clone", "-q", f"file://{bare}", str(clone))
        self.git(clone, "remote", "set-url", "origin", "https://github.com/acme/opsquire.git")
        self.git(clone, "config", f"url.file://{bare}.insteadOf", "https://github.com/acme/opsquire.git")
        bin_dir = self.tmp / "gh-bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(f'#!/bin/sh\nexec {sys.executable} {ROOT / "tests/fake_gh.py"} "$@"\n')
        (bin_dir / "gh").chmod(0o755)
        self.env |= {
            "PATH": f"{bin_dir}:{self.env['PATH']}",
            "FAKE_GH_STATE": str(self.tmp / "gh.json"),
            "FAKE_GH_LOG": str(self.tmp / "gh.log"),
            "FAKE_GH_REMOTE": str(bare),
        }
        return bare, clone

    def git(self, cwd: Path, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(cwd), *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def signed_commit(self, clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(ROOT / "scripts/signed_commit.py"), "--checkout", str(clone), *args]
        return subprocess.run(cmd, env=self.env, capture_output=True, text=True)

    def test_signed_commit_sends_the_staged_files_and_moves_the_checkout_onto_the_commit(self) -> None:
        bare, clone = self.signed_checkout()
        (clone / "pyproject.toml").write_text('version = "0.2.0"\n')
        (clone / "new.txt").write_bytes(b"new \xff bytes\n")
        (clone / "old.txt").unlink()
        (clone / "unstaged.txt").write_text("not yet\n")
        self.git(clone, "add", "pyproject.toml", "new.txt", "old.txt")
        r = self.signed_commit(clone, "-m", "release: v0.2.0", "-m", "Second paragraph.")
        self.assertEqual(r.returncode, 0, r.stderr)
        oid = r.stdout.strip()
        self.assertEqual(self.git(bare, "rev-parse", "main"), oid)
        message = self.git(bare, "log", "-1", "--format=%s%n%n%b", "main")
        self.assertEqual(message, "release: v0.2.0\n\nSecond paragraph.")
        self.assertEqual(self.git(bare, "show", "main:pyproject.toml"), 'version = "0.2.0"')
        self.assertEqual(self.git(bare, "ls-tree", "--name-only", "main").split(), ["new.txt", "pyproject.toml"])
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), oid)
        self.assertEqual(self.git(clone, "rev-parse", "origin/main"), oid)
        self.assertEqual(self.git(clone, "status", "--porcelain"), "?? unstaged.txt")
        self.assertEqual((clone / "new.txt").read_bytes(), b"new \xff bytes\n")

    def test_export_sends_only_what_differs_in_parts_and_finds_its_last_export(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(ROOT / "scripts"))
        export = self.load("export")
        ours = {"a.py": "1", "b.py": "2", "c.md": "3"}
        theirs = {"a.py": "1", "b.py": "old", "gone.txt": "4"}
        self.assertEqual(export.changes(ours, theirs), (["b.py", "c.md"], ["gone.txt"]))
        sizes = {"a": 200, "b": 200, "big": 900, "c": 50}
        self.assertEqual(export.parts(sizes, limit=400), [["a", "b"], ["big"], ["c"]], "a big file goes alone")
        self.assertEqual(export.parts({"x": 10}, limit=400), [["x"]])
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(export.exported_from(f"opsquire 0.9.0\n\nnotes\n\nExported from {sha}"), sha)
        self.assertIsNone(export.exported_from("Fix a typo"), "an outside change is no export")

    def export_checkout(self) -> tuple[Any, Path, Path]:
        """scripts/export.py loaded against a private checkout whose origin is a bare repository, and the public bare
        repository the fake gh answers for, holding one commit no export made. Listed words are made up."""
        public, _ = self.signed_checkout()
        private, origin = self.tmp / "private", self.tmp / "private.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(private))
        (private / "pyproject.toml").write_text('version = "0.3.0"\n')
        (private / "README.md").write_text("opsquire\n")
        (private / "src").mkdir()
        (private / "src" / "app.py").write_text("print('hello')\n")
        self.commit(private, "app: hello")
        sys.path.insert(0, str(ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(ROOT / "scripts"))
        export = self.load("export")
        self.addCleanup(export.clean_text.words.cache_clear)
        for patcher in (
            mock.patch.dict(os.environ, {**self.env, "OPSQUIRE_BANNED_WORDS": "zorblax\nquuxly"}),
            mock.patch.object(export, "ROOT", private),
            mock.patch.object(export.release, "ROOT", private),
            mock.patch.object(export.release, "PYPROJECT", private / "pyproject.toml"),
            mock.patch.object(export.clean_text, "LIST_FILE", self.tmp / "no-banned-words.txt"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        export.clean_text.words.cache_clear()
        return export, public, private

    def commit(self, repo: Path, message: str, push: bool = True) -> None:
        self.git(repo, "add", "-A")
        self.git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
        if push:
            self.git(repo, "push", "-q", "origin", "main")

    def run_export(self, export: Any, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = export.main(["--to", "acme/opsquire", *args])
        return code, out.getvalue(), err.getvalue()

    def assert_nothing_sent(self, public: Path, head: str) -> None:
        self.assertEqual(self.git(public, "rev-parse", "main"), head, "the public main has not moved")
        log = self.tmp / "gh.log"
        self.assertNotIn('"graphql"', log.read_text() if log.exists() else "")

    def assert_export_refused(self, export: Any, public: Path, reason: str) -> None:
        head = self.git(public, "rev-parse", "main")
        code, _, err = self.run_export(export)
        self.assertEqual(code, 2, err)
        self.assertIn(reason, err)
        self.assert_nothing_sent(public, head)

    def test_export_refuses_without_a_word_list(self) -> None:
        export, public, _ = self.export_checkout()
        del os.environ["OPSQUIRE_BANNED_WORDS"]
        export.clean_text.words.cache_clear()
        self.assert_export_refused(export, public, "no word list here")

    def test_export_refuses_uncommitted_changes(self) -> None:
        export, public, private = self.export_checkout()
        (private / "README.md").write_text("opsquire, changed\n")
        self.assert_export_refused(export, public, "uncommitted changes")

    def test_export_refuses_a_commit_not_pushed_to_origin_main(self) -> None:
        export, public, private = self.export_checkout()
        (private / "README.md").write_text("opsquire, changed\n")
        self.commit(private, "docs: not pushed", push=False)
        self.assert_export_refused(export, public, "HEAD is not origin/main")

    def test_export_refuses_a_public_main_with_changes_no_export_made(self) -> None:
        export, public, _ = self.export_checkout()
        code, _, err = self.run_export(export, "--dry")
        self.assertEqual(code, 0, f"a single public commit without the line comes before the first export: {err}")
        outside = self.tmp / "outside"
        self.git(self.tmp, "clone", "-q", str(public), str(outside))
        (outside / "fix.txt").write_text("an accepted pull request\n")
        self.commit(outside, "Fix a typo")
        self.assert_export_refused(export, public, "holds changes no export made")

    def test_export_refuses_a_listed_word_in_a_file_it_writes(self) -> None:
        export, public, private = self.export_checkout()
        (private / "src" / "app.py").write_text("print('restart the zorblax')\n")
        self.commit(private, "app: say what restarts")
        self.assert_export_refused(export, public, "src/app.py: zorblax")

    def test_export_dry_shows_everything_and_changes_nothing(self) -> None:
        export, public, private = self.export_checkout()
        head, private_head = self.git(public, "rev-parse", "main"), self.git(private, "rev-parse", "HEAD")
        code, out, err = self.run_export(export, "--dry")
        self.assertEqual(code, 0, err)
        self.assertIn("as opsquire 0.3.0 to acme/opsquire: 3 files to write, 1 to delete,", out)
        self.assertIn("in 1 commit(s); the word list found nothing.", out)
        self.assertIn("The first public release of opsquire.", out)
        self.assert_nothing_sent(public, head)
        code, out, err = self.run_export(export, "--bump", "patch", "--dry")
        self.assertEqual(code, 0, err)
        self.assertIn("(--dry: the version would first become 0.3.1 here)", out)
        self.assertEqual((private / "pyproject.toml").read_text(), 'version = "0.3.0"\n')
        self.assertEqual(self.git(private, "rev-parse", "HEAD"), private_head)
        self.assert_nothing_sent(public, head)

    def test_export_sends_the_private_tree_and_then_only_what_changed(self) -> None:
        export, public, private = self.export_checkout()
        code, _, err = self.run_export(export)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.git(public, "rev-parse", "main^{tree}"), self.git(private, "rev-parse", "HEAD^{tree}"))
        message = self.git(public, "log", "-1", "--format=%B", "main")
        self.assertTrue(message.endswith(f"Exported from {self.git(private, 'rev-parse', 'HEAD')}"), message)
        self.assertIn("The first public release of opsquire.", message)
        (private / "README.md").write_text("opsquire, changed\n")
        (private / "NEWS.md").write_text("news\n")
        (private / "src" / "app.py").unlink()
        self.commit(private, "docs: news instead of the app")
        code, _, err = self.run_export(export)
        self.assertEqual(code, 0, err)
        sent = json.loads((self.tmp / "gh.json").read_text())["commits"][-1]
        self.assertEqual((sorted(sent["added"]), sent["deleted"]), (["NEWS.md", "README.md"], ["src/app.py"]))
        self.assertEqual(self.git(public, "rev-parse", "main^{tree}"), self.git(private, "rev-parse", "HEAD^{tree}"))
        message = self.git(public, "log", "-1", "--format=%B", "main")
        self.assertTrue(message.endswith(f"Exported from {self.git(private, 'rev-parse', 'HEAD')}"), message)

    def test_export_too_big_for_one_commit_goes_in_parts_and_only_the_last_names_its_source(self) -> None:
        export, public, private = self.export_checkout()
        for name in ("big1.txt", "big2.txt"):
            (private / name).write_text("x" * (export.PART_BYTES * 2 // 3) + "\n")
        self.commit(private, "docs: two big files")
        code, out, err = self.run_export(export)
        self.assertEqual(code, 0, err)
        self.assertIn("in 2 commit(s)", out)
        self.assertEqual(self.git(public, "rev-list", "--count", "main"), "3", "the public seed and two parts")
        self.assertNotIn("Exported from", self.git(public, "log", "-1", "--format=%B", "main~1"))
        message = self.git(public, "log", "-1", "--format=%B", "main")
        self.assertTrue(message.endswith(f"Exported from {self.git(private, 'rev-parse', 'HEAD')}"), message)
        self.assertEqual(self.git(public, "rev-parse", "main^{tree}"), self.git(private, "rev-parse", "HEAD^{tree}"))

    def test_signed_commit_refuses_words_that_never_belong_here(self) -> None:
        bare, clone = self.signed_checkout()
        word = "zorblax"
        self.env["OPSQUIRE_BANNED_WORDS"] = f"{word}\nquuxly"
        (clone / "a.txt").write_text(f"restart the {word}\n")
        self.git(clone, "add", "a.txt")
        r = self.signed_commit(clone, "-m", "x: add a")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"a.txt: {word}", r.stderr)
        (clone / "a.txt").write_text("restart the workers\n")
        self.git(clone, "add", "a.txt")
        r = self.signed_commit(clone, "-m", f"x: restart the {word.upper()}")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"the message: {word}", r.stderr)
        self.assertEqual(self.git(bare, "log", "--format=%s", "main"), "base", "nothing reached GitHub")

    def test_the_text_guard_reads_issues_comments_pull_requests_and_commits(self) -> None:
        word = "zorblax"
        self.addCleanup(os.environ.pop, "OPSQUIRE_BANNED_WORDS", None)
        os.environ["OPSQUIRE_BANNED_WORDS"] = f"quuxly, {word}"
        clean_text = self.load("clean_text")
        event: dict[str, Any] = {
            "issue": {"number": 3, "title": "Retry the upload", "body": f"seen while {word}"},
            "comment": {"html_url": "https://github.com/o/r/issues/3#c1", "body": "fine"},
        }
        path = self.tmp / "event.json"
        path.write_text(json.dumps(event))
        self.assertEqual(clean_text.main([str(path)]), 1)
        event["issue"]["body"] = "seen while uploading"
        path.write_text(json.dumps(event))
        self.assertEqual(clean_text.main([str(path)]), 0)
        repo = self.tmp / "range"
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "base")
        self.git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x", "-m", f"for {word}")
        cwd = os.getcwd()
        try:
            os.chdir(repo)
            self.assertEqual(clean_text.commit_texts("HEAD~1..HEAD")[0][1].strip(), f"x\n\nfor {word}")
            self.assertEqual(clean_text.main([str(path), "HEAD~1..HEAD"]), 1)
        finally:
            os.chdir(cwd)

    def test_signed_commit_refuses_nothing_staged_and_a_checkout_behind_origin(self) -> None:
        bare, clone = self.signed_checkout()
        nothing = self.signed_commit(clone, "-m", "x: nothing")
        self.assertEqual(nothing.returncode, 2)
        self.assertIn("nothing is staged", nothing.stderr)
        self.assertEqual(self.git(bare, "log", "--format=%s", "main"), "base")
        (clone / "a.txt").write_text("a\n")
        self.git(clone, "add", "a.txt")
        self.git(clone, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "local only")
        (clone / "b.txt").write_text("b\n")
        self.git(clone, "add", "b.txt")
        ahead = self.signed_commit(clone, "-m", "x: ahead")
        self.assertEqual(ahead.returncode, 2)
        self.assertIn("is not origin/main", ahead.stderr)
        self.assertEqual(self.git(bare, "log", "--format=%s", "main"), "base")
        self.assertEqual(self.git(clone, "status", "--porcelain"), "A  b.txt")
