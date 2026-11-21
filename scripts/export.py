"""Publish a release of this private repository to the public one, without its history.

    python3 scripts/export.py [--bump patch|minor|major] [--to opsquire/opsquire] [--dry]

With --bump it first sets the new version here, as a GitHub-signed commit on main. Then it takes every tracked file
of main, refuses unless the private word list (scripts/clean_text.py) is present and none of its words is in any file
or in the release notes, refuses when the public main holds changes that no export made, and sends the files that
differ as GitHub-signed commits on the public main: one commit, or several parts when the change is too big for one
request. The last commit carries the release notes, built from this repository's commits since the previous export,
and names the commit it was exported from. The public repository's release workflow then tags and publishes that
version. --dry shows all of it and changes nothing."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import clean_text
import release
import signed_commit

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = "opsquire/opsquire"
EXCLUDED: frozenset[str] = frozenset()
MARKER = "Exported from "
PART_BYTES = 300_000


class ExportError(Exception):
    pass


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if r.returncode:
        raise ExportError(f"git {' '.join(args[:2])} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout.decode(errors="replace")


def tracked(ref: str) -> dict[str, str]:
    """path -> blob sha of every file at `ref` that goes out."""
    out = {}
    for rec in git("ls-tree", "-r", "-z", ref).split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        if path not in EXCLUDED:
            out[path] = meta.split()[2]
    return out


def changes(ours: dict[str, str], theirs: dict[str, str]) -> tuple[list[str], list[str]]:
    """Paths to write (new or different) and paths to delete, both sorted."""
    write = sorted(p for p, sha in ours.items() if theirs.get(p) != sha)
    delete = sorted(p for p in theirs if p not in ours)
    return write, delete


def exported_from(message: str) -> str | None:
    m = re.search(rf"^{MARKER}([0-9a-f]{{40}})$", message, re.M)
    return m.group(1) if m else None


def parts(sizes: dict[str, int], limit: int = PART_BYTES) -> list[list[str]]:
    """The paths to write, grouped so each group's files stay under `limit` bytes (a bigger file goes alone)."""
    out: list[list[str]] = [[]]
    total = 0
    for path in sorted(sizes):
        if out[-1] and total + sizes[path] > limit:
            out.append([])
            total = 0
        out[-1].append(path)
        total += sizes[path]
    return out


def blob(sha: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", sha], capture_output=True, check=True).stdout


def unclean(paths: list[str], blobs: dict[str, str], texts: list[str]) -> list[str]:
    found = [f"{p}: {', '.join(w)}" for p in paths if (w := clean_text.hits(blob(blobs[p]).decode(errors="replace")))]
    return found + [f"the release notes: {', '.join(w)}" for t in texts if (w := clean_text.hits(t))]


def gh_json(*args: str) -> object:
    return json.loads(signed_commit.run("gh", "api", *args))


def public_head(to: str) -> tuple[str, str, bool]:
    """The public main's head commit: its oid, its message, and whether it has a parent."""
    head = gh_json(f"repos/{to}/commits/main")
    assert isinstance(head, dict)
    return str(head["sha"]), str(head["commit"]["message"]), bool(head.get("parents"))


def public_files(to: str, oid: str) -> dict[str, str]:
    tree = gh_json(f"repos/{to}/git/trees/{oid}?recursive=1")
    assert isinstance(tree, dict)
    if tree.get("truncated"):
        raise ExportError(f"the public tree at {oid[:10]} is too big to list in one request")
    return {str(e["path"]): str(e["sha"]) for e in tree["tree"] if e["type"] == "blob"}


def previous_tag(to: str) -> str | None:
    tags = gh_json(f"repos/{to}/tags?per_page=100")
    assert isinstance(tags, list)
    names = [str(t["name"]) for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", str(t["name"]))]
    return max(names, key=lambda n: tuple(int(x) for x in n[1:].split(".")), default=None)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--bump", choices=["patch", "minor", "major"], help="set the next version here first")
    p.add_argument("--to", default=PUBLIC, help=f"the public repository (default {PUBLIC})")
    p.add_argument("--dry", action="store_true", help="show what would happen and change nothing")
    a = p.parse_args(argv)
    try:
        if not clean_text.words():
            raise ExportError("no word list here, so nothing could be checked: add .private/banned-words.txt first")
        if git("status", "--porcelain", "--untracked-files=no").strip():
            raise ExportError("the checkout has uncommitted changes: commit or drop them first")
        git("fetch", "-q", "origin", "main")
        if git("rev-parse", "HEAD").strip() != git("rev-parse", "origin/main").strip():
            raise ExportError("HEAD is not origin/main: only what is pushed to this repository's main goes out")
        head, message, has_parent = public_head(a.to)
        since = exported_from(message)
        if since is None and has_parent:
            raise ExportError(
                f"the public main ({head[:10]}) holds changes no export made: bring them into this repository first"
            )
        version = release.current_version()
        if a.bump:
            version = release.bump(version, a.bump)
            if not a.dry:
                release.set_version(version)
                git("add", "pyproject.toml")
                signed_commit.commit_staged(ROOT, "main", [f"release: v{version}"])
        ref = git("rev-parse", "HEAD").strip()
        ours = tracked(ref)
        write, delete = changes(ours, public_files(a.to, head))
        tag = f"v{version}"
        if since is None:
            notes = f"The first public release of opsquire.\n\n**Full changelog**: https://github.com/{a.to}/commits/{tag}\n"
        else:
            notes = release.notes(tag, previous_tag(a.to), release.commits_between(since, ref), a.to)
        bad = unclean(write, ours, [notes])
        if bad:
            raise ExportError("words that never belong in the public repository: " + "; ".join(bad))
        sizes = {path: int(git("cat-file", "-s", ours[path]).strip()) for path in write}
        groups = parts(sizes) if write else [[]]
        print(
            f"export {ref[:10]} as opsquire {version} to {a.to}: {len(write)} files to write, {len(delete)} to delete,"
        )
        print(f"in {len(groups)} commit(s); the word list found nothing. Release notes:\n\n{notes}")
        if a.dry:
            if a.bump:
                print(f"(--dry: the version would first become {version} here)")
            return 0
        oid = head
        for n, group in enumerate(groups, 1):
            last = n == len(groups)
            headline = f"opsquire {version}" + ("" if len(groups) == 1 else f", part {n} of {len(groups)}")
            body = f"{notes}\n{MARKER}{ref}" if last else f"Part {n} of the export of {ref[:10]}."
            additions = [{"path": path, "contents": base64.b64encode(blob(ours[path])).decode()} for path in group]
            oid = signed_commit.create_commit(
                a.to, "main", oid, headline, body, additions, [{"path": d} for d in delete] if n == 1 else []
            )
            print(f"committed {oid[:10]}: {headline}")
        print(f"\nPublish it: gh workflow run release.yml --repo {a.to}")
    except (ExportError, release.ReleaseError, signed_commit.SignedCommitError) as e:
        print(f"export: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
