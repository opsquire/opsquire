"""How runs are kept on disk. A run's snapshot is the record of what it executed, so it must stay immutable, but most
snapshots repeat the same files: each distinct file is stored once in `var/blobs/<aa>/<sha256>` (read-only) and every
snapshot hard-links to it. A script never runs from the snapshot: it gets a private working copy (plain copies, with the
modes the run record keeps) in its run folder's `.work/`, removed when the run ends, so nothing a script changes there
reaches a blob or another run. A run's output.log and events.jsonl are plain text while it runs and are gzipped in place
once it has been finished for a day; every reader goes through `read_log`, which takes either. Blobs no snapshot links
to any more are removed. All of this runs from the daily sweep in the operator's terminal and never changes what a
record says."""

from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .model import group_mode, parse_iso, var_dir

LOGS = ("output.log", "events.jsonl")
WORK = ".work"
INPUTS = "inputs"
COMPRESS_AFTER = dt.timedelta(days=1)
DONE = ".compacted"


def blobs_dir() -> Path:
    return var_dir() / "blobs"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def blob_for(src: Path, sha: str) -> Path | None:
    """The stored copy of this content, written once (read-only) and checked before it is shared again; None when
    the store cannot be written here, or when another operator's blob holds it in a shared home: the kernel lets only
    a read-only file's owner hard-link it (fs.protected_hardlinks)."""
    blob = blobs_dir() / sha[:2] / sha
    tmp = blob.with_name(f".{sha}.{os.getpid()}.tmp")
    try:
        if blob.is_file() and blob.stat().st_uid != os.geteuid():
            return None
        if blob.is_file() and blob.stat().st_size == src.stat().st_size and digest(blob) == sha:
            return blob
        blob.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, tmp)
        tmp.chmod(0o444)
        os.replace(tmp, blob)
        return blob
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return None


def place(src: Path, dest: Path) -> None:
    """Put `src` at `dest` in a snapshot: a hard link to its blob, or a plain copy when linking is not possible."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = blob_for(src, digest(src))
    if blob is not None:
        with contextlib.suppress(FileNotFoundError):
            dest.unlink()
        try:
            os.link(blob, dest)
            return
        except OSError:
            pass
    shutil.copy2(src, dest)
    dest.chmod(group_mode(dest.stat().st_mode & 0o777))


def working_copy(snapshot: Path, work: Path, modes: dict[str, int]) -> None:
    """Copy each named snapshot file into `work` with its mode; a file the snapshot lacks is left out, for the
    caller's digest check to name."""
    for name, mode in modes.items():
        src = snapshot / name
        if not src.is_file():
            continue
        dest = work / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        dest.chmod(group_mode(mode))


def read_log(folder: Path, name: str) -> bytes:
    """A run's log as bytes, plain or gzipped; empty when the run has none."""
    plain = folder / name
    if plain.is_file():
        return plain.read_bytes()
    packed = folder / f"{name}.gz"
    if packed.is_file():
        with gzip.open(packed, "rb") as f:
            return f.read()
    return b""


def gzip_file(path: Path) -> int:
    """Replace a finished log with its gzip copy; the bytes saved."""
    packed = path.with_name(path.name + ".gz")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.gz.tmp")
    try:
        with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        size = path.stat().st_size
        os.replace(tmp, packed)
        path.unlink()
        return size - packed.stat().st_size
    finally:
        tmp.unlink(missing_ok=True)


@dataclass
class Compacted:
    runs: int = 0
    linked: int = 0
    logs: int = 0
    saved: int = 0
    pruned: int = 0
    errors: list[str] = field(default_factory=list)

    def text(self) -> str:
        return (
            f"compacted {self.runs} runs: {self.linked} snapshot files shared, {self.logs} logs gzipped, "
            f"{self.pruned} unused blobs removed, {self.saved / (1 << 20):.1f} MB freed"
        )


def finished_long_enough(folder: Path, at: dt.datetime) -> bool:
    try:
        rec = json.loads((folder / "run.json").read_text())
        ended = parse_iso(str(rec.get("ended") or ""))
    except (OSError, ValueError):
        return False
    return rec.get("status") != "running" and at - ended >= COMPRESS_AFTER


def compact_run(folder: Path, out: Compacted) -> None:
    snap = folder / "snapshot"
    if snap.is_dir():
        for p in sorted(snap.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            st = p.stat()
            if st.st_nlink > 1:
                continue
            sha = digest(p)
            blob = blob_for(p, sha)
            if blob is None:
                continue
            tmp = p.with_name(f".{p.name}.{os.getpid()}.link")
            try:
                os.link(blob, tmp)
                os.replace(tmp, p)
                out.linked += 1
                out.saved += st.st_size
            except OSError as e:
                tmp.unlink(missing_ok=True)
                out.errors.append(f"{p}: {e.strerror}")
    shutil.rmtree(folder / WORK, ignore_errors=True)
    shutil.rmtree(folder / INPUTS, ignore_errors=True)
    for name in LOGS:
        log = folder / name
        if log.is_file():
            out.saved += gzip_file(log)
            out.logs += 1
    (folder / DONE).touch()
    out.runs += 1


def prune_blobs(out: Compacted) -> None:
    root = blobs_dir()
    if not root.is_dir():
        return
    for blob in root.glob("*/*"):
        with contextlib.suppress(OSError):
            st = blob.stat()
            if blob.is_file() and st.st_nlink == 1 and not blob.name.startswith("."):
                blob.unlink()
                out.pruned += 1
                out.saved += st.st_size


def compact(runs_root: Path, at: dt.datetime) -> Compacted:
    """Share the snapshot files and gzip the logs of every run finished for a day that was not compacted yet, then
    drop blobs nothing links to. Safe to run again: done runs carry a marker and are skipped."""
    out = Compacted()
    if not runs_root.is_dir():
        return out
    for folder in sorted(runs_root.glob("*/*")):
        if folder.is_dir() and not (folder / DONE).exists() and finished_long_enough(folder, at):
            try:
                compact_run(folder, out)
            except OSError as e:
                out.errors.append(f"{folder}: {e.strerror}")
    prune_blobs(out)
    return out
