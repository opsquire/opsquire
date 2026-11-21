"""Progress output, prompts and times as people read them."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from support import BASH_HEADER, ROOT, OpsTestCase


class ProgressTest(OpsTestCase):
    def test_prompt_appears_after_everything_printed_before_it(self) -> None:
        py = "import opsquire_helpers as sq\n\nfor i in range(300):\n    print(f'plan line {i}')\nsq.confirm('Go?')\n"
        sh = BASH_HEADER + 'for i in $(seq 0 299); do echo "plan line $i"; done\nsq_confirm "Go?"\n'
        for name, body, lang in (("order-py", py, "py"), ("order-sh", sh, "sh")):
            self.new(name, body, "--changes-prod", "--rollback", "x", lang=lang)
            r = self.cli("run", name, "--apply", *self.NO_DRY, answers=[("[y/N]", "y")])
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertLess(r.stdout.index("plan line 299"), r.stdout.index(">>> Go? [y/N]"), name)

    def test_terminal_is_quiet_by_default_and_full_with_verbose(self) -> None:
        body = "import opsquire_helpers as sq\n\nsq.step('working')\nsq.data('internal', rows=3)\nprint('plain line')\n"
        self.new("quiet", body)
        r = self.cli("run", "quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout + r.stderr
        for hidden in ("[spawn]", "[lint]", "[lock]", "[data]", "@@OPSQUIRE"):
            self.assertNotIn(hidden, out)
        self.assertIn("[step] working", out)
        self.assertIn("plain line", out)
        self.assertRegex(r.stderr, r"OK in [0-9.]+s; details: opsquire log \S+quiet")
        log = Path(self.last("quiet")["paths"]["output"]).read_text()
        self.assertIn("[spawn]", log)
        self.assertIn("@@OPSQUIRE data: internal", log)
        loud = self.cli("run", "quiet", "-v")
        self.assertIn("[spawn]", loud.stderr)
        self.assertIn("[data] internal", loud.stdout)

    def test_progress_without_terminal_prints_every_ten_percent_and_logs_sparingly(self) -> None:
        self.new("prog", self.PROGRESS_BODY)
        r = self.cli("run", "prog")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [x for x in r.stdout.splitlines() if x.startswith("download [")]
        self.assertLessEqual(len(lines), 12)
        self.assertIn("100/100 MB 100%", lines[-1])
        self.assertIn("halfway note", r.stdout)
        rec = self.last("prog")
        events = [e for e in self.events(rec) if e["event"] == "progress"]
        self.assertTrue(2 <= len(events) <= 12, len(events))
        self.assertEqual(events[-1]["done"], events[-1]["total"])

    def test_progress_on_a_terminal_redraws_one_line_and_steps_print_above_it(self) -> None:
        self.new("progtty", self.PROGRESS_BODY)
        r = self.cli("run", "progtty", answers=[])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("\r\x1b[K", r.stdout)
        self.assertIn("█", r.stdout)
        self.assertRegex(r.stdout, r"\r\x1b\[Khalfway note")

    def test_bash_progress_helper(self) -> None:
        body = BASH_HEADER + "sq_progress files 3 10\nsq_progress files 10 10\n"
        self.new("progsh", body, lang="sh")
        r = self.cli("run", "progsh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("files [", r.stdout)
        self.assertIn("10/10 100%", r.stdout)

    def test_progress_formatting(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import progress

        self.assertEqual(progress.human(113 << 20, "B"), "113 MB")
        self.assertEqual(progress.human(1.5 * (1 << 20), "B"), "1.5 MB")
        self.assertEqual(progress.amount(113 << 20, 181 << 20, "B"), "113/181 MB")
        self.assertEqual(progress.amount(900 << 10, 2 << 20, "B"), "900 KB/2.0 MB")
        self.assertEqual(progress.amount(3, 10, "files"), "3/10 files")
        self.assertEqual(progress.duration(42), "~42 s")
        self.assertEqual(progress.duration(300), "~5 min")

    def test_run_line_keeps_quoted_args_together(self) -> None:
        tid = self.new("quoted", "print(1)\n").name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        code = (
            "import sys\nfrom opsquire import handover\n"
            "print(handover.record_run_lines([('RUN', sys.argv[1]), ('RUN', sys.argv[2])], 'why', 'by'))\n"
        )
        run = f"opsquire run {tid} -- focus 'two words'"
        unbalanced = f"opsquire run {tid} -- 'open"
        r = subprocess.run([sys.executable, "-c", code, unbalanced, run], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), repr([tid]), r.stderr)
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual((item["command"], item["args"]), (run, "focus 'two words'"))

    def test_a_run_keeps_its_times_when_the_wall_clock_jumps(self) -> None:
        from opsquire.model import RunClock, iso

        def utc(text: str) -> dt.datetime:
            return dt.datetime.fromisoformat(text + "+00:00")

        def replay(readings: list[tuple[str, float]], offset: float = -18000.0) -> tuple[RunClock, list[str]]:
            """Each reading is (what the wall clock said, seconds the steady timer had counted)."""
            walls, timers = iter(utc(w) for w, _ in readings), iter(t for _, t in readings)
            clock = RunClock(wall=lambda: next(walls), timer=lambda: next(timers), offset=lambda: offset)
            return clock, [iso(clock.now()) for _ in readings[1:]]

        # seen on a real host: inside one run the clock flapped between UTC and New York time read as UTC
        clock, stamps = replay(
            [
                ("2026-11-12T20:05:23", 0),
                ("2026-11-12T15:05:35", 12),
                ("2026-11-12T20:05:48", 25),
                ("2026-11-12T15:05:52", 29),
            ]
        )
        self.assertEqual(stamps, ["2026-11-12T20:05:35.000Z", "2026-11-12T20:05:48.000Z", "2026-11-12T20:05:52.000Z"])
        self.assertEqual(iso(clock.start), "2026-11-12T20:05:23.000Z", "the start stands")
        move = clock.moves["local-as-utc"]
        self.assertEqual((move["moved_s"], move["count"]), (-18000, 2))
        self.assertEqual([m["kind"] for m in clock.fresh()], ["local-as-utc"])
        self.assertEqual(clock.fresh(), [], "each kind is warned about once")
        self.assertIn("UTC offset", clock.warning(move))

        clock, stamps = replay([("2026-11-12T15:05:23", 0), ("2026-11-12T20:05:35", 12), ("2026-11-12T15:05:40", 17)])
        self.assertEqual(iso(clock.start), "2026-11-12T20:05:23.000Z", "a start read during the slip is corrected")
        self.assertEqual(stamps[-1], "2026-11-12T20:05:40.000Z")

        clock, stamps = replay([("2026-11-12T20:00:00", 0), ("2026-11-12T20:47:00", 60)])
        self.assertEqual(iso(clock.start), "2026-11-12T20:46:00.000Z", "any other jump: the wall clock is trusted")
        self.assertEqual(clock.moves["jump"]["moved_s"], 2760)
        self.assertIn("moved by +2760s", clock.warning(clock.moves["jump"]))

        clock, stamps = replay([("2026-11-12T20:00:00", 0), ("2026-11-12T21:00:00", 3600)])
        self.assertEqual((clock.moves, iso(clock.start)), ({}, "2026-11-12T20:00:00.000Z"), "sleep counts as time")
        clock, stamps = replay([("2026-11-12T20:00:00", 0), ("2026-11-12T20:00:40", 10)])
        self.assertEqual((clock.moves, stamps), ({}, ["2026-11-12T20:00:40.000Z"]), "drift within the slack is fine")
        clock, stamps = replay([("2026-11-12T20:00:00", 0), ("2026-11-12T15:00:10", 10)], offset=0.0)
        self.assertEqual(list(clock.moves), ["jump"], "on a UTC machine no jump looks like a zone slip")

    def test_times_people_read_follow_the_display_zone(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        import datetime as dt

        from opsquire.milestones import render
        from opsquire.model import configured_time_zone, shown, update_config, valid_time_zone

        stamp = "2026-11-14T13:14:40.280Z"
        self.assertEqual(shown(stamp, "UTC"), "2026-11-14 13:14:40 UTC")
        self.assertEqual(shown(stamp, "America/New_York"), "2026-11-14 08:14:40 EST")
        self.assertEqual(shown(stamp, "Asia/Kolkata", seconds=False), "2026-11-14 18:44 IST")
        self.assertEqual(shown("not a time", "UTC"), "not a time")
        self.assertEqual(shown(None, "UTC"), "")
        for good in ("UTC", "local", "Europe/London"):
            self.assertTrue(valid_time_zone(good), good)
        for bad in ("", "Mars/Olympus", "../etc", 5, None):
            self.assertFalse(valid_time_zone(bad), bad)
        cfg = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cfg, True)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cfg)}))
        self.assertEqual(configured_time_zone(), "UTC", "the default")
        update_config(time_zone="America/New_York")
        self.assertEqual(configured_time_zone(), "America/New_York")
        update_config(time_zone="Mars/Olympus")
        self.assertEqual(configured_time_zone(), "UTC", "an unknown zone falls back")
        item = {
            "at": stamp,
            "kind": "done",
            "task_id": "20261114-0447_shop_cart-repair",
            "thread": "",
            "thread_title": "",
            "text": "ok",
        }
        at = dt.datetime(2026, 11, 14, 15, 0, tzinfo=dt.UTC)
        self.assertIn("  08:14 EST  done", render([item], at, "America/New_York"))
        self.assertIn("  13:14 UTC  done", render([item], at))
        tomorrow = dt.datetime(2026, 11, 15, 15, 0, tzinfo=dt.UTC)
        self.assertIn("  2026-11-14 08:14 EST  done", render([item], tomorrow, "America/New_York"))

    def test_the_command_line_shows_times_in_the_zone_init_sets(self) -> None:
        self.env["XDG_CONFIG_HOME"] = str(self.tmp / "cfg")
        self.new("count", "print(1)\n")
        self.assertEqual(self.cli("lint", "count").returncode, 0)
        self.assertEqual(self.cli("run", "count").returncode, 0)
        started = json.loads(self.cli("runs", "count", "--json").stdout)[0]["started"]
        self.assertRegex(self.cli("runs", "count").stdout, r"\d{2}:\d{2}:\d{2} UTC", "UTC by default")
        bad = self.cli("init", "--time-zone", "Mars/Olympus")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("zone name", bad.stderr)
        self.assertIn("times: shown in Asia/Kolkata", self.cli("init", "--time-zone", "Asia/Kolkata").stdout)
        table = self.cli("runs", "count").stdout
        from opsquire.model import shown

        self.assertIn(shown(started, "Asia/Kolkata"), table)
        self.assertIn(" IST", table)
        self.assertEqual(json.loads(self.cli("runs", "count", "--json").stdout)[0]["started"], started, "JSON is UTC")
        self.assertEqual(len(json.loads(self.cli("runs", "--since", "1h", "--json").stdout)), 1)
        held = self.cli("hold", "count", "--why", "later", "--until", "23:59")
        self.assertEqual(held.returncode, 0, held.stderr)
        self.assertIn("23:59 IST", held.stdout)
