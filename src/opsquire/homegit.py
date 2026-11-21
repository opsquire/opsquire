"""The home as a local-only git repository: opsquire commits ready versions (lint passes), snapshots loose changes
before a parallel ask, and gives each parallel piece its own worktree and branch that merge back when done.

Home commits and merges are never signed, whatever the operator's git config says: the repository cannot have a
remote, so a signature would prove nothing to anyone, and signing would ask for a passphrase before every run. They
use the operator's git identity, or a placeholder when git has none, so a fresh machine is never stopped by it.

Agents write the home, so git must not run anything they wrote with the operator's permissions. Every git command
here runs with hooks and fsmonitor off, and only after the repository's own config files hold nothing but known-safe
keys and no attributes file names a filter, diff or merge driver (each of which git runs as a command); otherwise it
refuses, naming the setting. Each command trusts the one repository it runs in (`-c safe.directory`), so git works
in a home another operator owns (a shared home) without any config file saying so; the checks above are what keep
that safe."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from .model import IGNORED_PARTS, OpsquireError

GITIGNORE = """# Local-only repository: it holds operator-specific tasks and run history. Never add a remote.

# run records, locks and asks: written by the runner, kept on disk, not versioned
var/

# generated: `opsquire docs`, `opsquire contract --write` and lint rebuild these
docs/
AGENTS.md
CLAUDE.md
.lint.json

# caches and local agent config
__pycache__/
.mypy_cache/
.ruff_cache/
.claude/
.mcp.json
"""

WORKTREES = ".worktrees"
REPO_COPIES = ".repos"
READ_LEDGERS = ".reads"
EXCLUDE_LOCK = threading.Lock()
PLACEHOLDER_IDENTITY = ("-c", "user.name=opsquire", "-c", "user.email=opsquire@localhost")
HARDENED = ("-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=")
SAFE_CONFIG = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.symlinks",
        "core.autocrlf",
        "core.safecrlf",
        "core.eol",
        "core.quotepath",
        "core.checkstat",
        "core.trustctime",
        "core.untrackedcache",
        "core.sharedrepository",
        "extensions.objectformat",
        "extensions.refstorage",
        "extensions.worktreeconfig",
        "user.name",
        "user.email",
        "commit.gpgsign",
        "init.defaultbranch",
        "gc.auto",
        "gc.autodetach",
        "i18n.commitencoding",
        "i18n.logoutputencoding",
        "index.version",
    }
)
SAFE_PER_NAME = {"remote": {"url", "fetch"}, "branch": {"remote", "merge"}}
DRIVER_ATTRIBUTES = ("filter=", "diff=", "merge=")
BUILTIN_MERGE = {"merge=text", "merge=binary", "merge=union"}
NOT_WALKED = {".git", "var", WORKTREES, REPO_COPIES, READ_LEDGERS, *IGNORED_PARTS}


class GitError(OpsquireError):
    pass


class UnsafeGit(GitError):
    pass


def git(home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("git")
    if not exe:
        raise GitError("git is not installed")
    refuse_unsafe(exe, home)
    base = [exe, "-C", str(home), *HARDENED, *trusted(home)]
    if args[0] in ("commit", "merge") and not has_identity(base):
        base += PLACEHOLDER_IDENTITY
    r = subprocess.run([*base, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def trusted(path: Path) -> tuple[str, str]:
    """Git accepts the repository at `path` whoever owns it, for this one command."""
    return ("-c", f"safe.directory={path.resolve()}")


def layout(exe: str, cwd: Path) -> tuple[Path, Path, Path | None] | None:
    """(git dir, common dir, work tree) of the repository git finds from `cwd`, None outside any repository."""
    base = [exe, "-C", str(cwd), *HARDENED, *trusted(cwd)]
    for ask in (("--show-toplevel",), ()):
        r = subprocess.run(
            [*base, "rev-parse", "--absolute-git-dir", "--git-common-dir", *ask],
            capture_output=True,
            text=True,
        )
        lines = r.stdout.splitlines()
        if r.returncode == 0 and len(lines) == 2 + len(ask):
            common = Path(lines[1])
            return Path(lines[0]), common if common.is_absolute() else cwd / common, Path(lines[2]) if ask else None
    return None


def unsafe_config(exe: str, path: Path) -> str:
    """The first key of a repository config file that is not known to be safe, "" when there is none."""
    if not path.is_file():
        return ""
    r = subprocess.run(
        [exe, *HARDENED, "config", "--file", str(path), "--list", "--null"], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise UnsafeGit(f"git cannot read {path}, so opsquire cannot check it: {(r.stderr or r.stdout).strip()[:200]}")
    for entry in r.stdout.split("\0"):
        key = entry.split("\n", 1)[0]
        if key and not safe_key(key):
            return key
    return ""


def safe_key(key: str) -> bool:
    """In SAFE_CONFIG, or a remote's url and fetch or a branch's upstream: never run by opsquire, which neither
    fetches nor pushes, and kept readable so a home with a remote is refused for that reason by name."""
    if key.lower() in SAFE_CONFIG:
        return True
    section, _, rest = key.partition(".")
    name = rest.rpartition(".")[2].lower()
    return "." in rest and name in SAFE_PER_NAME.get(section.lower(), set())


def driver_attribute(path: Path) -> str:
    """The first line of an attributes file that names a filter, diff or merge driver, "" when there is none."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        for token in line.split():
            if token.startswith(DRIVER_ATTRIBUTES) and token not in BUILTIN_MERGE:
                return f"line {n}: {token}"
    return ""


def attribute_files(top: Path) -> list[Path]:
    found = []
    for folder, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in NOT_WALKED]
        if ".gitattributes" in files:
            found.append(Path(folder) / ".gitattributes")
    return found


def refuse_unsafe(exe: str, cwd: Path) -> None:
    """Raise UnsafeGit when the repository at `cwd` holds a setting that makes git run a command: a config key outside
    SAFE_CONFIG (hooks paths, filters, diff and merge drivers, aliases, includes, editors, pagers, credential
    helpers and the like) or an attribute naming a driver. Read afresh before every git command, since agents can
    write the work tree and, where their sandbox allows it, the repository itself."""
    found = layout(exe, cwd)
    if found is None:
        return
    git_dir, common, top = found
    for path in dict.fromkeys([common / "config", common / "config.worktree", git_dir / "config.worktree"]):
        key = unsafe_config(exe, path)
        if key:
            raise UnsafeGit(
                f"{path} sets {key}, which opsquire does not let git read: it can make git run a command with your "
                f"permissions. No git runs in {cwd} until it is gone; check who wrote it, then remove it with "
                f"`git config --file {path} --unset-all {key}`"
            )
    attributes = [common / "info" / "attributes", git_dir / "info" / "attributes"]
    for path in dict.fromkeys([*attributes, *(attribute_files(top) if top else [])]):
        line = driver_attribute(path)
        if line:
            raise UnsafeGit(
                f"{path} {line} names a git filter, diff or merge driver, which git runs as a command with your "
                f"permissions. No git runs in {cwd} until that line is gone; check who wrote it"
            )


def has_identity(base: list[str]) -> bool:
    """Whether git would accept a commit's author and committer here, by whatever means it finds them."""
    idents = ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")
    return all(subprocess.run([*base, "var", i], capture_output=True).returncode == 0 for i in idents)


def is_repo(home: Path) -> bool:
    if not shutil.which("git") or not home.is_dir():
        return False
    return git(home, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"


def remotes(home: Path) -> list[str]:
    return git(home, "remote").stdout.split()


def ensure_repo(home: Path, dry: bool) -> str:
    """Make the home a local-only git repository (init, .gitignore, first commit); refuse one with a remote."""
    if not shutil.which("git"):
        return "home repo: skipped, git is not installed; `opsquire integrate` makes it one once it is"
    if is_repo(home):
        found = remotes(home)
        if found:
            return f"home repo: refusing: it has remotes ({', '.join(found)}); it must stay local-only"
        return f"home repo: local-only git repository ({home})"
    if dry:
        return f"home repo: would initialize a local git repository in {home}"
    git(home, "init", "-q")
    ignore = home / ".gitignore"
    if not ignore.exists():
        ignore.write_text(GITIGNORE)
    commit(home, None, "Opsquire home: tasks, general scripts, local rules")
    return f"home repo: initialized a local git repository in {home}"


def exclude_worktrees(home: Path) -> None:
    """Keep opsquire's scratch folders (parallel piece worktrees, repo copies, read ledgers) out of the home's
    commits, without touching the operator's .gitignore."""
    with EXCLUDE_LOCK:
        common = Path(git(home, "rev-parse", "--git-common-dir").stdout.strip())
        exclude = (common if common.is_absolute() else home / common) / "info" / "exclude"
        lines = exclude.read_text().splitlines() if exclude.is_file() else []
        missing = [e for e in (f"/{WORKTREES}/", f"/{REPO_COPIES}/", f"/{READ_LEDGERS}/") if e not in lines]
        if missing:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text("\n".join([*lines, *missing]) + "\n")


def commit(home: Path, paths: list[Path] | None, message: str) -> bool:
    """Stage `paths` (everything when None) and commit if anything changed. True when a commit was made."""
    exclude_worktrees(home)
    if paths is not None:
        paths = [p for p in paths if p.exists()]
        if not paths:
            return False
    git(home, "add", "-A", "--", *([str(p) for p in paths] if paths else ["."]))
    if git(home, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git(home, "commit", "-q", "-m", message)
    return True


def commit_quietly(home: Path, paths: list[Path], message: str) -> str:
    """Best-effort commit (after a lint pass, before a run, after an ask); a note when it could not be made, shown
    only on a terminal: an agent sandbox may not be able to write the repository, and the next commit includes it."""
    if not is_repo(home):
        return ""
    try:
        commit(home, paths, message)
    except GitError as e:
        if not os.isatty(2):
            return ""
        return f"(home not committed: {str(e).splitlines()[0]}; the next successful commit includes it)"
    return ""


def branch(home: Path) -> str:
    return git(home, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def add_worktree(home: Path, path: Path, name: str) -> None:
    """Check out branch `name` at `path`, creating the branch from HEAD when it does not exist yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = git(home, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0
    git(home, "worktree", "add", "-q", *([str(path), name] if exists else ["-b", name, str(path)]))


def remove_worktree(home: Path, path: Path) -> None:
    if path.exists():
        git(home, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    git(home, "worktree", "prune", check=False)
    for parent in (path.parent, path.parent.parent):
        if parent.name and parent != home and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def merge_into(home: Path, name: str, message: str) -> bool:
    """Merge branch `name` into the home's checked-out branch. False (and nothing changed) on a conflict."""
    r = git(home, "merge", "--no-ff", "-q", "-m", message, name, check=False)
    if r.returncode == 0:
        return True
    git(home, "merge", "--abort", check=False)
    return False


def merge_from(worktree: Path, name: str) -> list[str]:
    """Merge branch `name` into a worktree; the files left with conflict markers (empty when clean)."""
    r = git(worktree, "merge", "-q", "-m", f"merge {name}", name, check=False)
    if r.returncode == 0:
        return []
    files = git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.split()
    if not files:
        raise GitError(f"merging {name} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return files


def has_markers(worktree: Path, files: list[str]) -> list[str]:
    left = []
    for f in files:
        p = worktree / f
        if p.is_file() and any(line.startswith(("<<<<<<< ", ">>>>>>> ")) for line in p.read_text().splitlines()):
            left.append(f)
    return left


def delete_branch(home: Path, name: str) -> None:
    git(home, "branch", "-D", name, check=False)
