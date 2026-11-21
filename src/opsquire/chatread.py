"""When the operator last read each ask conversation in the console: `var/chats-read/<user>.json`, one file per
account, so every console tab of one operator agrees on which chats have news and operators sharing the home keep
their own. A conversation is unread when one of its asks ended after the operator last opened it. The file starts on
the console's first start (`since`): chats that ended before it count as read, so a home does not light up with its
whole history. The home's one file from before, `var/chats-read.json`, is each account's starting point until it has
its own."""

from __future__ import annotations

import contextlib
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .followup import conversation_of
from .model import OpsquireError, iso, now, read_json, user_name, var_dir, write_json

LOCK = threading.Lock()


def path() -> Path:
    return var_dir() / "chats-read" / f"{user_name()}.json"


def legacy_path() -> Path:
    return var_dir() / "chats-read.json"


def opened(rec: dict[str, Any]) -> dict[str, Any]:
    found = rec.get("read")
    return found if isinstance(found, dict) else {}


def load() -> dict[str, Any]:
    try:
        rec = read_json(path() if path().is_file() else legacy_path())
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def unread(asks: Iterable[dict[str, Any]], read: dict[str, Any]) -> list[str]:
    since = str(read.get("since") or "")
    if not since:
        return []
    seen = opened(read)
    last: dict[str, str] = {}
    for a in asks:
        conv, ended = conversation_of(a), str(a.get("ended") or "")
        last[conv] = max(last.get(conv, ""), ended)
    return sorted(conv for conv, ended in last.items() if ended > str(seen.get(conv) or since))


def start() -> None:
    with LOCK, contextlib.suppress(OSError):
        if not load().get("since"):
            path().parent.mkdir(parents=True, exist_ok=True)
            write_json(path(), {"since": iso(now()), "read": {}})


def mark(conv: str, asks: Iterable[dict[str, Any]]) -> None:
    """Mark `conv` read now; conversations whose asks are gone leave the file."""
    if not re.fullmatch(r"[0-9A-Za-z_.-]+", conv):
        raise OpsquireError("bad conversation id")
    known = {conversation_of(a) for a in asks}
    if conv not in known:
        raise OpsquireError(f"no conversation {conv}")
    with LOCK:
        rec = load()
        kept = {c: at for c, at in opened(rec).items() if c in known}
        stamp = iso(now())
        path().parent.mkdir(parents=True, exist_ok=True)
        write_json(path(), {"since": rec.get("since") or stamp, "read": {**kept, conv: stamp}})
