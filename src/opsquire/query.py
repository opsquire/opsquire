from __future__ import annotations

import ast
import bisect
import collections
import contextlib
import functools
import itertools
import json
import marshal
import math
import os
import re
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .lint import cached_lint_ok
from .model import (
    DATA_DIR,
    HEARTBEAT_EVERY,
    OUTCOME_STATES,
    TEMPLATES_DIR,
    Task,
    all_task_folders,
    home_dir,
    is_last_stage,
    is_offboarded,
    load_task,
    offboarded_folders,
    runs_dir,
    var_dir,
)
from .runner import VANISHED, load_run

TASK_STATUSES = ("draft", "ready", "done", "partial", "superseded", "abandoned", "archived")
SCRIPT_STATUSES = ("lint-ok", "lint-failing", "unchecked")


def runs_for(task_id: str) -> list[dict[str, Any]]:
    folder = runs_dir() / task_id
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(folder.glob("*/run.json")):
        try:
            out.append(load_run(p))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: str(r.get("started")))


def all_runs() -> list[dict[str, Any]]:
    root = runs_dir()
    if not root.is_dir():
        return []
    recs: list[dict[str, Any]] = []
    for p in root.glob("*/*/run.json"):
        try:
            recs.append(load_run(p))
        except (OSError, ValueError):
            continue
    return sorted(recs, key=lambda r: str(r.get("started")))


def recorded_end(raw: dict[str, Any]) -> str | None:
    """How meta.json says the task ended. Read from the file as written: an error elsewhere in meta.json (a general
    script it used was renamed since) must not turn a retired or closed task back into a draft."""
    if raw.get("archived"):
        return "archived"
    if raw.get("abandoned_reason"):
        return "abandoned"
    if raw.get("superseded_by"):
        return "superseded"
    outcome = raw.get("outcome")
    if isinstance(outcome, dict) and outcome.get("state") in OUTCOME_STATES:
        return str(outcome["state"])
    return None


def script_status(task: Task) -> str:
    """A general script never runs on its own, so its status is its lint: of the files as they are now."""
    ok = cached_lint_ok(task)
    if ok is None:
        return "unchecked"
    return "lint-ok" if ok else "lint-failing"


def task_status(task: Task, runs: list[dict[str, Any]]) -> str:
    end = recorded_end(task.raw)
    if end:
        return end
    if task.kind == "general":
        return script_status(task)
    final_mode = "apply" if task.raw.get("changes_prod") is True else "read"
    last_stage = [
        r for r in runs if task.meta is None or is_last_stage(task.meta, [str(x) for x in r.get("argv") or []])
    ]
    if any(r.get("mode") == final_mode and r.get("status") == "ok" for r in last_stage):
        return "done"
    if task.meta and cached_lint_ok(task):
        return "ready"
    return "draft"


def used_by(script_id: str, tasks: list[Task]) -> list[str]:
    """The tasks whose meta.json names this general script, valid or not."""
    return [t.id for t in tasks if isinstance(t.raw.get("uses"), list) and script_id in t.raw["uses"]]


def summarize(task: Task) -> dict[str, Any]:
    runs = runs_for(task.id)
    last = runs[-1] if runs else None
    m = task.meta
    return {
        "id": task.id,
        "kind": task.kind,
        "status": task_status(task, runs),
        "area": m.area if m else task.raw.get("area"),
        "purpose": m.purpose if m else task.raw.get("purpose"),
        "changes_prod": m.changes_prod if m else task.raw.get("changes_prod"),
        "targets": m.targets if m else task.raw.get("targets"),
        "path": str(task.folder),
        "offboarded": is_offboarded(task.folder),
        "meta_errors": task.errors,
        "runs": len(runs),
        "last_run": (
            {k: last.get(k) for k in ("run_id", "mode", "status", "started", "last_error")}
            | {"dir": last["paths"]["dir"]}
            if last
            else None
        ),
    }


def list_tasks() -> list[dict[str, Any]]:
    return [summarize(load_task(f)) for f in all_task_folders()]


def find_run(query: str) -> Path | None:
    root = runs_dir()
    if not root.is_dir():
        return None
    runs = [p for p in root.glob("*/*") if p.is_dir()]
    candidates = [p for p in runs if query == p.name or query in f"{p.parent.name}/{p.name}"]
    return sorted(candidates)[-1] if candidates else None


SEARCH_WEIGHTS = {
    "id": 3.0,
    "area": 3.0,
    "targets": 2.5,
    "purpose": 2.0,
    "uses": 1.0,
    "history": 1.0,
    "docstring": 0.75,
    "design": 0.5,
    "thread": 0.5,
    "runs": 0.5,
}
PREFIX_FIELDS = frozenset({"id", "area", "targets", "purpose", "uses"})
STOPWORDS = frozenset("a an and as at be by for from in is it of on or the to with".split())
STEM_STRENGTH, PARTS_STRENGTH, PREFIX_STRENGTH, SYNONYM_FACTOR = 0.85, 0.35, 0.5, 0.6
ECHO_FACTOR = 0.3
RATED_MODES = ("apply", "read")
SYNONYMS_FILE = "synonyms.json"
_SUFFIXES = ("ations", "ation", "ions", "ion", "ings", "ing", "ies", "ed", "es", "s")


@functools.cache
def stem(word: str) -> str:
    """A light stem so that word forms meet: rejections, rejected and rejects all become reject; closing and closed
    become clos, stopped becomes stop. Short words and words in -ss are left as they are."""
    if len(word) <= 3 or word.endswith("ss") or not word.isalpha():
        return word
    for suffix in _SUFFIXES:
        base = word[: -len(suffix)]
        if word.endswith(suffix) and len(base) >= 3:
            if suffix == "ies":
                return base + "y"
            if suffix == "es" and not base.endswith(("s", "x", "z", "ch", "sh")):
                base = word[:-1]
            if suffix in ("ed", "ing") and len(base) > 3 and base[-1] == base[-2] and base[-1] not in "lsz":
                base = base[:-1]
            return base.removesuffix("e") if len(base) > 3 else base
    return word.removesuffix("e") if len(word) > 4 else word


_FORM_ENDINGS = ("", "s", "es", "e", "ed", "ing", "ings", "ion", "ions", "ation", "ations")


@functools.cache
def forms(base: str) -> frozenset[str]:
    """The words that stem to `base`, among its usual forms: what a stem matches in a task's words."""
    found = {base + e for e in _FORM_ENDINGS} | {base + base[-1] + e for e in ("ed", "ing")}
    found |= {base + "e" + e for e in ("s", "d")}
    if base.endswith("y"):
        found |= {base[:-1] + e for e in ("ies", "ied")}
    return frozenset(w for w in found if stem(w) == base) | {base}


WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def search_words(text: str) -> list[str]:
    """Words as search sees them, in order: a hyphenated or underscored name stays one word (underscores read as
    hyphens); stop words and one-letter words drop out."""
    return [w for w in map(_word, WORD_RE.findall(text.lower())) if len(w) > 1 and w not in STOPWORDS]


@functools.cache
def _word(token: str) -> str:
    return token.replace("_", "-").strip("-")


@functools.cache
def _indexed(token: str) -> tuple[str, ...]:
    word = _word(token)
    if len(word) < 2 or word in STOPWORDS:
        return ()
    return (word, *(p for p in word.split("-") if len(p) > 1)) if "-" in word else (word,)


def text_words(text: str, drop: frozenset[str] = frozenset()) -> set[str]:
    """The words a field indexes: each hyphenated or underscored word both whole and split, less `drop`."""
    words = set(itertools.chain.from_iterable(map(_indexed, set(WORD_RE.findall(text.lower())))))
    return words - drop if drop else words


class Term:
    """A query word, a run of query words, or a synonym: matched whole first, then by stem, then part by part."""

    def __init__(self, text: str) -> None:
        words = search_words(text)
        self.whole = "-".join(words)
        self.parts = tuple(stem(p) for w in words for p in w.split("-") if len(p) > 1 and p not in STOPWORDS)


def synonym_groups() -> list[list[str]]:
    """The package's synonym groups, then the home's own (`<home>/synonyms.json`, same shape: {"groups": [[...]]}).
    A word in several groups matches every word of each; a home group extends the package's, it never removes one.
    An unreadable home file is ignored."""
    groups: list[list[str]] = []
    for path in (DATA_DIR / SYNONYMS_FILE, home_dir() / SYNONYMS_FILE):
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        listed = raw.get("groups") if isinstance(raw, dict) else None
        if isinstance(listed, list):
            groups += [[str(w) for w in g] for g in listed if isinstance(g, list)]
    return groups


FIELD_NAMES = tuple(SEARCH_WEIGHTS)
FIELD_BITS = {name: 1 << i for i, name in enumerate(FIELD_NAMES)}
PREFIX_MASK = sum(FIELD_BITS[name] for name in PREFIX_FIELDS)
ALL_FIELDS = (1 << len(FIELD_NAMES)) - 1
WEIGHTS = [SEARCH_WEIGHTS[name] for name in FIELD_NAMES]


def field_bits(mask: int) -> list[int]:
    return [i for i in range(len(FIELD_NAMES)) if mask >> i & 1]


class Concept:
    """One thing the query asks for: its own term plus the synonyms of its group(s). Matching reads a task's words
    (word -> bitmask of the fields it occurs in) and gives each field the strongest way the concept meets it: the
    term whole (1.0), a form of its one word (STEM_STRENGTH), all the parts of a longer term (PARTS_STRENGTH), a
    prefix in a prefix field (PREFIX_STRENGTH), or any of these for a synonym, scaled by SYNONYM_FACTOR."""

    def __init__(self, term: Term, synonyms: list[Term], label: str) -> None:
        self.term, self.label = term, label
        self.levels: dict[str, float] = {}
        self.runs: list[tuple[float, list[frozenset[str]]]] = []
        for t, factor in [(term, 1.0), *((syn, SYNONYM_FACTOR) for syn in synonyms)]:
            if not t.parts:
                continue
            self.level(t.whole, factor)
            if len(t.parts) == 1:
                for w in forms(t.parts[0]):
                    self.level(w, factor * STEM_STRENGTH)
            else:
                self.runs.append((factor * PARTS_STRENGTH, [forms(p) for p in t.parts]))
        self.candidates = frozenset(self.levels)
        word = term.whole
        self.prefix = word if "-" not in word and len(word) >= 3 and len(term.parts) == 1 else ""

    def level(self, word: str, value: float) -> None:
        if value > self.levels.get(word, 0.0):
            self.levels[word] = value

    def strengths(self, words: dict[str, int]) -> list[float]:
        """Per field (in FIELD_NAMES order), how strongly the concept meets it."""
        best = [0.0] * len(FIELD_NAMES)

        def give(mask: int, value: float) -> None:
            for i in field_bits(mask):
                if value > best[i]:
                    best[i] = value

        for w in words.keys() & self.candidates:
            give(words[w], self.levels[w])
        for value, part_forms in self.runs:
            mask = ALL_FIELDS
            for fs in part_forms:
                found = 0
                for w in words.keys() & fs:
                    found |= words[w]
                mask &= found
                if not mask:
                    break
            give(mask, value)
        if self.prefix:
            give(self.prefix_mask(words) & PREFIX_MASK, PREFIX_STRENGTH)
        return best

    def prefix_mask(self, words: dict[str, int]) -> int:
        """Fields with a word that starts with the term, or that the term starts with (four letters or more). The
        index keeps a task's words sorted, so the first kind is one bisect away."""
        word = self.prefix
        found = 0
        ordered = list(words)
        i = bisect.bisect_left(ordered, word)
        while i < len(ordered) and ordered[i].startswith(word):
            found |= words[ordered[i]]
            i += 1
        for k in range(4, len(word)):
            found |= words.get(word[:k], 0)
        return found


def concepts(query: str, groups: list[list[str]]) -> list[Concept]:
    """The query read left to right: a run of two or three words that is a synonym (kill switch) is one concept,
    every other word is one; a repeated concept counts once."""
    terms = [[Term(w) for w in g] for g in groups]
    by_parts: dict[tuple[str, ...], list[Term]] = {}
    for group in terms:
        for t in group:
            if t.parts:
                by_parts.setdefault(t.parts, []).extend(o for o in group if o is not t)
    words = search_words(query)
    out: list[Concept] = []
    i = 0
    while i < len(words):
        for k in (3, 2, 1):
            run = " ".join(words[i : i + k])
            term = Term(run)
            if k == 1 or (len(words) >= i + k and term.parts in by_parts):
                out.append(Concept(term, by_parts.get(term.parts, []), run))
                i += k
                break
    unique = {c.label: c for c in out if c.term.parts}
    return list(unique.values())


def task_runs(task_id: str) -> list[tuple[str, dict[str, Any]]]:
    """A task's run records as written (path, record), oldest first."""
    folder = os.path.join(str(runs_dir()), task_id)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        path = os.path.join(folder, name, "run.json")
        try:
            with open(path, "rb") as f:
                rec = json.loads(f.read())
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append((path, rec))
    return sorted(out, key=lambda pr: str(pr[1].get("started")))


def track_record(runs: int, ok: int) -> dict[str, Any]:
    """Applies and reads, and the share of them that ended ok; None when there are none."""
    return {"runs": runs, "ok": ok, "ok_rate": round(ok / runs, 2) if runs else None}


def script_files(folder: str) -> list[str]:
    """The files a script describes itself in: every .py and .sh but the tests."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if n.endswith((".py", ".sh")) and not n.startswith("test_")]


def script_text(paths: list[str]) -> str:
    """What a script says about itself: the module docstring and argparse description of each Python file, the
    leading comment block of each shell file. Never the code itself."""
    out: list[str] = []
    for path in paths:
        try:
            text = Path(path).read_text(errors="ignore")
        except OSError:
            continue
        if path.endswith(".py"):
            doc = re.match(r"\s*(?:#[^\n]*\n\s*)*(?:\"\"\"|\'\'\')(.*?)(?:\"\"\"|\'\'\')", text, re.S)
            out += [doc.group(1)] if doc else []
            out += [m.group(2) for m in re.finditer(r"description\s*=\s*(\"\"\"|\"|')(.*?)\1", text, re.S)]
        else:
            for line in text.splitlines()[1:40]:
                if not line.startswith("#"):
                    break
                if "shellcheck" not in line:
                    out.append(line.lstrip("# "))
    return "\n".join(out)


def design_drop() -> frozenset[str]:
    """Words of the design.md template: every task has them, so they say nothing about one."""
    try:
        return frozenset(text_words((TEMPLATES_DIR / "design.md").read_text()))
    except OSError:
        return frozenset()


INDEX_FORMAT = 3
INDEX_FILE = "find-index.marshal"
CACHE_ENV = "OPSQUIRE_CACHE_DIR"


def stat_of(path: str) -> list[Any]:
    """[path, mtime_ns, size]; -1s for a missing file, so its appearing counts as a change."""
    try:
        st = os.stat(path)
    except OSError:
        return [path, -1, -1]
    return [path, st.st_mtime_ns, st.st_size]


def unchanged(sig: list[list[Any]]) -> bool:
    return all(stat_of(entry[0]) == entry for entry in sig)


def cache_dir() -> Path:
    env = os.environ.get(CACHE_ENV)
    return Path(env) if env else var_dir() / "cache"


def index_key() -> list[Any]:
    """What every entry depends on beyond its own files: the opsquire and Python versions, this module (an edited
    search rebuilds the index) and the design.md template whose words are left out."""
    return [
        INDEX_FORMAT,
        __version__,
        list(sys.version_info[:3]),
        stat_of(__file__)[1:],
        stat_of(str(TEMPLATES_DIR / "design.md"))[1:],
    ]


def task_entry(folder: str, threads: dict[str, Any], drop: frozenset[str]) -> dict[str, Any]:
    """The indexed form of one task folder: its words per field, what search needs from meta.json and its runs, and
    the stat signature of every file it was built from."""
    task = load_task(Path(folder))
    raw = task.raw
    archived_raw = raw.get("archived")
    archived: dict[str, Any] = archived_raw if isinstance(archived_raw, dict) else {}
    history = " ".join(json.dumps(h) for h in archived.get("history", []) if isinstance(h, dict))
    design_path = os.path.join(folder, "design.md")
    try:
        design = Path(design_path).read_text(errors="ignore")
    except OSError:
        design = ""
    scripts = script_files(folder)
    runs = task_runs(task.id)
    last = runs[-1][1] if runs else {}
    listed = last.get("results")
    results = [str(x) for x in listed] if isinstance(listed, list) else []
    rated = [r for _, r in runs if r.get("mode") in RATED_MODES]
    runs_folder = os.path.join(str(runs_dir()), task.id)
    watched = [p for i, (p, r) in enumerate(runs) if i == len(runs) - 1 or r.get("status") == "running"]
    thread = str(raw.get("thread") or "")
    if thread not in threads:
        threads[thread] = thread_entry(thread)
    uses = raw.get("uses")
    fields = {
        "id": task.id,
        "area": str(raw.get("area", "")),
        "targets": " ".join(str(t) for t in raw.get("targets", []) or []),
        "purpose": f"{raw.get('purpose', '')} {archived.get('note', '')}",
        "uses": " ".join(str(u) for u in raw.get("uses", []) or []),
        "history": history,
        "docstring": script_text(scripts),
        "runs": " ".join([str(last.get("last_error") or ""), *results]),
    }
    masks: dict[str, int] = {}
    for name, found in [*((n, text_words(t)) for n, t in fields.items()), ("design", text_words(design, drop))]:
        bit = FIELD_BITS[name]
        for w in found:
            masks[w] = masks.get(w, 0) | bit
    sources = [folder, os.path.join(folder, "meta.json"), design_path, *scripts, runs_folder, *watched]
    return {
        "sig": [stat_of(p) for p in sources],
        "id": task.id,
        "kind": task.kind,
        "created": str(raw.get("created") or ""),
        "purpose": str(raw.get("purpose") or ""),
        "places": sorted(text_words(" ".join([str(raw.get("area", "")), fields["targets"]]))),
        "uses": [str(u) for u in uses] if isinstance(uses, list) else None,
        "offboarded": is_offboarded(task.folder),
        "thread": thread,
        "rated": [len(rated), sum(1 for r in rated if r.get("status") == "ok")],
        "runs": [[str(r.get(k) or "") for k in RUN_FIELDS] + [r.get("duration_s"), r.get("argv") or []] for r in rated],
        "usage": script_usage(folder, str(raw.get("entry") or "")) if task.kind == "general" else "",
        "words": dict(sorted(masks.items())),
    }


RUN_FIELDS = ("run_id", "mode", "status", "started", "last_error")
USAGE_RE = re.compile(r"^\s*usage\b[^:\n]{0,30}:\s*(\S.*)$", re.I | re.M)
FLAG_ACTIONS = frozenset({"store_true", "store_false", "store_const", "count", "help", "version"})


def script_usage(folder: str, entry: str) -> str:
    """How a general script is called, read without running it: a `Usage:` line in its entry file's docstring or
    header comment, else the arguments its argparse parser declares; "" when it says neither."""
    path = os.path.join(folder, entry or "main.py")
    said = USAGE_RE.search(script_text([path]))
    if said:
        return " ".join(said.group(1).split())
    if not path.endswith(".py"):
        return ""
    try:
        tree = ast.parse(Path(path).read_text(errors="ignore"))
    except (OSError, SyntaxError, ValueError):
        return ""
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
    ]
    options: list[str] = []
    positionals: list[str] = []
    for call in sorted(calls, key=lambda c: (c.lineno, c.col_offset)):
        names = [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if names:
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            (options if names[0].startswith("-") else positionals).append(argument_usage(names, kw))
    return " ".join(options + positionals)


def argument_usage(names: list[str], kw: dict[str, ast.expr]) -> str:
    """One argparse argument as its usage line shows it: `[--flag]`, `--name VALUE`, `[--name {a|b}]`, `target ...`."""

    def const(key: str) -> Any:
        node = kw.get(key)
        return node.value if isinstance(node, ast.Constant) else None

    listed = kw.get("choices")
    elements = listed.elts if isinstance(listed, ast.List | ast.Tuple | ast.Set) else []
    picked = [str(e.value) for e in elements if isinstance(e, ast.Constant)]
    name = max(names, key=len)
    option = name.startswith("-")
    if option and const("action") in FLAG_ACTIONS:
        return f"[{name}]"
    value = str(const("metavar") or ("{" + "|".join(picked) + "}" if picked else const("dest") or name.lstrip("-")))
    value = value.replace("-", "_")
    if option and not const("metavar") and not picked:
        value = value.upper()
    nargs = const("nargs")
    value = {"*": f"[{value} ...]", "+": f"{value} ...", "?": f"[{value}]"}.get(str(nargs), value)
    if not option:
        return value
    shown = f"{name} {value}"
    return shown if const("required") is True else f"[{shown}]"


def thread_entry(thread: str) -> dict[str, Any]:
    path = str(home_dir() / "threads" / f"{thread}.md")
    try:
        text = Path(path).read_text(errors="ignore") if thread else ""
    except OSError:
        text = ""
    bit = FIELD_BITS["thread"]
    words = {w: bit for w in sorted(text_words(f"{thread} {text}"))}
    return {"sig": [stat_of(path)] if thread else [], "words": words}


class SearchIndex:
    """Every task's indexed words, kept in a cache file and rebuilt entry by entry: an entry whose files' stat
    signature changed is rebuilt, a gone folder dropped, a new one added; the file is rewritten (atomically) only
    when something changed. A cache that cannot be read is rebuilt; one that cannot be written is skipped, and the
    index is then built in memory: search never fails on its cache."""

    def __init__(self) -> None:
        self.path = cache_dir() / INDEX_FILE
        key = index_key()
        cached = self.read(key)
        old_entries: dict[str, Any] = cached.get("entries", {})
        old_threads: dict[str, Any] = cached.get("threads", {})
        self.threads: dict[str, Any] = {}
        for name, entry in old_threads.items():
            if unchanged(entry["sig"]):
                self.threads[name] = entry
        drop = design_drop()
        self.entries: dict[str, dict[str, Any]] = {}
        self.rebuilt: list[str] = []
        for folder in map(str, [*all_task_folders(), *offboarded_folders()]):
            entry = old_entries.get(folder)
            if entry is None or not unchanged(entry["sig"]):
                entry = task_entry(folder, self.threads, drop)
                self.rebuilt.append(folder)
            elif entry["thread"] not in self.threads:
                self.threads[entry["thread"]] = thread_entry(entry["thread"])
            self.entries[folder] = entry
        changed = bool(self.rebuilt) or set(old_entries) != set(self.entries) or old_threads != self.threads
        if changed:
            self.write({"key": key, "entries": self.entries, "threads": self.threads})

    def read(self, key: list[Any]) -> dict[str, Any]:
        try:
            data = marshal.loads(self.path.read_bytes())
        except (OSError, EOFError, ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) and data.get("key") == key else {}

    def write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(marshal.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()


STATUS_ORDER = {"done": 0, "lint-ok": 0, "partial": 1, "ready": 1, "draft": 2, "unchecked": 2, "lint-failing": 2}
STATUS_ORDER |= {"archived": 3, "superseded": 4, "abandoned": 5}


def last_failure(summary: dict[str, Any]) -> str | None:
    last = summary.get("last_run") or {}
    if not last or last.get("status") in ("ok", "running"):
        return None
    return str(last.get("last_error") or last.get("status"))


@dataclass
class Scored:
    """One task or general script's match: its own score (and the query words it matched), and for a general script
    the votes of the matching tasks that use it, with the strongest voters first."""

    folder: str
    own: float
    matched: list[str]
    track: dict[str, Any]
    votes: float = 0.0
    voters: list[tuple[float, str]] = field(default_factory=list)
    score: float = 0.0


def own_scores(wanted: list[Concept], index: SearchIndex, exclude: str = "") -> dict[str, Scored]:
    """Each query word (or synonym phrase) scores per field by how closely it matches (whole word, stem, hyphen
    parts, prefix, synonym) times the field's weight; the sum is scaled by the share of query words that matched and
    nudged by the ok share of the applies and reads behind it (a general script's are its users')."""
    entries = index.entries
    users = users_of(index, exclude)
    rated = {e["id"]: e["rated"] for e in entries.values()}
    threads: dict[tuple[str, str], list[float]] = {}
    scored: dict[str, Scored] = {}
    for folder, entry in entries.items():
        if entry["id"] == exclude:
            continue
        score = covered = 0.0
        matched: list[str] = []
        for c in wanted:
            key = (c.label, entry["thread"])
            if key not in threads:
                threads[key] = c.strengths(index.threads[entry["thread"]]["words"])
            strengths = [max(a, b) for a, b in zip(c.strengths(entry["words"]), threads[key], strict=True)]
            per_field = sorted((WEIGHTS[i] * s for i, s in enumerate(strengths)), reverse=True)
            got = per_field[0] + ECHO_FACTOR * sum(per_field[1:])
            if got:
                score += got
                matched.append(c.label)
                covered += 1.0 if max(strengths) > PARTS_STRENGTH else 0.5
        if not matched and entry["kind"] != "general":
            continue
        score *= covered / len(wanted)
        runs, ok = rated.get(entry["id"], [0, 0])
        for user in users.get(entry["id"], []) if entry["kind"] == "general" else []:
            runs, ok = runs + entries[user]["rated"][0], ok + entries[user]["rated"][1]
        track = track_record(runs, ok)
        if track["ok_rate"] is not None:
            score *= 0.9 + 0.2 * track["ok_rate"]
        scored[folder] = Scored(folder, round(score, 2), sorted(matched), track, score=round(score, 2))
    return scored


def users_of(index: SearchIndex, exclude: str = "") -> dict[str, list[str]]:
    """general id -> the folders of the tasks whose meta.json names it."""
    users: dict[str, list[str]] = {}
    for folder, e in index.entries.items():
        if e["kind"] != "general" and e["id"] != exclude:
            for used in dict.fromkeys(e["uses"] or []):
                users.setdefault(used, []).append(folder)
    return users


VOTE_WEIGHT = 0.25


def damping(users: int, total: int) -> float:
    """What a matching user's vote is worth to a general script that `users` of the `total` tasks using any general
    script use: its inverse document frequency, scaled to 1 for a script one task uses. A helper nearly every task
    uses gains almost nothing from the many matching ones; a specific script gains from each of its few."""
    return math.log((total + 1) / (users + 1)) / math.log((total + 1) / 2) if total > 1 else 1.0


def blend(index: SearchIndex, scored: dict[str, Scored], exclude: str = "") -> list[Scored]:
    """The general scripts, each scored by its own match plus VOTE_WEIGHT times the damped votes of the matching
    tasks that use it: a task votes its score, shared among the general scripts it uses."""
    entries = index.entries
    users = users_of(index, exclude)
    total = sum(1 for e in entries.values() if e["kind"] != "general" and e["uses"] and e["id"] != exclude)
    generals = {e["id"]: f for f, e in entries.items() if e["kind"] == "general" and e["id"] != exclude}
    for s in scored.values():
        uses = [u for u in dict.fromkeys(entries[s.folder]["uses"] or []) if u in generals]
        if entries[s.folder]["kind"] == "general" or not uses:
            continue
        for used in uses:
            g = scored.get(generals[used])
            if g is not None:
                g.votes += s.own / len(uses)
                g.voters.append((s.own / len(uses), entries[s.folder]["id"]))
    out: list[Scored] = []
    for g in (scored[f] for f in generals.values() if f in scored):
        g.score = round(g.own + VOTE_WEIGHT * damping(len(users.get(entries[g.folder]["id"], [])), total) * g.votes, 2)
        g.votes = round(g.votes, 2)
        g.voters.sort(key=lambda v: (-v[0], v[1]))
        if g.score > 0:
            out.append(g)
    return ordered(index, out)


def ordered(index: SearchIndex, scored: list[Scored]) -> list[Scored]:
    """Highest score first; ties go to the active, then the newest."""
    e = index.entries
    out = sorted(scored, key=lambda s: s.folder)
    out.sort(key=lambda s: str(e[s.folder]["created"]), reverse=True)
    out.sort(key=lambda s: (-s.score, e[s.folder]["offboarded"]))
    return out


def rank(query: str, index: SearchIndex, exclude: str = "") -> tuple[list[Scored], list[Scored]]:
    """The query's matches in two rankings: general scripts to reuse (see blend) and past one-off tasks and plans.
    `exclude` leaves one task out, as if it did not exist."""
    wanted = concepts(query, synonym_groups())
    if not wanted:
        return [], []
    scored = own_scores(wanted, index, exclude)
    past = ordered(index, [s for s in scored.values() if index.entries[s.folder]["kind"] != "general"])
    return blend(index, scored, exclude), past


def settle(index: SearchIndex, scored: list[Scored], limit: int) -> list[tuple[Scored, Task]]:
    """The first `limit` of a ranking, ties at the cut kept and then broken by status (settled ok first)."""
    if len(scored) > limit:
        scored = [s for s in scored if s.score >= scored[limit - 1].score] if limit > 0 else []
    tasks = {s.folder: load_task(Path(s.folder)) for s in scored}
    status = {f: STATUS_ORDER.get(task_status(t, runs_for(t.id)), 9) for f, t in tasks.items()}
    by_status = sorted(scored, key=lambda s: (-s.score, index.entries[s.folder]["offboarded"], status[s.folder]))
    return [(s, tasks[s.folder]) for s in by_status[:limit]]


SHOWN_SHARE, CLOSEST = 0.2, 3


def find(query: str, limit: int = 10) -> dict[str, Any]:
    """`opsquire find`: the general scripts to reuse that score at least SHOWN_SHARE of the best match of either
    kind or that one of the CLOSEST past tasks uses, each with how to call it (see how_to_call), and the past one-off
    tasks and plans like the query, each with its track record; plus a hint when several past tasks share a shape
    that no general script covers (see missing_general). The words come from the search index (SearchIndex), so a
    repeated search reads only what changed."""
    index = SearchIndex()
    reuse, past = rank(query, index)
    best = max([s.score for s in reuse[:1] + past[:1]], default=0.0)
    closest = {u for s in past[:CLOSEST] for u in index.entries[s.folder]["uses"] or []}
    shown = [s for s in reuse if s.score >= SHOWN_SHARE * best or index.entries[s.folder]["id"] in closest]
    users = users_of(index)
    hits = []
    for s, task in settle(index, shown, limit):
        hits.append(summarize(task) | {"score": s.score, "own": s.own, "votes": s.votes, "matched": s.matched})
        hits[-1] |= {"voters": [v for _, v in s.voters[:3]], "track": s.track} | how_to_call(index, task, users)
    history = []
    for s, task in settle(index, past, limit):
        summary = summarize(task)
        uses = [str(u) for u in task.raw.get("uses") or []]
        extra = {"uses": uses, "track": s.track, "last_failure": last_failure(summary)}
        history.append(summary | extra | {"score": s.score, "matched": s.matched})
    return {"reuse": hits, "history": history, "hint": missing_general(index, reuse, past)}


def how_to_call(index: SearchIndex, general: Task, users_by_general: dict[str, list[str]]) -> dict[str, Any]:
    """How a general script is used: its arguments (script_usage), its API doc, how many tasks use it, the latest ok
    apply or read of one of them (an example to copy, with the arguments it ran with), their typical duration and
    their latest failure."""
    entries = index.entries
    users = users_by_general.get(general.id, [])
    runs = sorted(
        (
            dict(zip((*RUN_FIELDS, "duration_s", "argv"), r, strict=True)) | {"task": entries[f]["id"]}
            for f in users
            for r in entries[f]["runs"]
        ),
        key=lambda r: str(r["started"]),
    )
    ok = [r for r in runs if r["status"] == "ok"]
    failed = [r for r in runs if r["status"] not in ("ok", "running")]
    durations = [float(r["duration_s"]) for r in ok if isinstance(r["duration_s"], int | float)]
    doc = home_dir() / "docs" / f"{general.id}.md"
    example = {k: ok[-1][k] for k in ("task", "run_id", "mode", "started", "argv", "duration_s")} if ok else None
    failure = {k: failed[-1][k] for k in ("task", "run_id", "mode", "started", "last_error")} if failed else None
    return {
        "usage": str(entries[str(general.folder)]["usage"]) if str(general.folder) in entries else "",
        "doc": str(doc) if doc.is_file() else None,
        "users": len(users),
        "example": example,
        "typical_s": round(statistics.median(durations), 1) if durations else None,
        "last_failure": failure,
    }


STRONG_SHARE, SHAPE_TASKS = 0.5, 3
HELPER_SHARE = 1 / 3


def broad_helpers(index: SearchIndex) -> set[str]:
    """General scripts so widely used that naming one says little about a task: used by more than HELPER_SHARE of the
    tasks that use any general script."""
    users = users_of(index)
    total = sum(1 for e in index.entries.values() if e["kind"] != "general" and e["uses"])
    return {g for g, u in users.items() if len(u) > HELPER_SHARE * total}


def name_words(task_id: str) -> dict[str, str]:
    """The words of a task's name (its id without date and area), numbers left out, by stem."""
    name = task_id.split("_", 2)[-1]
    return {stem(w): w for w in search_words(re.sub(r"\d+", " ", name.replace("-", " ")))}


def missing_general(index: SearchIndex, reuse: list[Scored], past: list[Scored]) -> dict[str, Any] | None:
    """A nudge to make a general script, or None. It is given when no general script scores STRONG_SHARE of the best
    past task, and SHAPE_TASKS or more past one-off tasks that do score that much and use no general script (but
    broad helpers) share two words of their names (words that name an area or target left out) and were made on as
    many different days: the same job done again, not one job's steps."""
    if not past or (reuse and reuse[0].score >= STRONG_SHARE * past[0].score):
        return None
    entries, helpers = index.entries, broad_helpers(index)
    strong = [
        entries[s.folder]
        for s in past
        if s.score >= STRONG_SHARE * past[0].score
        and entries[s.folder]["kind"] == "task"
        and not set(entries[s.folder]["uses"] or []) - helpers
    ]
    places = {stem(w) for e in entries.values() for w in e["places"]}
    words = {str(e["id"]): {k: w for k, w in name_words(e["id"]).items() if k not in places} for e in strong}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in strong:
        for pair in itertools.combinations(sorted(words[e["id"]]), 2):
            groups.setdefault(pair, []).append(e)
    shapes = [
        (pair, group) for pair, group in groups.items() if len({str(e["created"])[:10] for e in group}) >= SHAPE_TASKS
    ]
    if not shapes:
        return None
    pair, group = min(shapes, key=lambda pg: (-len(pg[1]), pg[0]))
    tasks = [str(e["id"]) for e in group]
    shape = [words[tasks[0]][w] for w in pair]
    shown = ", ".join(tasks[:4]) + (f" and {len(tasks) - 4} more" if len(tasks) > 4 else "")
    text = (
        f"no general script fits, but {len(tasks)} past tasks made on different days share '{' '.join(shape)}': "
        f"{shown}. Consider moving their logic into a general script with arguments (opsquire new ... --general)."
    )
    return {"tasks": tasks, "shape": shape, "text": text}


REUSE_SCORE, REUSE_SHARE = 6.0, 0.6


def reuse_candidates(area: str, name: str, purpose: str, skip: list[str], limit: int = 3) -> list[dict[str, Any]]:
    """General scripts that strongly match a new task's area, name and purpose: every word of the name matches the
    script itself, the score is high, and close to the best general script's. A hint for the author; it never blocks
    anything."""
    words = name.replace("-", " ")
    index = SearchIndex()
    reuse, _ = rank(f"{area} {words} {purpose}", index)
    entries = index.entries
    hits = [s for s in reuse if entries[s.folder]["id"] not in skip and not entries[s.folder]["offboarded"]]
    names = set(search_words(words))
    floor = max(REUSE_SCORE, REUSE_SHARE * hits[0].score) if hits else REUSE_SCORE
    strong = [s for s in hits if s.score >= floor and names <= {w for m in s.matched for w in m.split()}]
    return [{"id": entries[s.folder]["id"], "purpose": entries[s.folder]["purpose"], "score": s.score} for s in strong][
        :limit
    ]


EVAL_WORDS, EVAL_KS, EVAL_MISS = 12, (1, 3, 5, 10), 5


def evaluate() -> dict[str, Any]:
    """How well find's reuse ranking serves this home's own tasks: each task that names a general script in its
    uses (broad helpers left out, see broad_helpers) is a case; the query is the first EVAL_WORDS words of its
    purpose, the task itself is left out, and the case's rank is that of the first general script it uses. Gives
    recall at EVAL_KS, the mean reciprocal rank, and the general scripts most often ranked below EVAL_MISS."""
    index = SearchIndex()
    entries = sorted(index.entries.values(), key=lambda e: str(e["id"]))
    generals = {e["id"] for e in entries if e["kind"] == "general"}
    helpers = broad_helpers(index)
    cases: list[dict[str, Any]] = []
    for e in entries:
        answers = set(e["uses"] or []) & generals - helpers
        words = str(e["purpose"]).split()
        if e["kind"] != "task" or not answers or not words:
            continue
        query = " ".join(words[:EVAL_WORDS])
        reuse, _ = rank(query, index, exclude=e["id"])
        ids = [index.entries[s.folder]["id"] for s in reuse]
        found = next((i + 1 for i, g in enumerate(ids) if g in answers), None)
        cases.append({"task": e["id"], "query": query, "answers": sorted(answers), "rank": found})
    n = len(cases)
    ranks = [c["rank"] for c in cases if c["rank"]]
    missed = [c for c in cases if not c["rank"] or c["rank"] > EVAL_MISS]
    worst = collections.Counter(a for c in missed for a in c["answers"])
    per_general = collections.Counter(a for c in cases for a in c["answers"])
    return {
        "cases": n,
        "helpers_left_out": sorted(helpers),
        "recall": {f"@{k}": round(sum(1 for r in ranks if r <= k) / n, 2) if n else None for k in EVAL_KS},
        "mrr": round(sum(1 / r for r in ranks) / n, 3) if n else None,
        "worst": [
            {
                "general": g,
                "missed": m,
                "cases": per_general[g],
                "example": next(c for c in missed if g in c["answers"]),
            }
            for g, m in sorted(worst.items(), key=lambda gm: (-gm[1], gm[0]))[:5]
        ],
        "cases_detail": cases,
    }


VANISHED_GRACE = 2 * HEARTBEAT_EVERY + 1


def wait_for_run(
    task_id: str,
    since: str | None,
    timeout: float,
    progress: Callable[[str], None],
    decided: Callable[[], str | None] = lambda: None,
) -> dict[str, Any] | None:
    """The next finished run of the task, None on timeout, or {"decided": why} when the task was abandoned or
    superseded while no run of it was going."""
    existing = runs_for(task_id)
    baseline = since or (str(existing[-1]["started"]) if existing else "")
    progress(f"waiting for a run of {task_id} started after {baseline or 'now'} (Ctrl-C to stop)")
    deadline = time.monotonic() + timeout
    announced = asked = ""
    lost = 0.0
    while True:
        fresh = [r for r in runs_for(task_id) if str(r.get("started")) > baseline]
        if fresh:
            rec = fresh[-1]
            gone = rec.get("last_error") == VANISHED
            lost = (lost or time.monotonic()) if gone else 0.0
            if rec.get("status") != "running" and not (gone and time.monotonic() - lost < VANISHED_GRACE):
                return rec
            if rec["run_id"] != announced:
                announced = str(rec["run_id"])
                progress(f"run {announced} started, waiting for it to finish")
            question = str((rec.get("waiting") or {}).get("question") or "")
            if question != asked:
                asked = question
                if question:
                    progress(f"run {announced} is waiting for the operator to answer: {question}")
        else:
            why = decided()
            if why:
                return {"decided": why}
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)
