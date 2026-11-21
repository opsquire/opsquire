"""The home's tamper-evident ledger, `var/ledger/`: one line per thing that happened (a run started or ended, a decision
written to a task's meta.json, a lock taken or given back, a policy sealed). Each entry holds the hash of the entry
before it and of the same actor's entry before it, so the ledger is one hash chain with an independently verifiable
chain per actor (user, host and author) inside it; a run's end entry holds a digest of its whole record. The entries
are the leaves of an RFC 9162 Merkle tree whose root is kept in `checkpoints.jsonl` after every run.

Nothing signs it, and a key kept as the same user would prove nothing against that user: anything that can write var/
can write here too. What it gives is that no entry, and no record of a run sealed in it, can change or disappear
without `opsquire audit` noticing, and that history cannot be rewritten consistently past a checkpoint the operator
kept somewhere else. Appending never fails the caller: where var/ cannot be written (an agent sandbox) nothing is
recorded. Decisions an agent writes to meta.json from its sandbox reach the ledger later, as `observed` entries,
the next time an operator-side process (a run starting, the console building its state) catches up with them."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import shlex
import signal
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store
from .model import (
    OpsquireError,
    all_task_folders,
    config_path,
    content_digest,
    group_mode,
    host_name,
    iso,
    now,
    offboarded_folders,
    read_json,
    run_alive,
    run_origin,
    runs_dir,
    task_folder,
    update_config,
    user_name,
    var_dir,
    write_json,
    write_text,
)

ENTRIES = "entries.jsonl"
CHECKPOINTS = "checkpoints.jsonl"
STATE = "state.json"
OBSERVED = "observed.json"
EXPORTED = "export.json"
EXPORT_KEY = "checkpoint_command"
EXPORT_ENV = "OPSQUIRE_CHECKPOINT"
EXPORT_TIMEOUT_S = 20.0
LOCK = ".lock"
HELD = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
EMPTY_ROOT = hashlib.sha256(b"").digest()
DECIDES = {
    "close": ("outcome", "abandoned_reason"),
    "supersede": ("superseded_by", "superseded", "handover"),
    "reopen": ("outcome", "abandoned_reason", "superseded_by", "superseded", "reopened"),
    "handover": ("handover",),
    "withdraw": ("handover",),
    "hold": ("hold",),
    "release": ("hold",),
    "judge": ("judged",),
}
FIELDS = ("outcome", "abandoned_reason", "superseded_by", "superseded", "reopened", "handover", "hold", "judged")


def ledger_dir() -> Path:
    return var_dir() / "ledger"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry_hash(entry: dict[str, Any]) -> str:
    return sha(canonical({k: v for k, v in entry.items() if k != "hash"}))


def line_hash(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    """A ledger line parsed, with its hash: of the entry without `hash`, or of the raw bytes when it is no entry."""
    try:
        entry = json.loads(raw)
    except ValueError:
        return None, sha(raw)
    if not isinstance(entry, dict):
        return None, sha(raw)
    return entry, entry_hash(entry)


def lines_of(data: bytes) -> list[bytes]:
    if not data:
        return []
    lines = data.split(b"\n")
    return lines[:-1] if data.endswith(b"\n") else lines


def actor() -> dict[str, Any]:
    return {
        "user": user_name(),
        "uid": os.geteuid(),
        "host": host_name(),
        "author": os.environ.get("OPSQUIRE_AUTHOR", ""),
        "origin": run_origin(),
        "pid": os.getpid(),
    }


def actor_key(who: Any) -> str:
    """Whose chain an entry is in: user, host and author; origin and pid vary within one session."""
    who = who if isinstance(who, dict) else {}
    author = str(who.get("author") or "")
    return f"{who.get('user')}@{who.get('host')}" + (f" ({author})" if author else "")


def leaf(entry_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(entry_hex)).digest()


def node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def split(n: int) -> int:
    """The largest power of two below n (n > 1)."""
    return 1 << ((n - 1).bit_length() - 1)


def tree_hash(leaves: list[bytes]) -> bytes:
    """RFC 9162 Merkle tree hash over leaf hashes."""
    if not leaves:
        return EMPTY_ROOT
    if len(leaves) == 1:
        return leaves[0]
    k = split(len(leaves))
    return node(tree_hash(leaves[:k]), tree_hash(leaves[k:]))


def inclusion_proof(index: int, leaves: list[bytes]) -> list[bytes]:
    """RFC 9162 PATH(index, leaves): the sibling hashes from the leaf up to the root."""
    if len(leaves) <= 1:
        return []
    k = split(len(leaves))
    if index < k:
        return [*inclusion_proof(index, leaves[:k]), tree_hash(leaves[k:])]
    return [*inclusion_proof(index - k, leaves[k:]), tree_hash(leaves[:k])]


def consistency_proof(first: int, leaves: list[bytes]) -> list[bytes]:
    """RFC 9162 PROOF(first, leaves): what shows the tree of the first `first` leaves is a prefix of this one."""
    return subproof(first, leaves, True) if 0 < first < len(leaves) else []


def subproof(m: int, leaves: list[bytes], whole: bool) -> list[bytes]:
    n = len(leaves)
    if m == n:
        return [] if whole else [tree_hash(leaves)]
    k = split(n)
    if m <= k:
        return [*subproof(m, leaves[:k], whole), tree_hash(leaves[k:])]
    return [*subproof(m - k, leaves[k:], False), tree_hash(leaves[:k])]


def verify_inclusion(index: int, size: int, leaf_hash: bytes, proof: list[bytes], root: bytes) -> bool:
    """RFC 9162 section 2.1.3.2."""
    if index >= size:
        return False
    fn, sn, r = index, size - 1, leaf_hash
    for p in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node(p, r)
            while fn and not fn & 1:
                fn, sn = fn >> 1, sn >> 1
        else:
            r = node(r, p)
        fn, sn = fn >> 1, sn >> 1
    return sn == 0 and r == root


def verify_consistency(first: int, second: int, first_root: bytes, second_root: bytes, proof: list[bytes]) -> bool:
    """RFC 9162 section 2.1.4.2; equal sizes need equal roots, and the empty tree is a prefix of every tree."""
    if first == second:
        return not proof and first_root == second_root
    if first == 0:
        return not proof and first_root == EMPTY_ROOT
    if first > second or not proof:
        return False
    path = [first_root, *proof] if first & (first - 1) == 0 else list(proof)
    fn, sn = first - 1, second - 1
    while fn & 1:
        fn, sn = fn >> 1, sn >> 1
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr, sr = node(c, fr), node(c, sr)
            while fn and not fn & 1:
                fn, sn = fn >> 1, sn >> 1
        else:
            sr = node(sr, c)
        fn, sn = fn >> 1, sn >> 1
    return fr == first_root and sr == second_root and sn == 0


@dataclass
class Tip:
    """The ledger's end: its size, last hash, each actor's last hash and the roots of the perfect subtrees the tree
    is made of (largest first), which give the root without reading the entries."""

    size: int = 0
    hash: str = ""
    actors: dict[str, str] = field(default_factory=dict)
    frontier: list[bytes] = field(default_factory=list)

    def add(self, entry: dict[str, Any] | None, h: str) -> None:
        self.frontier.append(leaf(h))
        n = self.size
        while n & 1:
            right = self.frontier.pop()
            self.frontier.append(node(self.frontier.pop(), right))
            n >>= 1
        self.size += 1
        self.hash = h
        if entry is not None:
            self.actors[actor_key(entry.get("actor"))] = h

    def root(self) -> bytes:
        if not self.frontier:
            return EMPTY_ROOT
        r = self.frontier[-1]
        for h in reversed(self.frontier[:-1]):
            r = node(h, r)
        return r

    def to_json(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "hash": self.hash,
            "actors": self.actors,
            "frontier": [h.hex() for h in self.frontier],
        }


def saved_tip(path: Path) -> Tip | None:
    try:
        raw = json.loads(path.read_bytes())
        return Tip(int(raw["size"]), str(raw["hash"]), dict(raw["actors"]), [bytes.fromhex(h) for h in raw["frontier"]])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def tail_line(fd: int, end: int) -> bytes:
    """The last line of the first `end` bytes of a file, without reading the rest."""
    if end and os.pread(fd, 1, end - 1) == b"\n":
        end -= 1
    buf, pos = b"", end
    while pos > 0:
        step = min(1 << 16, pos)
        pos -= step
        buf = os.pread(fd, step, pos) + buf
        cut = buf.rfind(b"\n")
        if cut >= 0:
            return buf[cut + 1 :]
    return buf


def tip(folder: Path, fd: int, end: int) -> Tip:
    """The state file when it agrees with the ledger's last line, else rebuilt from every line."""
    if not end:
        return Tip()
    saved = saved_tip(folder / STATE)
    last, _ = line_hash(tail_line(fd, end))
    if saved and last and last.get("seq") == saved.size and last.get("hash") == saved.hash:
        return saved
    rebuilt = Tip()
    for raw in lines_of(os.pread(fd, end, 0)):
        rebuilt.add(*line_hash(raw))
    return rebuilt


@contextlib.contextmanager
def locked() -> Iterator[Path]:
    """The ledger folder, held against every other process and user for one change."""
    folder = ledger_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / LOCK).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield folder


@contextlib.contextmanager
def signals_held() -> Iterator[None]:
    """Ctrl-C and termination wait until the line and the state are written: a line is whole or absent."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, HELD)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def write_line(fd: int, end: int, line: bytes) -> bool:
    """Append one line in one write, after ending a torn last line; a short write is undone."""
    if end and os.pread(fd, 1, end - 1) != b"\n":
        line = b"\n" + line
    if os.write(fd, line) != len(line):
        os.ftruncate(fd, end)
        return False
    os.fsync(fd)
    return True


def append(kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Record one entry; the entry as written, or None when var/ cannot be written here (an agent sandbox)."""
    try:
        with locked() as folder:
            written = write_entries(folder, [(kind, data)])
            if written and kind in DECIDES:
                settle(folder, str(data.get("task")), DECIDES[kind])
            return written[0] if written else None
    except OSError:
        return None


def write_entries(folder: Path, items: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Chain and append entries, all in one write; held lock required. [] when the write fell short."""
    fd = os.open(folder / ENTRIES, os.O_RDWR | os.O_APPEND | os.O_CREAT, group_mode(0o644))
    try:
        end = os.fstat(fd).st_size
        at = tip(folder, fd, end)
        who = actor()
        entries = []
        for kind, data in items:
            entry: dict[str, Any] = {
                "seq": at.size + 1,
                "at": iso(now()),
                "kind": kind,
                "actor": who,
                "data": json.loads(canonical(data)),
                "prev": at.hash,
                "actor_prev": at.actors.get(actor_key(who), ""),
            }
            entry["hash"] = entry_hash(entry)
            at.add(entry, entry["hash"])
            entries.append(entry)
        with signals_held():
            if not write_line(fd, end, b"".join(canonical(e) + b"\n" for e in entries)):
                return []
            with contextlib.suppress(OSError):
                write_text(folder / STATE, json.dumps(at.to_json()) + "\n")
        return entries
    finally:
        os.close(fd)


def value_digest(value: Any) -> str:
    return sha(canonical(value))


def read_meta(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def load_observed(folder: Path) -> dict[str, Any]:
    try:
        seen = json.loads((folder / OBSERVED).read_bytes())
    except (OSError, ValueError):
        return {}
    return seen if isinstance(seen, dict) else {}


def settle(folder: Path, task_id: str, fields: tuple[str, ...]) -> None:
    """A decision just recorded directly: its fields as meta.json holds them now count as seen. The file's stat is
    left as it was, so a catch-up still looks at the task's other fields."""
    found = task_folder(task_id)
    raw = read_meta(found / "meta.json") if found else None
    if raw is None:
        return
    seen = load_observed(folder)
    task = seen.setdefault(task_id, {"stat": None, "fields": {}})
    task["fields"] |= {f: value_digest(raw.get(f)) for f in fields}
    with contextlib.suppress(OSError):
        write_text(folder / OBSERVED, json.dumps(seen) + "\n")


def decided_by(raw: dict[str, Any], field: str) -> str:
    source = {"abandoned_reason": "outcome", "superseded_by": "superseded"}.get(field, field)
    record = raw.get(source)
    return str(record.get("by") or record.get("closed_by") or "") if isinstance(record, dict) else ""


def stamp(path: Path) -> list[int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def writable() -> bool:
    path = ledger_dir()
    while not path.exists() and path != path.parent:
        path = path.parent
    return os.access(path, os.W_OK)


def observe() -> list[dict[str, Any]]:
    """Catch up with decisions written to meta.json that the ledger has not seen (an agent's, from a sandbox that
    cannot write var/): one `observed` entry per changed field. Reads only the meta.json files whose stat changed
    since the last catch-up; nothing where var/ cannot be written."""
    if not writable():
        return []
    metas = {f.name: f / "meta.json" for f in [*all_task_folders(), *offboarded_folders()]}
    seen = load_observed(ledger_dir())
    stamps = {task: stamp(path) for task, path in metas.items()}
    moved = [t for t, st in stamps.items() if st is not None and (seen.get(t) or {}).get("stat") != st]
    if not moved:
        return []
    try:
        with locked() as folder:
            seen = load_observed(folder)
            items: list[tuple[str, dict[str, Any]]] = []
            for task_id in moved:
                raw = read_meta(metas[task_id])
                if raw is None:
                    continue
                task = seen.setdefault(task_id, {"stat": None, "fields": {}})
                for field in FIELDS:
                    digest = value_digest(raw.get(field))
                    if task["fields"].get(field, value_digest(None)) != digest:
                        value = {"task": task_id, "field": field, "value": raw.get(field)}
                        items.append(("observed", value | {"by": decided_by(raw, field)}))
                        task["fields"][field] = digest
                task["stat"] = stamps[task_id]
            written = write_entries(folder, items) if items else []
            if items and not written:
                return []
            write_text(folder / OBSERVED, json.dumps(seen) + "\n")
            return written
    except OSError:
        return []


def checkpoint() -> dict[str, Any] | None:
    """Keep the tree's size and root now in checkpoints.jsonl; None when nothing is recorded or var/ cannot be
    written."""
    try:
        with locked() as folder:
            if not (folder / ENTRIES).is_file():
                return None
            fd = os.open(folder / ENTRIES, os.O_RDONLY)
            try:
                at = tip(folder, fd, os.fstat(fd).st_size)
            finally:
                os.close(fd)
            if not at.size:
                return None
            mark = {"size": at.size, "root": at.root().hex(), "at": iso(now())}
            fd = os.open(folder / CHECKPOINTS, os.O_RDWR | os.O_APPEND | os.O_CREAT, group_mode(0o644))
            try:
                end = os.fstat(fd).st_size
                last, _ = line_hash(tail_line(fd, end))
                if last and last.get("size") == at.size and last.get("root") == mark["root"]:
                    return last
                with signals_held():
                    return mark if write_line(fd, end, canonical(mark) + b"\n") else None
            finally:
                os.close(fd)
    except OSError:
        return None


def files_of(rec: dict[str, Any]) -> dict[str, str]:
    files = rec.get("files")
    return {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {}


def manifest(rec: dict[str, Any], absent: list[str]) -> dict[str, Any]:
    """The snapshot files a run record names, with their content hashes and modes as it records them."""
    modes = rec.get("modes")
    modes = modes if isinstance(modes, dict) else {}
    return {n: {"sha256": h, "mode": modes.get(n)} for n, h in sorted(files_of(rec).items()) if n not in absent}


def run_digest(folder: Path, rec: dict[str, Any], absent: list[str]) -> dict[str, str]:
    """What a run's record is made of, hashed part by part: run.json as written, both logs by content (plain or
    gzipped) and the snapshot manifest. Nothing in run.json changes once the run ended, so all of it counts."""
    return {
        "record": sha(canonical(rec)),
        "events": sha(store.read_log(folder, "events.jsonl")),
        "output": sha(store.read_log(folder, "output.log")),
        "snapshot": sha(canonical(manifest(rec, absent))),
    }


def run_started(folder: Path, task_hash: str) -> None:
    """Record that a run began, once its first record is written: what it runs, how, and the lock it asks for."""
    try:
        rec = json.loads((folder / "run.json").read_bytes())
    except (OSError, ValueError):
        return
    lock = rec.get("lock") or {}
    data = {
        "task": rec.get("task_id"),
        "run": rec.get("run_id"),
        "task_kind": rec.get("task_kind"),
        "mode": rec.get("mode"),
        "argv": rec.get("argv") or [],
        "task_hash": task_hash,
        "lock": {"mode": lock.get("mode"), "targets": lock.get("targets")},
    }
    if rec.get("plan_run"):
        data["plan_run"] = rec["plan_run"]
    append("run_start", data)


def run_ended(folder: Path) -> None:
    """Seal a run that has a record: its status and digest, then a checkpoint."""
    try:
        rec = json.loads((folder / "run.json").read_bytes())
        absent = sorted(n for n in files_of(rec) if not (folder / "snapshot" / n).is_file())
        parts = run_digest(folder, rec, absent)
    except (OSError, ValueError):
        return
    data: dict[str, Any] = {
        "task": rec.get("task_id"),
        "run": rec.get("run_id"),
        "status": rec.get("status"),
        "digest": sha(canonical(parts)),
        "parts": parts,
    }
    if absent:
        data["absent"] = absent
    if append("run_end", data) and (mark := checkpoint()):
        export(mark)


def export_argv() -> list[str]:
    try:
        value = read_json(config_path()).get(EXPORT_KEY)
    except (OSError, ValueError, AttributeError):
        return []
    return [str(x) for x in value] if isinstance(value, list) and value else []


def export_command() -> str:
    """The command each new checkpoint is sent to, as one command line; empty when off."""
    return shlex.join(export_argv())


def set_export_command(text: Any) -> None:
    """Set (or, with an empty string, clear) the operator's checkpoint command in the config file."""
    if not isinstance(text, str):
        raise OpsquireError("checkpoint_command must be a command line, or empty to turn it off")
    try:
        argv = shlex.split(text)
    except ValueError as e:
        raise OpsquireError(f"checkpoint_command: {e}") from None
    update_config(**{EXPORT_KEY: argv or False})


def export(mark: dict[str, Any]) -> dict[str, Any] | None:
    """Send a checkpoint off this machine with the operator's command: `<size>:<root>` on stdin and in
    OPSQUIRE_CHECKPOINT. It never fails the run, and Ctrl-C stops only the send; the outcome is kept in
    var/ledger/export.json for `opsquire audit`."""
    argv = export_argv()
    if not argv:
        return None
    text = f"{mark['size']}:{mark['root']}"
    result: dict[str, Any] = {"at": iso(now()), "checkpoint": text, "command": shlex.join(argv), "ok": False}
    try:
        r = subprocess.run(
            argv,
            input=text + "\n",
            capture_output=True,
            text=True,
            timeout=EXPORT_TIMEOUT_S,
            env={**os.environ, EXPORT_ENV: text},
        )
        result["ok"] = r.returncode == 0
        if not result["ok"]:
            result["error"] = f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}"
    except subprocess.TimeoutExpired:
        result["error"] = f"no answer in {EXPORT_TIMEOUT_S:.0f}s"
    except KeyboardInterrupt:
        result["error"] = "stopped by Ctrl-C; the run itself had ended"
    except OSError as e:
        result["error"] = str(e)
    with contextlib.suppress(OSError):
        write_json(ledger_dir() / EXPORTED, result)
    return result


def last_export() -> dict[str, Any] | None:
    try:
        value = read_json(ledger_dir() / EXPORTED)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_lines(path: Path) -> list[bytes]:
    try:
        return lines_of(path.read_bytes())
    except FileNotFoundError:
        return []


@dataclass
class Ledger:
    """Every entry as read, with its hash recomputed from its content, and the leaves of the tree."""

    entries: list[dict[str, Any] | None]
    hashes: list[str]

    @classmethod
    def read(cls) -> Ledger:
        pairs = [line_hash(raw) for raw in read_lines(ledger_dir() / ENTRIES)]
        return cls([e for e, _ in pairs], [h for _, h in pairs])

    def leaves(self) -> list[bytes]:
        return [leaf(h) for h in self.hashes]


def parse_checkpoint(text: str) -> tuple[int, bytes]:
    size, _, root = text.strip().partition(":")
    try:
        value = bytes.fromhex(root)
        if int(size) < 0 or len(value) != 32:
            raise ValueError
        return int(size), value
    except ValueError:
        raise ValueError(f"{text!r} is not a checkpoint: `<size>:<root>` as `opsquire audit` prints it") from None


@dataclass
class Audit:
    entries: int = 0
    actors: dict[str, int] = field(default_factory=dict)
    checkpoints: int = 0
    runs: dict[str, int] = field(default_factory=lambda: {"sealed": 0, "before_ledger": 0, "running": 0, "unsealed": 0})
    snapshot_files: int = 0
    problems: list[str] = field(default_factory=list)
    before_ledger_changed: list[str] = field(default_factory=list)
    unrecorded: list[str] = field(default_factory=list)
    checkpoint: str = ""
    against: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def check_entries(ledger: Ledger, out: Audit, roots_at: set[int]) -> tuple[Tip, dict[int, bytes]]:
    """The global chain and every actor's chain; the tree's root at each size in `roots_at`."""
    t = Tip()
    roots: dict[int, bytes] = {}
    last_seq = 0
    for i, (e, h) in enumerate(zip(ledger.entries, ledger.hashes, strict=True), 1):
        if e is None:
            out.problems.append(f"entry {i}: not a ledger entry")
            last_seq += 1
        else:
            key = actor_key(e.get("actor"))
            out.actors[key] = out.actors.get(key, 0) + 1
            if e.get("seq") != last_seq + 1:
                out.problems.append(
                    f"entry {i}: numbered {e.get('seq')} after {last_seq}: entries were removed or reordered"
                )
            if e.get("hash") != h:
                out.problems.append(f"entry {i}: changed after it was written (its hash does not match its content)")
            if e.get("prev") != t.hash:
                out.problems.append(f"entry {i}: does not follow the entry before it")
            if e.get("actor_prev") != t.actors.get(key, ""):
                out.problems.append(f"entry {i}: breaks the chain of {key}")
            seq = e.get("seq")
            last_seq = seq if isinstance(seq, int) else last_seq + 1
        t.add(e, h)
        if i in roots_at:
            roots[i] = t.root()
    out.entries = t.size
    return t, roots


def check_checkpoints(marks: list[dict[str, Any] | None], size: int, roots: dict[int, bytes], out: Audit) -> None:
    for j, mark in enumerate(marks, 1):
        if mark is None or not isinstance(mark.get("size"), int):
            out.problems.append(f"checkpoint {j}: not a checkpoint")
            continue
        out.checkpoints += 1
        if mark["size"] > size:
            out.problems.append(
                f"checkpoint {j} kept {mark['size']} entries but the ledger has {size}: entries were removed"
            )
        elif mark["size"] and roots.get(mark["size"], b"").hex() != mark.get("root"):
            out.problems.append(f"checkpoint {j} ({mark['size']} entries): the entries no longer give its root")


def check_against(text: str, ledger: Ledger, t: Tip, out: Audit) -> None:
    size, root = parse_checkpoint(text)
    out.against = {"checkpoint": text, "consistent": False, "proof": []}
    if size > t.size:
        out.problems.append(f"--against {text}: the ledger has only {t.size} entries: history was cut")
        return
    proof = consistency_proof(size, ledger.leaves())
    out.against["proof"] = [p.hex() for p in proof]
    if verify_consistency(size, t.size, root, t.root(), proof):
        out.against["consistent"] = True
    else:
        out.problems.append(f"--against {text}: the ledger does not extend that checkpoint: history was rewritten")


class Digests:
    """A snapshot file's hashes, content (how run records hash meta.json, without its annotations) and raw (how
    records before that did), once per stored file: most snapshots share the same blobs."""

    def __init__(self) -> None:
        self.seen: dict[tuple[int, int, bool], tuple[str, str]] = {}

    def of(self, path: Path) -> tuple[str, str] | None:
        try:
            st = path.stat()
            key = (st.st_dev, st.st_ino, path.name == "meta.json")
            if key not in self.seen:
                self.seen[key] = (content_digest(path), store.digest(path))
            return self.seen[key]
        except OSError:
            return None


def check_snapshot(
    folder: Path, rec: dict[str, Any], names: list[str], label: str, digests: Digests, out: Audit, into: list[str]
) -> None:
    files = files_of(rec)
    for name in names:
        out.snapshot_files += 1
        found = digests.of(folder / "snapshot" / name)
        if found is None:
            into.append(f"run {label}: snapshot file {name} is missing")
        elif files[name] not in found:
            into.append(f"run {label}: snapshot file {name} changed")


def started_runs(ledger: Ledger) -> set[tuple[str, str]]:
    return {
        (str(e["data"].get("task")), str(e["data"].get("run")))
        for e in ledger.entries
        if e and e.get("kind") == "run_start" and isinstance(e.get("data"), dict)
    }


def check_runs(ledger: Ledger, out: Audit, later: Callable[[], Ledger]) -> None:
    """Every run record against its entries: a sealed run's digest and snapshot, an unsealed run's start. A record
    missing from `ledger` whose start the ledger as it is after the scan (`later`) holds began during the audit."""
    starts: dict[tuple[str, str], dict[str, Any]] = {}
    ends: dict[tuple[str, str], dict[str, Any]] = {}
    for e in ledger.entries:
        data = e.get("data") if e else None
        if e is None or not isinstance(data, dict):
            continue
        key = (str(data.get("task")), str(data.get("run")))
        if e.get("kind") == "run_start":
            starts[key] = data
        elif e.get("kind") == "run_end":
            ends[key] = data
    began = next((str(e.get("at")) for e in ledger.entries if e), "")
    digests = Digests()
    seen: set[tuple[str, str]] = set()
    root = runs_dir()
    for path in sorted(root.glob("*/*/run.json")) if root.is_dir() else []:
        folder = path.parent
        key = (folder.parent.name, folder.name)
        label = f"{key[0]}/{key[1]}"
        seen.add(key)
        try:
            rec = json.loads(path.read_bytes())
            assert isinstance(rec, dict)
        except (OSError, ValueError, AssertionError):
            out.problems.append(f"run {label}: run.json is unreadable")
            continue
        alive = rec.get("status") == "running" and run_alive(
            int(rec.get("pid") or 0), str(rec.get("host")), folder, str(rec.get("pid_ns") or "")
        )
        end = ends.get(key)
        if end is not None:
            out.runs["sealed"] += 1
            absent = [str(n) for n in end.get("absent") or []]
            parts = end.get("parts")
            parts = parts if isinstance(parts, dict) else {}
            now_parts = run_digest(folder, rec, absent)
            changed = [p for p in now_parts if now_parts[p] != parts.get(p)]
            if changed or end.get("digest") != sha(canonical(parts)):
                names = {"record": "run.json", "events": "events.jsonl", "output": "output.log", "snapshot": "manifest"}
                what = ", ".join(names[p] for p in changed) or "its digest"
                out.problems.append(f"run {label}: {what} changed since the run ended")
            check_snapshot(
                folder, rec, [n for n in files_of(rec) if n not in absent], label, digests, out, out.problems
            )
            continue
        start = starts.get(key)
        into = out.problems
        if alive:
            out.runs["running"] += 1
        elif start is not None:
            out.runs["unsealed"] += 1
            same = (start.get("mode"), start.get("argv") or []) == (rec.get("mode"), rec.get("argv") or [])
            if not same:
                out.problems.append(f"run {label}: its mode or arguments differ from its start in the ledger")
        elif began and str(rec.get("started") or "") >= began:
            if key in started_runs(later()):
                out.runs["running"] += 1
            else:
                out.problems.append(f"run {label}: started after the ledger began but is not in it")
            continue
        else:
            out.runs["before_ledger"] += 1
            into = out.before_ledger_changed
        present = [n for n in files_of(rec) if (folder / "snapshot" / n).is_file()]
        check_snapshot(folder, rec, present, label, digests, out, into)
    for key in sorted((set(starts) | set(ends)) - seen):
        out.problems.append(f"run {key[0]}/{key[1]}: in the ledger but its record is gone")


def check_decisions(ledger: Ledger, out: Audit, later: Callable[[], Ledger]) -> None:
    """Decisions meta.json holds now that no entry recorded, directly or observed; one recorded while the audit ran
    (in `later` only) counts as recorded."""
    recorded = recorded_decisions(ledger)
    since = functools.cache(lambda: recorded_decisions(later()))
    for folder in [*all_task_folders(), *offboarded_folders()]:
        raw = read_meta(folder / "meta.json") or {}
        for f in FIELDS:
            digest = value_digest(raw[f]) if raw.get(f) is not None else None
            key = (folder.name, f)
            if digest and digest not in recorded.get(key, set()) and digest not in since().get(key, set()):
                out.unrecorded.append(f"{folder.name}: {f}")


def recorded_decisions(ledger: Ledger) -> dict[tuple[str, str], set[str]]:
    recorded: dict[tuple[str, str], set[str]] = {}
    for e in ledger.entries:
        data = e.get("data") if e else None
        if e is None or not isinstance(data, dict):
            continue
        task, kind = str(data.get("task")), e.get("kind")
        pairs = [(str(data.get("field")), data.get("value"))] if kind == "observed" else []
        pairs += [(f, data[f]) for f in DECIDES.get(str(kind), ()) if f in data]
        outcome = data.get("outcome") if kind == "close" else None
        if isinstance(outcome, dict) and outcome.get("state") == "abandoned":
            pairs.append(("abandoned_reason", outcome.get("note")))
        for f, value in pairs:
            recorded.setdefault((task, f), set()).add(value_digest(value))
    return recorded


def audit(against: str | None = None) -> Audit:
    """Check the ledger and every run record; see the module docstring for what that proves."""
    out = Audit()
    # Checkpoints before entries: a checkpoint is written after the entries it counts and both files only grow, so
    # every checkpoint read here counts entries the read after it holds, however many runs append meanwhile.
    marks = [line_hash(raw)[0] for raw in read_lines(ledger_dir() / CHECKPOINTS)]
    ledger = Ledger.read()
    sizes = {m["size"] for m in marks if m and isinstance(m.get("size"), int)}
    t, roots = check_entries(ledger, out, sizes)
    check_checkpoints(marks, t.size, roots, out)
    if against is not None:
        check_against(against, ledger, t, out)
    later = functools.cache(Ledger.read)
    check_runs(ledger, out, later)
    check_decisions(ledger, out, later)
    out.checkpoint = f"{t.size}:{t.root().hex()}"
    return out


def run_entries(run_id: str) -> dict[str, Any]:
    """A run's entries, each with its proof of inclusion in the tree as it is now."""
    ledger = Ledger.read()
    leaves = ledger.leaves()
    size, root = len(leaves), tree_hash(leaves)
    found = []
    for i, e in enumerate(ledger.entries):
        data = e.get("data") if e else None
        if not isinstance(data, dict):
            continue
        holder = data.get("holder")
        held_by = holder.get("run_id") if isinstance(holder, dict) else None
        if run_id not in (data.get("run"), data.get("run_id"), held_by):
            continue
        proof = inclusion_proof(i, leaves)
        found.append(
            {
                "index": i,
                "entry": e,
                "proof": [p.hex() for p in proof],
                "included": verify_inclusion(i, size, leaves[i], proof, root),
            }
        )
    return {"run": run_id, "checkpoint": f"{size}:{root.hex()}", "entries": found}
