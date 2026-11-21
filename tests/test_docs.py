from __future__ import annotations

import argparse
import re
import unittest
from collections.abc import Iterator
from pathlib import Path

from opsquire.cli import parser

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src/opsquire/data"
README, CONTRACT = ROOT / "README.md", PACKAGE / "AGENTS.md"
COMMAND_RE = re.compile(r"(?:^|&&|;|\||\(|\$)\s*opsquire\s+([a-z][a-z-]*)((?:(?!\bopsquire\s|&&|;|\s--\s|#).)*)")


def commands() -> dict[str, argparse.ArgumentParser]:
    sub = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
    return dict(sub.choices)


def options(p: argparse.ArgumentParser) -> set[str]:
    out: set[str] = set()
    for a in p._actions:
        out.update(a.option_strings)
        if isinstance(a, argparse._SubParsersAction):
            for child in a.choices.values():
                out |= options(child)
    return out


def code(text: str) -> Iterator[str]:
    """Command lines in code blocks, without their aligned descriptions or comments, and inline code spans."""
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
        yield from (re.split(r"\s{2,}#?|\s#", line.strip())[0] for line in block.splitlines())
    yield from re.findall(r"(?<!`)`([^`\n]+)`(?!`)", re.sub(r"```.*?```", "", text, flags=re.S))


class DocsTest(unittest.TestCase):
    def test_every_documented_command_and_option_exists(self) -> None:
        cmds, top = commands(), options(parser())
        docs = [README, ROOT / "docs/WHY.md", CONTRACT, *PACKAGE.glob("skills/*/*.md"), *PACKAGE.glob("templates/*.md")]
        wrong = []
        for doc in docs:
            for line in code(doc.read_text()):
                for m in COMMAND_RE.finditer(line):
                    cmd, rest = m.groups()
                    if cmd not in cmds:
                        wrong.append(f"{doc.name}: no command {cmd}: {line}")
                        continue
                    wrong += [
                        f"{doc.name}: {cmd} has no {flag}: {line}"
                        for flag in re.findall(r"(?<![\w-])(--[a-z][\w-]*)", rest)
                        if flag not in options(cmds[cmd]) | top
                    ]
        self.assertEqual(wrong, [])

    def test_every_command_is_documented_for_operators_or_agents(self) -> None:
        documented = {
            m.group(1)
            for doc in (README, CONTRACT)
            for line in code(doc.read_text())
            for m in COMMAND_RE.finditer(line)
        }
        sub = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
        internal = {c.dest for c in sub._choices_actions if str(c.help).startswith("(started by")}
        self.assertEqual(sorted(set(commands()) - documented - internal), [])
