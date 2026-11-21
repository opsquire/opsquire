"""The boundary an agent's own sandbox must keep: run records and locks are written only by `opsquire run` in the
operator's terminal, and the credentials file is never read by an agent. Opsquire sets it for the Claude Code agents
`opsquire ask` starts (`--settings` in retinue.json); any other agent's sandbox must keep it, so opsquire states it and
checks it: an agent runs `opsquire doctor --boundary` from inside its sandbox, and every refused access is a pass.
Nothing is printed from any file it manages to read."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .model import creds_path, var_dir


def can_write(folder: Path) -> bool:
    try:
        fd, name = tempfile.mkstemp(prefix=".boundary-probe-", dir=folder)
    except OSError:
        return False
    os.close(fd)
    Path(name).unlink(missing_ok=True)
    return True


def can_read(path: Path) -> bool:
    """True only when the file opens and gives bytes: a sandbox that masks it with an empty file keeps it."""
    try:
        with path.open("rb") as f:
            return bool(f.read(1))
    except OSError:
        return False


def probe() -> tuple[bool, list[str]]:
    """var/ is the main home's: a parallel piece's home is its worktree, but its records and locks are not there."""
    var = var_dir()
    checks: list[tuple[str, bool]] = []
    for folder in (var / "runs", var / "locks", var):
        if folder.is_dir():
            checks.append((f"write {folder}", can_write(folder)))
    creds = creds_path()
    checks.append((f"read {creds}", can_read(creds)))
    if creds.parent.is_dir():
        checks.append((f"write next to {creds}", can_write(creds.parent)))
    lines = [f"{'EXPOSED' if open_ else 'kept   '} {what}" for what, open_ in checks]
    ok = not any(open_ for _, open_ in checks)
    lines.append(
        "boundary kept: this sandbox cannot forge run records or read credentials"
        if ok
        else "boundary NOT kept: configure the agent's sandbox to deny these, then run this again"
    )
    return ok, lines
