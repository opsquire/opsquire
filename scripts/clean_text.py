"""Words that never belong in this repository: names of companies, people and machines, and the jargon of the field
opsquire was first used in, so that it stays a generic tool. The list itself is not in the repository: it is read from
OPSQUIRE_BANNED_WORDS (one word per line or comma; CI sets it from a secret) or else from .private/banned-words.txt
in the checkout, which git ignores. Where neither is there, nothing is checked.

Three places use it: the test suite checks every file in the repository, signed_commit.py refuses a commit whose
message or files hold one, and the text guard workflow checks issues, comments, pull requests and commit messages:

    python3 scripts/clean_text.py EVENT_JSON [BASE..HEAD]"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

LIST_ENV = "OPSQUIRE_BANNED_WORDS"
LIST_FILE = Path(__file__).resolve().parent.parent / ".private" / "banned-words.txt"


@functools.cache
def words() -> tuple[str, ...]:
    """The listed words, lowercased: from the environment, else the private file; none when neither is there."""
    raw = os.environ.get(LIST_ENV) or (LIST_FILE.read_text() if LIST_FILE.is_file() else "")
    return tuple(sorted({w.strip().lower() for w in re.split(r"[\n,]", raw) if w.strip()}))


def hits(text: str) -> list[str]:
    """The listed words in the text, each once."""
    if not words():
        return []
    pattern = re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, words())) + r")(?![a-z0-9])")
    return sorted(set(pattern.findall(text.lower())))


def event_texts(event: dict[str, Any]) -> list[tuple[str, str]]:
    """(where, text) for the words a GitHub event carries: an issue's or pull request's title and body, a comment, and
    the messages of pushed commits."""
    out = []
    for kind in ("issue", "pull_request"):
        item = event.get(kind) or {}
        if item:
            out += [(f"{kind} #{item.get('number')} {part}", str(item.get(part) or "")) for part in ("title", "body")]
    comment = event.get("comment") or {}
    if comment:
        out.append((f"comment {comment.get('html_url')}", str(comment.get("body") or "")))
    out += [(f"commit {str(c.get('id'))[:7]}", str(c.get("message") or "")) for c in event.get("commits") or []]
    return out


def commit_texts(commits: str) -> list[tuple[str, str]]:
    """(where, text) for the message of every commit in a git range such as BASE..HEAD."""
    log = subprocess.run(["git", "log", "--format=%h%x00%B%x01", commits], capture_output=True, text=True, check=True)
    entries = [e.strip("\n").split("\0", 1) for e in log.stdout.split("\x01") if e.strip()]
    return [(f"commit {sha}", message) for sha, message in entries]


def main(argv: list[str]) -> int:
    if not words():
        print(f"no word list ({LIST_ENV} or {LIST_FILE.name}): nothing checked")
        return 0
    texts = event_texts(json.loads(Path(argv[0]).read_text()))
    if len(argv) > 1 and argv[1]:
        texts += commit_texts(argv[1])
    found = [(where, hits(text)) for where, text in texts]
    bad = [f"{where}: {', '.join(listed)}" for where, listed in found if listed]
    for line in bad:
        print(f"does not belong in this repository: {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
