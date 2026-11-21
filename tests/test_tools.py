"""What ask agents may use: tool rules, shell commands and the operator's confirmation."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from unittest import mock

from support import CLI, ROOT, OpsTestCase, free_port


class ToolRulesTest(OpsTestCase):
    def test_ask_tools_follow_the_homes_rules_and_confirm_needs_an_operator(self) -> None:
        self.tool_rules()
        mail, fresh = {"name": "mcp__mail__send", "input": {}}, {"name": "mcp__fresh__anything", "input": {}}
        self.env["FAKE_TOOLS"] = json.dumps([self.SLACK_READ, self.SLACK_SEND, mail, fresh])
        self.env["FAKE_LOG"] = str(self.tmp / "fake.log")
        r = subprocess.run(
            [*CLI, "ask", "post", "hello"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            start_new_session=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Tool decisions: allow deny deny deny", r.stdout, "a server nobody switched on stays off")
        argv = json.loads((self.tmp / "fake.log").read_text().splitlines()[-1])["argv"]
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "mcp__mail")
        self.assertEqual(list((self.home / "var/asks").glob("*.approvals")), [], "an ask leaves no approvals behind")
        seen = json.loads((self.home / "var/tools-seen.json").read_text())
        self.assertEqual(
            sorted(seen),
            ["mcp__chat__read_thread", "mcp__chat__send_message", "mcp__fresh__anything", "mcp__mail__send"],
        )

    def test_shell_tool_rules_judge_each_simple_command_and_compound_or_expanding_commands_ask(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import tools

        cfg = {"shell": {"gh pr view *": "allow", "rm *": "off"}, "read_only": "confirm"}

        def choice(command: str) -> str:
            return tools.decide("Bash", {"command": command}, self.TOOLS_SPEC, cfg)[0]

        for command in ("gh pr view 1", "gh pr view 1 && gh pr view 2", 'gh pr view "a; b"', "gh pr view 1 2>&1"):
            self.assertEqual(choice(command), "allow", command)
        self.assertEqual(choice("gh pr view 1 >/dev/null"), "allow")
        asks = [
            "gh pr view 1; echo UNRELATED_COMMAND",
            "gh pr view 1\necho X",
            "gh pr view $(whoami)",
            "gh pr view `whoami`",
            "gh pr view $HOME",
            "gh pr view ${HOME}",
            "gh pr view 1 > file",
            "gh pr view 1 >> file",
            "gh pr view 1 < file",
            "gh pr view 1 2> file",
            "gh pr view <<EOF\n1\nEOF",
            "gh pr view <(echo 1)",
            "(gh pr view 1)",
            "{ gh pr view 1; }",
            "gh pr view 'unbalanced",
            "gh pr view 1 \\\n2",
            "bash -c 'gh pr view 1'",
            "eval gh pr view 1",
            "gh pr view {1,2}",
        ]
        for command in asks:
            self.assertEqual(choice(command), "confirm", command)
        for command in ("gh pr view 1 | rm -rf x", "gh pr view 1 && rm x", "gh pr view $(rm x)", "echo `rm x`"):
            self.assertEqual(choice(command), "off", command)

    def test_a_chain_of_commands_the_agent_may_run_alone_needs_no_card(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import tools

        spec = {**self.TOOLS_SPEC, "allowed": ["Bash(opsquire show *)", "Bash(opsquire log:*)", "Read"]}
        cfg = {"shell": {"rm *": "off"}, "read_only": "confirm"}

        def choice(command: str) -> str:
            return tools.decide("Bash", {"command": command}, spec, cfg)[0]

        for command in ("cd ~/ops && opsquire show x", "export A=1 && opsquire log x", "cd /tmp; pwd; opsquire show y"):
            self.assertEqual(choice(command), "allow", command)
        for command in ("cd x && opsquire run y", "export A=$(id) && opsquire show x", "echo hi && opsquire show x"):
            self.assertEqual(choice(command), "confirm", command)
        self.assertEqual(choice("cd x && rm y"), "off", "the operator's off rule still wins")
        self.assertEqual(tools.decide("Bash", {"command": "opsquire show x"}, self.TOOLS_SPEC, cfg)[0], "confirm")

    def test_in_reads_mode_commands_that_only_read_need_no_card_even_in_loops(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import tools

        def choice(command: str, **cfg: Any) -> str:
            return tools.decide("Bash", {"command": command}, self.TOOLS_SPEC, {"shell_mode": "reads", **cfg})[0]

        reads = [
            'for g in a b; do echo "== $g"; ls general/$g; cat general/$g/meta.json; done | head -40',
            'f=$(find / -name x.py -not -path "*/proc/*" 2>/dev/null | head -1); grep -n "def use" -A40 $f',
            "sed -n 1,110p main.py; sed -n '28,42p' x.py",
            "git -C /x diff --stat -- . | tail -8; opsquire find ssh remote 2>&1 | head",
            "timeout 5 cat x; xargs grep foo < list; env -i HOME=/tmp ls",
            "cat <<E\nhello\nE",
        ]
        for command in reads:
            self.assertEqual(choice(command), "allow", command)
        changes = [
            "echo hi > out.txt",
            "rm -rf x",
            "find . -name x -delete",
            "find . -exec rm {} +",
            "sed -i s/a/b/ f",
            "sed 's/a/b/w out' f",
            "sort -o out f",
            "$CMD x",
            'python3 -c "print(1)"',
            "python3 - <<E\nprint(1)\nE",
            "opsquire lint x",
            "opsquire tend --fix",
            "timeout 5 rm x",
            "xargs rm < list",
            "echo $(touch x)",
            "case x in a) ls;; esac",
            "git push",
            "cat 'unbalanced",
        ]
        for command in changes:
            self.assertEqual(choice(command), "confirm", command)
        self.assertEqual(choice("grep x f", default="off"), "off", "an operator's off default stays off")
        self.assertEqual(choice("cat x | grep y", shell={"grep *": "off"}), "off")
        self.assertEqual(choice("cat secret", shell={"cat *": "confirm"}), "confirm", "the operator's own rule wins")
        self.assertEqual(choice("for g in a; do cat $g; done", shell_mode="confirm"), "confirm")

    def test_by_default_only_commands_that_reach_out_need_a_card(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import tools

        def choice(command: str, **cfg: Any) -> str:
            return tools.decide("Bash", {"command": command}, self.TOOLS_SPEC, cfg)[0]

        local = [
            "cd x && sed -i 's/a/b/' f.py && cat > g.py <<'E'\nprint(1)\nE",
            "for g in a b; do opsquire lint $g; done",
            "python3 -m unittest discover; rm -rf build",
            "git diff --stat; gh pr view 1; gh api repos/o/r",
            "opsquire handover t --why x",
        ]
        for command in local:
            self.assertEqual(choice(command), "allow", command)
        out = [
            "ssh host uptime",
            "timeout 5 scp a host:b",
            "x=$(curl https://example.com); echo $x",
            "git push origin main",
            "gh pr create --title x",
            "gh api -X POST repos/o/r/issues",
            "gh api repos/o/r/issues -f title=x",
            "sudo ls",
            "opsquire run t --apply",
            "opsquire repo pr copy --title x",
            "$CMD x",
            "bash -c 'ls'",
        ]
        for command in out:
            self.assertEqual(choice(command), "confirm", command)
        self.assertEqual(choice("rm -rf build", shell={"rm *": "confirm"}), "confirm", "the operator's rule wins")
        self.assertEqual(choice("ls", default="off"), "off", "an Off default stays off")
        self.assertEqual(choice("cat f", read_only="confirm"), "confirm", "the earlier switch still means always ask")

    def test_the_operator_allows_a_confirmed_tool_on_the_terminal(self) -> None:
        self.tool_rules()
        self.env["FAKE_TOOLS"] = json.dumps([self.SLACK_SEND, self.SLACK_SEND])
        r = self.cli("ask", "post", "hello", answers=[("[Enter = deny] >", "y"), ("[Enter = deny] >", "")])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Allow chat: send_message? channel_id=C1; text=hello", r.stdout.replace("\r\n", " "))
        self.assertIn("Tool decisions: allow deny", r.stdout)
        chat = next((self.home / "var/asks").glob("*.chat.jsonl")).read_text().splitlines()
        kinds = [(c["kind"], c["text"]) for c in map(json.loads, chat)]
        self.assertIn(("approve", "chat: send_message"), kinds)
        self.assertEqual([t for k, t in kinds if k == "you"][1:], ["allow", "deny"])

    def test_console_settings_change_the_homes_tool_rules(self) -> None:
        self.tool_rules()
        (self.home / "var").mkdir(exist_ok=True)
        (self.home / "var/tools-seen.json").write_text(json.dumps({"mcp__chat__send_message": "x", "WebFetch": "x"}))
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 10)

            def call(method: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(
                    method, "/api/tools", json.dumps(body) if body else None, {"Content-Type": "application/json"}
                )
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            status, view = call("GET")
            self.assertEqual(status, 200, view)
            self.assertTrue(view["supported"])
            chat = next(s for s in view["servers"] if s["id"] == "chat")
            self.assertEqual((chat["tools"], chat["seen"]), ({"read_*": "allow"}, ["send_message"]))
            self.assertFalse(next(s for s in view["servers"] if s["id"] == "mail")["enabled"])
            self.assertEqual(view["agent_tools_seen"], ["WebFetch"])
            call("POST", {"kind": "rule", "scope": "chat", "pattern": "send_*", "value": "confirm"})
            call("POST", {"kind": "rule", "scope": "shell", "pattern": "gh pr view *", "value": "allow"})
            status, view = call("POST", {"kind": "add", "name": "notes", "url": "https://notes.example/mcp"})
            self.assertEqual(status, 200, view)
            self.assertEqual(call("POST", {"kind": "add", "name": "bad", "url": "ftp://x"})[0], 400)
            rules = json.loads((self.home / "tools.json").read_text())
            self.assertEqual(rules["servers"]["chat"]["tools"], {"read_*": "allow", "send_*": "confirm"})
            self.assertEqual(rules["shell"], {"gh pr view *": "allow"})
            self.assertEqual(rules["added"], {"notes": {"url": "https://notes.example/mcp"}})
            self.assertIn("notes", [s["name"] for s in view["servers"] if s["source"] == "added"])
            call("POST", {"kind": "remove", "name": "notes"})
            self.assertNotIn("notes", json.loads((self.home / "tools.json").read_text()).get("added", {}))
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(20)

    def test_the_console_lists_every_waiting_request_and_answers_any_of_them(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import jobs, tools
        from opsquire.model import OpsquireError

        with mock.patch.dict(os.environ, self.env):
            box = self.home / "var/asks/20261118T202410632Z-4242.approvals"
            box.mkdir(parents=True)
            labels = ["shell: opsquire lint x", "shell: for g in a b; do cat $g; done", "shell: rm -rf build"]
            for n, label in enumerate(labels, 1):
                (box / f"{n}.req.json").write_text(json.dumps({"tool": "Bash", "label": label, "detail": ""}))
            read_fd, write_fd = os.pipe()
            self.addCleanup(os.close, read_fd)
            self.addCleanup(os.close, write_fd)
            js = jobs.Jobs()
            job = jobs.Job("j1", "", "ask", "2026-11-18T20:24:10.634Z", ask_id="a1")
            job.proc, job.fd = SimpleNamespace(pid=4242), write_fd  # type: ignore[assignment]
            js.jobs["j1"] = job

            queue = js.approvals("j1")
            self.assertEqual([(r["id"], r["read_only"]) for r in queue], [("1", False), ("2", True), ("3", False)])
            js.approve("j1", "2", True)
            self.assertEqual(
                json.loads((box / "2.ans.json").read_text())["allow"], True, "a queued one is answered in place"
            )
            js.approve("j1", "3", False)
            self.assertEqual(json.loads((box / "3.ans.json").read_text())["allow"], False)
            chat = [json.loads(x) for x in (self.home / "var/asks/a1.chat.jsonl").read_text().splitlines()]
            self.assertEqual([(c["kind"], c["text"]) for c in chat][-2:], [("approve", labels[2]), ("you", "deny")])
            self.assertEqual([r["id"] for r in js.approvals("j1")], ["1"])
            js.approve("j1", "1", True)
            self.assertEqual(os.read(read_fd, 10), b"y\n", "the one its terminal asks is answered there")
            with self.assertRaises(OpsquireError):
                js.approve("j1", "9", True)

            spec = {"shell": {"tool": "Bash", "input": "command"}}
            self.assertEqual(tools.view(spec, [])["shell_mode"], "outward")
            tools.update({"kind": "shell_mode", "value": "reads"}, spec)
            self.assertEqual(tools.view(spec, [])["shell_mode"], "reads")
