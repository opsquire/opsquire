"""Library upkeep: `opsquire tend` and its agent hand-off."""

from __future__ import annotations

import datetime as dt
import json
import signal
import time
from pathlib import Path
from typing import Any

from support import OpsTestCase, free_port

PARSE = (
    "def parse(text: str) -> dict[str, str]:\n"
    '    """Rows of key=value."""\n'
    "    rows: dict[str, str] = {}\n"
    "    for line in text.splitlines():\n"
    "        key, _, value = line.partition('=')\n"
    "        rows[key.strip()] = value.strip()\n"
    "    return rows\n"
)
BY_HAND = (
    "import importlib.util\nimport os\nfrom pathlib import Path\n\n\n"
    "def lib() -> object:\n"
    "    path = Path(os.environ['OPSQUIRE_GENERAL']) / 'web1_lib' / 'main.py'\n"
    "    spec = importlib.util.spec_from_file_location('lib', path)\n"
    "    assert spec and spec.loader\n"
    "    mod = importlib.util.module_from_spec(spec)\n"
    "    spec.loader.exec_module(mod)\n"
    "    return mod\n"
)


class TendTest(OpsTestCase):
    FIRST, SECOND, USER = "20261101-1000_web1_first", "20261102-1000_web1_second", "20261103-1000_web1_user"

    def home_with_findings(self) -> None:
        def days_ago(n: int) -> str:
            return (dt.datetime.now(dt.UTC) - dt.timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        lib = self.fixture("web1_lib", "shared parsing", body=PARSE, created=days_ago(3))
        (lib / "test_main.py").write_text(
            "import unittest\n\nfrom main import parse\n\n\nclass T(unittest.TestCase):\n"
            "    def test_parse(self) -> None:\n        self.assertEqual(parse('a = 1'), {'a': '1'})\n"
        )
        recent = days_ago(1)
        self.fixture(self.FIRST, "first copy", body=PARSE.replace("Rows of", "The rows of"), created=recent)
        self.fixture(self.SECOND, "second copy", body="# a copy\n\n" + PARSE, created=recent)
        self.fixture(self.USER, "uses the library", "web1_lib", body="print('uses lib')\n", created=recent)
        loader = BY_HAND + BY_HAND.replace("lib() ->", "two() ->")
        self.fixture("web1_loader", "loads lib by hand", "web1_lib", body=loader, created=days_ago(2))
        self.fixture("web1_old", "nothing uses it", created=days_ago(60))

    def tend(self, *args: str) -> dict[str, Any]:
        r = self.cli("tend", "--json", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        out: dict[str, Any] = json.loads(r.stdout)
        return out

    def test_tend_reports_usage_copies_hand_imports_untested_and_unused_scripts(self) -> None:
        self.home_with_findings()
        r = self.cli("run", self.USER)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = self.tend()
        self.assertEqual(
            [(c["names"], c["lines"], c["folders"], c["generals"]) for c in rep["copies"]],
            [
                (
                    ["parse"],
                    7,
                    [self.FIRST, self.SECOND, "web1_lib"],
                    ["web1_lib"],
                )
            ],
            "a docstring, comments and layout do not hide a copy; loading code by hand is not counted as one",
        )
        self.assertEqual(rep["hand_imports"], [{"id": "web1_loader", "kind": "general", "files": ["main.py"]}])
        self.assertEqual(rep["untested"], ["web1_loader", "web1_old"])
        self.assertEqual(rep["unused"], ["web1_old"], "web1_loader is new, web1_lib has open users")
        rows = {u["id"]: u for u in rep["usage"]}
        self.assertEqual(
            (rows["web1_lib"]["users_open"], rows["web1_lib"]["users"], rows["web1_lib"]["runs_recent"]),
            (["web1_loader"], 2, 1),
            "the read-only user ran ok, so it is settled: it counts as a user and a run, no longer as open code",
        )
        self.assertIsNotNone(rows["web1_lib"]["last_used"])
        self.assertEqual(rows["web1_old"]["last_used"], None)
        self.assertTrue((self.home / "var/tend-cache.json").is_file())
        self.assertEqual(self.tend()["copies"], rep["copies"], "the cached fingerprints give the same answer")

        text = self.cli("tend").stdout
        self.assertIn("parse (7 lines) in 3 folders; defined in web1_lib", text)
        self.assertIn("web1_loader: main.py", text)
        self.assertIn("web1_old   (opsquire offboard web1_old)", text)
        self.assertIn("4 findings an agent can fix: opsquire tend --fix (at most $10)", text)

    def test_only_open_copies_and_twin_libraries_are_left_for_an_agent(self) -> None:
        test = (
            "import unittest\n\nfrom main import parse\n\n\nclass T(unittest.TestCase):\n"
            "    def test_parse(self) -> None:\n        self.assertEqual(parse('a = 1'), {'a': '1'})\n"
        )
        lib = self.fixture("web1_lib", "shared parsing", body=PARSE)
        (lib / "test_main.py").write_text(test)
        for n, tid in enumerate((self.FIRST, self.SECOND, "20261104-1000_web1_third")):
            self.fixture(tid, "a settled copy", body=PARSE)
            self.fixture_run(tid, f"2026-11-0{n + 1}T10:00:00.000Z", "ok", 1.0)

        def findings() -> list[tuple[list[str], bool]]:
            return [(c["generals"], c["fixable"]) for c in self.tend()["copies"]]

        self.assertEqual(findings(), [(["web1_lib"], False)], "settled tasks keep their copies")
        self.assertIn("(only settled tasks keep their copy)", self.cli("tend").stdout)
        self.assertIn("nothing for an agent to fix", self.cli("tend", "--fix").stdout)

        twin = self.fixture("web1_twin", "the same parsing again", body=PARSE)
        (twin / "test_main.py").write_text(test)
        self.assertEqual(findings(), [(["web1_lib", "web1_twin"], True)], "two libraries hold it")
        for f in twin.iterdir():
            f.unlink()
        twin.rmdir()

        (lib / "main.py").write_text(PARSE.replace("rows: dict[str, str] = {}", "rows: dict[str, str] = dict()"))
        self.assertEqual(findings(), [(["web1_lib"], False)], "a library's drifted copy is still its home, by name")

    def test_tend_fix_waits_for_the_last_upkeep_ask_to_be_finished(self) -> None:
        self.git_home()
        self.agents_file()
        self.home_with_findings()
        asks = self.home / "var/asks"
        asks.mkdir(parents=True)
        prompt = "Library upkeep of this home's general scripts (from `opsquire tend`)."
        parked = {"ask_id": "20261119T025328285Z_claude", "status": "waiting", "prompt": prompt}
        (asks / "20261119T025328285Z_claude.json").write_text(json.dumps(parked))
        r = self.cli("tend", "--fix")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("Continue it: opsquire ask --resume 20261119T025328285Z_claude", r.stderr)

    def test_tend_fix_hands_the_findings_to_an_ask_with_a_hard_cap_and_no_interview(self) -> None:
        self.git_home()
        self.agents_file()
        log = self.tmp / "agent-log.jsonl"
        self.env["FAKE_LOG"] = str(log)
        self.assertIn("nothing for an agent to fix", self.cli("tend", "--fix").stdout)
        self.assertFalse(log.exists(), "no agent starts without findings")

        self.home_with_findings()
        r = self.cli("tend", "--fix")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("opsquire: ask ", r.stderr)
        call = json.loads(log.read_text().splitlines()[0])
        prompt, argv = call["prompt"], call["argv"]
        self.assertIn("Change how code is shared, never what it does", prompt)
        self.assertIn("parse (7 lines) is defined in web1_lib and copied into", prompt)
        self.assertIn("web1_loader loads general scripts by hand (importlib or sys.path) in main.py", prompt)
        self.assertIn("web1_old has no tests", prompt)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "10.00", "the cap reaches the agent itself")
        self.assertIn("Do not ask questions", argv[argv.index("--sys") + 1])
        rec = json.loads(next(Path(self.home / "var/asks").glob("*.json")).read_text())
        self.assertTrue(rec["prompt"].startswith("Library upkeep"), rec["prompt"][:80])

    def test_the_console_shows_the_report_and_starts_the_fix_as_an_ask(self) -> None:
        self.git_home()
        self.agents_file()
        self.env["FAKE_LOG"] = str(self.tmp / "agent-log.jsonl")
        self.home_with_findings()
        port = free_port()
        proc = self.ui_process(port)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = self.http(port, 20)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            status, rep = call("GET", "/api/tend")
            self.assertEqual(
                (status, rep["unused"], [c["names"] for c in rep["copies"]]), (200, ["web1_old"], [["parse"]])
            )
            status, started = call("POST", "/api/tendfix", {"go": True})
            self.assertEqual(status, 200, started)
            deadline = time.time() + 60
            while time.time() < deadline:
                job: dict[str, Any] = call("GET", f"/api/job?id={started['job_id']}&offset=0")[1]
                if job["status"] == "done":
                    break
                time.sleep(0.25)
            self.assertEqual((job["status"], job["mode"], job["exit_code"]), ("done", "ask", 0), job.get("text"))
            self.assertTrue(job["ask_id"], "the page can open the ask's chat")
            ask = next(a for a in call("GET", "/api/state")[1]["asks"] if a["ask_id"] == job["ask_id"])
            self.assertTrue(ask["prompt"].startswith("Library upkeep"), ask["prompt"][:80])
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(20)
