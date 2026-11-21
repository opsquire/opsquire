"""What an ask agent may use: the home's tools.json, decided per tool as allow, confirm or off.

An agent's tool is a server tool (named by the agent's retinue `tools.name_re`, which yields the server and the tool),
a shell command (the retinue's `tools.shell`, its command in the input) or another of the agent's own tools. Rules
are glob patterns; the longest matching pattern wins, then the server's default, then the home's default. A shell
command is split into its simple commands (at ; && || | & and newlines) and each is judged on its own words: any one
switched off denies the whole command, and it is allowed only when every one is. A command the rules cannot fully
judge (substitutions, variables, redirections other than to /dev/null or between stdout and stderr, here-documents,
subshells and groups, brace expansion, unbalanced quotes, eval, exec, source, a shell's -c) runs without the
operator only when the shell mode accepts the whole line (outward: stays_local; reads: read_only); confirm mode always
asks. Switched-off rules still apply to every simple command found in it, substitutions included. A server is
off until it is switched on here, so a newly installed one does nothing for ask agents until the operator enables it.
Servers added here are given to ask agents only, never written into the agent's own configuration.

tools.json:
  {"default": "confirm",
   "shell_mode": "outward",
   "servers": {"<server id>": {"enabled": true, "default": "confirm", "tools": {"read_*": "allow"}}},
   "shell": {"gh pr view *": "allow"},
   "agent_tools": {"WebFetch": "allow"},
   "added": {"<name>": {"url": "https://..."} or {"command": "...", "args": [...], "env": {...}}}}
"""

from __future__ import annotations

import contextlib
import fnmatch
import re
import subprocess
from typing import Any

from .model import OpsquireError, home_dir, iso, now, read_json, var_dir, write_json

FILE = "tools.json"
SEEN = "tools-seen.json"
CHOICES = ("allow", "confirm", "off")
OWN_SERVER = "opsquire"


def load() -> dict[str, Any]:
    path = home_dir() / FILE
    if not path.is_file():
        return {}
    cfg = read_json(path)
    if not isinstance(cfg, dict):
        raise OpsquireError(f"{path} must hold a JSON object")
    return cfg


def save(cfg: dict[str, Any]) -> None:
    write_json(home_dir() / FILE, cfg)


def _choice(value: Any, fallback: str) -> str:
    return value if value in CHOICES else fallback


def _best(rules: dict[str, Any], subject: str) -> str | None:
    hits = [p for p in rules if fnmatch.fnmatchcase(subject, p)]
    return _choice(rules[max(hits, key=len)], "confirm") if hits else None


RESERVED = frozenset(
    "! [[ ]] { } case do done elif else esac fi for function if in select then time until while".split()
)
RUNS_TEXT = frozenset(("eval", "exec", "source", "."))
SHELLS = frozenset(("sh", "bash", "dash", "zsh", "ksh", "mksh", "fish"))
REDIRECTS = ("&>>", "&>", "<<<", "<<-", "<<", ">>", ">&", "<&", "<>", ">|", ">", "<")
HARMLESS_REDIRECTS = frozenset(
    ((">", "/dev/null"), (">>", "/dev/null"), ("&>", "/dev/null"), ("&>>", "/dev/null"), (">&", "1"), (">&", "2"))
)
BRACE_EXPANSION = re.compile(r"\{[^{}]*(,|\.\.)[^{}]*\}")
HARMLESS = frozenset(("cd", "pwd", "true", ":"))
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


class ShellScan:
    """A conservative reading of one shell command: its simple commands as words (`commands`), the simple commands
    inside its substitutions (`inner`), and `safe`, false whenever anything could run or expand beyond those words."""

    def __init__(self, text: str) -> None:
        self.text, self.i = text, 0
        self.commands: list[list[str]] = []
        self.inner: list[list[str]] = []
        self.words: list[str] = []
        self.word: str | None = None
        self.unquoted = ""
        self.heredocs: list[str] = []
        self.safe = True
        self.writes = self.broken = self.branching = False
        self._run()

    def _run(self) -> None:
        s = self.text
        while self.i < len(s):
            c = s[self.i]
            if c in " \t":
                self._end_word()
                self.i += 1
            elif c == "&" and s.startswith("&>", self.i):
                self._redirect()
            elif c in "\n;&|":
                self._end_command()
                if c == "\n":
                    self.i += 1
                    self._skip_heredocs()
                else:
                    while self.i < len(s) and s[self.i] in ";&|":
                        self.i += 1
            elif c in "()":
                self.safe = False
                self._end_command()
                self.i += 1
            elif c == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    self._fail()
                else:
                    self._add(s[self.i + 1 : end], quoted=True)
                    self.i = end + 1
            elif c == '"':
                self._double()
            elif c == "\\":
                self._escape(quoted=False)
            elif c == "$":
                self._dollar(quoted=False)
            elif c == "`":
                self._backtick(quoted=False)
            elif c in "<>":
                self._redirect()
            elif c == "#" and self.word is None:
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end
            else:
                self._add(c, quoted=False)
                self.i += 1
        self._end_command()

    def _fail(self) -> None:
        self.safe = False
        self.broken = True
        self.i = len(self.text)

    def _add(self, text: str, quoted: bool) -> None:
        self.word = (self.word or "") + text
        if not quoted:
            self.unquoted += text

    def _end_word(self) -> None:
        if self.word is None:
            return
        if BRACE_EXPANSION.search(self.unquoted):
            self.safe = False
        self.words.append(self.word)
        self.word, self.unquoted = None, ""

    def _end_command(self) -> None:
        self._end_word()
        words, self.words = self.words, []
        while words and words[0] in RESERVED:
            self.safe = False
            if words[0] in ("for", "select"):
                return
            self.branching |= words[0] in ("case", "function")
            words.pop(0)
        if not words:
            return
        name = words[0].rsplit("/", 1)[-1]
        if name in RUNS_TEXT or (
            name in SHELLS and any(w.startswith("-") and not w.startswith("--") and "c" in w for w in words[1:])
        ):
            self.safe = False
        self.commands.append(words)

    def _escape(self, quoted: bool) -> None:
        nxt = self.text[self.i + 1 : self.i + 2]
        if nxt in ("", "\n"):
            self.safe = False
        else:
            self._add(nxt if not quoted or nxt in '"\\$`' else "\\" + nxt, quoted=True)
        self.i += 2

    def _double(self) -> None:
        s = self.text
        self.i += 1
        self._add("", quoted=True)
        while self.i < len(s) and s[self.i] != '"':
            c = s[self.i]
            if c == "\\":
                self._escape(quoted=True)
            elif c == "$":
                self._dollar(quoted=True)
            elif c == "`":
                self._backtick(quoted=True)
            else:
                self._add(c, quoted=True)
                self.i += 1
        if self.i >= len(s):
            self._fail()
        self.i += 1

    def _closing(self, start: int) -> int:
        s, depth, j = self.text, 0, start
        while j < len(s):
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c == "'":
                j = s.find("'", j + 1)
                if j < 0:
                    return -1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        return -1

    def _nested(self, text: str) -> None:
        sub = ShellScan(text)
        self.inner += sub.commands + sub.inner

    def _dollar(self, quoted: bool) -> None:
        s, i = self.text, self.i
        self.safe = False
        if s.startswith("$(", i):
            end = self._closing(i + 1)
            if end < 0:
                self._fail()
                return
            self._nested(s[i + 2 : end])
        elif s.startswith("${", i):
            end = s.find("}", i)
            if end < 0:
                self._fail()
                return
        else:
            end = i
        self._add(s[i : end + 1], quoted)
        self.i = end + 1

    def _backtick(self, quoted: bool) -> None:
        s, i = self.text, self.i
        self.safe = False
        end = i + 1
        while end < len(s) and s[end] != "`":
            end += 2 if s[end] == "\\" else 1
        if end >= len(s):
            self._fail()
            return
        self._nested(s[i + 1 : end].replace("\\`", "`"))
        self._add(s[i : end + 1], quoted)
        self.i = end + 1

    def _redirect(self) -> None:
        s = self.text
        if s.startswith(("<(", ">("), self.i):
            self.safe = False
            end = self._closing(self.i + 1)
            if end < 0:
                self._fail()
                return
            self._nested(s[self.i + 2 : end])
            self._add(s[self.i : end + 1], quoted=False)
            self.i = end + 1
            return
        if self.word is not None and self.word.isdigit() and self.word == self.unquoted:
            self.word, self.unquoted = None, ""
        else:
            self._end_word()
        op = next(o for o in REDIRECTS if s.startswith(o, self.i))
        self.i += len(op)
        while self.i < len(s) and s[self.i] in " \t":
            self.i += 1
        start = self.i
        while self.i < len(s) and s[self.i] not in " \t\n;&|<>()":
            self.i += 1
        target = s[start : self.i]
        if op in ("<<", "<<-"):
            self.heredocs.append(re.sub(r"[\\'\"]", "", target))
        if (op, target) not in HARMLESS_REDIRECTS:
            self.safe = False
            self.writes |= op not in ("<", "<<", "<<-", "<<<", "<&")

    def _skip_heredocs(self) -> None:
        s = self.text
        for delimiter in self.heredocs:
            while self.i < len(s):
                end = s.find("\n", self.i)
                end = len(s) if end < 0 else end
                line = s[self.i : end].lstrip("\t")
                self.i = end + 1
                if line == delimiter:
                    break
        self.heredocs = []


READS = frozenset(
    "cat head tail wc grep egrep fgrep rg ls pwd echo printf true false test [ basename dirname realpath readlink stat "
    "file cut tr column diff cmp comm jq seq nl tac rev fold od md5sum sha1sum sha256sum tree du df id whoami uname "
    "which type wait sleep find".split()
)
NEVER = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "sort": ("-o", "--output"),
    "tree": ("-o",),
    "date": ("-s", "--set"),
}
GIT_READS = frozenset(
    "status diff log show rev-parse ls-files ls-tree blame grep describe shortlog cat-file merge-base".split()
)
OPSQUIRE_READS = frozenset("find show ls runs log last inbox threads milestones unread locks tend".split())
SED_ADDRESS = r"(?:\d+|\$|/(?:[^/\\]|\\.)*/)"
SED_COMMAND = r"(?:p|d|q|=|n|s/(?:[^/\\]|\\.)*/(?:[^/\\]|\\.)*/[gpI0-9]*)"
SED_READS = re.compile(rf"\s*(?:{SED_ADDRESS}(?:,{SED_ADDRESS})?\s*!?\s*{SED_COMMAND}\s*(?:;\s*|$))+\s*")
TAKES_VALUE = {"timeout": ("-s", "--signal", "-k", "--kill-after"), "xargs": ("-I", "-n", "-d", "-P", "-L", "-s", "-E")}


def reads_only(words: list[str]) -> bool:
    """A simple command that only reads and prints: its name is literal (never a variable or a substitution), and
    no option of it writes a file or runs another command, unless that command only reads too."""
    while words and ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]
    if not words:
        return True
    if any(c in words[0] for c in "$`"):
        return False
    name, args = words[0].rsplit("/", 1)[-1], words[1:]
    if name in ("timeout", "xargs", "env", "nice"):
        rest = started(name, args)
        return reads_only(rest) if rest else name != "timeout"
    if name in NEVER and any(a.split("=")[0] in NEVER[name] for a in args):
        return False
    if name in READS:
        return True
    if name == "sed":
        return sed_reads(args)
    if name == "git":
        while args and args[0] in ("-C", "-c", "--no-pager", "--git-dir", "--work-tree"):
            args = args[2:] if args[0] in ("-C", "-c", "--git-dir", "--work-tree") else args[1:]
        return bool(args) and args[0] in GIT_READS and not any(a.startswith("--output") for a in args)
    if name == "opsquire":
        return bool(args) and args[0] in OPSQUIRE_READS and "--fix" not in args
    if name in ("uniq", "xxd"):
        return len([a for a in args if not a.startswith("-")]) <= 1
    return name == "command" and args[:1] == ["-v"]


def started(name: str, args: list[str]) -> list[str]:
    """The command that `timeout 30 cmd`, `xargs grep x`, `env -i A=1 cmd` or `nice cmd` starts: the one judged."""
    takes = TAKES_VALUE.get(name, ())
    i = 0
    while i < len(args) and (args[i].startswith("-") or (name == "env" and ASSIGNMENT.fullmatch(args[i]))):
        i += 2 if args[i] in takes or (name in ("env", "nice") and args[i] in ("-u", "-n")) else 1
    return args[i + (name == "timeout") :]


def sed_reads(args: list[str]) -> bool:
    """sed that only prints: no in-place edit, and scripts of addresses with p, d, q, =, n or s/// (no w or e)."""
    scripts: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith(("-i", "--in-place", "-f", "--file")):
            return False
        if a in ("-e", "--expression"):
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if not a.startswith("-") and not scripts:
            scripts.append(a)
        i += 1
    return bool(scripts) and all(SED_READS.fullmatch(x) for x in scripts)


REACH_OUT = frozenset(
    "ssh scp sftp rsync curl wget nc ncat netcat socat telnet ftp sudo su doas pkexec docker podman kubectl helm "
    "systemctl supervisorctl service aws gcloud az psql mysql mongo mongosh redis-cli influx".split()
)
GIT_REACH_OUT = frozenset("push pull fetch clone ls-remote submodule".split())
GH_READS = {
    "pr": {"view", "list", "diff", "checks", "status"},
    "issue": {"view", "list", "status"},
    "run": {"view", "list", "watch"},
    "repo": {"view", "list"},
    "release": {"view", "list"},
    "workflow": {"view", "list"},
    "search": {"code", "issues", "prs", "repos", "commits"},
}
OPSQUIRE_REACH_OUT = frozenset("run ui share integrate".split())
SHELL_MODES = ("outward", "reads", "confirm")


def reaches_out(words: list[str]) -> bool:
    """A simple command that talks to another machine or service, or acts as someone else: what an ask agent's own
    copy of the work cannot contain. Wrappers (timeout, env, xargs, nice) are judged by the command they start."""
    while words and ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]
    if not words:
        return False
    name, args = words[0].rsplit("/", 1)[-1], words[1:]
    if name in ("timeout", "xargs", "env", "nice"):
        return reaches_out(started(name, args))
    if name in REACH_OUT:
        return True
    if name == "git":
        while args and args[0] in ("-C", "-c", "--no-pager", "--git-dir", "--work-tree"):
            args = args[2:] if args[0] in ("-C", "-c", "--git-dir", "--work-tree") else args[1:]
        return bool(args) and (args[0] in GIT_REACH_OUT or args[:2] in (["remote", "add"], ["remote", "set-url"]))
    if name == "gh":
        if args[:1] == ["api"]:
            pairs = zip(args, [*args[1:], ""], strict=True)
            method = next((v for a, v in pairs if a in ("-X", "--method")), "")
            method = method or next((a[2:] for a in args if a.startswith("-X") and len(a) > 2), "")
            sends = any(a.split("=")[0] in ("-f", "-F", "--field", "--raw-field", "--input") for a in args)
            return sends or method.upper() not in ("", "GET")
        return len(args) < 2 or args[1] not in GH_READS.get(args[0], set())
    if name == "opsquire":
        return bool(args) and (args[0] in OPSQUIRE_REACH_OUT or args[:2] == ["repo", "pr"])
    return False


def stays_local(command: str) -> bool:
    """Whether a whole shell command line stays on this machine as this user: no command in it (inside loops, pipes
    and substitutions too) reaches out, every command name is literal, and nothing hides commands in text
    (eval, sh -c)."""
    scan = ShellScan(command)
    if scan.broken or not (scan.commands or scan.inner):
        return False
    for words in scan.commands + scan.inner:
        rest = [w for w in words if not ASSIGNMENT.fullmatch(w)] if not harmless(words) else []
        if rest and any(c in rest[0] for c in "$`"):
            return False
        name = rest[0].rsplit("/", 1)[-1] if rest else ""
        if name in RUNS_TEXT or (name in SHELLS and any(w.startswith("-") and "c" in w for w in rest[1:])):
            return False
        if reaches_out(words):
            return False
    return True


def read_only(command: str) -> bool:
    """Whether a whole shell command only reads: every simple command in it (inside loops, pipes, conditions and
    substitutions too) only reads and prints, and no redirection writes a file. Variables and substitutions may
    feed arguments, never name the command."""
    scan = ShellScan(command)
    if scan.broken or scan.writes or scan.branching or not (scan.commands or scan.inner):
        return False
    return all(harmless(w) or reads_only(w) for w in scan.commands + scan.inner)


def harmless(words: list[str]) -> bool:
    """A builtin that only moves the shell or sets its variables: `cd`, `pwd`, `export NAME=value` and the like."""
    if not words:
        return False
    if words[0] == "export":
        return all(ASSIGNMENT.fullmatch(w) for w in words[1:])
    return words[0] in HARMLESS


def agent_allows(words: list[str], spec: dict[str, Any]) -> bool:
    """Whether the agent's own allowed tools (its retinue entry, `<shell tool>(<pattern>)`) let this simple command
    run alone: a chain of such commands is no more than the agent may already run one by one."""
    tool = str((spec.get("shell") or {}).get("tool") or "")
    if not tool or not words:
        return False
    command = " ".join(words)
    for rule in spec.get("allowed") or []:
        m = re.fullmatch(re.escape(tool) + r"\((.*)\)", str(rule))
        if not m:
            continue
        pattern = m.group(1)
        pattern = pattern[:-2] + " *" if pattern.endswith(":*") else pattern
        if fnmatch.fnmatchcase(command, pattern) or (pattern.endswith(" *") and command == pattern[:-2]):
            return True
    return False


def shell_mode(cfg: dict[str, Any]) -> str:
    """How shell commands without a rule are judged: outward (ask only for a command that reaches another machine
    or acts as someone else), reads (ask unless the line only reads) or confirm (always ask)."""
    mode = cfg.get("shell_mode")
    if mode in SHELL_MODES:
        return str(mode)
    return "confirm" if cfg.get("read_only") == "confirm" else "outward"


def _shell(
    rules: dict[str, Any], command: str, default: str, spec: dict[str, Any] | None = None, mode: str = "outward"
) -> str:
    """allow, confirm or off for a shell command. The operator's rules decide first, and an Off default stays off;
    then the mode (see shell_mode) judges every simple command in the line, loops and substitutions included."""
    scan = ShellScan(command)
    if _best(rules, command) == "off":
        return "off"

    def choice(words: list[str]) -> str:
        found = _best(rules, " ".join(words))
        if found:
            return found
        if harmless(words) or agent_allows(words, spec or {}):
            return "allow"
        if default == "off":
            return "off"
        if mode == "outward":
            return "confirm" if reaches_out(words) else "allow"
        return "allow" if mode == "reads" and reads_only(words) else default

    choices = [choice(w) for w in [*scan.commands, *scan.inner] or [[]]]
    if "off" in choices:
        return "off"
    whole = {"outward": stays_local, "reads": read_only}.get(mode)
    if all(c == "allow" for c in choices) and (scan.safe or (whole is not None and whole(command))):
        return "allow"
    return "confirm"


def server_id(name: str, spec: dict[str, Any]) -> str:
    sub = spec.get("server_id") or {}
    return re.sub(str(sub.get("sub", r"[^A-Za-z0-9_-]")), str(sub.get("with", "_")), name)


def split_name(tool: str, spec: dict[str, Any]) -> tuple[str, str] | None:
    """(server id, tool) for a server tool, None for any other tool."""
    m = re.match(str(spec.get("name_re") or r"^$a"), tool)
    return (m.group("server"), m.group("tool")) if m else None


def decide(tool: str, tool_input: dict[str, Any], spec: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    """(allow | confirm | off, a label the operator reads)."""
    default = _choice(cfg.get("default"), "confirm")
    shell = spec.get("shell") or {}
    if shell and tool == shell.get("tool"):
        command = str(tool_input.get(shell.get("input", "command"), ""))
        return _shell(cfg.get("shell") or {}, command, default, spec, shell_mode(cfg)), f"shell: {command}"
    parts = split_name(tool, spec)
    if parts:
        sid, name = parts
        server = (cfg.get("servers") or {}).get(sid) or {}
        if server.get("enabled") is not True:
            return "off", f"{sid}: {name}"
        found = _best(server.get("tools") or {}, name)
        return found or _choice(server.get("default"), default), f"{sid}: {name}"
    return _best(cfg.get("agent_tools") or {}, tool) or default, tool


def mcp_config(cfg: dict[str, Any], approve_cmd: list[str]) -> dict[str, Any]:
    """The servers an ask agent gets on top of its own: opsquire's approval server and the home's added servers."""
    servers: dict[str, Any] = {OWN_SERVER: {"type": "stdio", "command": approve_cmd[0], "args": approve_cmd[1:]}}
    for name, s in (cfg.get("added") or {}).items():
        if not isinstance(s, dict) or s.get("enabled") is False:
            continue
        if s.get("url"):
            servers[name] = {
                "type": s.get("type", "http"),
                "url": s["url"],
                **({"headers": s["headers"]} if s.get("headers") else {}),
            }
        elif s.get("command"):
            servers[name] = {
                "type": "stdio",
                "command": s["command"],
                "args": list(s.get("args") or []),
                "env": dict(s.get("env") or {}),
            }
    return {"mcpServers": servers}


def disabled_servers(cfg: dict[str, Any]) -> list[str]:
    return sorted(
        sid for sid, s in (cfg.get("servers") or {}).items() if isinstance(s, dict) and s.get("enabled") is False
    )


def note_seen(tool: str) -> None:
    """Remember a tool an agent asked for, so Settings can offer it; never in the way of a decision."""
    path = var_dir() / SEEN
    with contextlib.suppress(OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_json(path) if path.is_file() else {}
        write_json(path, {**(data if isinstance(data, dict) else {}), tool: iso(now())})


def seen() -> dict[str, str]:
    path = var_dir() / SEEN
    try:
        data = read_json(path) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def listed_servers(spec: dict[str, Any], timeout: float = 30) -> list[dict[str, str]]:
    """The servers the agent itself knows (its `tools.list` command), as [{name, id, target, status}]."""
    lister = spec.get("list") or {}
    if not lister.get("argv"):
        return []
    try:
        r = subprocess.run(list(lister["argv"]), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    line_re = re.compile(str(lister.get("line_re", r"^(?P<name>[^:]+): (?P<target>.+?) - (?P<status>.+)$")))
    out = []
    for line in r.stdout.splitlines():
        m = line_re.match(line.strip())
        if m:
            name = m.group("name").strip()
            out.append(
                {"name": name, "id": server_id(name, spec), "target": m.group("target"), "status": m.group("status")}
            )
    return out


def view(spec: dict[str, Any], listed: list[dict[str, str]]) -> dict[str, Any]:
    """Everything the console's Settings shows: each server with its rules and the tools seen from it."""
    cfg = load()
    rules = cfg.get("servers") or {}
    servers: dict[str, dict[str, Any]] = {}
    for found in listed:
        if found["id"] != OWN_SERVER:
            servers[found["id"]] = {**found, "source": "agent", "seen": []}
    for name in cfg.get("added") or {}:
        sid = server_id(name, spec)
        servers.setdefault(sid, {"id": sid, "name": name, "status": "", "seen": []})["source"] = "added"
    other_seen: list[str] = []
    shell_tool = (spec.get("shell") or {}).get("tool")
    for tool in seen():
        parts = split_name(tool, spec)
        if parts and parts[0] != OWN_SERVER:
            servers.setdefault(
                parts[0], {"id": parts[0], "name": parts[0], "status": "", "source": "seen", "seen": []}
            )["seen"].append(parts[1])
        elif not parts and tool != shell_tool:
            other_seen.append(tool)
    for sid in rules:
        servers.setdefault(sid, {"id": sid, "name": sid, "status": "", "source": "rules", "seen": []})
    for sid, server in servers.items():
        r = rules.get(sid) or {}
        server.update(
            enabled=r.get("enabled") is True,
            default=r.get("default", ""),
            tools=r.get("tools") or {},
            seen=sorted(set(server["seen"])),
        )
    return {
        "default": _choice(cfg.get("default"), "confirm"),
        "servers": sorted(servers.values(), key=lambda s: s["name"].lower()),
        "shell": cfg.get("shell") or {},
        "shell_mode": shell_mode(cfg),
        "agent_tools": cfg.get("agent_tools") or {},
        "agent_tools_seen": sorted(set(other_seen)),
        "added": cfg.get("added") or {},
        "can_list": bool((spec.get("list") or {}).get("argv")),
        "supported": bool(spec.get("approval_argv")),
    }


def update(change: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Apply one change from the console's Settings. Kinds: default, server (enabled/default), rule (server or
    shell or agent_tools pattern -> choice, or null to drop it), shell_mode, add, remove."""
    cfg = load()
    kind = change.get("kind")
    if kind == "default":
        cfg["default"] = _choice(change.get("value"), "confirm")
    elif kind == "shell_mode":
        if change.get("value") not in SHELL_MODES:
            raise OpsquireError(f"shell mode is one of {', '.join(SHELL_MODES)}")
        cfg["shell_mode"] = change["value"]
        cfg.pop("read_only", None)
    elif kind == "server":
        s = cfg.setdefault("servers", {}).setdefault(str(change["id"]), {})
        if "enabled" in change:
            s["enabled"] = bool(change["enabled"])
        if "default" in change:
            if change["default"] in CHOICES:
                s["default"] = change["default"]
            else:
                s.pop("default", None)
    elif kind == "rule":
        scope, pattern, value = str(change.get("scope")), str(change.get("pattern", "")).strip(), change.get("value")
        if not pattern:
            raise OpsquireError("a rule needs a pattern")
        if scope in ("shell", "agent_tools"):
            rules = cfg.setdefault(scope, {})
        else:
            rules = cfg.setdefault("servers", {}).setdefault(scope, {}).setdefault("tools", {})
        if value is None:
            rules.pop(pattern, None)
        elif value in CHOICES:
            rules[pattern] = value
        else:
            raise OpsquireError(f"choice must be one of {', '.join(CHOICES)}")
    elif kind == "add":
        name = str(change.get("name", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,40}", name) or name == OWN_SERVER:
            raise OpsquireError("a server name is letters, digits, space, dot, dash or underscore")
        url, command = str(change.get("url") or "").strip(), str(change.get("command") or "").strip()
        if bool(url) == bool(command):
            raise OpsquireError("give a URL or a command, not both")
        if url and not re.match(r"https?://", url):
            raise OpsquireError("the URL must start with http:// or https://")
        entry: dict[str, Any] = {"url": url} if url else {"command": command, "args": list(change.get("args") or [])}
        cfg.setdefault("added", {})[name] = entry
        cfg.setdefault("servers", {}).setdefault(server_id(name, spec), {})["enabled"] = True
    elif kind == "remove":
        name = str(change.get("name"))
        (cfg.get("added") or {}).pop(name, None)
        (cfg.get("servers") or {}).pop(server_id(name, spec), None)
    else:
        raise OpsquireError(f"unknown settings change {kind!r}")
    save(cfg)
    return cfg
