"""The code a run executes, where to read it, and what changed since the task's last run. Before a script starts,
`opsquire run` lists every file it runs (the task's own first, then the general scripts it brings) with a link that
opens the run's exact copy in the operator's editor; the console lists the same files of the task as they are now.
The Markdown and the unit tests are left out: they do not run."""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any

from .handover import code_files
from .model import OpsquireError, Task, config_path, home_dir, read_json, update_config

EDITOR_KEY = "editor_url"
DIFF_CACHE: dict[tuple[str, str], tuple[int, int]] = {}


def runs_code(name: str) -> bool:
    return not Path(name).name.startswith("test_")


def default_editor_url() -> str:
    """VS Code, reaching into this WSL distribution when there is one."""
    distro = os.environ.get("WSL_DISTRO_NAME")
    return f"vscode://vscode-remote/wsl+{distro}{{path}}" if distro else "vscode://file{path}"


def editor_template() -> str:
    try:
        value = read_json(config_path()).get(EDITOR_KEY)
    except (OSError, ValueError, AttributeError):
        value = None
    return value if isinstance(value, str) and "{path}" in value else default_editor_url()


def set_editor_template(text: Any) -> None:
    """Set the operator's editor link (a URL with {path}), or with an empty string go back to the default."""
    if not isinstance(text, str) or (text.strip() and "{path}" not in text):
        raise OpsquireError("the editor link is a URL with {path} in it, e.g. vscode://file{path}")
    update_config(**{EDITOR_KEY: text.strip() or False})


def editor_url(path: Path, template: str | None = None) -> str:
    return (template or editor_template()).replace("{path}", str(path))


def counts(old: Path, new: Path) -> tuple[int, int]:
    """Lines added and removed from `old` to `new`, cached by the two files' sizes and times."""
    try:
        key = (
            f"{old}:{old.stat().st_size}:{old.stat().st_mtime_ns}",
            f"{new}:{new.stat().st_size}:{new.stat().st_mtime_ns}",
        )
        if key not in DIFF_CACHE:
            a = old.read_text(errors="replace").splitlines()
            b = new.read_text(errors="replace").splitlines()
            diff = list(difflib.unified_diff(a, b, lineterm="", n=0))
            plus = len([x for x in diff if x.startswith("+") and not x.startswith("+++")])
            minus = len([x for x in diff if x.startswith("-") and not x.startswith("---")])
            DIFF_CACHE[key] = (plus, minus)
        return DIFF_CACHE[key]
    except OSError:
        return (0, 0)


def listing(task: Task, files: dict[str, str], root: Path, before: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Each file that runs (`files`: home-relative name to digest, read under `root`) against the run `before`: new,
    changed (with lines added and removed) or same. The task's own files come first, then the general scripts."""
    template = editor_template()
    old = code_files((before or {}).get("files") or {})
    snap = Path(str(((before or {}).get("paths") or {}).get("snapshot") or ""))
    own = f"{task.folder.relative_to(home_dir()).as_posix()}/"
    out = []
    for name, digest in sorted(code_files(files).items(), key=lambda kv: (not kv[0].startswith(own), kv[0])):
        if not runs_code(name):
            continue
        state = "new" if before is None or name not in old else "same" if old[name] == digest else "changed"
        plus, minus = counts(snap / name, root / name) if state == "changed" and snap.parts else (0, 0)
        out.append(
            {
                "name": name,
                "short": name[len(own) :] if name.startswith(own) else name,
                "state": state,
                "added": plus,
                "removed": minus,
                "path": str(root / name),
                "url": editor_url(root / name, template),
            }
        )
    return out


def last_run(runs: list[dict[str, Any]], but: str = "") -> dict[str, Any] | None:
    ran = [r for r in runs if r.get("status") != "blocked" and r.get("run_id") != but]
    return ran[-1] if ran else None


def link(text: str, url: str, stream: Any) -> str:
    """`text` as a terminal hyperlink (OSC 8) when the stream is a terminal; most open it on Ctrl+click."""
    if not getattr(stream, "isatty", lambda: False)() or os.environ.get("TERM", "dumb") == "dumb":
        return text
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def terminal_lines(rows: list[dict[str, Any]], before: dict[str, Any] | None, root: Path, stream: Any) -> list[str]:
    """The listing as `opsquire run` prints it before the script starts."""
    since = f"since its last run, {before.get('run_id')}" if before else "its first run"
    lines = [f"code this run executes ({since}):"]
    states = [f"changed +{r['added']} -{r['removed']}" if r["state"] == "changed" else r["state"] for r in rows]
    width = max((len(x) for x in states), default=0)
    for r, state in zip(rows, states, strict=True):
        lines.append(f"  {state:<{width}}  {link(r['short'], r['url'], stream)}")
    lines.append(f"  exact copies: {root}")
    return lines
