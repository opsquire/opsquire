"""Where lint runs the tests agents write: never with the operator's environment, home, credentials or network.

Every test run gets a scratch directory holding copies of the files under test, a fresh empty HOME and TMPDIR, and
an environment built from a short allowlist, so no token, key, agent socket or credentials path reaches it. On top
of that it runs at the strongest level this machine allows, probed once per process:

- `bwrap`: a bubblewrap sandbox that sees only the system directories, the interpreter and the scratch directory
  (read-write), with no network, its own pid namespace and its own session, and the credentials file masked.
- `sandbox-exec` (macOS): a deny-by-default profile that reads only the system directories, the interpreter and the
  scratch directory, writes only the scratch directory and has no network. It keeps files out as `bwrap` does.
- `netns`: `unshare -rn`, no network; files outside the scratch directory stay readable by absolute path.
- `env-only`: the scrubbed environment and the copies alone.

Only `bwrap` and `sandbox-exec` keep an agent's test from reading the operator's files, so lint runs tests at a
weaker level only when the operator runs opsquire with `OPSQUIRE_TEST_ISOLATION=weaker-ok` in their own shell's
environment (no file an agent could write), or when the caller itself is withheld the credentials file (an agent's
sandbox masks or denies it): the test then gets no more than its caller already has. Otherwise it refuses to run
them.
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import site
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .model import HELPERS_DIR, creds_path

BWRAP = "bwrap"
SANDBOX_EXEC = "sandbox-exec"
STRONG = (BWRAP, SANDBOX_EXEC)
NETNS = "netns"
ENV_ONLY = "env-only"
ENV_KEY = "OPSQUIRE_TEST_ISOLATION"
WEAKER_OK = "weaker-ok"

ENV_KEEP = ("PATH", "LANG", "LANGUAGE", "TERM", "TZ")
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc")
PROBE_TIMEOUT = 10


def scrubbed_env(scratch: Path, **extra: str) -> dict[str, str]:
    """The allowlisted variables of this process, HOME and TMPDIR inside `scratch`, and `extra`. PYTHONUSERBASE keeps
    naming the operator's user packages, where a `pip install --user` mypy lives, since HOME no longer finds them."""
    home, tmp = scratch / "home", scratch / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP or k.startswith("LC_")}
    base = {"HOME": str(home), "TMPDIR": str(tmp), "PYTHONUSERBASE": site.getuserbase(), "PYTHONDONTWRITEBYTECODE": "1"}
    return env | base | extra


REFUSED: dict[str, str] = {}


def _works(cmd: list[str]) -> bool:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        REFUSED[cmd[0]] = str(e)
        return False
    if out.returncode != 0:
        lines = [ln.strip() for ln in (out.stderr or out.stdout).splitlines() if ln.strip()]
        REFUSED[cmd[0]] = lines[-1][:200] if lines else f"exit {out.returncode}"
    return out.returncode == 0


def why_not_bwrap() -> str:
    """Why the bwrap probe was refused here, empty when it was not tried or worked."""
    return REFUSED.get("bwrap", "")


def _masked() -> list[Path]:
    return [Path.home().resolve(), creds_path().resolve()]


def _exposes_masked(path: Path) -> bool:
    return any(m == path or m.is_relative_to(path) for m in _masked())


def link_chain(path: Path) -> list[Path]:
    """The path and every symlink hop it goes through, unresolved: a venv's python often points at a version
    folder that is itself a symlink, and the sandbox must have each hop at its own path for the exec to work."""
    hops = [path]
    seen: set[str] = set()
    while hops[-1].is_symlink() and str(hops[-1]) not in seen and len(hops) < 40:
        seen.add(str(hops[-1]))
        target = Path(os.readlink(hops[-1]))
        hops.append(target if target.is_absolute() else hops[-1].parent / target)
    for hop in list(hops):
        for parent in hop.parents:
            if parent.is_symlink():
                hops.append(parent)
    return hops


def prefixes(exe: Path) -> set[Path]:
    """The installation folders an interpreter at `exe` runs from, read from the files alone: the folder above each
    `bin/` it passes through, and for a virtual environment the base installation `pyvenv.cfg` names."""
    out: set[Path] = set()
    for hop in [*link_chain(exe), exe.resolve()]:
        if hop.parent.name in ("bin", "Scripts"):
            out.add(hop.parent.parent)
    for prefix in list(out):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            for line in (prefix / "pyvenv.cfg").read_text().splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "home" and os.path.isabs(value.strip()):
                    base = Path(value.strip())
                    out.add(base.parent if base.name in ("bin", "Scripts") else base)
    return out


def pth_dirs(prefix: Path) -> set[str]:
    """Absolute folders the `.pth` files of a prefix's site-packages add to sys.path (editable installs)."""
    out: set[str] = set()
    for pth in prefix.glob("lib/python3*/site-packages/*.pth"):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            for line in pth.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "import ", "import\t")) and os.path.isabs(line):
                    out.add(line)
    return out


@functools.cache
def python_dirs(python: str) -> tuple[str, ...]:
    """The directories `python` loads its standard library and installed packages from, which a bwrap sandbox binds
    read-only. Found from the files, never by running `python`: a task names its interpreter, and running it here,
    outside any sandbox, would run the task's choice of program with the operator's access. A directory holding the
    operator's home or the credentials file is never one of them."""
    exe = Path(shutil.which(python) or python)
    dirs = {str(exe.resolve().parent), *(str(hop.parent) for hop in link_chain(exe))}
    for prefix in prefixes(exe):
        dirs.add(str(prefix.resolve()))
        dirs.update(str(Path(d).resolve()) for d in pth_dirs(prefix))
    keep = [d for d in dirs if Path(d).is_dir() and d != "/" and not _exposes_masked(Path(d))]
    return tuple(sorted(d for d in keep if not any(o != d and Path(d).is_relative_to(o) for o in keep)))


def bwrap_prefix(writable: Path, python: str, cwd: Path) -> list[str]:
    cmd = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    bound: list[Path] = []
    for d in SYSTEM_DIRS:
        p = Path(d)
        if p.is_symlink():
            cmd += ["--symlink", os.readlink(p), d]
        elif p.is_dir():
            cmd += ["--ro-bind", d, d]
            bound.append(p.resolve())
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for d in python_dirs(python):
        cmd += ["--ro-bind", d, d]
        bound.append(Path(d))
    cmd += ["--bind", str(writable), str(writable)]
    for m in _masked():
        if m.exists() and any(m.is_relative_to(b) for b in bound):
            cmd += ["--tmpfs", str(m)] if m.is_dir() else ["--ro-bind", "/dev/null", str(m)]
    return [*cmd, "--chdir", str(cwd), "--"]


def sbpl(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(writable: Path, python: str) -> str:
    """The macOS sandbox profile: deny everything, then read the system, the interpreter and the scratch directory,
    write the scratch directory alone; the credentials file is denied by name last, so no allowed folder exposes it."""
    reads = [str(Path(d).resolve()) for d in (*SYSTEM_DIRS, "/System", "/private/etc") if Path(d).is_dir()]
    reads += [*python_dirs(python), str(writable.resolve())]
    devices = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/dtracehelper")
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process-fork process-exec signal sysctl-read file-read-metadata)",
            "(allow file-read* " + " ".join(f"(subpath {sbpl(d)})" for d in sorted(set(reads))) + ")",
            "(allow file-read* file-write* " + " ".join(f"(literal {sbpl(d)})" for d in devices) + ")",
            f"(allow file-write* (subpath {sbpl(str(writable.resolve()))}))",
            f"(deny file-read* (literal {sbpl(str(creds_path().resolve()))}))",
        ]
    )


@functools.cache
def level(python: str) -> str:
    """The strongest isolation that works here for tests run by `python`."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        with tempfile.TemporaryDirectory(prefix="opsquire-probe-") as scratch:
            if _works(["sandbox-exec", "-p", sandbox_profile(Path(scratch), python), python, "-I", "-c", "pass"]):
                return SANDBOX_EXEC
    if shutil.which("bwrap"):
        with tempfile.TemporaryDirectory(prefix="opsquire-probe-") as scratch:
            if _works([*bwrap_prefix(Path(scratch), python, Path(scratch)), python, "-I", "-c", "pass"]):
                return BWRAP
    if shutil.which("unshare") and _works(["unshare", "-rn", "--", "true"]):
        return NETNS
    return ENV_ONLY


def weaker_allowed() -> bool:
    """Whether the operator accepted, in the environment of the shell running opsquire, running tests at `netns` or
    `env-only` where bubblewrap does not work. Any value but "weaker-ok" means no."""
    return os.environ.get(ENV_KEY) == WEAKER_OK


def credentials_withheld() -> bool:
    """Whether this process is kept from the credentials file: it exists but cannot be opened, or something other than
    a regular file is mounted over it. A missing file proves nothing, so it counts as not withheld."""
    path = creds_path()
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return True
        with path.open("rb") as f:
            f.read(1)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def weaker_ok_here() -> bool:
    return weaker_allowed() or credentials_withheld()


def why_no_bwrap() -> str:
    if sys.platform == "darwin":
        return f"sandbox-exec was refused here ({REFUSED.get('sandbox-exec') or 'it is not installed'})"
    if not shutil.which("bwrap"):
        return "bubblewrap is not installed"
    return f"bubblewrap was refused here ({why_not_bwrap() or 'its probe failed'})"


def exposure(chosen: str) -> str:
    return "files outside the copy stay readable" + (" and the network is open" if chosen == ENV_ONLY else "")


def opt_in(chosen: str) -> str:
    return f"the operator alone may accept {chosen} by running opsquire with {ENV_KEY}={WEAKER_OK} in their own shell"


def refusal(chosen: str) -> str:
    """Why lint ran no tests: the strongest level here keeps files readable and the operator did not accept it."""
    return (
        f"tests NOT run: {why_no_bwrap()}, and the strongest isolation here, {chosen}, leaves {exposure(chosen)}. "
        f"Make {'sandbox-exec' if sys.platform == 'darwin' else 'bubblewrap'} work, or {opt_in(chosen)}"
    )


class Sandbox:
    """A scratch directory with copies of `files` (laid out as under `root`) and of the helpers, removed on exit,
    Ctrl-C included. Symlinks are copied as links, never followed."""

    def __init__(self, files: list[Path], root: Path, python: str) -> None:
        self.files, self.root, self.python = files, root, python
        self.dir = Path()

    def __enter__(self) -> Sandbox:
        self.dir = Path(tempfile.mkdtemp(prefix="opsquire-tests-"))
        try:
            for f in self.files:
                dst = self.copy_of(f)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst, follow_symlinks=False)
            shutil.copytree(HELPERS_DIR, self.helpers, ignore=shutil.ignore_patterns("__pycache__"))
        except BaseException:
            shutil.rmtree(self.dir, ignore_errors=True)
            raise
        return self

    def __exit__(self, *_: object) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    @property
    def src(self) -> Path:
        return self.dir / "src"

    @property
    def helpers(self) -> Path:
        return self.dir / "helpers"

    def copy_of(self, real: Path) -> Path:
        rel = real.relative_to(self.root) if real.is_relative_to(self.root) else Path("_abs", *real.parts[1:])
        return self.src / rel

    def level(self) -> str:
        return level(self.python)

    def env(self, **extra: str) -> dict[str, str]:
        return scrubbed_env(self.dir, OPSQUIRE_HELPERS=str(self.helpers), **extra)

    def command(self, cmd: list[str], cwd: Path) -> list[str]:
        chosen = self.level()
        if chosen == BWRAP:
            return [*bwrap_prefix(self.dir, self.python, cwd), *cmd]
        if chosen == SANDBOX_EXEC:
            return ["sandbox-exec", "-p", sandbox_profile(self.dir, self.python), *cmd]
        if chosen == NETNS:
            return ["unshare", "-rn", "--", *cmd]
        return cmd

    def real_paths(self, text: str) -> str:
        return text.replace(str(self.src), str(self.root))
