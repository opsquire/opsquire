"""Code and config changes as GitHub PRs, without touching the operator's own checkouts. `checkout` exports a repo's
base branch (a best-effort `git fetch`, then `git archive`) into a copy under the home's `.repos/`; the agent edits
and tests the copy; `pr` compares it with the base tree and turns the differences into one GitHub-signed commit
(createCommitOnBranch) on a branch, then opens the PR or updates it. Nothing is ever merged or deployed."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import homegit
from .model import OpsquireError, group_mode, home_dir, main_home, read_json, shared_home, write_json

REPOS_FILE = "repos.json"
COPIES = homegit.REPO_COPIES
STATE = ".opsquire-pr.json"
CHAT = "chat"


@dataclass
class Repo:
    name: str
    path: Path
    github: str
    base: str
    branch_prefix: str
    auto_deploy: frozenset[str] = frozenset()


def registry() -> dict[str, Repo]:
    """Repos agents may change, from the home's repos.json: {"repos": {"app": {"path": "~/app", "github": "owner/app",
    "base": "main", "auto_deploy": ["release"]}, ...}, "branch_prefix": "opsquire/"} (branch_prefix optional, default
    "opsquire/"). `auto_deploy` names branches whose merge deploys; a PR into one gets a warning."""
    path = home_dir() / REPOS_FILE
    if not path.is_file():
        return {}
    raw: dict[str, Any] = read_json(path)
    prefix = str(raw.get("branch_prefix", "opsquire/"))
    repos = {}
    for name, cfg in (raw.get("repos") or {}).items():
        auto = frozenset(str(b) for b in cfg.get("auto_deploy") or [])
        repos[name] = Repo(name, Path(cfg["path"]).expanduser(), cfg["github"], cfg.get("base", "main"), prefix, auto)
    return repos


def get_repo(name: str) -> Repo:
    repos = registry()
    if name not in repos:
        known = ", ".join(repos) or f"none (add them to {home_dir() / REPOS_FILE})"
        raise OpsquireError(f"unknown repo {name!r}; registered: {known}")
    return repos[name]


def git(repo: Path, *args: str, binary: bool = False) -> Any:
    exe = shutil.which("git")
    if not exe:
        raise OpsquireError("git is not installed")
    r = subprocess.run([exe, "-C", str(repo), *args], capture_output=True, text=not binary)
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace")
        raise OpsquireError(f"git {' '.join(args[:2])} in {repo}: {err.strip()[:300]}")
    return r.stdout


def gh(*args: str, stdin: str | None = None) -> str:
    exe = shutil.which("gh")
    if not exe:
        raise OpsquireError("the GitHub CLI (gh) is not installed or not on PATH")
    r = subprocess.run([exe, *args], capture_output=True, text=True, input=stdin)
    if r.returncode != 0:
        raise OpsquireError(f"gh {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout


def fetch(repo: Repo, base: str) -> bool:
    """Best effort: refresh origin/<base>. Fails quietly where .git is not writable (agent sandboxes); asks fetch
    every registered repo from the operator's terminal before the agent starts."""
    exe = shutil.which("git")
    if not exe or not repo.path.is_dir():
        return False
    r = subprocess.run([exe, "-C", str(repo.path), "fetch", "-q", "origin", base], capture_output=True)
    return r.returncode == 0


def copies_dir(owner: str) -> Path:
    """Copies live in the main home (not a parallel piece's worktree), so they outlive a parked piece."""
    return main_home() / COPIES / owner


def owner_id() -> str:
    """The ask this copy belongs to (or "chat" for agents outside an ask)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", os.environ.get("OPSQUIRE_ASK_ID", CHAT))


def checkout(name: str, base: str | None = None) -> Path:
    """Export the repo's base branch (or `base`, e.g. a colleague's PR branch) into a copy; returns its path. An ask
    reuses its one copy per repo and base; outside an ask every checkout is a new copy, so chat sessions never share
    (and push) each other's edits."""
    repo = get_repo(name)
    base = base or repo.base
    if (
        base.startswith("-")
        or subprocess.run(["git", "check-ref-format", "--branch", base], capture_output=True).returncode
    ):
        raise OpsquireError(f"not a branch name: {base!r}")
    owner = owner_id()
    label = name if base == repo.base else f"{name}-{slug(base)}"
    dest = copies_dir(owner) / (label if owner != CHAT else f"{label}-{secrets.token_hex(4)}")
    if (dest / STATE).is_file():
        return dest
    fetch(repo, base)
    sha = git(repo.path, "rev-parse", f"origin/{base}").strip()
    staging = dest.with_name(f".{name}.{os.getpid()}.tmp")
    try:
        staging.mkdir(parents=True)
        archive = staging / ".archive.tar"
        archive.write_bytes(git(repo.path, "archive", "--format=tar", sha, binary=True))
        with tarfile.open(archive) as tar:
            tar.extractall(staging, filter=group_filter if shared_home() else "tar")
        archive.unlink()
        write_json(staging / STATE, {"repo": name, "base": base, "base_sha": sha, "extracted": time.time()})
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


def group_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
    """The "tar" filter for a shared home: the group gets the owner's access, and directories keep the setgid that
    makes what is created in them the group's."""
    member = tarfile.tar_filter(member, path)
    mode = group_mode(member.mode)
    return member.replace(mode=mode | 0o070 | stat.S_ISGID if member.isdir() else mode, deep=False)


def copy_state(copy: Path) -> dict[str, Any]:
    state = copy / STATE
    if not state.is_file():
        raise OpsquireError(f"{copy} is not a repo copy made by `opsquire repo checkout`")
    data: dict[str, Any] = read_json(state)
    return data


def changes(copy: Path) -> tuple[list[str], list[str]]:
    """(changed or added paths, deleted paths) of a copy against its base commit, by git blob hash."""
    base = base_blobs(copy_state(copy))
    files = [f for f in walk(copy) if not ignored(f)]
    hashes = {f: blob_sha(copy / f) for f in files}
    changed = [f for f in files if base.get(f) != hashes[f]]
    deleted = sorted(f for f in base if not os.path.lexists(copy / f))
    return changed, deleted


def walk(copy: Path) -> list[str]:
    """Every file and symlink in a copy (symlinked folders are entries, never followed), relative and sorted."""
    out = []
    for root, dirs, names in os.walk(copy):
        rel = Path(root).relative_to(copy)
        for d in dirs[:]:
            if (Path(root) / d).is_symlink():
                dirs.remove(d)
                names.append(d)
        out += [str(rel / n) for n in names if not (rel == Path(".") and n == STATE)]
    return sorted(out)


def blob_sha(path: Path) -> str:
    """Git's blob id: a symlink is stored as its target text, never followed."""
    data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def ignored(path: str) -> bool:
    """Build and test leftovers that never belong in a PR."""
    parts = Path(path).parts
    caches = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
    return any(p in caches for p in parts) or path.endswith((".pyc", ".pyo"))


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > 40:
            break
        out = f"{out}-{w}" if out else w
    return out or "change"


def open_pr(copy: Path, title: str, body: str, branch: str | None = None) -> dict[str, Any]:
    """Commit the copy's changes (GitHub-signed) to a branch and open its PR, or add a commit to an existing one."""
    state = copy_state(copy)
    repo = get_repo(state["repo"])
    base = state.get("base", repo.base)
    changed, deleted = changes(copy)
    if not changed and not deleted and not state.get("pushed"):
        raise OpsquireError(f"{copy} has no changes against {base}")
    links = [f for f in changed if (copy / f).is_symlink()]
    if links:
        raise OpsquireError(f"symlinks cannot be changed through a PR from here: {', '.join(links)}")
    explicit = branch is not None
    earlier = state.get("branch")
    if earlier and not explicit and state.get("title") != title:
        before = repr(state["title"]) if state.get("title") else "an earlier PR"
        raise OpsquireError(
            f"{copy} already pushed to branch {earlier} ({state.get('pr')}) for {before}; "
            f"for a new PR run `opsquire repo checkout {repo.name}` and edit the new copy, "
            f"or pass --branch {earlier} to add these changes to that PR"
        )
    branch = branch or earlier or f"{repo.branch_prefix}{slug(title)}"
    if earlier and branch != earlier:
        state["pushed"] = {}
    onto_base = branch == base
    if onto_base and base == repo.base:
        raise OpsquireError(f"refusing to commit straight onto {base}; open a PR from a new branch")
    head = branch_head(repo, branch)
    if onto_base:
        # The copy holds whole files from base_sha; if the branch moved since, pushing them would revert those
        # commits, so GitHub must refuse unless the head is still what this copy last saw.
        head = state.get("commit") or state["base_sha"]
    elif head is None:
        gh(
            "api",
            "-X",
            "POST",
            f"repos/{repo.github}/git/refs",
            "-f",
            f"ref=refs/heads/{branch}",
            "-f",
            f"sha={state['base_sha']}",
        )
        head = state["base_sha"]
    elif not earlier and not explicit:
        raise OpsquireError(f"branch {branch} already exists on {repo.github}; pass --branch with a new name")
    pushed: dict[str, str] = state.get("pushed") or {}
    current = {f: blob_digest(copy / f) for f in changed} | {f: "deleted" for f in deleted}
    current |= {f: blob_digest(copy / f) if (copy / f).is_file() else "deleted" for f in pushed if f not in current}
    send = {f: d for f, d in current.items() if pushed.get(f) != d}
    if not send and state.get("commit"):
        return summary(repo, state, changed, deleted)
    additions = [
        {"path": f, "contents": base64.b64encode((copy / f).read_bytes()).decode()}
        for f, d in send.items()
        if d != "deleted"
    ]
    removals = [{"path": f} for f, d in send.items() if d == "deleted"]
    mutation = "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }"
    headline, _, rest = title.partition("\n")
    payload = {
        "query": mutation,
        "variables": {
            "input": {
                "branch": {"repositoryNameWithOwner": repo.github, "branchName": branch},
                "expectedHeadOid": head,
                "message": {
                    "headline": headline,
                    "body": (rest.strip() or body.strip()) if onto_base else rest.strip(),
                },
                "fileChanges": {"additions": additions, "deletions": removals},
            }
        },
    }
    out = json.loads(gh("api", "graphql", "--input", "-", stdin=json.dumps(payload)))
    oid = ((out.get("data") or {}).get("createCommitOnBranch") or {}).get("commit", {}).get("oid")
    if not oid:
        raise OpsquireError(f"GitHub refused the commit: {json.dumps(out.get('errors') or out)[:400]}")
    existing = gh("pr", "list", "--repo", repo.github, "--head", branch, "--state", "open", "--json", "url")
    urls = [p["url"] for p in json.loads(existing or "[]")]
    if urls or onto_base:
        url = urls[0] if urls else ""
    else:
        create = ["pr", "create", "--repo", repo.github, "--base", base, "--head", branch]
        url = gh(*create, "--title", headline, "--body", body).strip().splitlines()[-1]
    state |= {"branch": branch, "title": title, "pr": url, "commit": oid, "pushed": pushed | send}
    write_json(copy / STATE, state)
    return summary(repo, state, changed, deleted)


def summary(repo: Repo, state: dict[str, Any], changed: list[str], deleted: list[str]) -> dict[str, Any]:
    base = state.get("base", repo.base)
    return {
        "repo": repo.name,
        "branch": state["branch"],
        "base": base,
        "commit": state["commit"],
        "url": state["pr"],
        "changed": changed,
        "deleted": deleted,
        "auto_deploy_base": base in repo.auto_deploy or state["branch"] in repo.auto_deploy,
    }


def blob_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def branch_head(repo: Repo, branch: str) -> str | None:
    exe = shutil.which("gh")
    if not exe:
        raise OpsquireError("the GitHub CLI (gh) is not installed or not on PATH")
    r = subprocess.run(
        [exe, "api", f"repos/{repo.github}/git/ref/heads/{branch}", "--jq", ".object.sha"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def remote_tip(repo: Repo, base: str) -> str | None:
    """The branch's commit on origin right now, read without writing anything; None when origin cannot be reached."""
    exe = shutil.which("git")
    if not exe or not repo.path.is_dir():
        return None
    r = subprocess.run(
        [exe, "-C", str(repo.path), "ls-remote", "origin", f"refs/heads/{base}"], capture_output=True, text=True
    )
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else None


def stale_copy_note(copy: Path) -> str:
    """`checkout` exports the last fetched origin/<base>, and a fetch fails quietly in an agent's sandbox."""
    state = copy_state(copy)
    repo = get_repo(state["repo"])
    tip = remote_tip(repo, state["base"])
    if not tip or tip == state["base_sha"]:
        return ""
    return (
        f"{copy} is {state['base']} at {state['base_sha'][:10]} but origin is at {tip[:10]}: this machine could not "
        f"fetch, so its code is old. Have the operator run `git -C {repo.path} fetch origin {state['base']}`, "
        "then check out again"
    )


def freshness(repo: Repo) -> str:
    """Where the operator's own checkout stands against origin, for anyone tempted to read code from it."""
    try:
        branch = git(repo.path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except OpsquireError:
        return "unknown"
    tip = remote_tip(repo, repo.base)
    if not tip:
        return f"{branch}, origin not reachable"
    if git(repo.path, "rev-parse", "HEAD").strip() == tip:
        return f"{branch}, up to date"
    try:
        behind = git(repo.path, "rev-list", "--count", f"HEAD..{tip}").strip()
    except OpsquireError:
        return f"{branch}, behind origin/{repo.base}: not fetched"
    return f"{branch}, {behind} behind origin/{repo.base}"


def overlapping_prs(copy: Path, mine: set[str] | None = None) -> list[dict[str, Any]]:
    """Open PRs of others that change a file this copy changes (`mine`, when the caller already has them): read them
    before building on the same code. Advice only; if GitHub cannot be asked, nothing is reported."""
    state = copy_state(copy)
    repo = get_repo(state["repo"])
    if mine is None:
        changed, deleted = changes(copy)
        mine = {*changed, *deleted}
    if not mine:
        return []
    fields = "number,url,author,headRefName,files"
    try:
        prs = json.loads(gh("pr", "list", "--repo", repo.github, "--state", "open", "--limit", "200", "--json", fields))
    except (OpsquireError, ValueError):
        return []
    out = []
    for pr in prs:
        shared = sorted(mine & {str(f.get("path")) for f in pr.get("files") or []})
        if shared and pr.get("headRefName") != state.get("branch"):
            author = (pr.get("author") or {}).get("login", "")
            out.append({"url": pr.get("url"), "author": author, "branch": pr.get("headRefName"), "files": shared})
    return out


def drop(owner: str) -> None:
    """Remove every repo copy of an ask (when the ask is finished)."""
    shutil.rmtree(copies_dir(owner), ignore_errors=True)
    parent = main_home() / COPIES
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


LEFTOVER_KEEP_S = 86400.0
EXPORT_GAP_S = 2.0
CTIME_SLACK_S = 1.0


@dataclass
class CopyScan:
    last_written: float
    size: int
    unpushed: list[str] | None


def scan_copy(copy: Path) -> CopyScan:
    """When a copy was last written, its size, and the edits in it that never reached GitHub (None when its base
    commit cannot be read). Only files that may have been edited are hashed: written after the export, or with an
    mtime other than the archive's (so `cp -p` and touch-backs still count). The kernel stamps ctime from a coarse
    clock that can lag time.time() by milliseconds, so anything changed within CTIME_SLACK_S of the export is hashed
    too; hashing only confirms, it never makes a file count as edited."""
    state = copy_state(copy)
    files: list[tuple[str, os.stat_result]] = []
    last = (copy / STATE).lstat().st_mtime
    size = 0
    for root, dirs, names in os.walk(copy):
        st = Path(root).lstat()
        last = max(last, st.st_mtime, st.st_ctime)
        rel = Path(root).relative_to(copy)
        for n in [*names, *(d for d in dirs if (Path(root) / d).is_symlink())]:
            if rel == Path(".") and n == STATE:
                continue
            st = (Path(root) / n).lstat()
            last = max(last, st.st_mtime, st.st_ctime)
            size += st.st_size
            files.append((str(rel / n), st))
    exported = state.get("extracted")
    if not isinstance(exported, (int, float)):
        exported = export_end(sorted(st.st_ctime for _, st in files))
    archive_mtime = Counter(int(st.st_mtime) for _, st in files).most_common(1)[0][0] if files else 0
    maybe = [
        f
        for f, st in files
        if not ignored(f) and (st.st_ctime > exported - CTIME_SLACK_S or int(st.st_mtime) != archive_mtime)
    ]
    try:
        base = base_blobs(state)
    except OpsquireError:
        return CopyScan(last, size, None)
    pushed: dict[str, str] = state.get("pushed") or {}
    unpushed = [
        f
        for f in maybe
        if base.get(f) != blob_sha(copy / f) and ((copy / f).is_symlink() or pushed.get(f) != blob_digest(copy / f))
    ]
    unpushed += sorted(f for f in base if not os.path.lexists(copy / f) and pushed.get(f) != "deleted")
    return CopyScan(last, size, unpushed)


def written_since(copy: Path, cutoff: float) -> bool:
    """Stops at the first write after `cutoff`, so a copy in use costs a few stats, not a walk."""
    for root, _, names in os.walk(copy):
        for p in (Path(root), *(Path(root) / n for n in names)):
            st = p.lstat()
            if max(st.st_mtime, st.st_ctime) >= cutoff:
                return True
    return False


def export_end(ctimes: list[float]) -> float:
    """For copies made before checkout recorded the time: the end of the burst of writes that unpacked them."""
    if not ctimes:
        return 0.0
    end = ctimes[0]
    for c in ctimes:
        if c - end > EXPORT_GAP_S:
            break
        end = c
    return end


def base_blobs(state: dict[str, Any]) -> dict[str, str]:
    repo = get_repo(state["repo"])
    out = {}
    for line in git(repo.path, "ls-tree", "-r", "-z", state["base_sha"]).split("\0"):
        if line:
            meta, path = line.split("\t", 1)
            if meta.split()[1] == "blob":
                out[path] = meta.split()[2]
    return out


def remove_copy(copy: Path) -> None:
    """Renamed out of the way first, so an interrupted removal leaves a leftover the next prune clears, never a
    half copy that looks real."""
    gone = copy.with_name(f".{copy.name}.prune.tmp")
    copy.rename(gone)
    shutil.rmtree(gone, ignore_errors=True)


def prune_chat_copies(days: float, dry: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Chat copies idle `days` or more: deleted when every edit is on GitHub (or there never was one), kept and
    reported when an edit is not, or when the base commit cannot be read to tell. An ask's copies are its own and
    stay; leftovers of an interrupted checkout or prune go after a day."""
    at = time.time()
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    root = copies_dir(CHAT)
    if not root.is_dir():
        return {"removed": removed, "kept": kept}
    for copy in sorted(root.iterdir()):
        if copy.name.startswith("."):
            if copy.name.endswith(".tmp") and copy.is_dir() and at - copy.lstat().st_mtime > LEFTOVER_KEEP_S:
                if not dry:
                    shutil.rmtree(copy, ignore_errors=True)
                removed.append({"path": str(copy), "leftover": True})
            continue
        if not (copy / STATE).is_file():
            continue
        if written_since(copy, at - days * 86400):
            continue
        scan = scan_copy(copy)
        idle = (at - scan.last_written) / 86400
        if idle < days:
            continue
        state = copy_state(copy)
        info = {"path": str(copy), "idle_days": round(idle, 1), "pr": state.get("pr") or "", "bytes": scan.size}
        if scan.unpushed is None or scan.unpushed:
            kept.append(info | {"unpushed": scan.unpushed})
            continue
        if not dry:
            remove_copy(copy)
        removed.append(info)
    return {"removed": removed, "kept": kept}


def size_text(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
